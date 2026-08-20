"""CCC Head auth store — the User + Session tables behind login.

Pure stdlib `sqlite3` + `argon2-cffi`, no ORM/framework (deliberate — see temp/ROADMAP.md Phase 3a).
The Head API process (`main.py`) and the admin CLI (`admin.py`) both open the same `head.db`; setup
seeds the schema + first admin. Owned by the `ccc` service account, 0600 — enforced on every open by
`_restrict()`, because sqlite's own default is 0644 and nothing else would ever change it.

Design notes worth keeping:
- **Session tokens are stored hashed** (`sha256`, not the raw token): a leak of head.db grants no live
  sessions. The token is 256-bit random, so plain sha256 is enough — no salt/argon2 for it.
- **Passwords** use argon2id (`argon2-cffi` defaults).
- Times are ISO-8601 UTC text; expiry is compared in SQL against `datetime('now')`.
"""

import hashlib
import json
import os
import secrets
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from . import credentials
from . import guises

# Path comes from the environment (the systemd unit sets CCC_HEAD_DB) so the API package never
# imports the CLI's config. Default matches cuttle.config.HEAD_DB.
DB_PATH = Path(os.environ.get("CCC_HEAD_DB", "/var/lib/ccc/head/head.db"))

SESSION_TTL_DAYS = 30
# head.db has TWO writers (the API: submit→escrow, override→ledger; the Karma tick: settle→ledger).
# WAL permits that, but a writer that collides with another gets SQLITE_BUSY back *immediately* unless
# it is told to wait — a submit landing during a settle would simply fail. The writes are milliseconds
# long, so any timeout that isn't zero is enough.
BUSY_TIMEOUT_MS = 5000
_TOKEN_BYTES = 32  # secrets.token_urlsafe → ~43 url-safe chars, 256 bits

_ph = PasswordHasher()

# Schema as a migration ladder keyed by PRAGMA user_version: each step takes the DB from version N-1
# to N, so a fresh DB runs the whole ladder and a deployed one runs only what it's missing.
_SCHEMA_V1 = """
CREATE TABLE users (
  id                   INTEGER PRIMARY KEY,
  username             TEXT    NOT NULL UNIQUE,
  pw_hash              TEXT    NOT NULL,
  is_admin             INTEGER NOT NULL DEFAULT 0,
  must_change_password INTEGER NOT NULL DEFAULT 0,
  created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE sessions (
  token_sha256 TEXT    PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  expires_at   TEXT    NOT NULL
);
CREATE INDEX idx_sessions_user ON sessions(user_id);
"""

# The attribution ledger: one row per submitted Spawn, keyed by the Slurm job id (the spawn id).
# user_id is ON DELETE SET NULL so removing a user keeps the historical attribution row (shown as
# "unknown" rather than vanishing). This is the single source of truth for who submitted what — no
# sbatch --comment stamp to drift against.
_SCHEMA_V2 = """
CREATE TABLE spawns (
  job_id       INTEGER PRIMARY KEY,
  user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
  submitted_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  script       TEXT    NOT NULL,
  staging_uuid TEXT    NOT NULL
);
CREATE INDEX idx_spawns_user ON spawns(user_id);
"""

# The Karma economy. Two tables, deliberately NOT a third holding balances:
#
# `karma_ledger` is ONE ROW PER EVENT, both parties nullable — a NULL `from_user` is Karma conjured (an
# Override credit), a NULL `to_user` is Karma destroyed (a Spawn on a commons Arm, no owner to pay). Σ
# of every User's balance is then a single query.
#
# No `karma_balance` table: Decay is linear, so a decayed sum over the rows and a decay-on-touch
# running total are exactly — not approximately — equal. A cached balance could only ever disagree with
# the ledger. See karma.py for the arithmetic.
#
# `karma_escrow` is a live hold, not a movement: it suppresses a submitter's Share while their Spawn is
# outstanding (so flooding the queue instantly costs your own queue position), released at Settlement.
# Does not decay — it is not history, it is an outstanding claim.
_SCHEMA_V3 = """
CREATE TABLE karma_ledger (
  id        INTEGER PRIMARY KEY,
  at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  kind      TEXT    NOT NULL,
  amount    REAL    NOT NULL,
  from_user INTEGER REFERENCES users(id) ON DELETE SET NULL,
  to_user   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  job_id    INTEGER,
  note      TEXT
);
CREATE INDEX idx_karma_ledger_from ON karma_ledger(from_user);
CREATE INDEX idx_karma_ledger_to   ON karma_ledger(to_user);
CREATE TABLE karma_escrow (
  job_id  INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  amount  REAL    NOT NULL,
  at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX idx_karma_escrow_user ON karma_escrow(user_id);
"""

# The meter. Two moves:
#
# 1. `spawns` learns what the User DECLARED. The tick needs a queued Spawn's needs to know which Arms
#    it is eligible for (and so which Arms' Demand it adds to), and its `minutes` to quote a hold — all
#    known at submit, none worth re-parsing out of Slurm's ~120-field job schema every 30s. Eligibility
#    itself is NOT frozen here, only the need is — the tick re-derives the Arm set from the live
#    registry each pass, so an Arm admitted after this Spawn queued joins its eligible set, and a
#    drained one leaves it (making the rest scarcer, which is correct).
#
# 2. `karma_escrow` becomes the Spawn's **outstanding claim** — and so the meter itself. It holds both:
#      * `hold`    — the forecast, quoted once at submit. A FLOOR under the claim.
#      * `accrued` — what the Spawn has really cost so far, ticked while it RUNS. A RATCHET on top.
#    Share subtracts MAX(hold, accrued), so the claim only ever goes UP: an under-quoted hold cannot be
#    exploited, and a Demand spike that overshoots the forecast is felt in the submitter's standing
#    immediately rather than as a surprise at Settlement. At Settlement the row is DELETED and one
#    ledger row (= accrued) is written; the escrow is a live claim, never history.
#
# `karma_demand` is history ONLY — never a pricing path. The tick consumes each sample the instant it
# takes it; these rows exist so the Tide can forecast Demand by hour-of-day and the Dashboard can chart
# it. Raw (one row per Arm per tick), pruned at 30 days: ~10MB/month at 3 Arms.
_SCHEMA_V4 = """
ALTER TABLE spawns ADD COLUMN minutes INTEGER;
ALTER TABLE spawns ADD COLUMN gpus    INTEGER;
ALTER TABLE spawns ADD COLUMN vram_gb INTEGER;
ALTER TABLE spawns ADD COLUMN node    TEXT;

DROP TABLE karma_escrow;
CREATE TABLE karma_escrow (
  job_id  INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  hold    REAL    NOT NULL,
  accrued REAL    NOT NULL DEFAULT 0,
  node    TEXT,
  last_at TEXT,
  at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX idx_karma_escrow_user ON karma_escrow(user_id);

CREATE TABLE karma_demand (
  at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  node   TEXT NOT NULL,
  demand REAL NOT NULL
);
CREATE INDEX idx_karma_demand_at ON karma_demand(at);
"""

# `spawns` learns its ENDING: the exit code, and when it ended. Recorded once, by the tick, the pass it
# notices the Spawn has left the queue — the durable outcome `GET /spawns/history` reads, because squeue
# forgets a finished job after MinJobAge and we run no slurmdbd. `exit_code` NULL with `finished_at` set
# is a Spawn that ended without recording one (scancel'd, OOM, or its Arm dropped before stage-out) — a
# failure, exactly as `GET /spawns/{id}` already reports it live.
_SCHEMA_V5 = """
ALTER TABLE spawns ADD COLUMN exit_code   INTEGER;
ALTER TABLE spawns ADD COLUMN finished_at TEXT;
"""

# Arm Retract/Return — an owner's self-service pause of their own machine. Two tables because the two
# things they track have different lifetimes:
#
# `arm_retractions` — presence of a row means "this node takes no NEW placement". submit_checked reads
# it to build --exclude= and to refuse a pinned Spawn aimed at it. Return deletes the row; that is all
# Return ever does.
#
# `retracted_jobs` — one row per Spawn that was RUNNING on the node at the moment Retract fired,
# deadline = when the Karma tick should force-kill it if it hasn't exited on its own. Survives Return —
# a Spawn already caught by a Retract is doomed regardless of an early Return (it never held the
# arm_retractions row to begin with, and Return doesn't touch this table). Cleared only when the tick
# settles the job, which is also where it is forced to zero regardless of what it accrued.
_SCHEMA_V6 = """
CREATE TABLE arm_retractions (
  node TEXT PRIMARY KEY
);
CREATE TABLE retracted_jobs (
  job_id   INTEGER PRIMARY KEY,
  node     TEXT NOT NULL,
  deadline TEXT NOT NULL
);
"""

# A User's own W&B key, so the **Dashboard** can log metrics at all: the browser has no ~/.ccc to read
# a key out of, so every Spawn submitted from it ran with WANDB_MODE=disabled. The key belongs to the
# User, not the box, so it is a column on `users` — NULL is unset, and deleting a User takes their key
# with them.
#
# BLOB, not TEXT: it was stored encrypted under a keyfile beside this database, which protected a
# head.db that leaked on its own and nothing more — the Head could decrypt unattended.
#
# **Superseded by _SCHEMA_V20**, which drops this column for the `user_secrets` envelope. Kept here
# because the ladder is history: a database at version 6 still has to grow this column before V20 can
# drop it.
_SCHEMA_V7 = """
ALTER TABLE users ADD COLUMN wandb_key BLOB;
"""

