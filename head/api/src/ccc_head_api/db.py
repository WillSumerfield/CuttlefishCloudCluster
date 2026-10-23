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

# The whole schema, created once on a fresh head.db. 2.0 is a clean install (teardown + setup), so there
# is no migration ladder: a 1.x store is refused rather than upgraded (see `init_schema`).
#
# `users` / `sessions` — the auth store. Session tokens are stored hashed (see the module docstring).
#
# `spawns` — the Attribution ledger: one row per submitted Spawn, keyed by the Slurm job id (the spawn
# id). `user_id` is ON DELETE SET NULL so removing a user keeps the historical row (shown as "unknown"
# rather than vanishing). The single source of truth for who submitted what.
#   * `minutes`/`gpus`/`vram_gb`/`node` — what the User DECLARED, frozen at submit. The tick needs a
#     queued Spawn's needs to know which Arms it is eligible for (and so which Arms' Demand it adds to);
#     eligibility itself is re-derived from the live registry each pass. `node` is what the submitter
#     *demanded* (an Admin pin; nothing sets it since 2.0 dropped pinning, so it is NULL).
#   * `exit_code`/`finished_at` — the ending, recorded once by the tick the pass it notices the Spawn has
#     left the queue (squeue forgets a finished job and there is no slurmdbd). `exit_code` NULL with
#     `finished_at` set is a Spawn that ended without recording one — a failure.
#   * `placed_node` — where the cluster actually PUT it, which is not `node` and must never be folded
#     into it: `spawn_needs` reads `node` as the declaration. Copied off the escrow row by the meter,
#     since `clear_escrow` deletes that at Settlement — precisely when History starts wanting it. NULL
#     means "not known", never "unplaced".
#   * `tidepool` — the job is a **Dive** (every CCC job since 2.0; 0 = the enrollment benchmark era).
#   * `script` — what a 1.x batch Spawn ran; written '' since. Still read by the history views.
#
# The Karma economy, deliberately with no balances table: Decay is linear, so a decayed sum over the
# ledger and a decay-on-touch running total are exactly equal, and a cached balance could only disagree.
#   * `karma_ledger` — ONE ROW PER EVENT, both parties nullable: a NULL `from_user` is Karma conjured
#     (an Override credit), a NULL `to_user` is Karma destroyed (a Spawn on a commons Arm).
#   * `karma_escrow` — a Spawn's outstanding claim, and so the meter itself. `hold` is the forecast
#     quoted at submit (a FLOOR); `accrued` is what it has really cost so far, ticked while it RUNS (a
#     RATCHET). Share subtracts MAX(hold, accrued). At Settlement the row is DELETED and one ledger row
#     written; the escrow is a live claim, never history, and does not decay.
#   * `karma_demand` — history ONLY, never a pricing path: raw samples (one row per Arm per tick) so the
#     Tide can forecast Demand by hour-of-day and the Dashboard can chart it. Pruned at 30 days.
#
# Arm Retract/Return — two tables because the two things they track have different lifetimes:
#   * `arm_retractions` — a row means "this node takes no NEW placement"; submit builds --exclude= from
#     it. Return deletes the row, and that is all Return ever does.
#   * `retracted_jobs` — one row per Spawn RUNNING on the node when Retract fired; `deadline` is when the
#     tick force-kills it. Survives Return (a caught Spawn is doomed regardless); cleared when the tick
#     settles the job, at zero.
#
# Dives — two tables because the port has to be reserved BEFORE the job_id exists (baked into the
# job script, rendered before sbatch runs):
#   * `dive_ports` — the reservation, keyed by the port alone, so two requests racing the same
#     "lowest free port" collide on ITS primary key (dive.reserve_port retries).
#   * `dives` — everything else, written once the job_id is known. `token` keys the helper's records,
#     rsync modules and snapshot; `main` is what single-writer checks; `relay_pid`/`node` are the
#     Head-side relay, once started. `closed` once the tick has run the helper's dive-close (the port is
#     released then too); the row stays for history.
#
# `guises` — the uid a User's Spawns run as, on every node (guises.py). This table is only the allocator,
# and its one job is **never hand out a uid twice**: `remove_user` HARD-deletes, so a removed User
# leaves a tombstone here (`user_id` goes NULL, the row stays forever to keep that uid retired). UNIQUE
# on user_id makes it one Guise per User — SQLite allows many NULLs in a UNIQUE column, which is exactly
# right: many tombstones, one live row each.
_SCHEMA = """
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

CREATE TABLE spawns (
  job_id       INTEGER PRIMARY KEY,
  user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
  submitted_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  script       TEXT    NOT NULL DEFAULT '',
  minutes      INTEGER,
  gpus         INTEGER,
  vram_gb      INTEGER,
  node         TEXT,
  exit_code    INTEGER,
  finished_at  TEXT,
  tidepool     INTEGER NOT NULL DEFAULT 0,
  placed_node  TEXT
);
CREATE INDEX idx_spawns_user ON spawns(user_id);

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

CREATE TABLE arm_retractions (
  node TEXT PRIMARY KEY
);
CREATE TABLE retracted_jobs (
  job_id   INTEGER PRIMARY KEY,
  node     TEXT NOT NULL,
  deadline TEXT NOT NULL
);

CREATE TABLE dive_ports (
  port        INTEGER PRIMARY KEY,
  reserved_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE TABLE dives (
  job_id     INTEGER PRIMARY KEY REFERENCES spawns(job_id) ON DELETE CASCADE,
  token      TEXT    NOT NULL UNIQUE,
  main       TEXT    NOT NULL,
  deps       TEXT    NOT NULL DEFAULT '[]',
  port       INTEGER NOT NULL,
  relay_pid  INTEGER,
  node       TEXT,
  closed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE ssh_keys (
  id       INTEGER PRIMARY KEY,
  user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name     TEXT,
  pubkey   TEXT    NOT NULL,
  added_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  UNIQUE (user_id, pubkey)
);

CREATE TABLE guises (
  uid        INTEGER PRIMARY KEY,
  user_id    INTEGER UNIQUE REFERENCES users(id) ON DELETE SET NULL,
  created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
"""

