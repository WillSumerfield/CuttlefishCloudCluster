"""An in-memory Slurm, for driving the real Karma economy across simulated weeks (Phase 4 slice 4).

This is **not a mock of the calls the tick makes** — it is a model of the scheduler *behaviour* the
economy depends on, and only that behaviour. The short list it encodes was each verified against real
Slurm on hardware 2026-07-14 (see temp/KARMA.md, "Verified on real hardware"):

  * an owner may not set a negative nice (the tick only ever lowers to >= 0; a negative nice is the
    operator-only Bump flag, which the tick must never touch and never forge);
  * a Spawn's priority is `PriorityWeightPartition + PriorityWeightAge·age_factor − nice`, and nothing
    else (fairshare/QOS/jobsize are weighted 0 in our slurm.conf);
  * age_factor ramps linearly to 1.0 over PRIORITY_MAX_AGE (1 day) and then saturates;
  * a free eligible card goes to the highest-priority waiting Spawn; ties break by node Weight
    (∝ 1/Power), so the fastest free card wins;
  * a Spawn ends at its `--time` limit, or when cancelled — and a cancelled/ended Spawn simply vanishes
    from `squeue`, which is the *only* signal the economy gets that it is over.

It does NOT model backfill, real placement latency, or any factor our conf zeroes. Modelling only the
checked behaviours is deliberate: a fuller replica would model code paths we never exercise, and — more
importantly — inventing scheduler rules the economy also assumes would make the test circular. The
economy is the real code; only Slurm is the model.

The whole thing runs on the simulation clock threaded through `tick.once(now=…)`, `karma.quote_hold`,
and the DB window queries, so a run of simulated weeks is internally consistent about *when* each
Demand sample, accrual, and settlement happened.
"""

from datetime import datetime, timedelta, timezone

from ccc_head_api import db, karma, tick
from cuttle import config as cli_config

_MAX_AGE_DAYS = 1.0  # mirrors cli_config.PRIORITY_MAX_AGE = "1-0"; age_factor saturates here


