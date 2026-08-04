"""Uploaded projects are **staging, not storage**.

`spawns.submit` extracts each upload into `PROJECTS_DIR/<uuid>` (up to `MAX_UPLOAD_BYTES`, 4GB). Nothing
reads it again once the Spawn has its own copy (`cp -a` on the Head, rsync on a remote Arm), so it's
deleted THERE, at stage-in, not when the Spawn ends — peak usage becomes roughly zero instead of the sum
of every running Spawn's upload.

The Head learns stage-in finished via a marker file, `staged`, under `SPAWNS_DIR/<id>/` — same
presence-means-done convention as `exit` and `recovery.size`. `sweep` polls for it.

**`sbatch --no-requeue` is a prerequisite, not a nicety.** Slurm's default requeue behavior re-runs the
script from the top, re-staging a project this module already deleted — a Spawn runs once (CONTEXT.md).

Deleting the directory also revokes the Siphon's `[proj-…]` module in the same breath
(`siphons.close_proj`); the `[out-…]` half stays open since the Spawn hasn't finished writing.

Disk-first, like `recovery.sweep_orphans`: every directory is exactly one of three states —
**staged** (marker landed, the normal path), **finished, unstaged** (stage-in never landed;
`--no-requeue` makes that permanent), or **unbound** (no `spawns` row yet — deleted only after
`_ORPHAN_MINUTES`, so a live submission mid-write is never destroyed).
"""

import logging
import shutil
from datetime import datetime, timedelta, timezone

from . import db, siphons

log = logging.getLogger("ccc.api")

# How long an unbound project dir is left alone before it's debris. Matches `siphons._ORPHAN_MINUTES`
# deliberately — same underlying failure (submission raised between `siphons.open` and `db.record_spawn`).
_ORPHAN_MINUTES = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def sweep(conn, now: datetime | None = None) -> int:
    """Delete every uploaded project whose Spawn no longer needs it. Returns how many.

    Safe to call every tick pass: a project still staging in is simply skipped and comes round again."""
    from . import spawns
    now = now or _now()
    try:
        entries = sorted(spawns.PROJECTS_DIR.iterdir())
    except OSError:
        return 0  # nothing has ever been submitted here
    rows = db.spawns_by_staging_uuid(conn, [e.name for e in entries])
    cutoff = now - timedelta(minutes=_ORPHAN_MINUTES)
    swept = 0
    for entry in entries:
        row = rows.get(entry.name)
        if row is None:
            # Unbound. Never delete a young one — that is also what a Spawn looks like in the
            # milliseconds between sbatch returning and its row being written.
            try:
                mtime = datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc)
            except OSError:
                continue
            if mtime >= cutoff:
                continue
        else:
            staged = spawns.SPAWNS_DIR / str(row["job_id"]) / "staged"
            try:
                landed = staged.exists()
            except OSError:
                continue  # unreadable — try again next pass rather than wedge this one
            if not landed and row["finished_at"] is None:
                continue  # still staging in, or running and yet to say it staged
            siphons.close_proj(row["job_id"])
        shutil.rmtree(entry, ignore_errors=True)
        swept += 1
    return swept