# PRAGMA user_version of a 2.0 head.db. Above every step of the 1.x ladder (1..21), so a 1.x store can
# never read as this one. 23: ssh_keys (Tidepool). 24: dives + dive_ports (replace tidepools).
SCHEMA_VERSION = 24


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
    """Idempotent: create the schema on a fresh store (user_version 0); no-op on a current one. A 1.x
    store is refused — 2.0 has no upgrade path, only teardown + setup."""
    (ver,) = conn.execute("PRAGMA user_version").fetchone()
    if ver == SCHEMA_VERSION:
        return
    if ver:
        raise RuntimeError(f"head.db is schema v{ver}, not v{SCHEMA_VERSION} — 2.0 is a clean install: "
                           f"`cuttle teardown` then `cuttle setup --head`")
    conn.executescript(_SCHEMA)
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
    """
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
    """Rotate the password and clear the must-change flag. Live sessions are untouched, so nobody is
    logged out by changing their own password."""
    conn.execute(
        "UPDATE users SET pw_hash=?, must_change_password=0 WHERE username=?",
        (_ph.hash(new_password), username),
    )
    conn.commit()


# A precomputed hash so verify_password does equal work whether or not the user exists.
_DUMMY_HASH = _ph.hash("__ccc_dummy__")


# --- Guises (temp/spawn-isolation.md) ----------------------------------------
# See _SCHEMA for the table and why it is its own. Allocating a uid needs no privilege whatsoever —
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


# --- spawn ledger -------------------------------------------------------------

def record_spawn(conn: sqlite3.Connection, job_id: int, user_id: int, script: str,
                 minutes: int = 0, gpus: int = 0, vram_gb: int = 0,
                 node: str | None = None, hold: float | None = None,
                 tidepool: bool = False) -> None:
    """Attribute a submitted Spawn to its user (keyed by the Slurm job id) and place its Escrow — in
    ONE transaction. A Spawn that existed without its hold, even briefly, would be a Spawn its
    submitter's Share had not yet been charged for, which is exactly the window a queue-flooder wants.

    `hold` is None for a Spawn nobody is charged for: the **Admin** is outside the economy, so their
    Spawns get an Attribution row (the Dashboard still says who submitted what) and no Escrow at all.
    """
    conn.execute(
        """INSERT INTO spawns (job_id, user_id, script, minutes, gpus, vram_gb, node, tidepool)
           VALUES (?,?,?,?,?,?,?,?)""",
        (job_id, user_id, script, minutes, gpus, vram_gb, node, int(tidepool)),
    )
    if hold is not None:
        conn.execute("INSERT INTO karma_escrow (job_id, user_id, hold) VALUES (?,?,?)",
                     (job_id, user_id, hold))
    conn.commit()


def get_spawn(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM spawns WHERE job_id=?", (job_id,)).fetchone()


def spawn_submitters(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, str | None]:
    """Map job_id -> who submitted it (**Attribution**) — the one thing the live `GET /spawns` queue
    cannot learn from Slurm, which says `ccc` for every Spawn. A job id with no row (submitted outside
    CCC, or already reaped) is simply absent."""
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


# The archive row, shared by the many-row read (`spawn_history`) and the one-row read
# (`spawn_detail`). One string rather than two, because the Spawn page and the History row show the
# same facts about the same thing and drifting them apart is how a Spawn comes to cost one number in
# the list and another on its own page. The settled cost joins in from the ledger's `settle` row (a
# Spawn that never settled — Admin-owned, or ended before its escrow was metered — has NULL cost,
# which reads as `—`).
_SPAWN_ROW = """SELECT s.job_id, s.submitted_at, s.finished_at, s.exit_code, s.script,
                       s.gpus, s.vram_gb, s.minutes, s.node, s.placed_node,
                       u.username AS submitted_by,
                       l.amount AS cost, d.main AS main
                FROM spawns s
                LEFT JOIN users u ON u.id = s.user_id
                LEFT JOIN dives d ON d.job_id = s.job_id
                LEFT JOIN karma_ledger l ON l.job_id = s.job_id AND l.kind = 'settle'
