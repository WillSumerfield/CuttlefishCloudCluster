"""CCC Head auth store (Phase 3a slice 1) — the User + Session tables behind login.

Pure stdlib `sqlite3` + `argon2-cffi`, no ORM/framework (deliberate — see temp/ROADMAP.md Phase 3a).
The Head API process (`main.py`) and the admin CLI (`admin.py`) both open the same `head.db`; setup
seeds the schema + first admin. Owned by the `ccc` service account, 0600.

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

# Path comes from the environment (the systemd unit sets CCC_HEAD_DB) so the API package never
# imports the CLI's config. Default matches cuttle.config.HEAD_DB.
DB_PATH = Path(os.environ.get("CCC_HEAD_DB", "/var/lib/ccc/head/head.db"))

SCHEMA_VERSION = 4
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

_MIGRATIONS = [_SCHEMA_V1, _SCHEMA_V2, _SCHEMA_V3, _SCHEMA_V4]  # index i takes user_version i -> i+1


# --- connection + schema ----------------------------------------------------

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")  # two writers now — see BUSY_TIMEOUT_MS
    return conn


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
    temp password to hand off once — it is never recoverable after this."""
    temp_pw = secrets.token_urlsafe(12)
    conn.execute(
        "INSERT INTO users (username, pw_hash, is_admin, must_change_password) VALUES (?,?,?,1)",
        (username, _ph.hash(temp_pw), int(is_admin)),
    )
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
                 node: str | None = None, hold: float | None = None) -> None:
    """Attribute a submitted Spawn to its user (keyed by the Slurm job id) and place its Escrow — in
    ONE transaction. A Spawn that existed without its hold, even briefly, would be a Spawn its
    submitter's Share had not yet been charged for, which is exactly the window a queue-flooder wants.

    `hold` is None for a Spawn nobody is charged for: the **Admin** is outside the economy, so their
    Spawns get an Attribution row (the Dashboard still says who submitted what) and no Escrow at all.
    """
    conn.execute(
        """INSERT INTO spawns (job_id, user_id, script, staging_uuid, minutes, gpus, vram_gb, node)
           VALUES (?,?,?,?,?,?,?,?)""",
        (job_id, user_id, script, staging_uuid, minutes, gpus, vram_gb, node),
    )
    if hold is not None:
        conn.execute("INSERT INTO karma_escrow (job_id, user_id, hold) VALUES (?,?,?)",
                     (job_id, user_id, hold))
    conn.commit()


def get_spawn(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM spawns WHERE job_id=?", (job_id,)).fetchone()


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


# --- Karma ledger (slice 1) -------------------------------------------------

def record_karma(conn: sqlite3.Connection, kind: str, amount: float, from_user: int | None,
                 to_user: int | None, job_id: int | None = None, note: str | None = None) -> None:
    """Append one Karma event. `amount` is always positive — direction is carried by which side is
    NULL: `from` NULL = conjured, `to` NULL = destroyed, both set = a Transfer (and a Transfer is the
    only kind that leaves Σ balances at 0)."""
    conn.execute(
        "INSERT INTO karma_ledger (kind, amount, from_user, to_user, job_id, note) VALUES (?,?,?,?,?,?)",
        (kind, amount, from_user, to_user, job_id, note),
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


def prune_demand(conn: sqlite3.Connection, days: int) -> int:
    """History, not evidence: a sample older than the Tide's window forecasts nothing and prices
    nothing."""
    cur = conn.execute(
        "DELETE FROM karma_demand WHERE at < strftime('%Y-%m-%dT%H:%M:%SZ','now',?)", (f"-{days} days",)
    )
    conn.commit()
    return cur.rowcount
