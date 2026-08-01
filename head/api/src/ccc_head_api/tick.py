"""The Karma tick (`ccc-karma.service`) — the meter, and the loop that makes standing visible.

Every 30s, one pass, in this order:

1. **Sample Demand** on every Arm (running claims + queued claims spread over eligible cards).
2. **Accrue** onto every RUNNING Spawn: the seconds it held each of its cards, at *that card's* Power
   (Slurm reports which of the node's GPUs it holds) and the Demand it held them under.
3. **Force-kill** any Spawn a Retract caught whose grace period has expired and is still running (see
   ccc_head_api.retraction) — Retract itself only sends TERM; this is the deadline behind it.
4. **Settle** every Spawn that has vanished from the queue: one ledger row, and the claim is released.
   One caught by a Retract settles at ZERO regardless of what it accrued.
5. **Reap** any Tide Pool (docs/TODO.md) whose Spawn just vanished: kill its Head-side relay and forget
   its port/keypair (ccc_head_api.tidepool.reap_dead) — a live Tide Pool holds a Device exactly like any
   other RUNNING Spawn, so steps 1-4 above already meter/settle it with no branch of their own.
6. **Register** any Recovery Window copy (docs/TODO.md) that has fully landed since the last pass, and
   **expire** any whose 30 minutes are up (ccc_head_api.recovery) — see that module's docstring for why
   registration is polled here rather than pushed by the job script that made the copy.
7. **Re-rank** the queue: turn each pending Spawn's submitter's standing into a `nice` and push it.

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

from . import db, karma, recovery, siphons, slurm, spawns, tidepool

log = logging.getLogger("ccc.karma")

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TS).replace(tzinfo=timezone.utc)


def _held_powers(job: dict, arm: dict | None) -> list[float] | None:
    """The Power of every **Device** this RUNNING Spawn holds — the prices Settlement multiplies out.

    Slurm reports the GRES indices a running job was allocated (slurm._gpu_indices), and those index
    straight into the Arm's registry `powers`, because gres.conf's `File=` lines were written in the
    same order (templates.render_gres_conf). So a Spawn that landed on the throttled second 4090 of a
    two-card box is billed at *that* card's number.

    Returns **None** — not a guess — when the answer cannot be had: an unpriced Arm, a card count that
    disagrees with the priced list, or a GPU Spawn Slurm reports no indices for. Averaging the node's
    Powers would settle every one of those silently and plausibly, at a price nobody could ever check.
    A CPU-only Spawn is not that case: it holds no Device and legitimately costs [] → 0.
    """
    powers = (arm or {}).get("powers")
    if not powers:
        return None
    devices = job["devices"]
    if not devices:
        return [] if not job["gpus"] else None
    if max(devices) >= len(powers):
        return None      # Slurm knows about a card the registry does not: the Arm needs re-benchmarking
    return [powers[i] for i in devices]


def _meter(conn, jobs: list[dict], arms: dict[str, dict], demand: dict[str, float],
           now: datetime) -> None:
    """Bill every RUNNING Spawn for the time it has held its cards since we last looked.

    Elapsed *time*, not tick count: if the service was down for an hour, the User still held the card
    for that hour and still owes for it. The first accrual bills from Slurm's `start_time`, so the gap
    between a Spawn starting and the tick noticing is not free either.

    **Several Devices are several separate purchases** (glossary, Settlement): a Spawn on two cards is
    billed each card's own Power, summed — never a count times an average.
    """
    live = {e["job_id"]: e for e in db.live_escrows(conn)}
    for job in jobs:
        if job["state"] != "RUNNING":
            continue
        esc = live.get(job["id"])
        if esc is None:
            continue  # the benchmark, or an Admin's Spawn: nobody is charged for it
        node = job["node"]
        powers = _held_powers(job, arms.get(node))
        if powers is None:
            # A Device we cannot price cannot be sold. The intended path always has one (setup
            # benchmarks the Head's own Arm; _admit benchmarks each donor, and Slurm always reports the
            # indices of a running GPU job), so this means a benchmark FLAKED or the Arm's hardware
            # changed under us — and no invented fallback would be more honest than a loud zero. It is
            # a free GPU until someone reads this line. `last_at` still advances, so the free seconds
            # are not billed retroactively once the Arm is priced again.
            log.error("cannot price spawn %s on arm %s (devices=%s, gpus=%s) — it runs FREE. "
                      "Fix: cuttle arm benchmark %s",
                      job["id"], node, job["devices"], job["gpus"], node)
            db.accrue(conn, job["id"], node, esc["accrued"], now.strftime(_TS))
            continue
        since = _parse(esc["last_at"]) if esc["last_at"] else \
            datetime.fromtimestamp(job["start_time"], timezone.utc)
        seconds = max(0.0, (now - since).total_seconds())
        add = karma.cost(seconds, powers, demand.get(node, 1.0))
        db.accrue(conn, job["id"], node, esc["accrued"] + add, now.strftime(_TS))


def _force_expired_retractions(conn, live_ids: set[int], now: datetime) -> None:
    """Force-kill any Spawn a Retract caught whose grace period has passed and is still running.

    Retract itself only sends TERM (retraction.retract), so the job's own EXIT trap gets a chance to
    stage out first. A job absent from `live_ids` has already ended on its own — nothing to force here,
    and `_settle` below is what zero-bills it."""
    for row in db.retracting_jobs(conn):
        if row["job_id"] not in live_ids:
            continue
        if _parse(row["deadline"]) <= now:
            slurm.signal(row["job_id"], "KILL")


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

    A Spawn a Retract caught (db.retracting_jobs) settles at **ZERO regardless of what it accrued** —
    Retract must never be a way to farm Karma by donating a card and yanking it back mid-run — and its
    `retracted_jobs` row is cleared here, the moment it settles.
    """
    retracting = {r["job_id"] for r in db.retracting_jobs(conn)}
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
        amount = 0.0 if esc["job_id"] in retracting else esc["accrued"]
        db.record_karma(conn, "settle", amount, esc["user_id"], owner_id,
                        job_id=esc["job_id"], at=now.strftime(_TS))
        db.clear_escrow(conn, esc["job_id"])
        if esc["job_id"] in retracting:
            db.clear_retracting_job(conn, esc["job_id"])
        settled += 1
    return settled