"""


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
    _SCHEMA). Kept OFF the meter deliberately: an Admin's Spawn and the enrollment benchmark have
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


# --- Dives -----------------------------------------------------------------------
# See _SCHEMA's comment for the table shapes.

def used_dive_ports(conn: sqlite3.Connection) -> set[int]:
    """Every port currently reserved — a Dive being requested (job_id unknown yet) as well as a live
    one. The reservation has to exist before there is a job_id to key `dives` on."""
    return {r["port"] for r in conn.execute("SELECT port FROM dive_ports").fetchall()}


def reserve_dive_port(conn: sqlite3.Connection, port: int) -> bool:
    try:
        conn.execute("INSERT INTO dive_ports (port) VALUES (?)", (port,))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def release_dive_port(conn: sqlite3.Connection, port: int) -> None:
    conn.execute("DELETE FROM dive_ports WHERE port=?", (port,))
    conn.commit()


def create_dive(conn: sqlite3.Connection, job_id: int, token: str, main: str, deps: list[str],
                port: int) -> None:
    conn.execute("INSERT INTO dives (job_id, token, main, deps, port) VALUES (?,?,?,?,?)",
                 (job_id, token, main, json.dumps(deps), port))
    conn.commit()


def dive_mains(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, str]:
    if not job_ids:
        return {}
    q = f"SELECT job_id, main FROM dives WHERE job_id IN ({','.join('?' * len(job_ids))})"
    return {r["job_id"]: r["main"] for r in conn.execute(q, job_ids).fetchall()}


def get_dive(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM dives WHERE job_id=?", (job_id,)).fetchone()


def open_dives(conn: sqlite3.Connection, user_id: int | None = None) -> list[sqlite3.Row]:
    """Dives not yet closed (queued, running, or ended but not yet swept), optionally one User's."""
    q = ("SELECT d.*, s.user_id, s.submitted_at, s.minutes, s.gpus, s.vram_gb FROM dives d "
         "JOIN spawns s ON s.job_id = d.job_id WHERE d.closed = 0")
    if user_id is None:
        return conn.execute(q + " ORDER BY d.job_id").fetchall()
    return conn.execute(q + " AND s.user_id=? ORDER BY d.job_id", (user_id,)).fetchall()


def set_dive_relay(conn: sqlite3.Connection, job_id: int, relay_pid: int, node: str) -> None:
    conn.execute("UPDATE dives SET relay_pid=?, node=? WHERE job_id=?", (relay_pid, node, job_id))
    conn.commit()


def close_dive(conn: sqlite3.Connection, job_id: int) -> None:
    """The helper has closed it: release the port, keep the row (history shows main/deps)."""
    row = get_dive(conn, job_id)
    if row is None:
        return
    conn.execute("UPDATE dives SET closed=1, relay_pid=NULL WHERE job_id=?", (job_id,))
    conn.execute("DELETE FROM dive_ports WHERE port=?", (row["port"],))
    conn.commit()


# --- SSH keys (Tidepool) ------------------------------------------------------
# A User's public keys for their Tidepool's sshd. head.db is the source; tidepool.sync_keys writes the
# authorized_keys the container reads. `cuttle login` adds this box's own; more via CLI or Dashboard.

def ssh_keys(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name, pubkey, added_at FROM ssh_keys WHERE user_id=? ORDER BY id",
                        (user_id,)).fetchall()


def add_ssh_key(conn: sqlite3.Connection, user_id: int, pubkey: str, name: str | None) -> bool:
    """False if this User already has the key (one row per key; the name of the first add stays)."""
    cur = conn.execute("INSERT OR IGNORE INTO ssh_keys (user_id, name, pubkey) VALUES (?,?,?)",
                       (user_id, name, pubkey))
    conn.commit()
    return cur.rowcount > 0


def remove_ssh_key(conn: sqlite3.Connection, user_id: int, key_id: int) -> bool:
    cur = conn.execute("DELETE FROM ssh_keys WHERE id=? AND user_id=?", (key_id, user_id))
    conn.commit()
    return cur.rowcount > 0
