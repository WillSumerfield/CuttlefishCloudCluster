"""The Karma tick (`ccc-karma.service`) — the meter, and the loop that makes standing visible.

Every 30s, one pass, in this order:

1. **Sample Demand** on every Arm (running + queued claims spread over eligible cards).
2. **Accrue** onto every RUNNING Spawn: seconds held per card, at that card's Power (Slurm reports which
   GPU it holds) and the Demand it held them under.
3. **Force-kill** any Spawn a Retract caught whose grace period expired and is still running — Retract
   itself only sends TERM (ccc_head_api.retraction); this is the deadline behind it.
4. **Settle** every Spawn that has vanished from the queue: one ledger row, claim released. One caught by
   a Retract settles at ZERO regardless of what it accrued.
5. **Reap** any Tide Pool whose Spawn just vanished: kill its relay, forget its port/keypair
   (tidepool.reap_dead) — a live Tide Pool holds a Device like any RUNNING Spawn, so steps 1-4 already
   meter/settle it with no branch of their own.
6. **Register** any Recovery Window copy that's fully landed since the last pass, and **expire** any
   whose day is up (recovery.py) — registration is polled here, not pushed by the job script.
7. **Re-rank** the queue: turn each pending Spawn's submitter's standing into a `nice` and push it.

Runs as the **Mantle**, which owns every job in the cluster, so it re-ranks the whole queue with no
privilege — no sudo, no root helper. Its own service, not a task inside the Head API: the economy must
not be able to wedge the API, nor the API the economy.

Settlement is **poll-based** — there's no alternative. A Spawn that ends writes us no end-time; Slurm
forgets it after `MinJobAge`; `sacct` would need the slurmdbd we deliberately don't run. So *absence
from a successful poll* is the end-of-life signal — if slurmctld is down the poll raises and the pass is
skipped, and nothing settles, which is why "successful" is load-bearing.

Three kinds of pending Spawn are skipped by re-rank:
- **nice < 0** — an admin has Queue-bumped it (Slurm refuses a negative nice from a job's own owner).
- **no attribution row** — the enrollment benchmark, sbatched outside the API.
- **submitted by an Admin** — outside the economy, no standing to rank by.
The last two are the same skip: `db.spawn_owners` omits both.
"""

import logging
import sys
import time
from datetime import datetime, timezone

from . import db, karma, projects, recipes, recovery, retention, siphons, slurm, spawns, tidepool

log = logging.getLogger("ccc.karma")

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, _TS).replace(tzinfo=timezone.utc)


def _held_powers(job: dict, arm: dict | None) -> list[float] | None:
    """The Power of every **Device** this RUNNING Spawn holds — the prices Settlement multiplies out.

    Slurm reports the GRES indices a job holds (slurm._gpu_indices), which index straight into the Arm's
    registry `powers` since gres.conf's `File=` lines were written in the same order — so a Spawn on a
    throttled second card is billed at that card's number, not an average.

    Returns **None** — not a guess — when the answer can't be had: an unpriced Arm, a card count that
    disagrees with the priced list, or a Spawn with no reported indices. Averaging Powers would settle
    those silently at a price nobody could check. Every Spawn holds at least one Device, so an empty
    index list is always Slurm failing to say WHICH — never a Spawn that wanted none.
    """
    powers = (arm or {}).get("powers")
    if not powers:
        return None
    devices = job["devices"]
    if not devices:
        return None
    if max(devices) >= len(powers):
        return None      # Slurm knows about a card the registry does not: the Arm needs re-benchmarking
    return [powers[i] for i in devices]


