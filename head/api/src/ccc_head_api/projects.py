"""Uploaded projects are **staging, not storage** (docs/TODO.md "Storage retention", phase 1).

`spawns.submit` extracts every upload into `PROJECTS_DIR/<uuid>` — up to `MAX_UPLOAD_BYTES` (4GB) per
Spawn — and until this module nothing ever deleted one. The biggest of the Head's three permanent
consumers of disk, because every Spawn has a project while only some produce large outputs.

Nothing reads it once the Spawn has its own copy: the Head branch of the JOB_SCRIPT `cp -a`s it into
node-local scratch, a remote Arm rsyncs it in over its Siphon's `[proj-…]` module, and neither looks
again. So it is deleted THERE — at stage-in — rather than when the Spawn ends. The difference is not
cosmetic: peak usage becomes roughly zero instead of the sum of every running Spawn's upload.

The Head learns that stage-in finished the only way it ever learns anything about a running Spawn: a
marker file appearing under `SPAWNS_DIR/<id>/`. `staged` is the third to follow the convention `exit`
and `recovery.size` already do — written (and, from a remote Arm, rsync'd home) only once the copy has
fully landed, so its presence means done. `sweep` below polls for it, exactly as
`recovery.register_new` polls for `recovery.size`.

**`sbatch --no-requeue` is a prerequisite of all of this, not a nicety.** `JobRequeue` is unset in the
conf template, so Slurm's default (1) applies, and a requeued job re-runs the script from the very top
— re-staging a project this module has already deleted. A Spawn runs **once** (CONTEXT.md), which is
already how retraction behaves; `spawns.submit` now says so to sbatch outright.

Deleting the directory is only half of what stage-in ends. The Siphon's `[proj-…]` module is rooted at
it, so it is revoked in the same breath (`siphons.close_proj`) — a Spawn that has staged in has no
further business reading the Head's copy, and a module pointing at a deleted path is one rsyncd would
report as an auth failure anyway. The `[out-…]` half stays open: the Spawn has not finished writing.

Disk-first, like `recovery.sweep_orphans` and for the same reason — the failure this has to survive is
a project whose Spawn the ledger cannot account for. Three cases, and every directory is exactly one:
  * **staged** — the marker landed. The normal path, usually the tick right after the Spawn starts.
  * **finished, unstaged** — the Spawn ended without one (its stage-in failed, the Arm dropped, or the
    marker's own rsync did). It will never stage in now, and `--no-requeue` is what makes that a
    guarantee rather than a likelihood.
  * **unbound** — no `spawns` row names this uuid, because the submission died between `staging.mkdir()`
    and `sbatch` returning. Deleted on age alone (`_ORPHAN_MINUTES`), since the same "no row yet" state
    is also what a submission looks like for the milliseconds between sbatch returning and its row being
    written — and deleting THAT would destroy a live Spawn's project out from under it.
"""

import logging
import shutil
from datetime import datetime, timedelta, timezone

from . import db, siphons

log = logging.getLogger("ccc.api")

# How long a project directory that no `spawns` row names is left alone before it is treated as debris.
# Matches `siphons._ORPHAN_MINUTES` deliberately: the two describe the SAME failure (a submission that
# raised between `siphons.open` and `db.record_spawn`) and leave one artefact each, so they should not
# disagree about how long it takes to be sure.
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
