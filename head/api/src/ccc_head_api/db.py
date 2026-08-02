"""CCC Head auth store (Phase 3a slice 1) — the User + Session tables behind login.

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
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from . import atrest
from . import guises

# Path comes from the environment (the systemd unit sets CCC_HEAD_DB) so the API package never
# imports the CLI's config. Default matches cuttle.config.HEAD_DB.
DB_PATH = Path(os.environ.get("CCC_HEAD_DB", "/var/lib/ccc/head/head.db"))

SCHEMA_VERSION = 11
SESSION_TTL_DAYS = 30
# head.db has TWO writers from slice 1 on (the API: submit→escrow, override→ledger; the Karma tick:
# settle→ledger). WAL permits that, but a writer that collides with another gets SQLITE_BUSY back
# *immediately* unless it is told to wait — a submit landing during a settle would simply fail. The
# writes are milliseconds long, so any timeout that isn't zero is enough.
BUSY_TIMEOUT_MS = 5000
_TOKEN_BYTES = 32  # secrets.token_urlsafe → ~43 url-safe chars, 256 bits

_ph = PasswordHasher()

# Schema as a migration ladder keyed by PRAGMA user_version: each step takes the DB from version N-1
# to N, so a fresh DB runs the whole ladder and a deployed one runs only what it's missing (slice 1
# shipped v1; slice 2 adds the spawns ledger as v2).
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

# The Karma economy (Phase 4 slice 1). Two tables, and deliberately NOT a third holding balances:
#
# `karma_ledger` is ONE ROW PER EVENT, with both parties nullable — a NULL `from_user` is Karma
# conjured (an Override credit), a NULL `to_user` is Karma destroyed (a Spawn on a commons Arm, with no
# owner to pay). Σ of every User's balance is then a single query, and a settlement reads as a sentence.
#
# There is no `karma_balance` table because a Balance is a *consequence* of this ledger, not a separate
# truth: Decay is linear, so a decayed sum over the rows and a decay-on-touch running total are exactly
# — not approximately — equal. A cached balance could only ever disagree with the ledger, and a User's
# standing being quietly wrong forever, with the evidence sitting in a table nobody reads, is the worst
# failure this economy has. See karma.py for the arithmetic.
#
# `karma_escrow` is a live hold, not a movement: it suppresses a submitter's Share while their Spawn is
# outstanding (so flooding the queue instantly costs you your own queue position) and is released at
# Settlement. Created here, WRITTEN in slice 2 — Share's `- Escrow` term is exercised from day one
# rather than bolted on later. It does not decay: it is not history, it is an outstanding claim.
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

# The meter (Phase 4 slice 2). Two moves:
#
# 1. `spawns` learns what the User DECLARED. The meter needs a queued Spawn's needs to know which Arms
#    it is eligible for (and so which Arms' Demand it adds to), and its `minutes` to quote a hold. All
#    of it is known at submit and none of it is worth re-parsing out of Slurm's ~120-field job schema
#    every 30s. Eligibility itself is NOT frozen here — only the need is. The tick re-derives the Arm
#    set from the live registry each pass, so an Arm admitted after this Spawn queued joins its
#    eligible set, and a drained one leaves it (making the rest scarcer, which is correct).
#
# 2. `karma_escrow` becomes the Spawn's **outstanding claim** — and therefore the meter itself. It was
#    created empty in v3 and never written, so it is simply rebuilt here. It holds both:
#      * `hold`    — the forecast, quoted once at submit. A FLOOR under the claim.
#      * `accrued` — what the Spawn has really cost so far, ticked while it RUNS. A RATCHET on top.
#    Share subtracts MAX(hold, accrued), so the claim only ever goes UP: an under-quoted hold cannot be
#    exploited, and a Demand spike that overshoots the forecast is felt in the submitter's standing
#    immediately rather than being a surprise at Settlement. At Settlement the row is DELETED and one
#    ledger row (= accrued) is written; the escrow is a live claim, never history.
#
# `karma_demand` is history ONLY — never a pricing path. The tick consumes each sample the instant it
# takes it; these rows exist so slice 3's Tide can forecast Demand by hour-of-day and the Dashboard can
# chart it. Raw (one row per Arm per tick), pruned at 30 days: ~10MB/month at 3 Arms, and the raw series
# is what the per-machine chart wants.
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

# Arm Retract/Return (Phase 2 loose end — an owner's self-service pause of their own machine). Two
# tables because the two things they track have different lifetimes:
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
# BLOB, not TEXT: it is stored ENCRYPTED (AES-256-GCM, see atrest.py). Unlike a password, a W&B key must
# come back out in plaintext to be usable, so it cannot be hashed. The keyfile lives beside this
# database — which protects a head.db that leaks on its own (a copy, a backup archive, a pulled disk)
# and nothing more. atrest.py's docstring is honest about the limit.
_SCHEMA_V7 = """
ALTER TABLE users ADD COLUMN wandb_key BLOB;
"""

# Tide Pool (docs/TODO.md, now built): a Spawn whose payload is a live shell instead of a script.
# `spawns.tidepool` marks which ones (submit_checked skips the outputs/script requirements for them).
# Two tables because the port has to be reserved BEFORE the job_id exists (it is baked into the job
# script, rendered before sbatch ever runs — sshd inside the Pod and the Head's relay both bind it), and
# `tidepools.job_id` cannot be a key nothing has yet:
#   * `tidepool_ports` — the reservation itself, keyed by the port alone. Its own table so two
#                  submissions racing the same "lowest free port" collide on ITS primary key (caught and
#                  retried — see tidepool.reserve_port) instead of racing a job_id neither has yet.
#   * `tidepools`  — everything else, written once the job_id is known (mirrors record_spawn, written
#                  after sbatch returns). `port` is copied in here too, for reads that only care about one
#                  Tide Pool's state and would otherwise need a second table just to find its port.
#     - `pubkey`/`privkey` — a keypair minted once at submit and baked into the job script's
#                  authorized_keys (pubkey) — never regenerated, so a reattach after a dropped connection
#                  or a Dashboard-requested Tide Pool gets the SAME key back. `privkey` is encrypted at
#                  rest (atrest.py) exactly like a W&B key: a third-party-shaped secret that must come
#                  back out in plaintext to be handed to the CLI, so it cannot be hashed.
#     - `relay_pid` — the Head-side TCP relay subprocess forwarding this port to the Arm, once started
#                  (tidepool.ensure_relay). NULL until the Spawn is actually RUNNING somewhere.
#     - `node`     — the Arm the relay is forwarding to, cached so a restarted relay's liveness check does
#                  not have to re-derive it from squeue every poll.
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

# Recovery Window (docs/TODO.md, now built): one row per Spawn currently holding a copy of its
# undeclared working dir on the Head (ccc_head_api.recovery). `bytes` feeds the 50GB pool cap (SUM
# across every row); `created_at` is both the 30-minute expiry clock AND the oldest-first eviction
# order — the same column does both jobs because they're the same question ("how long has this been
# held?") asked two ways. A row here always has a matching `SPAWNS_DIR/<id>/recovery/` dir on disk;
# recovery.py's `_evict` deletes both together, never one without the other.
_SCHEMA_V9 = """
CREATE TABLE recovery_copies (
  job_id     INTEGER PRIMARY KEY REFERENCES spawns(job_id) ON DELETE CASCADE,
  bytes      INTEGER NOT NULL,
  created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""

# **Guises** (temp/spawn-isolation.md Phase 3): the uid a User's Spawns run as, on every node. See
# guises.py for what one is; this table is only the allocator, and it has exactly one job the obvious
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

# Output retention (docs/TODO.md "Storage retention", phase 2). Exactly `recovery_copies`' shape, one
# row per Spawn whose declared $CCC_OUT results the Head is holding, and `created_at` again does both
# jobs — the 7-day expiry clock AND the oldest-first eviction order. It is copied from the Spawn's
# `finished_at`, not stamped at registration, so a Spawn registered late by a tick that was down still
# expires 7 days after it RAN (see retention.py). A row here always has a matching
# `SPAWNS_DIR/<id>/outputs/` dir on disk; `retention._evict` deletes both together.
#
# No user_id column: the 8GB Allowance is per User, but `spawns.user_id` already says whose a Spawn is,
# and duplicating it here would be a second answer that could disagree with the first — including after
# `remove_user` NULLs the original.
_SCHEMA_V11 = """
CREATE TABLE output_copies (
  job_id     INTEGER PRIMARY KEY REFERENCES spawns(job_id) ON DELETE CASCADE,
  bytes      INTEGER NOT NULL,
  created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""

# index i takes user_version i -> i+1
_MIGRATIONS = [_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4, _SCHEMA_V5, _SCHEMA_V6, _SCHEMA_V7,
              _SCHEMA_V8, _SCHEMA_V9, _SCHEMA_V10, _SCHEMA_V11]


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
    a umask of 077 would trim that — so this mode has to be SET, never inherited. Measured on the
    deployed Head 2026-08-01: head.db was world-readable, and since **Guises** landed that means every
    User's Spawn could read the password hashes and session tokens in it. HEAD_STATE_DIR is 0711
    (traverse, never list) precisely on the assumption that it could not.

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
    """Admin-mediated recovery (Phase 8 loose end — see docs/TODO.md's "No password recovery exists"):
    a fresh temp password + forced change, exactly like `add_user`, so a forgotten password no longer
    means delete-and-recreate (which would wipe the User's Karma balance and orphan their Arms).

    Every existing session for this User is killed — a reset is a security action, not just a password
    change. Returns the plaintext temp password, or None if there is no such user."""
    user = get_user(conn, username)
    if user is None:
        return None
    temp_pw = secrets.token_urlsafe(12)
    conn.execute("UPDATE users SET pw_hash=?, must_change_password=1 WHERE username=?",
                 (_ph.hash(temp_pw), username))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
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


def set_password(conn: sqlite3.Connection, username: str, new_password: str) -> None:
    conn.execute(
        "UPDATE users SET pw_hash=?, must_change_password=0 WHERE username=?",
        (_ph.hash(new_password), username),
    )
    conn.commit()


# A precomputed hash so verify_password does equal work whether or not the user exists.
_DUMMY_HASH = _ph.hash("__ccc_dummy__")


# --- Guises (temp/spawn-isolation.md Phase 3) -------------------------------
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


# --- the stored W&B key -----------------------------------------------------
# One key per User, encrypted at rest (see atrest.py and _SCHEMA_V7). The Head holds it so BOTH clients
# get metrics: the Dashboard has no config file to read a key out of, and even the CLI no longer keeps
# one on disk. Every read goes through atrest.decrypt, which answers None rather than raising when the
# blob cannot be recovered — so an unreadable key is simply an unset one, everywhere below.

def set_wandb_key(conn: sqlite3.Connection, user_id: int, key: str) -> None:
    conn.execute("UPDATE users SET wandb_key=? WHERE id=?", (atrest.encrypt(key), user_id))
    conn.commit()


def get_wandb_key(conn: sqlite3.Connection, user_id: int) -> str | None:
    """This User's W&B key in plaintext, or None if they have none stored (or it cannot be read)."""
    row = conn.execute("SELECT wandb_key FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None or row["wandb_key"] is None:
        return None
    return atrest.decrypt(row["wandb_key"])


def wandb_key_status(conn: sqlite3.Connection, user_id: int) -> dict:
    """The masked view: is one stored, and its last 4 characters — enough to tell "the key I pasted"
    from "some other key" without ever putting the secret on the wire."""
    key = get_wandb_key(conn, user_id)
    return {"set": key is not None, "last4": key[-4:] if key else None}


def clear_wandb_key(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET wandb_key=NULL WHERE id=?", (user_id,))
    conn.commit()


# --- sessions ---------------------------------------------------------------

def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    """Mint an opaque session token, store only its sha256, return the raw token to the client."""
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    expires = (_now() + timedelta(days=SESSION_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO sessions (token_sha256, user_id, expires_at) VALUES (?,?,?)",
        (_sha256(token), user_id, expires),
    )
    conn.commit()
    return token


def lookup_session(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """Resolve a token to its user, or None if unknown/expired. Expiry checked in SQL (UTC)."""
    return conn.execute(
        """SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id
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


# --- spawn ledger (slice 2) -------------------------------------------------

def record_spawn(conn: sqlite3.Connection, job_id: int, user_id: int, script: str,
                 staging_uuid: str, minutes: int = 0, gpus: int = 0, vram_gb: int = 0,
                 node: str | None = None, hold: float | None = None,
                 tidepool: bool = False) -> None:
    """Attribute a submitted Spawn to its user (keyed by the Slurm job id) and place its Escrow — in
    ONE transaction. A Spawn that existed without its hold, even briefly, would be a Spawn its
    submitter's Share had not yet been charged for, which is exactly the window a queue-flooder wants.

    `hold` is None for a Spawn nobody is charged for: the **Admin** is outside the economy, so their
    Spawns get an Attribution row (the Dashboard still says who submitted what) and no Escrow at all.
    """
    conn.execute(
        """INSERT INTO spawns (job_id, user_id, script, staging_uuid, minutes, gpus, vram_gb, node,
                              tidepool)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (job_id, user_id, script, staging_uuid, minutes, gpus, vram_gb, node, int(tidepool)),
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


def spawn_submitters(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, str | None]:
    """Map job_id -> submitter username for the given ids (None if the user was since deleted).
    Used to enrich the live `GET /spawns` queue with app-level attribution."""
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    rows = conn.execute(
        f"""SELECT s.job_id, u.username FROM spawns s
            LEFT JOIN users u ON u.id = s.user_id WHERE s.job_id IN ({marks})""",
        job_ids,
    ).fetchall()
    return {r["job_id"]: r["username"] for r in rows}


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


def spawn_history(conn: sqlite3.Connection, since: str, exclude: list[int],
                  limit: int | None = None) -> list[sqlite3.Row]:
    """Every Spawn submitted since `since`, minus the ids in `exclude` (the live queue) — the durable
    archive, newest first. The settled cost joins in from the ledger's `settle` row (a Spawn that never
    settled — Admin-owned, or ended before its escrow was metered — has NULL cost, which reads as `—`).
    Everyone's, like the live queue: what ran on the shared cluster is not private. `limit` caps the
    result to the newest N (None = every row, the Dashboard's read); the CLI passes it for readability."""
    # `NOT IN ()` has no valid SQL spelling — `NOT IN (NULL)` is UNKNOWN for every row and drops the
    # whole table — so the exclusion is a clause only when there is something live to exclude.
    clause = f" AND s.job_id NOT IN ({','.join('?' * len(exclude))})" if exclude else ""
    limit_clause = " LIMIT ?" if limit else ""
    # recovery_copies joins in NULL for the (common) case of no open Recovery Window — main.py's
    # spawns_history route turns `recovery_created_at` into the wire-facing `recovery_expires_at`, since
    # the window length (recovery.WINDOW_MINUTES) is that module's constant, not this one's business.
    # output_copies rides alongside it on exactly the same terms (retention.TTL_DAYS), so a row carries
    # both countdowns or neither, and the Dashboard renders the pair the same way.
    return conn.execute(
        f"""SELECT s.job_id, s.submitted_at, s.finished_at, s.exit_code, s.script,
                   s.gpus, s.vram_gb, s.minutes, s.node, u.username AS submitted_by,
                   l.amount AS cost, r.bytes AS recovery_bytes, r.created_at AS recovery_created_at,
                   o.bytes AS output_bytes, o.created_at AS output_created_at
            FROM spawns s
            LEFT JOIN users u ON u.id = s.user_id
            LEFT JOIN karma_ledger l ON l.job_id = s.job_id AND l.kind = 'settle'
            LEFT JOIN recovery_copies r ON r.job_id = s.job_id
            LEFT JOIN output_copies o ON o.job_id = s.job_id
            WHERE s.submitted_at >= ?{clause}
            ORDER BY s.submitted_at DESC{limit_clause}""",
        (since, *exclude, *((limit,) if limit else ())),
    ).fetchall()


# --- Karma ledger (slice 1) -------------------------------------------------

def record_karma(conn: sqlite3.Connection, kind: str, amount: float, from_user: int | None,
                 to_user: int | None, job_id: int | None = None, note: str | None = None,
                 at: str | None = None) -> None:
    """Append one Karma event. `amount` is always positive — direction is carried by which side is
    NULL: `from` NULL = conjured, `to` NULL = destroyed, both set = a Transfer (and a Transfer is the
    only kind that leaves Σ balances at 0).

    `at` overrides the row's timestamp, and it is **not** cosmetic: Decay reads it, so the Fake Cluster
    (slice 4) must be able to say a settlement happened in a simulated week rather than right now. In
    production it is None and SQLite stamps the wall clock."""
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


def live_escrows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every outstanding claim — the meter's working set. A row here means "this Spawn has not settled
    yet", whatever Slurm currently thinks."""
    return conn.execute(
        "SELECT job_id, user_id, hold, accrued, node, last_at FROM karma_escrow").fetchall()


def accrue(conn: sqlite3.Connection, job_id: int, node: str, accrued: float, last_at: str) -> None:
    """Advance one running Spawn's meter. `last_at` is stored so the NEXT pass bills from real elapsed
    time rather than assuming a tick fired — a restarted karma service must not lose money, and the
    User held the card across the gap regardless."""
    conn.execute("UPDATE karma_escrow SET accrued=?, node=?, last_at=? WHERE job_id=?",
                 (accrued, node, last_at, job_id))
    conn.commit()


def clear_escrow(conn: sqlite3.Connection, job_id: int) -> None:
    """Release the claim. Called only at Settlement, once the ledger row is written."""
    conn.execute("DELETE FROM karma_escrow WHERE job_id=?", (job_id,))
    conn.commit()


# --- Demand history (slice 2; read by slice 3's Tide) -----------------------

def record_demand(conn: sqlite3.Connection, at: str, demand: dict[str, float]) -> None:
    conn.executemany("INSERT INTO karma_demand (at, node, demand) VALUES (?,?,?)",
                     [(at, node, d) for node, d in demand.items()])
    conn.commit()


def _window_start(days: int, now: datetime | None = None) -> str:
    """The cutoff of a rolling window, as an ISO string. Computed in Python rather than SQL `'now'` so
    the Fake Cluster (slice 4) can slide the window through simulated time; `now=None` is the wall clock,
    which is every production caller."""
    return ((now or _now()) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def prune_demand(conn: sqlite3.Connection, days: int, now: datetime | None = None) -> int:
    """History, not evidence: a sample older than the Tide's window forecasts nothing and prices
    nothing."""
    cur = conn.execute("DELETE FROM karma_demand WHERE at < ?", (_window_start(days, now),))
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


# --- the Ledger, as one User sees it (slice 3) ------------------------------

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


# --- Arm Retract/Return (Phase 2 loose end) ---------------------------------

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


# --- Tide Pool (docs/TODO.md, now built) -------------------------------------
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


# --- Recovery Window (docs/TODO.md, now built) -------------------------------
# See _SCHEMA_V9's comment for the table shape. All of this is driven by ccc_head_api.recovery, from the
# karma tick — nothing here decides policy (window length, size caps), it just reads/writes the ledger.

def recently_finished_without_recovery(conn: sqlite3.Connection, since: str) -> list[int]:
    """Spawns that finished at or after `since` and have no recovery_copies row yet — register_new's
    candidates each pass. Bounded to a recent window on purpose: a Spawn whose copy never lands (skipped
    for size, or the Arm dropped mid-transfer) must not be re-checked forever, or this list only grows."""
    return [r["job_id"] for r in conn.execute(
        """SELECT s.job_id FROM spawns s
           LEFT JOIN recovery_copies r ON r.job_id = s.job_id
           WHERE s.finished_at >= ? AND r.job_id IS NULL""",
        (since,),
    ).fetchall()]


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
    """The longest-held copy — `_make_room`'s eviction candidate when a new one would blow the pool cap."""
    return conn.execute(
        "SELECT job_id, bytes FROM recovery_copies ORDER BY created_at LIMIT 1").fetchone()


# --- Output retention (docs/TODO.md "Storage retention") ----------------------
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
    """What one User currently holds, against their 8GB Allowance. Scoped through `spawns.user_id`
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
