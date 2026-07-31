"""Recovery Window (docs/TODO.md) — a 30-minute safety net for a Spawn's UNDECLARED writes.

A Spawn's declared `$CCC_OUT` outputs already survive forever on the Head (nothing deletes
`SPAWNS_DIR/<id>/outputs/`). The gap this closes: everything ELSE a Spawn wrote — a script that
mis-declared `--output`, or wrote to a relative path it never named — used to be destroyed the instant
the Spawn ended, by the JOB_SCRIPT's `cleanup()` EXIT trap. `recover_scratch` (ccc_head_api.spawns'
JOB_SCRIPT) now copies `$proj` (the payload's whole working dir) home to `SPAWNS_DIR/<id>/recovery/`
before that trap deletes it — this module is what happens to that copy afterward: registering it,
capping it, and expiring it.

"Hold" is avoided as a name on purpose — it already means Escrow in this codebase.

Registration is polled, not pushed, and that is load-bearing, not incidental. The EXIT trap's copy-home
happens AFTER `stage_out()` has already sent the Head the `exit` file (bash EXIT traps fire once the
whole script body, including that explicit call, has finished) — so by the time the karma tick notices
`exit_code()` is set and stamps `finished_at`, `recover_scratch` may still be mid-rsync on a remote Arm.
The bash side writes a marker (`SPAWNS_DIR/<id>/recovery.size`, a plain byte count) only once the copy
has FULLY landed — the same "presence means done" convention the `exit` file itself already uses — and
`register_new` below polls recently-finished Spawns for that marker each tick pass, registering the DB
row the pass it appears. A Spawn skipped for size (the per-Spawn cap) never gets a marker at all — "no
copy attempted", per docs/TODO.md — so it is silently never registered, which is the correct outcome.

Two caps, both enforced here (never in the bash — a shell script can gate its OWN copy on its OWN size,
but it cannot know what else the Head is already holding for every other Spawn):
  * `MAX_SPAWN_BYTES` — baked into the rendered JOB_SCRIPT (spawns._render) as the size `recover_scratch`
    itself gates on. Read here too so `register_new` and this module's constant stay the one source.
  * `MAX_POOL_BYTES` — enforced at REGISTRATION time, not just by the periodic sweep: a new copy that
    would push the pool over it evicts the oldest currently-held copy first, repeated until it fits.
    Waiting for the next sweep (up to TICK_SECONDS later) would let the pool briefly overshoot by
    whatever just landed, which is fine for a scanty amount but not the whole point of having a ceiling.
"""

import os
import shutil
from datetime import datetime, timedelta, timezone

from . import db

WINDOW_MINUTES = int(os.environ.get("CCC_RECOVERY_MINUTES", 30))
# Mirrors ccc_head_api.main.MAX_UPLOAD_BYTES's shape exactly: an env-overridable int, not a magic number
# buried in the render call.
MAX_SPAWN_BYTES = int(os.environ.get("CCC_RECOVERY_MAX_SPAWN", 16 * 1024**3))
MAX_POOL_BYTES = int(os.environ.get("CCC_RECOVERY_MAX_POOL", 50 * 1024**3))

# How far back `register_new` looks for a Spawn whose recovery copy might still be landing. Comfortably
# longer than any transfer of up to MAX_SPAWN_BYTES over a mesh link; a Spawn whose marker still hasn't
# appeared after this long is treated as never sending one (dropped Arm, or the copy legitimately failed)
# — matching how a failed stage_out is already reported as "the Head will report it as lost".
_REGISTRATION_WINDOW_MINUTES = 10

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TS).replace(tzinfo=timezone.utc)


def _spawn_dir(job_id: int):
    # Imported lazily to avoid a spawns<->recovery import cycle (spawns.py imports this module for
    # MAX_SPAWN_BYTES, at render time).
    from . import spawns
    return spawns.SPAWNS_DIR / str(job_id)