# Tide Pool: a Spawn whose payload is a live shell instead of a script. `spawns.tidepool` marks which
# ones (submit_checked skips the outputs/script requirements for them). Two tables because the port has
# to be reserved BEFORE the job_id exists (baked into the job script, rendered before sbatch ever runs
# — sshd inside the Pod and the Head's relay both bind it), and `tidepools.job_id` can't be a key
# nothing has yet:
#   * `tidepool_ports` — the reservation itself, keyed by the port alone, so two submissions racing the
#                  same "lowest free port" collide on ITS primary key (caught and retried — see
#                  tidepool.reserve_port) instead of racing a job_id neither has yet.
#   * `tidepools`  — everything else, written once the job_id is known. `port` is copied in here too,
#                  for reads that only care about one Tide Pool's state and would otherwise need a
#                  second table just to find its port.
#     - `pubkey`/`privkey` — a keypair minted once at submit, baked into the job script's
#                  authorized_keys (pubkey), never regenerated, so a reattach after a dropped connection
#                  or a Dashboard-requested Tide Pool gets the SAME key back. `privkey` is encrypted at
#                  rest (atrest.py): must come back out in plaintext for the CLI, so it cannot be
#                  hashed, and it is the ONLY thing left the Head decrypts unattended — a Credential
#                  no longer is. It can't join them: an Admin-pinned Tide Pool (`cuttle run --node
#                  --tidepool`) has no Session, so there would be no DEK to seal it under.
#     - `relay_pid` — the Head-side TCP relay subprocess forwarding this port to the Arm, once started
#                  (tidepool.ensure_relay). NULL until the Spawn is actually RUNNING somewhere.
#     - `node`     — the Arm the relay is forwarding to, cached so a restarted relay's liveness check
#                  doesn't have to re-derive it from squeue every poll.
# Both rows are deleted (not decayed like the escrow, ended like retracted_jobs) the moment the Spawn
# leaves the queue — tick.py's reap_dead is the one place that happens, and it kills relay_pid first.
_SCHEMA_V8 = """
ALTER TABLE spawns ADD COLUMN tidepool INTEGER NOT NULL DEFAULT 0;
CREATE TABLE tidepool_ports (
  port        INTEGER PRIMARY KEY,
  reserved_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE tidepools (
  job_id     INTEGER PRIMARY KEY REFERENCES spawns(job_id) ON DELETE CASCADE,
  port       INTEGER NOT NULL,
  pubkey     TEXT    NOT NULL,
  privkey    BLOB    NOT NULL,
  relay_pid  INTEGER,
  node       TEXT
);
"""

