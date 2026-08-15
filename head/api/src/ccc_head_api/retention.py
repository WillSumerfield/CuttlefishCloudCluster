"""Output retention: a Spawn's declared **Output**s live **7 days**, and a User holds at most
**32GB** of them at once.

TTL runs from the Spawn's END, not registration: `created_at` is copied from `finished_at`, so a Spawn
registered late (tick was down) still expires 7 days after it ran, not 7 days after registration. No
pinning, no reset-on-download.

An output is always accepted — Karma's already spent, the work's already done — so eviction falls on the
User's OTHER, older outputs instead, oldest-first and silent. A single result bigger than the whole
Allowance still lands; there's no per-Spawn cap.

The Allowance is per User, not global (see CONTEXT.md, **Allowance**): filling your 32GB can't evict
anyone else's results. An Attribution row whose User was deleted has no Allowance to charge and is
registered anyway; TTL still reaps it.

Registration needs no new marker: `stage_out` sends `exit` LAST (see spawns.JOB_SCRIPT), so the tick pass
that stamps `finished_at` is already downstream of outputs having landed. Nothing here ever deletes an
UNregistered outputs dir — a Head that missed a week of ticks registers late and expires late.
"""

import os
import shutil
from datetime import datetime, timedelta, timezone

from . import db

TTL_DAYS = int(os.environ.get("CCC_OUTPUT_TTL_DAYS", 7))
# Per-User, independent of every other Allowance (e.g. Recipe space).
MAX_USER_BYTES = int(os.environ.get("CCC_OUTPUT_MAX_USER", 32 * 1024**3))

# A `spawns/<id>/` dir with no Attribution row yet — submission raised between sbatch returning and
# `db.record_spawn`. Young ones look identical to that in-flight race, so don't touch them.
_ORPHAN_MINUTES = 60

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TS).replace(tzinfo=timezone.utc)


def _spawn_dir(job_id: int):
    from . import spawns
    return spawns.SPAWNS_DIR / str(job_id)


def _outputs_dir(job_id: int):
    return _spawn_dir(job_id) / "outputs"


def _du(path) -> int:
    """Bytes under `path`. Walked here rather than shelled out to `du` because this side is Python and
    already has the tree open — the bash `du -sb` in JOB_SCRIPT exists only because that side is bash."""
    try:
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    except OSError:
        return 0


def _evict(conn, job_id: int) -> None:
    """Delete one Spawn's whole directory, on disk and in the ledger. Nothing is recreated empty: a
    later `GET /outputs` should 404 (and say the results expired), not serve an empty tar that reads as
    a Spawn which produced nothing.

    The WHOLE directory, not just `outputs/`: `out`, `err`, `exit`, `environment`, `build.log` and the
    markers had no deleter anywhere, so one directory per Spawn ever submitted accumulated forever —
    and `register_new` lists that directory on every 30-second pass, so the tick's cost grew with the
    cluster's whole history rather than with what is live. The Spawn's *row* is untouched: history and
    Attribution are the accounting record and outlive the files (see db.py).
    """
    shutil.rmtree(_spawn_dir(job_id), ignore_errors=True)
    db.delete_output(conn, job_id)


def _make_room(conn, user_id: int | None, incoming_bytes: int) -> None:
    """Evict that User's oldest outputs until `incoming_bytes` more fit under MAX_USER_BYTES.

    A None `user_id` (their account was deleted) counts against nothing — there is no Allowance left to
    charge, and the TTL still bounds it."""
    if user_id is None:
        return
    while db.user_output_bytes(conn, user_id) + incoming_bytes > MAX_USER_BYTES:
        oldest = db.oldest_output(conn, user_id)
        if oldest is None:
            return  # nothing of theirs left and one result still wouldn't fit — it lands anyway
        _evict(conn, oldest["job_id"])


def register_new(conn, now: datetime | None = None) -> int:
    """Register every finished Spawn's outputs that aren't in the ledger yet. Returns how many.

    Safe to call every tick pass: a Spawn still running has no `finished_at` and is skipped, and one
    already registered has a row. Sized once, here, and never re-measured — the payload has exited, so
    the tree cannot still be growing."""
    from . import spawns
    now = now or _now()
    try:
        entries = sorted(spawns.SPAWNS_DIR.iterdir())
    except OSError:
        return 0  # nothing has ever run here
    ids = [int(e.name) for e in entries if e.name.isdigit() and (e / "outputs").is_dir()]
    rows = db.output_registration_rows(conn, ids)
    cutoff = now - timedelta(minutes=_ORPHAN_MINUTES)
    registered = 0
    for job_id in ids:
        row = rows.get(job_id)
        outputs = _outputs_dir(job_id)
        if row is None:
            # No Attribution row names this Spawn. Never delete a young one — that is also what a
            # submission looks like between sbatch returning and its row being written.
            try:
                mtime = datetime.fromtimestamp(outputs.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if mtime < cutoff:
                shutil.rmtree(outputs, ignore_errors=True)
            continue
        if row["registered"] or row["finished_at"] is None:
            continue  # already in the ledger, or still running
        size = _du(outputs)
        _make_room(conn, row["user_id"], size)
        db.add_output(conn, job_id, size, at=row["finished_at"])
        registered += 1
    return registered


def reap_expired(conn, now: datetime | None = None) -> int:
    """Delete every Spawn's outputs whose 7 days are up. Returns how many."""
    now = now or _now()
    cutoff = (now - timedelta(days=TTL_DAYS)).strftime(_TS)
    expired = db.expired_outputs(conn, cutoff)
    for job_id in expired:
        _evict(conn, job_id)
    return len(expired)


def evicted(job_id: int) -> bool:
    """True when a Spawn's outputs directory is GONE from disk.

    `spawns.submit` creates one for EVERY Spawn, before sbatch — so its absence is never "this Spawn
    produced nothing" (that is an empty dir), only "`_evict` took it", whether for the TTL or the
    Allowance. That distinction is the whole reason `_evict` does not recreate it, and it is what lets
    a 404 say which of the two happened."""
    return not _outputs_dir(job_id).exists()


def status(conn, job_id: int) -> dict | None:
    """`{bytes, expires_at}` for a Spawn's outputs, or None if it has none held — never produced any,
    evicted for the Allowance, or expired. What both the History countdown and `cuttle outputs`' miss
    hint read; the same shape `recovery.status` returns, so every surface renders the two identically."""
    row = db.get_output(conn, job_id)
    if row is None:
        return None
    expires = _parse(row["created_at"]) + timedelta(days=TTL_DAYS)
    return {"bytes": row["bytes"], "expires_at": expires.strftime(_TS)}
