"""The Karma tick (`ccc-karma.service`) — the loop that makes standing visible to the scheduler.

Every 30s: read each queued Spawn's submitter, turn that User's standing into a `nice`, and push it.
That is the *entire* mechanism by which Karma reaches the queue. It runs as the **Mantle**, which owns
every job in the cluster, so it re-ranks the whole queue with no privilege whatsoever — no sudo, no
root helper, no SlurmUser.

Its own service, not a task inside the Head API: the economy must not be able to wedge the API, nor the
API the economy. Slice 2's meter (sample Demand, settle ended Spawns) is this same tick.

Three jobs are skipped, each for a reason:
- **nice < 0** — an admin has Queue-bumped it. Slurm refuses a negative nice from a job's owner, so
  only an operator could have set one; its presence is the flag, and needs no state of our own.
- **no attribution row** — the enrollment benchmark, which the CLI sbatches as the Mantle outside the
  API. It keeps nice 0 and therefore outranks queued work, which is what we want: admitting an Arm
  should not wait behind a poor-standing User's backlog.
- **nice already correct** — don't hammer slurmctld with writes that change nothing.
"""

import logging
import sys
import time

from . import db, karma, slurm

log = logging.getLogger("ccc.karma")


def once() -> int:
    """One pass. Returns the number of Spawns whose nice was actually changed."""
    jobs = slurm.pending()
    with db.connect() as conn:
        owners = db.spawn_owners(conn, [j["id"] for j in jobs])
        nice = karma.nice_by_user(conn)
    changed = 0
    for job in jobs:
        if job["nice"] < 0:              # Queue-bumped by an admin — the economy does not touch it
            continue
        user_id = owners.get(job["id"])  # unattributed (the enroll benchmark) — leave it at nice 0
        if user_id is None:
            continue
        want = nice[user_id]
        if want != job["nice"]:
            slurm.set_nice(job["id"], want)
            changed += 1
    return changed


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(levelname)s %(message)s")
    db.ensure_schema()
    while True:
        try:
            changed = once()
            if changed:
                log.info("re-ranked %d spawn(s)", changed)
        except Exception as e:
            # slurmctld restarting, a Spawn ending between squeue and scontrol — the next tick is 30s
            # away and re-reads the world from scratch, so there is nothing here worth dying over.
            log.warning("tick failed: %s", e)
        time.sleep(karma.TICK_SECONDS)


if __name__ == "__main__":
    main()