def _meter(conn, jobs: list[dict], arms: dict[str, dict], demand: dict[str, float],
           now: datetime) -> None:
    """Bill every RUNNING Spawn for the time it has held its cards since we last looked.

    Elapsed *time*, not tick count: an hour of service downtime is still an hour owed. First accrual
    bills from Slurm's `start_time`, so the gap before the tick notices isn't free either.

    **Several Devices are several separate purchases** (glossary, Settlement): a Spawn on two cards is
    billed each card's own Power, summed — never a count times an average.
    """
    live = {e["job_id"]: e for e in db.live_escrows(conn)}
    for job in jobs:
        if job["state"] != "RUNNING":
            continue
        # WHERE it ran, recorded before WHAT it costs — placement is a fact about the Spawn, not the
        # billing. An Admin's Spawn has no Escrow, so tying this to the meter left their History always
        # saying "arm unrecorded". Write-once, in db.record_placement.
        db.record_placement(conn, job["id"], job["node"])
        esc = live.get(job["id"])
        if esc is None:
            continue  # the benchmark, or an Admin's Spawn: nobody is charged for it
        node = job["node"]
        powers = _held_powers(job, arms.get(node))
        if powers is None:
            # A Device we cannot price cannot be sold. Every Arm should already have one (setup/`_admit`
            # benchmark it), so this means a benchmark flaked or the hardware changed under us — a free
            # GPU until someone reads this line. `last_at` still advances, so free seconds aren't billed
            # retroactively once the Arm is priced again.
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

    Retract only sends TERM (retraction.retract), so the EXIT trap gets a chance to stage out first. A
    job already absent from `live_ids` has ended on its own — `_settle` below zero-bills it."""
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

    Its own step, not folded into `_settle`: settlement only walks ESCROWED Spawns, so an Admin's Spawn
    (no hold) would never be stamped and `GET /spawns/history` would show it open forever. Exit file is
    read here, the moment we know the job ended and stage-out landed. A missing file means it ended
    recording no code (killed, OOM, Arm dropped) and is stamped NULL, which history reads as failed."""
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

    `now` is the pass's clock — injected by the Fake Cluster to run this function across simulated weeks,
    None on the real service. Flows into every timestamp this pass writes and every Balance it decays,
    so a simulated run stays internally consistent about when it happened."""
    jobs = slurm.queue()
    arms = {a["name"]: a for a in slurm.arms()}
    now = now or _now()
    ids = [j["id"] for j in jobs]

    with db.connect() as conn:
        needs = db.spawn_needs(conn, ids)
        demand = karma.demand_by_arm(list(arms.values()), jobs, needs)
        db.record_demand(conn, now.strftime(_TS), demand)
        db.prune_demand(conn, karma.DEMAND_RETENTION_DAYS, now)
        # Expired sessions are already REJECTED at lookup (db.lookup_session filters on expires_at),
        # so this is not a correctness fix — it is the row itself, which nothing deleted unless the
        # User explicitly logged out. Every other reaper in this module was wired; this one was written
        # and never plugged in.
        db.purge_expired(conn)
        db.prune_ledger(conn, karma.KARMA_LEDGER_RETENTION_DAYS, now)

        # `needs` is the queue's *declared* GPU count, feeds Demand only — the meter bills the Devices
        # Slurm actually gave a Spawn, not what it asked for.
        _meter(conn, jobs, arms, demand, now)
        _force_expired_retractions(conn, set(ids), now)
        settled = _settle(conn, set(ids), arms, now)
        _record_endings(conn, set(ids), now)
        # Kill the Head-side relay and drop its port/keypair the moment a Tide Pool's Spawn leaves the
        # queue — the one place a relay ever goes back down.
        tidepool.reap_dead(conn, set(ids))
        # Recovery Window: pick up any copy that finished landing since the last pass, drop anything
        # whose day is up, then sweep_orphans deletes what neither will ever touch (a copy that
        # landed but whose marker never followed). Order here is presentation, not correctness —
        # sweep_orphans skips everything still inside register_new's window by construction.
        recovery.register_new(conn, now)
        recovery.reap_expired(conn, now)
        recovery.sweep_orphans(conn, now)
        # Storage retention: delete the Head's copy of every staged-in upload and revoke the `[proj-…]`
        # half of its Siphon. BEFORE siphons.sweep, so "revoked at stage-in" stays true even for a Spawn
        # that ended this same tick (close_proj on an already-gone conf is a no-op either way).
        projects.sweep(conn, now)
        # Register every finished Spawn's declared results against its submitter's 8GB Allowance, then
        # drop whatever is past its 7 days. Needs no marker of its own — `stage_out` sends `exit` LAST,
        # so `_record_endings` above already established the outputs landed.
        retention.register_new(conn, now)
        retention.reap_expired(conn, now)
        # Recipes have no TTL and are never evicted — only collecting left is after a deleted account:
        # the rows CASCADE, but nothing in SQLite can reach the files they name.
        recipes.sweep_orphans(conn)
        # Close the rsync grant of every Spawn whose copy home has landed, or whose window has passed.
        # AFTER register_new deliberately: that call consumes the `recovery.size` marker this one closes
        # on, and closing first would revoke a Siphon still carrying its copy.
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
            # The Guise the job runs as, off this pass's own poll — the account the tick becomes to
            # re-nice it.
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