def _record_endings(conn, live_ids: set[int], now: datetime) -> None:
    """Stamp the durable outcome of every Spawn that has left the queue but has no ending recorded yet.

    Its own step, not folded into `_settle`, because settlement only walks ESCROWED Spawns — an Admin's
    Spawn carries no hold, so it would never be stamped and `GET /spawns/history` would show it open
    forever. The exit file is read here, the one moment we know the job has just ended and its stage-out
    has landed; a Spawn absent from a *successful* poll is the end-of-life signal (see the module
    docstring). A missing file is a Spawn that ended recording no code — killed, OOM, or its Arm dropped
    before stage-out — and is stamped with a NULL exit_code, which history reads as failed."""
    for job_id in db.unfinished_spawns(conn):
        if job_id in live_ids:
            continue
        try:
            code = spawns.exit_code(job_id)
        except OSError:
            code = None            # unreadable exit dir == no code recorded (failed); never wedge the pass
        db.mark_finished(conn, job_id, code, now.strftime(_TS))


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

        # `needs` is the queue's *declared* GPU count, and feeds Demand only. The meter no longer reads
        # it: what a Spawn is billed for is the Devices Slurm actually gave it, not what it asked for.
        _meter(conn, jobs, arms, demand, now)
        _force_expired_retractions(conn, set(ids), now)
        settled = _settle(conn, set(ids), arms, now)
        _record_endings(conn, set(ids), now)
        # Tide Pool (docs/TODO.md): kill the Head-side relay and drop its port/keypair the moment its
        # Spawn leaves the queue. `ensure_relay` (main.spawn_tidepool) is what brings a relay UP — this
        # is the one place a relay ever goes back down, belt-and-suspenders alongside it.
        tidepool.reap_dead(conn, set(ids))
        # Recovery Window (docs/TODO.md): pick up any copy that finished landing since the last pass
        # (register_new), then drop anything whose 30 minutes are up (reap_expired). Order doesn't
        # matter between the two — a copy just registered is never also expired in the same pass.
        # sweep_orphans then deletes what neither will ever touch: a copy that landed but whose marker
        # never followed it home. Its order here is presentation, not correctness — it skips everything
        # still inside register_new's window by construction (db.claimable_recovery_ids is that window's
        # complement), so it cannot delete a copy this pass was about to claim whichever runs first.
        recovery.register_new(conn, now)
        recovery.reap_expired(conn, now)
        recovery.sweep_orphans(conn, now)
        # Siphons (temp/spawn-isolation.md Phase 2): close the rsync grant of every Spawn whose copy
        # home has landed, or whose window for one has passed. AFTER register_new deliberately — that
        # call is what consumes the `recovery.size` marker this one closes on, and closing first would
        # revoke a Siphon in the same pass as the copy it was still carrying.
        siphons.sweep(conn, now)

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
            # The Guise the job runs as, straight off this pass's own poll — the account the tick has
            # to become to re-nice it (temp/spawn-isolation.md Phase 3b).
            slurm.set_nice(job["id"], want, job.get("user"))
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
