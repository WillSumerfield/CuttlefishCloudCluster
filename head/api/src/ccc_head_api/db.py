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

SCHEMA_VERSION = 2
SESSION_TTL_DAYS = 30
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

_MIGRATIONS = [_SCHEMA_V1, _SCHEMA_V2]  # index i takes user_version i -> i+1


# --- connection + schema ----------------------------------------------------

def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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
                 staging_uuid: str) -> None:
    """Attribute a submitted Spawn to its user, keyed by the Slurm job id."""
    conn.execute(
        "INSERT INTO spawns (job_id, user_id, script, staging_uuid) VALUES (?,?,?,?)",
        (job_id, user_id, script, staging_uuid),
    )
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
