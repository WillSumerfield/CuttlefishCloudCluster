"""The Karma tick (`ccc-karma.service`) — the meter, and the loop that makes standing visible.

Every 30s, one pass, in this order:

1. **Sample Demand** on every Arm (running claims + queued claims spread over eligible cards).
2. **Accrue** onto every RUNNING Spawn: the seconds it held its card, at that card's Power and the
   Demand it held it under.
3. **Settle** every Spawn that has vanished from the queue: one ledger row, and the claim is released.
4. **Re-rank** the queue: turn each pending Spawn's submitter's standing into a `nice` and push it.

It runs as the **Mantle**, which owns every job in the cluster, so it re-ranks the whole queue with no
privilege whatsoever — no sudo, no root helper, no SlurmUser. Its own service, not a task inside the
Head API: the economy must not be able to wedge the API, nor the API the economy.

Settlement is **poll-based** because there is no alternative. A Spawn that ends — completed, failed,
timed out, cancelled — writes us no end-time; Slurm forgets it after `MinJobAge`; and `sacct` would need
the slurmdbd we deliberately don't run. So *absence from a successful poll* is the end-of-life signal.
If slurmctld is down the poll raises and the pass is skipped, and nothing settles — which is why
"successful" is load-bearing.

Three kinds of pending Spawn are skipped by the re-rank, each for a reason:
- **nice < 0** — an admin has Queue-bumped it. Slurm refuses a negative nice from a job's owner, so
  only an operator could have set one; its presence is the flag, and needs no state of our own.
- **no attribution row** — the enrollment benchmark, sbatched outside the API.
- **submitted by an Admin** — the Admin is outside the economy and has no standing to rank by.
The last two are the same skip: `db.spawn_owners` omits both, and the tick skips whatever it omits.
"""

import logging
import sys
import time
from datetime import datetime, timezone

from . import db, karma, slurm

log = logging.getLogger("ccc.karma")

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TS).replace(tzinfo=timezone.utc)


def _meter(conn, jobs: list[dict], arms: dict[str, dict], demand: dict[str, float],
           needs: dict, now: datetime) -> None:
    """Bill every RUNNING Spawn for the time it has held its card since we last looked.

    Elapsed *time*, not tick count: if the service was down for an hour, the User still held the card
    for that hour and still owes for it. The first accrual bills from Slurm's `start_time`, so the gap
    between a Spawn starting and the tick noticing is not free either.
    """
    live = {e["job_id"]: e for e in db.live_escrows(conn)}
    for job in jobs:
        if job["state"] != "RUNNING":
            continue
        esc = live.get(job["id"])
        if esc is None:
            continue  # the benchmark, or an Admin's Spawn: nobody is charged for it
        node = job["node"]
        power = (arms.get(node) or {}).get("power")
        if not power:
            # An unpriced Arm cannot be sold. The intended path always has a Power (setup benchmarks
            # the Head's own Arm; _admit benchmarks each donor), so this means a benchmark FLAKED — and
            # no invented fallback constant would be more honest than a loud zero. It is a free GPU
            # until someone reads this line. `last_at` still advances, so the free seconds are not
            # billed retroactively if the Arm is later priced.
            log.error("arm %s has no Power — spawn %s runs FREE. Fix: cuttle arm benchmark %s",
                      node, job["id"], node)
            db.accrue(conn, job["id"], node, esc["accrued"], now.strftime(_TS))
            continue
        since = _parse(esc["last_at"]) if esc["last_at"] else \
            datetime.fromtimestamp(job["start_time"], timezone.utc)
        seconds = max(0.0, (now - since).total_seconds())
        gpus = (needs[job["id"]]["gpus"] if job["id"] in needs else job["gpus"]) or 0
        add = karma.cost(seconds, gpus, power, demand.get(node, 1.0))
        db.accrue(conn, job["id"], node, esc["accrued"] + add, now.strftime(_TS))


def _settle(conn, live_ids: set[int], arms: dict[str, dict], now: datetime) -> int:
    """Move the Karma of every Spawn that has ended, and release its claim.

    One ledger row, `amount = accrued` — what it really cost, never the hold. The counterparty is the
    whole of the economy's politics:

    * an Arm owned by another User  -> a **Transfer**; Σ balances stays exactly 0.
    * an Arm owned by the submitter -> from and to are the same person; it nets to 0. Self-use is free.
    * an Arm owned by the **Admin** -> a **Commons Arm**: the Karma is **destroyed** (`to` NULL). The
      Admin is outside the economy and earns nothing; Decay reabsorbs the drift.

    A Spawn cancelled while still PENDING accrued nothing and settles at 0 — its hold is simply
    released. The row is written anyway, so the ledger is a complete record of every Spawn's ending.
    """
    settled = 0
    for esc in db.live_escrows(conn):
        if esc["job_id"] in live_ids:
            continue
        owner_id = None
        owner = (arms.get(esc["node"]) or {}).get("owner") if esc["node"] else None
        if owner:
            row = db.get_user(conn, owner)
            if row and not row["is_admin"]:      # Admin-owned == Commons: destroy, don't credit
                owner_id = row["id"]
        db.record_karma(conn, "settle", esc["accrued"], esc["user_id"], owner_id,
                        job_id=esc["job_id"], at=now.strftime(_TS))
        db.clear_escrow(conn, esc["job_id"])
        settled += 1
    return settled


def once(now: datetime | None = None) -> tuple[int, int]:
    """One pass. Returns (Spawns re-niced, Spawns settled).

    `now` is the pass's clock — injected by the Fake Cluster (slice 4) to run this exact function across
    simulated weeks, None on the real service. It flows into every timestamp this pass writes (the Demand
    sample, an accrual, a settlement) and every Balance it decays, so a whole simulated run is internally
    consistent about when it happened."""
    jobs = slurm.queue()
    arms = {a["name"]: a for a in slurm.arms()}
    now = now or _now()
    ids = [j["id"] for j in jobs]

    with db.connect() as conn:
        needs = db.spawn_needs(conn, ids)
        demand = karma.demand_by_arm(list(arms.values()), jobs, needs)
        db.record_demand(conn, now.strftime(_TS), demand)
        db.prune_demand(conn, karma.DEMAND_RETENTION_DAYS, now)

        _meter(conn, jobs, arms, demand, needs, now)
        settled = _settle(conn, set(ids), arms, now)

        # AFTER metering and settling: an accrual that overtook its hold raises the submitter's claim,
        # and a settlement moves their Balance. Both change the nice we are about to push, and pushing
        # a stale one would leave the queue a tick behind the economy that ranks it.
        owners = db.spawn_owners(conn, ids)
        nice = karma.nice_by_user(conn, now)

    changed = 0
    for job in jobs:
        if job["state"] != "PENDING" or job["nice"] < 0:
            continue                     # running, or Queue-bumped by an admin: not ours to touch
        user_id = owners.get(job["id"])  # benchmark, or an Admin: no standing to rank by
        if user_id is None:
            continue
        want = nice[user_id]
        if want != job["nice"]:
            slurm.set_nice(job["id"], want)
            changed += 1
    return changed, settled


def main() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(levelname)s %(message)s")
    db.ensure_schema()
    while True:
        try:
            changed, settled = once()
            if changed or settled:
                log.info("re-ranked %d spawn(s), settled %d", changed, settled)
        except Exception as e:
            # slurmctld restarting, a Spawn ending between squeue and scontrol — the next tick is 30s
            # away and re-reads the world from scratch, so there is nothing here worth dying over.
            log.warning("tick failed: %s", e)
        time.sleep(karma.TICK_SECONDS)


if __name__ == "__main__":
    main()