# Recovery Window: one row per Spawn currently holding a copy of its undeclared working dir on the Head
# (ccc_head_api.recovery). `bytes` feeds both caps — SUM across every row for the pool, SUM joined
# through `spawns.user_id` for one User's Allowance (no user_id column here, for _SCHEMA_V11's reason:
# the Attribution row already answers whose it is, and a copy of the answer could only drift from it).
# `created_at` is both the expiry clock AND the oldest-first eviction order — the same column does both
# jobs because they're the same question ("how long has this been held?") asked two ways. A row here always
# has a matching `SPAWNS_DIR/<id>/recovery/` dir on disk; recovery.py's `_evict` deletes both together.
_SCHEMA_V9 = """
CREATE TABLE recovery_copies (
  job_id     INTEGER PRIMARY KEY REFERENCES spawns(job_id) ON DELETE CASCADE,
  bytes      INTEGER NOT NULL,
  created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""

# **Guises** (temp/spawn-isolation.md): the uid a User's Spawns run as, on every node. See guises.py
# for what one is; this table is only the allocator, and it has exactly one job the obvious
# `MAX(users.uid)+1` cannot do — **never hand out a uid twice**.
#
# `remove_user` HARD-deletes, so an allocator keyed off the users table would give a deleted User's uid
# to the next one created, along with every file they left behind on every Arm they ever ran on. That
# is precisely the failure a per-User uid exists to prevent, so the allocation is kept here instead,
# where a removed User leaves a **tombstone**: `uid` is the primary key, `user_id` goes NULL when they
# are deleted (ON DELETE SET NULL), and the row stays forever to keep that uid retired. UNIQUE on
# user_id is what makes it one Guise per User — SQLite allows many NULLs in a UNIQUE column, which is
# exactly right: many tombstones, one live row each.
#
# Backfilled for every User already in the database, in `id` order, so an upgrade needs no separate
# migration step and no User is left without one.
_SCHEMA_V10 = f"""
CREATE TABLE guises (
  uid        INTEGER PRIMARY KEY,
  user_id    INTEGER UNIQUE REFERENCES users(id) ON DELETE SET NULL,
  created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
INSERT INTO guises (uid, user_id)
  SELECT {guises.UID_BASE} + ROW_NUMBER() OVER (ORDER BY id) - 1, id FROM users;
"""

# Output retention. Exactly `recovery_copies`' shape, one row per Spawn whose declared Output results
# the Head is holding, and `created_at` again does both jobs — the 7-day expiry clock AND the
# oldest-first eviction order. It is copied from the Spawn's `finished_at`, not stamped at
# registration, so a Spawn registered late by a tick that was down still expires 7 days after it RAN
# (see retention.py). A row here always has a matching `SPAWNS_DIR/<id>/outputs/` dir on disk;
# `retention._evict` deletes both together.
#
# No user_id column: the 32GB Allowance is per User, but `spawns.user_id` already says whose a Spawn is,
# and duplicating it here would be a second answer that could disagree with the first — including after
# `remove_user` NULLs the original.
_SCHEMA_V11 = """
CREATE TABLE output_copies (
  job_id     INTEGER PRIMARY KEY REFERENCES spawns(job_id) ON DELETE CASCADE,
  bytes      INTEGER NOT NULL,
  created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""

# Recipes: a User's frozen, re-runnable snapshots. This row is *how* to run one, and
# `recipes/<user_id>/<name>/` on disk is the code itself. `bytes` is what that tree costs against the
# 32GB per-User **Allowance**, measured UNCOMPRESSED — the composer blocks a doomed save from
# `File.size` before spending the upload, and a Head that counted the gzip instead would be answering a
# different question than the one the browser asked.
#
# Unlike `output_copies` this table does carry `user_id`: there is no Spawn to ask whose it is. And it
# CASCADEs rather than leaving a `guises`-style tombstone — a Recipe is private to its owner and outlives
# nothing, so a deleted User's shelf goes with them (`recipes.sweep_orphans` takes the directories, which
# the cascade cannot reach).
#
# UNIQUE(user_id, name) is the shelf's own rule. A name is how its owner refers to a Recipe on a page
# that shows no ids at all, so two called the same thing would be indistinguishable in the only place
# they are ever seen. Scoped per User: two Users may both keep a "resnet".
_SCHEMA_V12 = """
CREATE TABLE recipes (
  id            INTEGER PRIMARY KEY,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT    NOT NULL,
  script        TEXT    NOT NULL,
  args          TEXT    NOT NULL DEFAULT '[]',
  outputs       TEXT    NOT NULL DEFAULT '[]',
  gpus          INTEGER NOT NULL,
  vram_mb       INTEGER NOT NULL,
  minutes       INTEGER NOT NULL,
  wandb         INTEGER NOT NULL DEFAULT 1,
  wandb_project TEXT    NOT NULL DEFAULT '',
  wandb_entity  TEXT    NOT NULL DEFAULT '',
  bytes         INTEGER NOT NULL,
  frozen_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE (user_id, name)
);
"""

# `build_state`, the one state CCC owned rather than read off `squeue`. Added when the Head built each
# Spawn's **Environment** and `sbatch --hold`ed it until the build finished, because squeue reports a
# held job as PENDING and CCC had to be able to tell the two apart. **Dropped again in _SCHEMA_V16** —
# the Arm builds its own Environment now and there is no hold. Left here because migrations are history:
# a Head that was ever on v13 ran this.
_SCHEMA_V13 = """
ALTER TABLE spawns ADD COLUMN build_state TEXT;
CREATE INDEX idx_spawns_build ON spawns(build_state) WHERE build_state IS NOT NULL;
"""

# `placed_node` — where the cluster actually PUT a Spawn, which is not `node` and must never be folded
# into it. `node` is what the submitter *demanded* (`--node`, Admin-only, NULL for everyone else), and
# the distinction is the economy's: Users declare needs, only an Admin names a machine. `spawn_needs`
# reads `node` as that declaration to compute Eligibility and Demand, so overwriting it with the real
# placement would quietly change what a queued Spawn is thought to be eligible for.
#
# The fact was always known and always thrown away: the meter writes the live node to `karma_escrow`
# every pass, and `clear_escrow` deletes that row at Settlement — precisely when History starts wanting
# it. So it is copied here on the way past instead, and outlives the claim that carried it.
#
# NULL for every Spawn that ended before this migration (their placement is genuinely gone), and for any
# that never ran long enough to be metered. Readers must treat NULL as "not known", never as "unplaced".
_SCHEMA_V14 = """
ALTER TABLE spawns ADD COLUMN placed_node TEXT;
"""

# `manifest`/`features` — the two declared-need fields `/submit` already carried that a Recipe couldn't
# freeze until now. Structural only, like `vram_mb`/`gpus` above: stored as declared, never checked
# against the fleet's current capability at save time — that check happens for real when the Recipe is
# actually run, exactly like any other Spawn (`spawns.submit_checked`).
#
# `recipe_name` on `spawns` is the other half of the same change: the link `recipes.running` reads to
# tell whether a Spawn on the live queue came from this Recipe. NULL for every ad-hoc Spawn (nearly all
# of them) and for every Spawn that predates this migration. Not a foreign key — a Recipe can be deleted
# out from under a Spawn that already ran from it, and that Spawn's history should not go with it.
_SCHEMA_V15 = """
ALTER TABLE recipes ADD COLUMN manifest TEXT;
ALTER TABLE recipes ADD COLUMN features TEXT NOT NULL DEFAULT '[]';
ALTER TABLE spawns ADD COLUMN recipe_name TEXT;
"""

# `build_state` goes away with the thing it described. It existed because a Spawn whose **Environment**
# the Head was building had to be `sbatch --hold`ed, and a held job is reported by squeue as PENDING —
# indistinguishable from one waiting for a card, so CCC had to remember the difference itself. The Arm
# builds its own Environment now, inside its own allocation, so there is no hold, no build the Head is
# waiting on, and no state Slurm cannot see. Dropped rather than left NULL forever: a column nothing
# writes is a column the next reader has to work out is dead.
_SCHEMA_V16 = """
DROP INDEX IF EXISTS idx_spawns_build;
ALTER TABLE spawns DROP COLUMN build_state;
"""

# Workspaces: the Tide Pool's durable form. This row is the DECLARATION (what a visit asks for) and
# `workspaces/<user_id>/<name>/` on disk is the tree itself — the same split as `recipes`, and the same
# `<user_id>/<name>` layout so `workspaces.sweep_orphans` can tell whose a directory is without the DB.
#
# **No `bytes` column, unlike `recipes`.** A Recipe's tree only changes when its owner uploads, so a
# stored figure is exact; a Workspace's changes every 60 seconds from inside a live session, over rsyncd,
# with nothing on the Head watching. A cached number would be stale the moment a visit started, and it
# is the number that decides whether a shrink is refused — so usage is measured off the disk instead
# (`workspaces.usage`) and the only thing stored is what was RESERVED.
#
# `storage_bytes` is this Workspace's **Storage**: what it may hold, carved out of its owner's Workspace
# Allowance when they created it (CONTEXT.md, **Storage**). The Allowance is the sum of these, so the pool
# is a query rather than a column.
#
# `live_job_id` is the current visit's Spawn, claimed transactionally before sbatch so two simultaneous
# opens cannot both submit, and cleared by the karma tick when that Spawn leaves the queue. Not a foreign
# key: `spawns` rows outlive nothing here, and a claim must be clearable even if the Spawn row is gone.
# NULL means idle, which is also what a Head that crashed mid-visit will show once the tick catches up.
_SCHEMA_V17 = """
CREATE TABLE workspaces (
  id              INTEGER PRIMARY KEY,
  user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name            TEXT    NOT NULL,
  storage_bytes     INTEGER NOT NULL,
  gpus            INTEGER NOT NULL,
  vram_mb         INTEGER NOT NULL,
  minutes         INTEGER NOT NULL,
  manifest        TEXT    NOT NULL DEFAULT '',
  outputs         TEXT    NOT NULL DEFAULT '[]',
  features        TEXT    NOT NULL DEFAULT '[]',
  created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  last_visited_at TEXT,
  live_job_id     INTEGER,
  UNIQUE (user_id, name)
);
"""

# A Spawn's **Name** (CONTEXT.md): what a person calls it in the queue and the archive, and the one
# thing about a Spawn that CCC never invents. Required at `/submit` exactly as `minutes`/`vram`/the
# **Environment** declaration are — refused before the Spawn exists rather than filed under something
# made up for it — so nothing reaching this insert is nameless.
#
# NOT the Slurm job name. That string is `.replace()`d into a bash job script (`spawns.JOB_SCRIPT`),
# where a User's own words would be a shell injection; `--job-name` stays the constant `ccc-spawn` and
# the Name lives here, served by the same DB join `/spawns` already does for Attribution.
#
# `DEFAULT ''` is a migration artifact and nothing else: an empty Name is unreachable through either
# client, and no row that predates this column survives the teardown that ships it. It exists because
# SQLite cannot ADD a NOT NULL column without one.
#
# A label, never a key — no UNIQUE here, deliberately. Two Spawns may share a Name (running one Recipe
# twice is the ordinary case, and both visits inherit it); the job_id is what tells them apart.
_SCHEMA_V18 = """
ALTER TABLE spawns ADD COLUMN name TEXT NOT NULL DEFAULT '';
"""

# The **Substrate** declaration, frozen wherever the other declarations are (`manifest`, `features`).
# The path a Spawn named, not the packages it resolved to: what a Recipe freezes is the declaration,
# and the file itself is in the tree the Recipe already holds — storing the resolved list instead would
# be a second copy that goes stale the moment its owner edits the file they can still see.
#
# `DEFAULT ''` on both, meaning "declared none", which is the ordinary case and the only thing a row
# predating this column could have meant.
_SCHEMA_V19 = """
ALTER TABLE recipes ADD COLUMN substrate TEXT NOT NULL DEFAULT '';
ALTER TABLE workspaces ADD COLUMN substrate TEXT NOT NULL DEFAULT '';
"""

# **Credentials** — the envelope that replaces the keyfile. See credentials.py for the scheme; this is
# only the shape it needs.
#   * `users.dek_salt` / `dek_wrapped_pw` — the master wrap. NULL for a User who has not logged in
#     since this migration; `/login` mints the pair lazily, which is the one place a password and a
#     fresh token coexist.
#   * `sessions.dek_wrapped_tok` — the working wrap, one per live login, openable only with the raw
#     token the client holds. This is what lets a request reach a Credential without a password.
#   * `user_secrets` — the secrets themselves, keyed by (user, kind) so a second kind is a row and not
#     a column. `ct` is AESGCM(DEK); `hint` is deliberately PLAINTEXT, the only part anyone ever sees
#     again. ON DELETE CASCADE so removing a User takes their Credentials with them, as `users.wandb_key`
#     did by being a column on the row.
#
# **Every existing session is deleted** and **`users.wandb_key` is dropped, unrecovered.** Neither is
# salvageable and that is by design, not an oversight: a stored key could only be re-encrypted by first
# decrypting it with the keyfile, and a migration that did that would be doing the exact unattended
# decrypt this change exists to make impossible. Users re-paste; everyone logs in again.
_SCHEMA_V20 = """
ALTER TABLE users ADD COLUMN dek_salt BLOB;
ALTER TABLE users ADD COLUMN dek_wrapped_pw BLOB;
ALTER TABLE users DROP COLUMN wandb_key;
ALTER TABLE sessions ADD COLUMN dek_wrapped_tok BLOB;
DELETE FROM sessions;
CREATE TABLE user_secrets (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind    TEXT    NOT NULL,
  ct      BLOB    NOT NULL,
  hint    TEXT    NOT NULL,
  set_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  PRIMARY KEY (user_id, kind)
);
"""

# index i takes user_version i -> i+1
_MIGRATIONS = [_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4, _SCHEMA_V5, _SCHEMA_V6, _SCHEMA_V7,
              _SCHEMA_V8, _SCHEMA_V9, _SCHEMA_V10, _SCHEMA_V11, _SCHEMA_V12, _SCHEMA_V13,
              _SCHEMA_V14, _SCHEMA_V15, _SCHEMA_V16, _SCHEMA_V17, _SCHEMA_V18, _SCHEMA_V19,
              _SCHEMA_V20]

# DERIVED from the ladder, never written by hand. It was a standalone constant until it fell one behind
# the list it describes: `init_schema` stamps this AFTER running `_MIGRATIONS[ver:]`, so a version that
# undercounts re-runs the last step on the very next call and dies on whatever it already did ("duplicate
# column name"). Every future migration is now one append with nothing to remember.
SCHEMA_VERSION = len(_MIGRATIONS)


# --- connection + schema ----------------------------------------------------

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")  # two writers now — see BUSY_TIMEOUT_MS
    _restrict()
    return conn


def _restrict() -> None:
    """0600 on the store and its WAL sidecars, on every open.

    SQLite creates a missing database at a hardcoded 0644 (`SQLITE_DEFAULT_FILE_PERMISSIONS`), and only
    a umask of 077 would trim that — so this mode has to be SET, never inherited. A world-readable
    head.db means every User's Guise could read the password hashes and session tokens in it.
    HEAD_STATE_DIR is 0711 (traverse, never list) precisely on the assumption that it could not.

    Here rather than in `cuttle setup` because the -wal/-shm sidecars are recreated at runtime by
    whichever process writes — the API service, the karma tick, and the admin CLI over sudo from an
    operator's tty, three different umasks — so a one-time chmod at provisioning does not hold. They
    appear on the first WRITE, not at the `journal_mode` pragma above, so on a brand-new store they are
    0644 until the next open. Every caller of this function is one, and the tick alone opens one every
    30 seconds.
    """
    for p in (DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")):
        if p.exists():
            p.chmod(0o600)


def init_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: run any migrations the DB is behind on, tracked by PRAGMA user_version. A fresh DB
    (version 0) runs the whole ladder; a deployed v1 DB runs only the v2 step."""
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    for step in _MIGRATIONS[ver:]:
        conn.executescript(step)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def ensure_schema() -> None:
    with connect() as conn:
        init_schema(conn)


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- users ------------------------------------------------------------------

def add_user(conn: sqlite3.Connection, username: str, is_admin: bool = False) -> str:
    """Create a user with a random temp password (forced-change on first login). Returns the plaintext
    temp password to hand off once — it is never recoverable after this.

    A **Guise** uid is allocated here, for everyone: an Admin gets one like any other User, because
    "the Admin's laptop is compromised" is as realistic as anyone's and an Admin's payload must not be
    able to write head.db either. Allocated with the User rather than lazily at first submit, so the
    account can be provisioned by the operator standing at the Head right now — the only moment there
    is one (see cuttle.auth.ensure_guise_accounts).
    """
    temp_pw = secrets.token_urlsafe(12)
    cur = conn.execute(
        "INSERT INTO users (username, pw_hash, is_admin, must_change_password) VALUES (?,?,?,1)",
        (username, _ph.hash(temp_pw), int(is_admin)),
    )
    allocate_guise(conn, cur.lastrowid)
    conn.commit()
    return temp_pw


def get_user(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT username, is_admin, must_change_password, created_at FROM users ORDER BY username"
    ).fetchall()


def remove_user(conn: sqlite3.Connection, username: str) -> bool:
    cur = conn.execute("DELETE FROM users WHERE username=?", (username,))
    conn.commit()
    return cur.rowcount > 0


def reset_password(conn: sqlite3.Connection, username: str) -> str | None:
    """Admin-mediated recovery: a fresh temp password + forced change, exactly like `add_user`, so a
    forgotten password no longer means delete-and-recreate (which would wipe the User's Karma balance
    and orphan their Arms).

    Every existing session for this User is killed — a reset is a security action, not just a password
    change. Returns the plaintext temp password, or None if there is no such user.

    **This destroys the User's Credentials**, and there is no way for it not to. Both wraps around
    their DEK die here: the password one because the password it was derived from is gone, the session
    ones because the sessions are. An Admin who can reset a password deliberately cannot read what that
    password was keeping — that is the property, not a gap in it. The rows are deleted rather than left
    as unopenable ciphertext, so `set` reads false and the User is simply invited to paste again.
    """
    user = get_user(conn, username)
    if user is None:
        return None
    temp_pw = secrets.token_urlsafe(12)
    conn.execute("UPDATE users SET pw_hash=?, must_change_password=1, dek_salt=NULL, "
                 "dek_wrapped_pw=NULL WHERE username=?", (_ph.hash(temp_pw), username))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
    conn.execute("DELETE FROM user_secrets WHERE user_id=?", (user["id"],))
    conn.commit()
    return temp_pw


def verify_password(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    """Return the user row on a correct password, else None. Constant-ish: a missing user still runs
    one hash verify against a dummy so timing doesn't leak existence."""
    user = get_user(conn, username)
    try:
        _ph.verify(user["pw_hash"] if user else _DUMMY_HASH, password)
    except (VerifyMismatchError, InvalidHashError):
        return None
    return user


def set_password(conn: sqlite3.Connection, username: str, new_password: str,
                 dek: bytes | None = None) -> None:
    """Rotate the password and, with it, the master wrap around `dek`.

    Passing the DEK re-wraps the SAME key under a salt derived fresh from the new password, so nothing
    stored under it has to be touched: every Credential ciphertext and every other live session's wrap
    stays valid, and nobody is logged out by changing their password.

    `dek=None` means the caller has no DEK to carry across — a test seeding a password, or a User who
    never had one. The wrap is then cleared rather than left stale, so the next `/login` mints a fresh
    DEK instead of failing to open a wrap the new password cannot possibly fit.
    """
    salt = credentials.new_salt() if dek is not None else None
    wrapped = credentials.wrap(credentials.password_kek(new_password, salt), dek) if dek else None
    conn.execute(
        "UPDATE users SET pw_hash=?, must_change_password=0, dek_salt=?, dek_wrapped_pw=? "
        "WHERE username=?",
        (_ph.hash(new_password), salt, wrapped, username),
    )
    conn.commit()


def unlock_dek(conn: sqlite3.Connection, user: sqlite3.Row, password: str) -> bytes:
    """This User's DEK, opened with their password — minting one if they have none yet.

    Called from exactly one place (`/login`), and that is forced rather than chosen: login is the only
    moment a password and a fresh session token exist together, which is the pair the whole envelope
    hangs off. The mint branch covers three cases with one line of code — a brand-new User, a User
    carried across the migration, and a User whose Admin reset destroyed the old DEK.

    A wrap that will not open (an old database restored over a newer password) is treated as no wrap:
    mint a new DEK. Anything sealed under the previous one becomes unreadable, which is the same
    outcome as an Admin reset and the only honest answer — the key that opened it is gone.
    """
    if user["dek_wrapped_pw"] is not None and user["dek_salt"] is not None:
        dek = credentials.unwrap(credentials.password_kek(password, user["dek_salt"]),
                                 user["dek_wrapped_pw"])
        if dek is not None:
            return dek
    dek = credentials.new_dek()
    salt = credentials.new_salt()
    conn.execute("UPDATE users SET dek_salt=?, dek_wrapped_pw=? WHERE id=?",
                 (salt, credentials.wrap(credentials.password_kek(password, salt), dek), user["id"]))
    clear_credentials(conn, user["id"])  # nothing left could be opened by this new DEK anyway
    conn.commit()
    return dek


# A precomputed hash so verify_password does equal work whether or not the user exists.
_DUMMY_HASH = _ph.hash("__ccc_dummy__")


# --- Guises (temp/spawn-isolation.md) ----------------------------------------
# See _SCHEMA_V10 for the table and why it is its own. Allocating a uid needs no privilege whatsoever —
# it is one row in head.db — which is what lets `cuttle user add` stay a Head-local command with an
# operator at a tty, and keeps a root helper out of the Head API entirely. CREATING the Unix account
# from that uid is the privileged half, and it lives in the CLI (cuttle.auth.ensure_guise_accounts).

def allocate_guise(conn: sqlite3.Connection, user_id: int) -> int:
    """This User's uid, allocating one if they have none. Idempotent — the second call returns the
    first call's answer, so `setup --head` can re-run over an already-provisioned Head.

    One statement, so two concurrent callers cannot read the same MAX(): the loser hits the UNIQUE on
    `uid` (or on `user_id`) and raises rather than quietly minting a duplicate.
    """
    row = conn.execute("SELECT uid FROM guises WHERE user_id=?", (user_id,)).fetchone()
    if row is not None:
        return row["uid"]
    conn.execute(
        "INSERT INTO guises (uid, user_id) "
        "SELECT COALESCE(MAX(uid), ?) + 1, ? FROM guises",
        (guises.UID_BASE - 1, user_id),
    )
    conn.commit()
    return conn.execute("SELECT uid FROM guises WHERE user_id=?", (user_id,)).fetchone()["uid"]


def guise_uid(conn: sqlite3.Connection, user_id: int) -> int | None:
    row = conn.execute("SELECT uid FROM guises WHERE user_id=?", (user_id,)).fetchone()
    return row["uid"] if row else None


def live_guises(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every uid with a User still behind it, newest last. Tombstones (a removed User's retired uid)
    are deliberately absent: this is what the Head provisions accounts FOR, not the retirement record.
    """
    return conn.execute(
        "SELECT g.uid, u.username FROM guises g JOIN users u ON u.id = g.user_id ORDER BY g.uid"
    ).fetchall()


# --- Credentials ------------------------------------------------------------
# One row per (User, kind) in `user_secrets`, each value sealed under that User's DEK — see
# credentials.py for the envelope and _SCHEMA_V20 for the shape. The Head holds these so BOTH clients
# get them: the Dashboard has no config file to read a key out of, and the CLI no longer keeps one on
# disk either.
#
# Every read here needs a `dek`, which only a live Session produces. `dek=None` is the ordinary
# session-less case and answers None, exactly as an unset Credential does — so no caller below has to
# distinguish "no Session" from "nothing stored", and none of them can fail.

def set_credential(conn: sqlite3.Connection, user_id: int, kind: str, dek: bytes,
                   value: str) -> None:
    """Store or replace one Credential. The `hint` goes in beside it in plaintext, computed per kind
    (credentials.KINDS) — it is the only part its owner will ever see again."""
    conn.execute(
        """INSERT INTO user_secrets (user_id, kind, ct, hint) VALUES (?,?,?,?)
           ON CONFLICT(user_id, kind) DO UPDATE SET ct=excluded.ct, hint=excluded.hint,
                                                    set_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')""",
        (user_id, kind, credentials.seal(dek, value), credentials.hint_for(kind, value)),
    )
    conn.commit()


def get_credential(conn: sqlite3.Connection, user_id: int, kind: str,
                   dek: bytes | None) -> str | None:
    """This User's Credential of `kind` in plaintext, or None — none stored, no Session behind the
    request, or a DEK that does not open it. The last case is what keeps this un-forgeable: a caller
    holding the wrong DEK gets None, never someone else's secret."""
    row = conn.execute("SELECT ct FROM user_secrets WHERE user_id=? AND kind=?",
                       (user_id, kind)).fetchone()
    return credentials.open_(dek, row["ct"]) if row else None


def credential_status(conn: sqlite3.Connection, user_id: int, kind: str) -> dict:
    """The masked view, and the ONLY view: is one stored, and its hint. Needs no DEK — nothing here
    is encrypted, which is the point. Enough to tell "the key I pasted" from "some other key"
    without the secret ever going back on the wire."""
    row = conn.execute("SELECT hint, set_at FROM user_secrets WHERE user_id=? AND kind=?",
                       (user_id, kind)).fetchone()
    return {"kind": kind, "set": row is not None,
            "hint": row["hint"] if row else None,
            "set_at": row["set_at"] if row else None}


def credential_statuses(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """Every kind the Head knows about, set or not — so the Account page renders the same rows for a
    User with nothing stored as for one with everything."""
    return [credential_status(conn, user_id, kind) for kind in credentials.KINDS]


def clear_credential(conn: sqlite3.Connection, user_id: int, kind: str) -> None:
    conn.execute("DELETE FROM user_secrets WHERE user_id=? AND kind=?", (user_id, kind))
    conn.commit()


def clear_credentials(conn: sqlite3.Connection, user_id: int) -> None:
    """All of them, for an Admin password reset: the DEK that opened these died with the old password,
    so the rows are already unreadable and keeping them would only be litter."""
    conn.execute("DELETE FROM user_secrets WHERE user_id=?", (user_id,))
    conn.commit()


# --- sessions ---------------------------------------------------------------

def create_session(conn: sqlite3.Connection, user_id: int, dek: bytes) -> str:
    """Mint an opaque session token, store only its sha256, return the raw token to the client.

    The `dek` is sealed into the row under a KEK derived from the **raw** token — the half that walks
    out the door and is never written down. That is what lets every later request reach this User's
    Credentials without their password, and it is why the wrap is worthless to anyone reading head.db.
    Required, not optional: the invariant everything downstream leans on is that a live Session
    always has a DEK."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    expires = (_now() + timedelta(days=SESSION_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO sessions (token_sha256, user_id, expires_at, dek_wrapped_tok) VALUES (?,?,?,?)",
        (_sha256(token), user_id, expires, credentials.wrap(credentials.token_kek(token), dek)),
    )
    conn.commit()
    return token


def lookup_session(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """Resolve a token to its user, or None if unknown/expired. Expiry checked in SQL (UTC).

    Selects the session's wrapped DEK alongside the user row, so `current_user` can unwrap it without
    a second query. `u.*` first means a caller reading `row["id"]` still gets the *user* id."""
    return conn.execute(
        """SELECT u.*, s.dek_wrapped_tok FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_sha256 = ? AND s.expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now')""",
        (_sha256(token),),
    ).fetchone()


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_sha256=?", (_sha256(token),))
    conn.commit()


def purge_expired(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "DELETE FROM sessions WHERE expires_at <= strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    )
    conn.commit()
    return cur.rowcount


# --- spawn ledger -------------------------------------------------------------

def record_spawn(conn: sqlite3.Connection, job_id: int, user_id: int, script: str,
                 staging_uuid: str, minutes: int = 0, gpus: int = 0, vram_gb: int = 0,
                 node: str | None = None, hold: float | None = None,
                 tidepool: bool = False, recipe_name: str | None = None,
                 name: str = "") -> None:
    """Attribute a submitted Spawn to its user (keyed by the Slurm job id) and place its Escrow — in
    ONE transaction. A Spawn that existed without its hold, even briefly, would be a Spawn its
    submitter's Share had not yet been charged for, which is exactly the window a queue-flooder wants.

    `hold` is None for a Spawn nobody is charged for: the **Admin** is outside the economy, so their
    Spawns get an Attribution row (the Dashboard still says who submitted what) and no Escrow at all.

    `recipe_name` is set only for a Spawn made by `recipes.run` — it's what `running_recipe_names`
    reads back to tell a Recipe's card it has a live Spawn on the queue right now. `name` is a
    different thing entirely and both can be set at once: the **Name** a person reads, which a Spawn
    from a Recipe *inherits* from that Recipe. One badges a card, the other titles a row.
    """
    conn.execute(
        """INSERT INTO spawns (job_id, user_id, script, staging_uuid, minutes, gpus, vram_gb, node,
                              tidepool, recipe_name, name)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (job_id, user_id, script, staging_uuid, minutes, gpus, vram_gb, node, int(tidepool),
         recipe_name, name),
    )
    if hold is not None:
        conn.execute("INSERT INTO karma_escrow (job_id, user_id, hold) VALUES (?,?,?)",
                     (job_id, user_id, hold))
    conn.commit()


def get_spawn(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM spawns WHERE job_id=?", (job_id,)).fetchone()


def spawns_by_staging_uuid(conn: sqlite3.Connection, uuids: list[str]) -> dict[str, sqlite3.Row]:
    """Map staging uuid -> its Spawn's `(job_id, finished_at)`, for the uuids currently on disk.

    `projects.sweep`'s one query per pass. Takes the disk's list rather than returning the whole table
    because that list is bounded by what has not been swept yet — a handful — while the `spawns` ledger
    grows forever. A uuid absent from the result is one no Spawn (yet) claims; see that function."""
    if not uuids:
        return {}
    marks = ",".join("?" * len(uuids))
    rows = conn.execute(
        f"SELECT staging_uuid, job_id, finished_at FROM spawns WHERE staging_uuid IN ({marks})", uuids,
    ).fetchall()
    return {r["staging_uuid"]: r for r in rows}


def spawn_labels(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, dict]:
    """Map job_id -> the two things the live `GET /spawns` queue cannot learn from Slurm: who submitted
    it (**Attribution** — Slurm says `ccc` for every Spawn, so only this ledger knows) and what it is
    **Name**d.

    The Name is here rather than on the Slurm job because it cannot safely be there: `--job-name` is
    substituted into a bash job script, so a User's own words in it would be a shell injection. The
    queue reads it back over the same join Attribution already needed, which is why this answers both
    at once instead of costing a second query per poll.

    A job id with no row (submitted outside CCC, or already reaped) is simply absent — the caller fills
    in its own default rather than being handed a fabricated one.
    """
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    rows = conn.execute(
        f"""SELECT s.job_id, s.name, u.username FROM spawns s
            LEFT JOIN users u ON u.id = s.user_id WHERE s.job_id IN ({marks})""",
        job_ids,
    ).fetchall()
    return {r["job_id"]: {"submitted_by": r["username"], "name": r["name"]} for r in rows}


def spawn_owners(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, int]:
    """Map job_id -> the user_id whose STANDING ranks this Spawn. The Karma tick's whole link from a
    queued Slurm job back to a person: Slurm says every Spawn belongs to `ccc`, so only this ledger
    knows whose standing to apply.

    Two kinds of Spawn are deliberately ABSENT, and the tick skips whatever is absent — so both take
    the same path, and it needs no branch for either:
      * **no Attribution row** — the enrollment benchmark, which the CLI sbatches outside the API.
      * **submitted by an Admin** — the Admin is outside the economy: they earn no Karma, pay none, and
        hold no Balance, so there is no standing to rank their Spawns by.
    Both therefore keep nice 0 and outrank queued work. For the benchmark that is wanted (admitting an
    Arm should not wait behind a poor-standing User's backlog); for the Admin it is the privilege of the
    role. See temp/KARMA.md.
    """
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    rows = conn.execute(
        f"""SELECT s.job_id, s.user_id FROM spawns s JOIN users u ON u.id = s.user_id
            WHERE u.is_admin = 0 AND s.job_id IN ({marks})""",
        job_ids,
    ).fetchall()
    return {r["job_id"]: r["user_id"] for r in rows}


def spawn_needs(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, sqlite3.Row]:
    """Map job_id -> what the User declared (gpus, vram_gb, node). What a queued Spawn is *eligible*
    for, and therefore whose Demand it adds to.

    Unlike spawn_owners this does NOT exclude Admins: an Admin's queued Spawn is a real claim on the
    cards it could run on, and every other User's price should reflect that it is waiting. The Admin is
    outside the economy as a *payer*, not as a *body in the queue*.
    """
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    rows = conn.execute(
        f"SELECT job_id, user_id, gpus, vram_gb, node FROM spawns WHERE job_id IN ({marks})", job_ids
    ).fetchall()
    return {r["job_id"]: r for r in rows}


def unfinished_spawns(conn: sqlite3.Connection) -> list[int]:
    """Every Spawn with no ending recorded yet — the tick's candidates for outcome-stamping. Once
    stamped (finished_at set) a Spawn drops out, so after the first pass this is just the live queue plus
    whatever ended in the last 30s."""
    return [r["job_id"] for r in
            conn.execute("SELECT job_id FROM spawns WHERE finished_at IS NULL").fetchall()]


def mark_finished(conn: sqlite3.Connection, job_id: int, exit_code: int | None, at: str) -> None:
    """Stamp a Spawn's ending, once. Idempotent on `finished_at`: a later pass cannot overwrite the code
    with a NULL once the exit file has aged out, and a Spawn's recorded end is its first observed one."""
    conn.execute(
        "UPDATE spawns SET exit_code=?, finished_at=? WHERE job_id=? AND finished_at IS NULL",
        (exit_code, at, job_id))
    conn.commit()


# The archive row, shared by the many-row read (`spawn_history`) and the one-row read
# (`spawn_detail`). One string rather than two, because the Spawn page and the History row show the
# same facts about the same thing and drifting them apart is how a Spawn comes to cost one number in
# the list and another on its own page. The settled cost joins in from the ledger's `settle` row (a
# Spawn that never settled — Admin-owned, or ended before its escrow was metered — has NULL cost,
# which reads as `—`).
#
# recovery_copies joins in NULL for the (common) case of no open Recovery Window — main.py turns
# `recovery_created_at` into the wire-facing `recovery_expires_at`, since the window length
# (recovery.WINDOW_MINUTES) is that module's constant, not this one's business. output_copies rides
# alongside it on exactly the same terms (retention.TTL_DAYS), so a row carries both countdowns or
# neither, and the Dashboard renders the pair the same way.
_SPAWN_ROW = """SELECT s.job_id, s.submitted_at, s.finished_at, s.exit_code, s.script, s.name,
                       s.gpus, s.vram_gb, s.minutes, s.node, s.placed_node,
                       u.username AS submitted_by,
                       l.amount AS cost, r.bytes AS recovery_bytes, r.created_at AS recovery_created_at,
                       o.bytes AS output_bytes, o.created_at AS output_created_at
                FROM spawns s
                LEFT JOIN users u ON u.id = s.user_id
                LEFT JOIN karma_ledger l ON l.job_id = s.job_id AND l.kind = 'settle'
                LEFT JOIN recovery_copies r ON r.job_id = s.job_id
                LEFT JOIN output_copies o ON o.job_id = s.job_id"""


def spawn_history(conn: sqlite3.Connection, since: str, exclude: list[int],
                  limit: int | None = None) -> list[sqlite3.Row]:
    """Every Spawn submitted since `since`, minus the ids in `exclude` (the live queue) — the durable
    archive, newest first. Everyone's, like the live queue: what ran on the shared cluster is not
    private. `limit` caps the result to the newest N (None = every row, the Dashboard's read); the CLI
    passes it for readability."""
    # `NOT IN ()` has no valid SQL spelling — `NOT IN (NULL)` is UNKNOWN for every row and drops the
    # whole table — so the exclusion is a clause only when there is something live to exclude.
    clause = f" AND s.job_id NOT IN ({','.join('?' * len(exclude))})" if exclude else ""
    limit_clause = " LIMIT ?" if limit else ""
    return conn.execute(
        f"""{_SPAWN_ROW}
            WHERE s.submitted_at >= ?{clause}
            ORDER BY s.submitted_at DESC{limit_clause}""",
        (since, *exclude, *((limit,) if limit else ())),
    ).fetchall()


def spawn_detail(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    """One Spawn's archive row — the same shape `spawn_history` serves, for a Spawn the caller names.

    Deliberately indifferent to whether the Spawn is still running: `submitted_at`, `script`, `node` and
    the shape it asked for are all recorded at submit, and the rest fill in as they become true. That is
    what lets `GET /spawns/{job_id}` answer with one payload whether the Spawn is queued, running or a
    year old — the alternative was a Dashboard that fetched the whole cluster archive to render one page.
    """
    return conn.execute(f"{_SPAWN_ROW}\n WHERE s.job_id = ?", (job_id,)).fetchone()


# --- Karma ledger --------------------------------------------------------------

def record_karma(conn: sqlite3.Connection, kind: str, amount: float, from_user: int | None,
                 to_user: int | None, job_id: int | None = None, note: str | None = None,
                 at: str | None = None) -> None:
    """Append one Karma event. `amount` is always positive — direction is carried by which side is
    NULL: `from` NULL = conjured, `to` NULL = destroyed, both set = a Transfer (and a Transfer is the
    only kind that leaves Σ balances at 0).

    `at` overrides the row's timestamp, and it is **not** cosmetic: Decay reads it, so the Fake Cluster
    must be able to say a settlement happened in a simulated week rather than right now. In production
    it is None and SQLite stamps the wall clock."""
    if at is None:
        conn.execute(
            "INSERT INTO karma_ledger (kind, amount, from_user, to_user, job_id, note) "
            "VALUES (?,?,?,?,?,?)",
            (kind, amount, from_user, to_user, job_id, note),
        )
    else:
        conn.execute(
            "INSERT INTO karma_ledger (at, kind, amount, from_user, to_user, job_id, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (at, kind, amount, from_user, to_user, job_id, note),
        )
    conn.commit()


def karma_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """The whole ledger. Balances are summed from it in Python, not SQL: the decay factor needs exp(),
    which SQLite only has when compiled with SQLITE_ENABLE_MATH_FUNCTIONS. A settle is one row, so this
    grows by thousands of rows a year, and a row older than a year contributes exp(-12) ≈ 6e-6."""
    return conn.execute("SELECT at, amount, from_user, to_user FROM karma_ledger").fetchall()


def escrow_totals(conn: sqlite3.Connection) -> dict[int, float]:
    """user_id -> outstanding claim. Not decayed: an Escrow is a live claim, not history.

    `MAX(hold, accrued)` — SQLite's *scalar* max — is the ratchet, and it is the whole reason this is
    one table rather than two. The hold is a FLOOR (so an under-quoted forecast cannot be exploited);
    the meter raises it if the Spawn's real cost overtakes it (so a Demand spike is felt in the
    submitter's Share within 30s, not at Settlement). The claim therefore only ever goes UP, and a
    Spawn can never quietly cheapen its own submitter's standing while it runs.
    """
    rows = conn.execute(
        "SELECT user_id, SUM(MAX(hold, accrued)) AS total FROM karma_escrow GROUP BY user_id"
    ).fetchall()
    return {r["user_id"]: r["total"] for r in rows}


def escrow_breakdown(conn: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    """user_id -> the individual claims that sum to `escrow_totals`, dearest first.

    The same `MAX(hold, accrued)` ratchet, left un-summed — the aggregate says a User is holding 140
    Karma, this says *which Spawns are holding it*, which is the only form of the number a User can act
    on (one of them is the sweep they forgot to cancel). Carries the Spawn's script and node so the
    Dashboard can name each claim without a second fetch; both are NULL-safe (a claim always has a
    spawns row, since the escrow is opened by the same submit that writes it).
    """
    rows = conn.execute(
        """SELECT e.user_id, e.job_id, MAX(e.hold, e.accrued) AS amount, e.node, s.script
           FROM karma_escrow e
           LEFT JOIN spawns s ON s.job_id = e.job_id
           ORDER BY amount DESC"""
    ).fetchall()
    out: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        out[r["user_id"]].append(r)
    return dict(out)


def usage_series(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """Karma spent per User per UTC day since `since` — who has been consuming the cluster.

    Settlements only, and *spend* only (`from_user`): a settle row is the one place the economy states
    what a finished Spawn actually cost, already weighted by the card's Power and the Demand it ran
    against. Raw GPU-hours would have called an hour on a quiet little card the same as an hour on the
    coveted one, which is exactly the distinction the Tide exists to draw.

    Undecayed on purpose — this is what a User spent at the time, not what that spending is worth to
    their standing now. Decay belongs to the Balance; usage is history and history does not fade.
    """
    return conn.execute(
        """SELECT from_user AS user_id, substr(at, 1, 10) AS day, SUM(amount) AS spent
           FROM karma_ledger
           WHERE kind = 'settle' AND from_user IS NOT NULL AND at >= ?
           GROUP BY user_id, day""",
        (since,),
    ).fetchall()


def live_escrows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every outstanding claim — the meter's working set. A row here means "this Spawn has not settled
    yet", whatever Slurm currently thinks."""
    return conn.execute(
        "SELECT job_id, user_id, hold, accrued, node, last_at FROM karma_escrow").fetchall()


def accrue(conn: sqlite3.Connection, job_id: int, node: str, accrued: float, last_at: str) -> None:
    """Advance one running Spawn's meter. `last_at` is stored so the NEXT pass bills from real elapsed
    time rather than assuming a tick fired — a restarted karma service must not lose money, and the
    User held the card across the gap regardless.

    Placement is recorded separately, by record_placement — it is not the meter's business and does not
    share its population.
    """
    conn.execute("UPDATE karma_escrow SET accrued=?, node=?, last_at=? WHERE job_id=?",
                 (accrued, node, last_at, job_id))
    conn.commit()


def record_placement(conn: sqlite3.Connection, job_id: int, node: str) -> None:
    """Write down which Arm a Spawn actually landed on, the first time we see it running.

    The escrow row already carries the live node, but `clear_escrow` deletes it at Settlement —
    precisely when History starts wanting it — so it is copied somewhere durable on the way past (see
    _SCHEMA_V14). Kept OFF the meter deliberately: an Admin's Spawn and the enrollment benchmark have
    no Escrow at all, and both still ran somewhere worth naming.

    Write-once (`WHERE placed_node IS NULL`): one write per Spawn rather than one per 30s pass, and a
    Spawn's placement cannot be rewritten by a later, confused reading. Commits, like every other writer
    here — the tick keeps no transaction of its own, and the Admin-Spawn path returns from the meter
    before anything else would commit for it.
    """
    conn.execute("UPDATE spawns SET placed_node=? WHERE job_id=? AND placed_node IS NULL",
                 (node, job_id))
    conn.commit()


def clear_escrow(conn: sqlite3.Connection, job_id: int) -> None:
    """Release the claim. Called only at Settlement, once the ledger row is written."""
    conn.execute("DELETE FROM karma_escrow WHERE job_id=?", (job_id,))
    conn.commit()


# --- Demand history (read by the Tide) ---------------------------------------

def record_demand(conn: sqlite3.Connection, at: str, demand: dict[str, float]) -> None:
    conn.executemany("INSERT INTO karma_demand (at, node, demand) VALUES (?,?,?)",
                     [(at, node, d) for node, d in demand.items()])
    conn.commit()


def _window_start(days: int, now: datetime | None = None) -> str:
    """The cutoff of a rolling window, as an ISO string. Computed in Python rather than SQL `'now'` so
    the Fake Cluster can slide the window through simulated time; `now=None` is the wall clock, which
    is every production caller."""
    return ((now or _now()) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def prune_demand(conn: sqlite3.Connection, days: int, now: datetime | None = None) -> int:
    """History, not evidence: a sample older than the Tide's window forecasts nothing and prices
    nothing."""
    cur = conn.execute("DELETE FROM karma_demand WHERE at < ?", (_window_start(days, now),))
    conn.commit()
    return cur.rowcount


def prune_ledger(conn: sqlite3.Connection, days: int, now: datetime | None = None) -> int:
    """Drop Karma events past the decay curve's floor (karma.KARMA_LEDGER_RETENTION_DAYS).

    The one table here that is deleted from rather than kept: `spawns` and `users` are the permanent
    accounting record, but a Karma row that no longer moves any Balance is not evidence of anything —
    at this age `karma.decay` has it at ~5% and falling off a cliff. Without this the ledger grows for
    the cluster's whole life to keep rows nobody's standing can feel."""
    cur = conn.execute("DELETE FROM karma_ledger WHERE at < ?", (_window_start(days, now),))
    conn.commit()
    return cur.rowcount


def demand_by_hour(conn: sqlite3.Connection, days: int, now: datetime | None = None) -> list[sqlite3.Row]:
    """The **Tide**: mean Demand per Arm per hour-of-day, over a rolling window. 24 buckets per Arm."""
    return conn.execute(
        "SELECT node, CAST(strftime('%H', at) AS INTEGER) AS hour, AVG(demand) AS demand "
        "FROM karma_demand WHERE at >= ? GROUP BY node, hour",
        (_window_start(days, now),),
    ).fetchall()


def demand_series(conn: sqlite3.Connection, days: int, now: datetime | None = None) -> list[sqlite3.Row]:
    """Demand over *clock* time per Arm — the Dashboard's chart, and the only surface the Tide is ever
    seen through (a User reads its shape and infers the quiet hours; nothing quotes them a forecast).

    Bucketed to the hour, because the tick samples every 30s: a month of raw rows is ~86k *per Arm*.
    """
    return conn.execute(
        "SELECT node, strftime('%Y-%m-%dT%H:00:00Z', at) AS at, AVG(demand) AS demand "
        "FROM karma_demand WHERE at >= ? GROUP BY node, at ORDER BY at",
        (_window_start(days, now),),
    ).fetchall()


# --- the Ledger, as one User sees it -------------------------------------------

def ledger_rows(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """Every real event this User was a party to. **Only real rows** — there is no Decay row to fetch,
    because Decay is not an event: it is the sag of the Balance curve *between* these rows."""
    return conn.execute(
        "SELECT l.id, l.at, l.kind, l.amount, l.from_user, l.to_user, l.job_id, l.note, "
        "       f.username AS from_name, t.username AS to_name "
        "FROM karma_ledger l "
        "LEFT JOIN users f ON f.id = l.from_user "
        "LEFT JOIN users t ON t.id = l.to_user "
        "WHERE l.from_user = ? OR l.to_user = ? "
        "ORDER BY l.at, l.id",
        (user_id, user_id),
    ).fetchall()


# --- Arm Retract/Return ---------------------------------------------------------

def mark_arm_retracted(conn: sqlite3.Connection, node: str) -> None:
    conn.execute("INSERT OR IGNORE INTO arm_retractions (node) VALUES (?)", (node,))
    conn.commit()


def clear_arm_retraction(conn: sqlite3.Connection, node: str) -> None:
    """Return: reopen `node` for new placement. Never touches `retracted_jobs` — a Spawn already
    caught by the Retract is doomed regardless (see the table's docstring above)."""
    conn.execute("DELETE FROM arm_retractions WHERE node=?", (node,))
    conn.commit()


def retracted_nodes(conn: sqlite3.Connection) -> set[str]:
    return {r["node"] for r in conn.execute("SELECT node FROM arm_retractions").fetchall()}


def catch_retracting_job(conn: sqlite3.Connection, job_id: int, node: str, deadline: str) -> None:
    """Snapshot one Spawn caught by a Retract, at the moment it fired."""
    conn.execute("INSERT OR REPLACE INTO retracted_jobs (job_id, node, deadline) VALUES (?,?,?)",
                 (job_id, node, deadline))
    conn.commit()


def retracting_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every Spawn a Retract has ever caught that has not settled yet — the tick's working set for the
    force-kill deadline and the zero-billing it enforces at Settlement."""
    return conn.execute("SELECT job_id, node, deadline FROM retracted_jobs").fetchall()


def clear_retracting_job(conn: sqlite3.Connection, job_id: int) -> None:
    """Called only at Settlement, once the zero-billed ledger row is written."""
    conn.execute("DELETE FROM retracted_jobs WHERE job_id=?", (job_id,))
    conn.commit()


# --- Tide Pool -------------------------------------------------------------------
# See _SCHEMA_V8's comment for the table shapes. `privkey` is always encrypted (atrest.encrypt) going in
# and decrypted (atrest.decrypt) coming out — nothing outside this module ever sees the ciphertext.

def used_tidepool_ports(conn: sqlite3.Connection) -> set[int]:
    """Every port currently reserved — a Tide Pool being placed (job_id unknown yet) as well as a live
    one. This is the WHOLE point of the separate `tidepool_ports` table: a reservation has to exist
    before there is a job_id to key `tidepools` on."""
    return {r["port"] for r in conn.execute("SELECT port FROM tidepool_ports").fetchall()}


def reserve_tidepool_port(conn: sqlite3.Connection, port: int) -> bool:
    """Claim `port`, or report someone already has it (its PRIMARY KEY does the real work — this just
    turns the race into a retry instead of an IntegrityError the caller has to know to catch)."""
    try:
        conn.execute("INSERT INTO tidepool_ports (port) VALUES (?)", (port,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def release_tidepool_port(conn: sqlite3.Connection, port: int) -> None:
    conn.execute("DELETE FROM tidepool_ports WHERE port=?", (port,))
    conn.commit()


def create_tidepool(conn: sqlite3.Connection, job_id: int, port: int, pubkey: str,
                    privkey_plain: str) -> None:
    from . import atrest
    conn.execute("INSERT INTO tidepools (job_id, port, pubkey, privkey) VALUES (?,?,?,?)",
                 (job_id, port, pubkey, atrest.encrypt(privkey_plain)))
    conn.commit()


def get_tidepool(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tidepools WHERE job_id=?", (job_id,)).fetchone()


def tidepool_private_key(conn: sqlite3.Connection, job_id: int) -> str | None:
    """The plaintext private key for a live Tide Pool, or None if there is none (not a Tide Pool, or the
    at-rest key cannot be read — see atrest.py; either way there is nothing to hand the CLI)."""
    from . import atrest
    row = get_tidepool(conn, job_id)
    return atrest.decrypt(row["privkey"]) if row else None


def set_tidepool_relay(conn: sqlite3.Connection, job_id: int, relay_pid: int, node: str) -> None:
    conn.execute("UPDATE tidepools SET relay_pid=?, node=? WHERE job_id=?", (relay_pid, node, job_id))
    conn.commit()


def live_tidepools(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every Tide Pool's connection state — the tick's working set for reaping a dead one."""
    return conn.execute("SELECT job_id, port, relay_pid, node FROM tidepools").fetchall()


def delete_tidepool(conn: sqlite3.Connection, job_id: int) -> None:
    """Release the port and forget the keypair. Called once the Spawn has left the queue — after its
    relay subprocess has already been killed (tidepool.reap_dead), never before."""
    row = get_tidepool(conn, job_id)
    if row is None:
        return
    conn.execute("DELETE FROM tidepools WHERE job_id=?", (job_id,))
    conn.execute("DELETE FROM tidepool_ports WHERE port=?", (row["port"],))
    conn.commit()


# --- Recovery Window -------------------------------------------------------------
# See _SCHEMA_V9's comment for the table shape. All of this is driven by ccc_head_api.recovery, from the
# karma tick — nothing here decides policy (window length, size caps), it just reads/writes the ledger.

def recently_finished_without_recovery(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """`(job_id, user_id)` per Spawn that finished at or after `since` and has no recovery_copies row yet
    — register_new's candidates each pass. Bounded to a recent window on purpose: a Spawn whose copy
    never lands (skipped for size, or the Arm dropped mid-transfer) must not be re-checked forever, or
    this list only grows.

    `user_id` rides along because charging the per-User Allowance needs it before the row exists, so
    there is nothing to join through yet — the same reason `output_registration_rows` carries one. It is
    NULL for a Spawn whose submitter was deleted (see the Attribution ledger's ON DELETE SET NULL)."""
    return conn.execute(
        """SELECT s.job_id, s.user_id FROM spawns s
           LEFT JOIN recovery_copies r ON r.job_id = s.job_id
           WHERE s.finished_at >= ? AND r.job_id IS NULL""",
        (since,),
    ).fetchall()


def claimable_recovery_ids(conn: sqlite3.Connection, since: str) -> set[int]:
    """Every Spawn a copy landing on disk RIGHT NOW could still legitimately belong to: still running
    (no `finished_at` yet), or finished recently enough that `register_new` is still watching for its
    marker. `sweep_orphans`' inverse — a `recovery/` dir whose job_id is absent from this set is one no
    ledger row will ever claim, so it is safe to delete. Deliberately the complement of
    `recently_finished_without_recovery`'s window rather than a second policy: the two must agree, or
    the sweep would delete a copy the registrar was about to pick up."""
    return {r["job_id"] for r in conn.execute(
        "SELECT job_id FROM spawns WHERE finished_at IS NULL OR finished_at >= ?", (since,),
    ).fetchall()}


def add_recovery(conn: sqlite3.Connection, job_id: int, size_bytes: int, at: str | None = None) -> None:
    if at is None:
        conn.execute("INSERT INTO recovery_copies (job_id, bytes) VALUES (?,?)", (job_id, size_bytes))
    else:
        conn.execute("INSERT INTO recovery_copies (job_id, bytes, created_at) VALUES (?,?,?)",
                     (job_id, size_bytes, at))
    conn.commit()


def get_recovery(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM recovery_copies WHERE job_id=?", (job_id,)).fetchone()


def delete_recovery(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("DELETE FROM recovery_copies WHERE job_id=?", (job_id,))
    conn.commit()


def expired_recoveries(conn: sqlite3.Connection, cutoff: str) -> list[int]:
    """Every Spawn whose recovery copy was created at or before `cutoff` — reap_expired's candidates."""
    return [r["job_id"] for r in
            conn.execute("SELECT job_id FROM recovery_copies WHERE created_at <= ?", (cutoff,)).fetchall()]


def recovery_pool_bytes(conn: sqlite3.Connection) -> int:
    (total,) = conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM recovery_copies").fetchone()
    return total


def oldest_recovery(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The longest-held copy anywhere in the pool — `_make_room`'s last-resort eviction candidate, once
    the arriving User has nothing of their own left to give up."""
    return conn.execute(
        "SELECT job_id, bytes FROM recovery_copies ORDER BY created_at LIMIT 1").fetchone()


def user_recovery_bytes(conn: sqlite3.Connection, user_id: int) -> int:
    """What one User's open Recovery Windows currently hold, against their Allowance. Scoped through
    `spawns.user_id` rather than a column here, exactly as `user_output_bytes` is."""
    (total,) = conn.execute(
        """SELECT COALESCE(SUM(r.bytes), 0) FROM recovery_copies r
           JOIN spawns s ON s.job_id = r.job_id WHERE s.user_id = ?""", (user_id,)).fetchone()
    return total


def oldest_user_recovery(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """That User's longest-held copy — `_make_room`'s candidate for both of its passes. Theirs alone, so
    what displaces a User's older copy is only ever their own newer one (CONTEXT.md, **Allowance**)."""
    return conn.execute(
        """SELECT r.job_id, r.bytes FROM recovery_copies r
           JOIN spawns s ON s.job_id = r.job_id WHERE s.user_id = ?
           ORDER BY r.created_at LIMIT 1""", (user_id,)).fetchone()


# --- Output retention -------------------------------------------------------------
# See _SCHEMA_V11 for the table shape. Driven by ccc_head_api.retention from the karma tick; the TTL and
# the per-User Allowance are that module's constants, never decided here.

def output_registration_rows(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, sqlite3.Row]:
    """Map job_id -> `(user_id, finished_at, registered)` for the Spawns whose outputs are on disk.

    `registered` is the LEFT JOIN's "is there already a row" as a 0/1, so one query answers all three
    of `register_new`'s questions. Takes the disk's list for the same reason
    `spawns_by_staging_uuid` does: it is bounded by the TTL, while the ledger is not."""
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    rows = conn.execute(
        f"""SELECT s.job_id, s.user_id, s.finished_at, (o.job_id IS NOT NULL) AS registered
            FROM spawns s LEFT JOIN output_copies o ON o.job_id = s.job_id
            WHERE s.job_id IN ({marks})""",
        job_ids,
    ).fetchall()
    return {r["job_id"]: r for r in rows}


def add_output(conn: sqlite3.Connection, job_id: int, size_bytes: int, at: str | None = None) -> None:
    if at is None:
        conn.execute("INSERT INTO output_copies (job_id, bytes) VALUES (?,?)", (job_id, size_bytes))
    else:
        conn.execute("INSERT INTO output_copies (job_id, bytes, created_at) VALUES (?,?,?)",
                     (job_id, size_bytes, at))
    conn.commit()


def get_output(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM output_copies WHERE job_id=?", (job_id,)).fetchone()


def delete_output(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute("DELETE FROM output_copies WHERE job_id=?", (job_id,))
    conn.commit()


def expired_outputs(conn: sqlite3.Connection, cutoff: str) -> list[int]:
    """Every Spawn whose outputs were created at or before `cutoff` — reap_expired's candidates."""
    return [r["job_id"] for r in
            conn.execute("SELECT job_id FROM output_copies WHERE created_at <= ?", (cutoff,)).fetchall()]


def user_output_bytes(conn: sqlite3.Connection, user_id: int) -> int:
    """What one User currently holds, against their 32GB Allowance. Scoped through `spawns.user_id`
    rather than a column here — see _SCHEMA_V11 for why there isn't one."""
    (total,) = conn.execute(
        """SELECT COALESCE(SUM(o.bytes), 0) FROM output_copies o
           JOIN spawns s ON s.job_id = o.job_id WHERE s.user_id = ?""", (user_id,)).fetchone()
    return total


def oldest_output(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    """That User's longest-held result — `_make_room`'s eviction candidate. Theirs alone: one User's
    results can never evict another's (CONTEXT.md, **Allowance**)."""
    return conn.execute(
        """SELECT o.job_id, o.bytes FROM output_copies o
           JOIN spawns s ON s.job_id = o.job_id WHERE s.user_id = ?
           ORDER BY o.created_at LIMIT 1""", (user_id,)).fetchone()


# --- recipes ----------------------------------------------------------------

def recipes_for(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """One User's whole shelf, oldest first. There is no all-Users read anywhere in this module: a
    Recipe is private to its owner, so `GET /recipes` is the only reader and it is always scoped."""
    return conn.execute("SELECT * FROM recipes WHERE user_id=? ORDER BY frozen_at, id",
                        (user_id,)).fetchall()


def get_recipe(conn: sqlite3.Connection, user_id: int, name: str) -> sqlite3.Row | None:
    """Scoped by owner, never by name alone — two Users may hold the same name, and a lookup that
    forgot the owner would hand one User the other's."""
    return conn.execute("SELECT * FROM recipes WHERE user_id=? AND name=?",
                        (user_id, name)).fetchone()


def add_recipe(conn: sqlite3.Connection, user_id: int, name: str, script: str, args: list[str],
               outputs: list[str], gpus: int, vram_mb: int, minutes: int, wandb: bool,
               wandb_project: str, wandb_entity: str, size_bytes: int,
               manifest: str | None, features: list[str], substrate: str = "") -> sqlite3.Row:
    conn.execute(
        """INSERT INTO recipes (user_id, name, script, args, outputs, gpus, vram_mb, minutes,
                                wandb, wandb_project, wandb_entity, bytes, manifest, features,
                                substrate)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, name, script, json.dumps(args), json.dumps(outputs), gpus, vram_mb, minutes,
         int(wandb), wandb_project, wandb_entity, size_bytes, manifest, json.dumps(features),
         substrate))
    conn.commit()
    return get_recipe(conn, user_id, name)


def update_recipe_meta(conn: sqlite3.Connection, user_id: int, name: str, script: str, args: list[str],
                        outputs: list[str], gpus: int, vram_mb: int, minutes: int, wandb: bool,
                        wandb_project: str, wandb_entity: str, manifest: str | None,
                        features: list[str], substrate: str = "") -> sqlite3.Row:
    """Change what a Recipe runs and how, without touching its code or `bytes`/`frozen_at` — the settings-
    only sibling of `add_recipe`'s insert. `recipes.update_meta` has already checked `script` against the
    code already on disk, so this is a plain column write."""
    conn.execute(
        """UPDATE recipes SET script=?, args=?, outputs=?, gpus=?, vram_mb=?, minutes=?,
                              wandb=?, wandb_project=?, wandb_entity=?, manifest=?, features=?,
                              substrate=?
           WHERE user_id=? AND name=?""",
        (script, json.dumps(args), json.dumps(outputs), gpus, vram_mb, minutes,
         int(wandb), wandb_project, wandb_entity, manifest, json.dumps(features), substrate,
         user_id, name))
    conn.commit()
    return get_recipe(conn, user_id, name)


def delete_recipe(conn: sqlite3.Connection, user_id: int, name: str) -> bool:
    cur = conn.execute("DELETE FROM recipes WHERE user_id=? AND name=?", (user_id, name))
    conn.commit()
    return cur.rowcount > 0


def user_recipe_bytes(conn: sqlite3.Connection, user_id: int) -> int:
    """What one User's shelf holds, against their 32GB Allowance — the left side of the wall's
    arithmetic. Independent of `user_output_bytes` in both directions (CONTEXT.md, **Allowance**)."""
    (total,) = conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM recipes WHERE user_id=?",
                            (user_id,)).fetchone()
    return total


def running_recipe_names(conn: sqlite3.Connection, user_id: int, live_job_ids: list[int]) -> set[str]:
    """Which of this User's Recipes has a Spawn among `live_job_ids` (the current Slurm queue) right
    now — what `GET /recipes` reports back as `running`. `live_job_ids` is handed in rather than read
    here because the caller already polled Slurm once for the whole page; asking again per-Recipe would
    be a query per card instead of one for the shelf."""
    if not live_job_ids:
        return set()
    marks = ",".join("?" * len(live_job_ids))
    rows = conn.execute(
        f"""SELECT DISTINCT recipe_name FROM spawns
            WHERE user_id=? AND recipe_name IS NOT NULL AND job_id IN ({marks})""",
        (user_id, *live_job_ids),
    ).fetchall()
    return {r["recipe_name"] for r in rows}


def recipe_owner_ids(conn: sqlite3.Connection) -> set[int]:
    """Every User id that still exists — what `recipes.sweep_orphans` checks the directories against.
    Read from `users`, not from `recipes`: the CASCADE has already taken the rows of a deleted User by
    the time the sweep runs, so asking `recipes` would say every directory is legitimate.

    `workspaces.sweep_orphans` asks the same question of its own directories, and reuses this."""
    return {r["id"] for r in conn.execute("SELECT id FROM users").fetchall()}


# --- workspaces -------------------------------------------------------------
# Deliberately shaped like the Recipe accessors above — same owner-scoped lookups, same "the name is
# the handle" rule — because the two are siblings (CONTEXT.md: a Recipe is frozen and batch, a
# Workspace mutable and interactive). The claim helpers at the bottom have no Recipe counterpart: a
# Recipe can be run twice at once and a Workspace cannot.

def workspaces_for(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """One User's Workspaces, oldest first. Private to their owner, exactly like a shelf of Recipes."""
    return conn.execute("SELECT * FROM workspaces WHERE user_id=? ORDER BY created_at, id",
                        (user_id,)).fetchall()


def get_workspace(conn: sqlite3.Connection, user_id: int, name: str) -> sqlite3.Row | None:
    """Scoped by owner, never by name alone — two Users may both keep a "scratch"."""
    return conn.execute("SELECT * FROM workspaces WHERE user_id=? AND name=?",
                        (user_id, name)).fetchone()


def get_workspace_by_id(conn: sqlite3.Connection, ws_id: int) -> sqlite3.Row | None:
    """By row id, for the paths that hold one already — the submit path, and the tick reconciling
    claims. Unscoped on purpose: the caller reached an id by way of an owner check, or is the tick,
    which is nobody's."""
    return conn.execute("SELECT * FROM workspaces WHERE id=?", (ws_id,)).fetchone()


def add_workspace(conn: sqlite3.Connection, user_id: int, name: str, storage_bytes: int, gpus: int,
                  vram_mb: int, minutes: int, manifest: str, outputs: list[str],
                  features: list[str], substrate: str = "") -> sqlite3.Row:
    conn.execute(
        """INSERT INTO workspaces (user_id, name, storage_bytes, gpus, vram_mb, minutes, manifest,
                                   outputs, features, substrate)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (user_id, name, storage_bytes, gpus, vram_mb, minutes, manifest,
         json.dumps(outputs), json.dumps(features), substrate))
    conn.commit()
    return get_workspace(conn, user_id, name)


def update_workspace_meta(conn: sqlite3.Connection, user_id: int, name: str, storage_bytes: int,
                          gpus: int, vram_mb: int, minutes: int, manifest: str, outputs: list[str],
                          features: list[str], substrate: str = "") -> sqlite3.Row:
    """Change what a visit asks for, and the **Storage** reserved for the tree — never the tree itself.
    There is no upload sibling of this: a Workspace is seeded once and written to from inside its own
    sessions thereafter."""
    conn.execute(
        """UPDATE workspaces SET storage_bytes=?, gpus=?, vram_mb=?, minutes=?, manifest=?,
                                 outputs=?, features=?, substrate=?
           WHERE user_id=? AND name=?""",
        (storage_bytes, gpus, vram_mb, minutes, manifest, json.dumps(outputs), json.dumps(features),
         substrate, user_id, name))
    conn.commit()
    return get_workspace(conn, user_id, name)


def delete_workspace(conn: sqlite3.Connection, user_id: int, name: str) -> bool:
    cur = conn.execute("DELETE FROM workspaces WHERE user_id=? AND name=?", (user_id, name))
    conn.commit()
    return cur.rowcount > 0


def user_workspace_storage(conn: sqlite3.Connection, user_id: int, excluding: int | None = None) -> int:
    """What this User has already RESERVED across their Workspaces — the left side of the pool's
    arithmetic. Reserved, not used: the Allowance is divided up at creation (CONTEXT.md, **Storage**), so
    an empty 16 GiB Workspace costs its owner the whole 16 GiB of their pool. `excluding` drops one
    Workspace's own reservation, which is how a resize is weighed on what it GROWS BY."""
    (total,) = conn.execute(
        "SELECT COALESCE(SUM(storage_bytes), 0) FROM workspaces WHERE user_id=? AND id IS NOT ?",
        (user_id, excluding)).fetchone()
    return total


# The claim a visit holds while its `sbatch` is still in flight. A real job id cannot be the claim
# value here, because sbatch is what MINTS one — and claiming only after it returned would let two
# simultaneous opens both submit before either could lose, which is the exact failure the claim exists
# to prevent (two Arms `--delete`-mirroring one tree). So the claim is taken first with this, and
# rebound to the real id the moment sbatch answers.
#
# `last_visited_at` is stamped in the same statement so the tick can tell a submit in flight from one
# that died between the claim and sbatch — see `stale_workspace_claims`.
SUBMITTING = -1


def claim_workspace(conn: sqlite3.Connection, ws_id: int, job_id: int = SUBMITTING) -> bool:
    """Take the visit claim, or say it is already held. One statement, so two simultaneous `open`s
    cannot both win it and then both `--delete`-mirror the same tree from different Arms — the loser
    reads the winner's `live_job_id` back and attaches to that session instead."""
    cur = conn.execute(
        "UPDATE workspaces SET live_job_id=?, last_visited_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
        "WHERE id=? AND live_job_id IS NULL", (job_id, ws_id))
    conn.commit()
    return cur.rowcount > 0


def rebind_workspace_claim(conn: sqlite3.Connection, ws_id: int, job_id: int) -> None:
    """Point a held claim at the Spawn sbatch just returned. Scoped to `SUBMITTING` so this can only
    ever move a claim out of the in-flight state, never steal a live one."""
    conn.execute("UPDATE workspaces SET live_job_id=? WHERE id=? AND live_job_id=?",
                 (job_id, ws_id, SUBMITTING))
    conn.commit()


def release_workspace(conn: sqlite3.Connection, ws_id: int, job_id: int) -> None:
    """Drop a claim this exact Spawn holds. `job_id` is in the WHERE so a stale release — a submit that
    failed after the claim, racing a tick that already cleared it and an open that already re-claimed —
    cannot free somebody else's live visit."""
    conn.execute("UPDATE workspaces SET live_job_id=NULL WHERE id=? AND live_job_id=?", (ws_id, job_id))
    conn.commit()


def visited_workspace(conn: sqlite3.Connection, ws_id: int) -> None:
    conn.execute("UPDATE workspaces SET last_visited_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') "
                 "WHERE id=?", (ws_id,))
    conn.commit()


def stale_workspace_claims(conn: sqlite3.Connection, live_job_ids: list[int],
                           grace_minutes: int = 5) -> list[sqlite3.Row]:
    """Every Workspace whose claimed Spawn is no longer on the queue — what the karma tick clears.
    Reconciled rather than released by the session itself, because the ways a visit ends that nothing
    on the Head sees (a crashed Arm, a killed job, a Head restart) are exactly the ones that would
    otherwise strand a Workspace as permanently live and unopenable.

    A `SUBMITTING` claim is by definition not on the queue yet, so it is spared for `grace_minutes` —
    otherwise a tick landing between the claim and sbatch would free a Workspace out from under a
    submission about to succeed. Past the grace it IS collected: that is a submit which died holding
    the claim, and nothing else will ever release it.
    """
    stale = "live_job_id IS NOT NULL"
    args: tuple = ()
    if live_job_ids:
        stale += f" AND live_job_id NOT IN ({','.join('?' * len(live_job_ids))})"
        args = tuple(live_job_ids)
    return conn.execute(
        f"""SELECT * FROM workspaces WHERE {stale}
            AND (live_job_id <> ? OR last_visited_at IS NULL
                 OR last_visited_at < strftime('%Y-%m-%dT%H:%M:%SZ','now',?))""",
        (*args, SUBMITTING, f"-{grace_minutes} minutes")).fetchall()
