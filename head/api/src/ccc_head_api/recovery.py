"""Recovery Window: a one-day, size-capped safety net for a Spawn's UNDECLARED writes (anything outside
`$CCC_OUT`). `recover_scratch` (spawns' JOB_SCRIPT) copies `$proj` to `SPAWNS_DIR/<id>/recovery/` before
the EXIT trap deletes it; this module registers, caps, and expires that copy.

A day rather than the half hour this started as: a User discovers a wrong `--output` the next morning,
not the same half hour. The window length alone would only have moved which cap ends a window, though —
at a day's retention the pool fills, so eviction rather than expiry would have become the thing that
closes one. Hence MAX_USER_BYTES below.

"Hold" is avoided as a name — it already means Escrow in this codebase.

Registration is polled: bash writes a `recovery.size` marker only once the copy has FULLY landed, and
`register_new` polls recently-finished Spawns for it each tick. A Spawn skipped for the size cap never
gets a marker, so it's silently never registered.

Polling means a copy can land without a marker ever following (Arm dropped mid-copy) — `sweep_orphans`
deletes those, since no ledger row will ever claim them.

Three caps, enforced here rather than in bash (a script can't see what the Head holds for other Spawns):
`MAX_SPAWN_BYTES` is baked into the rendered JOB_SCRIPT; `MAX_USER_BYTES` and `MAX_POOL_BYTES` are both
enforced at registration by evicting first, so neither ever even briefly overshoots.

`MAX_USER_BYTES` is an **Allowance** (CONTEXT.md) and behaves like the output one in retention.py — a
copy is always accepted and the holder's own oldest are dropped to fit, since the Spawn has already run.
It is the one Allowance a User is never shown: a ceiling they are told about is a ceiling they would
plan around, and this window must never be planned around. So it is named only once it has taken
something (main.py's 404 detail), never in advance.
"""

import os
import shutil
from datetime import datetime, timedelta, timezone

from . import db

WINDOW_MINUTES = int(os.environ.get("CCC_RECOVERY_MINUTES", 24 * 60))
MAX_SPAWN_BYTES = int(os.environ.get("CCC_RECOVERY_MAX_SPAWN", 16 * 1024**3))
MAX_USER_BYTES = int(os.environ.get("CCC_RECOVERY_MAX_USER", 32 * 1024**3))
MAX_POOL_BYTES = int(os.environ.get("CCC_RECOVERY_MAX_POOL", 128 * 1024**3))

# How far back `register_new` looks for a still-landing copy. Comfortably longer than any transfer up to
# MAX_SPAWN_BYTES; a marker that still hasn't shown by then is treated as never coming (dropped Arm).
_REGISTRATION_WINDOW_MINUTES = 10

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TS).replace(tzinfo=timezone.utc)


def _spawn_dir(job_id: int):
    # Lazy import: avoids a spawns<->recovery import cycle (spawns.py imports this module too).
    from . import spawns
    return spawns.SPAWNS_DIR / str(job_id)


def _evict(conn, job_id: int) -> None:
    """Delete one Spawn's recovery copy, on disk and in the ledger. Used by both the size-based
    (`_make_room`) and time-based (`reap_expired`) paths — eviction is eviction either way."""
    shutil.rmtree(_spawn_dir(job_id) / "recovery", ignore_errors=True)
    (_spawn_dir(job_id) / "recovery.size").unlink(missing_ok=True)
    db.delete_recovery(conn, job_id)


def _make_room(conn, user_id: int | None, incoming_bytes: int) -> None:
    """Evict until `incoming_bytes` more would fit under BOTH caps: the submitter's own Allowance first,
    then the pool.

    Called right before a new copy is registered, not left to the next sweep — a cap that could overshoot
    until the next tick pass would not really be a cap. Each `oldest` is re-queried per loop (not cached)
    since the row just evicted changes what's oldest next.

    Both passes reach for the arriving User's own oldest copy first, and the pool pass falls through to
    the global oldest only once that User has nothing left to give up. So a User arriving under their own
    Allowance never destroys a stranger's copy while still holding one of their own — which is the whole
    point of having a per-User cap, and would be given straight back at the pool wall otherwise (four
    Users at 32GiB exactly fill 128GiB, so the fifth would land there immediately).

    A None `user_id` (their account was deleted) has no Allowance to charge and no copies findable as
    theirs — the join goes through `spawns.user_id`, which is now NULL — so it skips to the pool pass and
    evicts globally. Same reasoning as retention.py's: there is no Allowance left to hold it under, and
    the window still bounds it."""
    while user_id is not None and db.user_recovery_bytes(conn, user_id) + incoming_bytes > MAX_USER_BYTES:
        oldest = db.oldest_user_recovery(conn, user_id)
        if oldest is None:
            break  # nothing of theirs left and one copy still wouldn't fit — it lands anyway
        _evict(conn, oldest["job_id"])
    while db.recovery_pool_bytes(conn) + incoming_bytes > MAX_POOL_BYTES:
        oldest = db.oldest_user_recovery(conn, user_id) if user_id is not None else None
        if oldest is None:
            oldest = db.oldest_recovery(conn)  # they hold nothing of their own; disk is still finite
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
    for row in db.recently_finished_without_recovery(conn, since):
        job_id = row["job_id"]
        marker = _spawn_dir(job_id) / "recovery.size"
        try:
            if not marker.exists():
                continue  # not landed yet (or never will be — skipped for size, or the Arm dropped)
            size = int(marker.read_text().strip())
        except (OSError, ValueError):
            continue  # unreadable, or a marker mid-write reading as garbage — try again next pass
        _make_room(conn, row["user_id"], size)
        db.add_recovery(conn, job_id, size, at=now.strftime(_TS))
        registered += 1
    return registered


def sweep_orphans(conn, now: datetime | None = None) -> int:
    """Delete every `recovery/` dir on disk that no ledger row will ever claim (copy landed but its
    marker never did, or arrived after `_REGISTRATION_WINDOW_MINUTES`). Returns how many.

    Disk-first, not ledger-first: the point is finding what the ledger doesn't know about.
    `claimable_recovery_ids` keeps this from racing the registrar — a Spawn still running, or recent
    enough that `register_new` is still watching for its marker, is never swept."""
    from . import spawns
    now = now or _now()
    since = (now - timedelta(minutes=_REGISTRATION_WINDOW_MINUTES)).strftime(_TS)
    try:
        entries = sorted(spawns.SPAWNS_DIR.iterdir())
    except OSError:
        return 0  # no Spawn dir at all yet — nothing has ever run here
    claimable = db.claimable_recovery_ids(conn, since)
    swept = 0
    for entry in entries:
        if not entry.name.isdigit() or not (entry / "recovery").is_dir():
            continue
        job_id = int(entry.name)
        if job_id in claimable or db.get_recovery(conn, job_id) is not None:
            continue  # still landing, or registered and thus reap_expired's to expire on schedule
        _evict(conn, job_id)
        swept += 1
    return swept


def reap_expired(conn, now: datetime | None = None) -> int:
    """Delete every recovery copy whose window has closed. Returns how many."""
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