class FakeCluster:
    def __init__(self, conn, arms: list[dict], start: datetime | None = None):
        """`arms`: registry-shaped dicts (name, powers, vram_mb, owner, admitted, state) — `powers` is
        one Power per **Device**, as the registry holds it. `conn`: the real temp head.db the economy
        reads and writes."""
        self.conn = conn
        self._arms = {a["name"]: {"state": "idle", "admitted": True, **a} for a in arms}
        self.jobs: dict[int, dict] = {}
        self.now = start or datetime(2026, 1, 6, 9, 0, tzinfo=timezone.utc)  # a Monday 09:00
        self._next_id = 1

    def install(self, monkeypatch) -> "FakeCluster":
        """Wire this cluster in as the tick's Slurm. Without it, tick.once() shells out to the real
        `squeue`/`sinfo` — which on a live Head sees the real cluster's jobs, not the simulation's, and
        would settle every simulated Spawn as 'ended' on the first pass."""
        monkeypatch.setattr(tick.slurm, "queue", self.queue)
        monkeypatch.setattr(tick.slurm, "arms", self.arms)
        monkeypatch.setattr(tick.slurm, "set_nice", self.set_nice)
        return self

    # --- the slurm surface the tick reads (monkeypatched onto tick.slurm) ------------------------

    def arms(self) -> list[dict]:
        return list(self._arms.values())

    def queue(self) -> list[dict]:
        """PENDING and RUNNING CCC jobs, exactly the shape slurm.queue() returns. A GONE job is absent —
        which is how the economy learns it ended (there is no other signal)."""
        out = []
        for j in self.jobs.values():
            if j["state"] in ("PENDING", "RUNNING"):
                # start_time is a Unix epoch, as slurm.queue() returns it (the meter calls
                # datetime.fromtimestamp on it); None while PENDING.
                st = j["start_time"].timestamp() if j["start_time"] else None
                # `devices` are the GRES indices Slurm reports for a RUNNING job (slurm._gpu_indices).
                # A fake Arm holds one card, so a placed Spawn holds index 0; a PENDING one holds none.
                devices = list(range(j["gpus"])) if j["state"] == "RUNNING" else []
                out.append({"id": j["id"], "state": j["state"], "nice": j["nice"],
                            "node": j["node"], "gpus": j["gpus"], "devices": devices,
                            "start_time": st})
        return out

    def set_nice(self, job_id: int, nice: int) -> None:
        # Real Slurm refuses a negative nice from the job's owner, and the tick is the owner (the Mantle).
        # If the economy ever tried to push one, this is where hardware would have rejected it.
        assert nice >= 0, "the economy must never push a negative nice — that is the operator's Bump"
        self.jobs[job_id]["nice"] = nice

    # --- submission, through the SAME quote production uses ---------------------------------------

    def submit(self, user: dict, vram_gb: int, minutes: int, gpus: int = 1,
               node: str | None = None) -> int:
        """Enqueue a Spawn. The hold is quoted by karma.quote_hold — the identical code the /submit route
        runs — so the simulation cannot price a Spawn differently from production."""
        jid = self._next_id
        self._next_id += 1
        need = {"node": node, "vram_gb": vram_gb, "gpus": gpus}
        hold = karma.quote_hold(self.conn, self.arms(), need, minutes, gpus, user["is_admin"], now=self.now)
        db.record_spawn(self.conn, jid, user["id"], "sim.py", f"u{jid}",
                        minutes=minutes, gpus=gpus, vram_gb=vram_gb, node=node, hold=hold)
        self.jobs[jid] = {"id": jid, "user": user, "need": need, "minutes": minutes, "gpus": gpus,
                          "nice": 0, "state": "PENDING", "node": None, "start_time": None,
                          "submitted": self.now}
        return jid

    def cancel(self, job_id: int) -> None:
        """Retract/scancel: the Spawn vanishes from the queue. If it had not started, it accrued nothing
        and settles at zero; if it was running, it settles on what it accrued."""
        self.jobs[job_id]["state"] = "GONE"

    # --- the scheduler: place and finish, the checked behaviours only -----------------------------

    def _priority(self, j: dict) -> float:
        age_days = (self.now - j["submitted"]).total_seconds() / 86400
        age_factor = min(1.0, age_days / _MAX_AGE_DAYS)
        return cli_config.PRIORITY_BASE + cli_config.PRIORITY_WEIGHT_AGE * age_factor - j["nice"]

    def _finish(self) -> None:
        for j in self.jobs.values():
            if j["state"] == "RUNNING":
                if (self.now - j["start_time"]).total_seconds() >= j["minutes"] * 60:
                    j["state"] = "GONE"

    def _schedule(self) -> None:
        free = set(self._arms)
        for j in self.jobs.values():
            if j["state"] == "RUNNING":
                free.discard(j["node"])
        pending = [j for j in self.jobs.values() if j["state"] == "PENDING"]
        for j in sorted(pending, key=lambda x: -self._priority(x)):  # highest priority first
            elig = [a for a in karma.eligible(self.arms(), j["need"]) if a["name"] in free]
            if not elig:
                continue
            elig.sort(key=lambda a: cli_config.node_weight(a["powers"] or [1e-9]))  # fastest free card
            pick = elig[0]["name"]
            free.discard(pick)
            j["state"] = "RUNNING"
            j["node"] = pick
            j["start_time"] = self.now

    def advance(self, minutes: int) -> tuple[int, int]:
        """Move the clock forward and run one step of the world:

            finish what is done  ->  run ONE real tick.once  ->  place what now fits

        The tick runs **before** placement on purpose. Slurm ranks by `nice`, and `nice` is the tick's
        output — so a scheduler that placed Spawns before the tick had pushed their standing would decide
        the first contention of every Spawn's life on a stale nice of 0, letting a poor-standing Spawn win
        a card purely by being submitted a moment earlier. On real Slurm the two run continuously; putting
        the rerank first is the faithful order for a discrete step. (`_finish` precedes the tick so an
        ended Spawn settles the same step; a freshly *placed* Spawn is metered from the next step, its
        first interval billed in full.)

        Returns the tick's (re-ranked, settled) counts, so a test can watch the economy react."""
        self.now += timedelta(minutes=minutes)
        self._finish()
        result = tick.once(now=self.now)
        self._schedule()
        return result

    # --- convenience for assertions --------------------------------------------------------------

    def state(self, job_id: int) -> str:
        return self.jobs[job_id]["state"]

    def waited_days(self, job_id: int) -> float:
        """How long a job sat PENDING before it started (or has waited so far, if still pending)."""
        j = self.jobs[job_id]
        end = j["start_time"] or self.now
        return (end - j["submitted"]).total_seconds() / 86400