def _evict(conn, job_id: int) -> None:
    """Delete one Spawn's recovery copy, on disk and in the ledger. Used by both the size-based
    (`_make_room`) and time-based (`reap_expired`) paths — eviction is eviction either way."""
    shutil.rmtree(_spawn_dir(job_id) / "recovery", ignore_errors=True)
    (_spawn_dir(job_id) / "recovery.size").unlink(missing_ok=True)
    db.delete_recovery(conn, job_id)


def _make_room(conn, incoming_bytes: int) -> None:
    """Evict the oldest held copies until `incoming_bytes` more would fit under MAX_POOL_BYTES.

    Called right before a new copy is registered, not left to the next sweep — a pool cap that could
    overshoot until the next tick pass would not really be a cap. `oldest_recovery` is re-queried each
    loop (not cached) since the row just evicted changes what's oldest next."""
    while db.recovery_pool_bytes(conn) + incoming_bytes > MAX_POOL_BYTES:
        oldest = db.oldest_recovery(conn)
        if oldest is None:
            return  # pool is empty and a single copy still wouldn't fit — nothing left to evict
        _evict(conn, oldest["job_id"])


def register_new(conn, now: datetime | None = None) -> int:
    """Find any recently-finished Spawn whose recovery copy has fully landed (its `recovery.size`
    marker exists) but isn't in the ledger yet, make room for it, and register it. Returns how many.

    Safe to call every tick pass: a Spawn whose marker hasn't appeared yet is simply tried again next
    pass, and one already registered never reappears in `recently_finished_without_recovery` (its row is
    the reason it's excluded). Every filesystem touch is wrapped in one `try` — `Path.exists()` only
    swallows a MISSING path (ENOENT); a permission error or a dangling mount raises straight through it,
    same as any other OSError, and none of those are worth wedging a tick pass over (mirrors
    `_record_endings`'s own `except OSError` around reading the `exit` marker, right above this in the
    same tick pass)."""
    now = now or _now()
    since = (now - timedelta(minutes=_REGISTRATION_WINDOW_MINUTES)).strftime(_TS)
    registered = 0
    for job_id in db.recently_finished_without_recovery(conn, since):
        marker = _spawn_dir(job_id) / "recovery.size"
        try:
            if not marker.exists():
                continue  # not landed yet (or never will be — skipped for size, or the Arm dropped)
            size = int(marker.read_text().strip())
        except (OSError, ValueError):
            continue  # unreadable, or a marker mid-write reading as garbage — try again next pass
        _make_room(conn, size)
        db.add_recovery(conn, job_id, size, at=now.strftime(_TS))
        registered += 1
    return registered


def reap_expired(conn, now: datetime | None = None) -> int:
    """Delete every recovery copy whose 30-minute window has closed. Returns how many."""
    now = now or _now()
    cutoff = (now - timedelta(minutes=WINDOW_MINUTES)).strftime(_TS)
    expired = db.expired_recoveries(conn, cutoff)
    for job_id in expired:
        _evict(conn, job_id)
    return len(expired)


def status(conn, job_id: int) -> dict | None:
    """`{bytes, expires_at}` for a Spawn's open recovery copy, or None if it has none (never made, over
    the size cap, evicted, or its window already closed) — the one thing both the API route and the
    CLI's `cuttle outputs` miss-hint need to know."""
    row = db.get_recovery(conn, job_id)
    if row is None:
        return None
    expires = _parse(row["created_at"]) + timedelta(minutes=WINDOW_MINUTES)
    return {"bytes": row["bytes"], "expires_at": expires.strftime(_TS)}


def tar_bytes(job_id: int) -> bytes | None:
    """gzip-tar of a Spawn's recovery copy, for `cuttle recover <id>` / the Dashboard's download button.
    None if there is nothing on disk — the caller (main.py) is expected to have already checked
    `status()` isn't None, but this re-checks disk directly rather than trusting the DB row alone: a race
    between the sweep evicting it and a download landing should read as 404, not a half-written tar."""
    import io
    import tarfile

    recovery_dir = _spawn_dir(job_id) / "recovery"
    if not recovery_dir.is_dir() or not any(p.is_file() for p in recovery_dir.rglob("*")):
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(recovery_dir, arcname="recovery")
    return buf.getvalue()
