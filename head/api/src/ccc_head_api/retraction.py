"""Arm Retract/Return — an owner's self-service pause of their own machine (Phase 2 loose end).

App-layer, not real Slurm DRAIN. That would need genuine Slurm-operator privilege (`scontrol update`
on a *node*) — the Privileged Sidecar this once implied was killed as a roadmap goal (every other
admin-level command — admit, benchmark, remove, karma bump — stays Head-CLI-only forever). Retract
needs none of that: every Spawn already runs as the Mantle, so ending one is an owner's right with no
privilege at all, and steering NEW placement away from a node is just an ordinary `--exclude=` at
submit time (see spawns.submit_checked). Two callers share this module: the owner-scoped Head API
route (any box, over HTTP) and the Admin's `--any` escape hatch (`ccc_head_api.admin`, Head-local).

Retract fires immediately: every Spawn RUNNING on the node right now is sent SIGTERM. Slurm's
proctrack/cgroup delivers that to every process in the job's cgroup, not just the wrapper script, so
the containerized process sees it directly and the job's existing EXIT trap still reclaims scratch and
stages out whatever it can. It then has GRACE_SECONDS to exit on its own; the Karma tick force-kills
anything still alive past the deadline (see tick._force_expired_retractions). Either way it settles at
ZERO (tick._settle) — Retract must never be a way to farm Karma by donating a card and yanking it back
mid-run.

Return only reopens the node for NEW placement (db.clear_arm_retraction). It does not reprieve a
Spawn already caught — see db.py's `retracted_jobs` docstring for why that is its own table.
"""

from datetime import datetime, timedelta, timezone

from . import db, slurm

GRACE_SECONDS = 180
_TS = "%Y-%m-%dT%H:%M:%SZ"


def retract(node: str) -> None:
    now = datetime.now(timezone.utc)
    deadline = (now + timedelta(seconds=GRACE_SECONDS)).strftime(_TS)
    running = [j["id"] for j in slurm.queue() if j["state"] == "RUNNING" and j["node"] == node]
    with db.connect() as conn:
        db.mark_arm_retracted(conn, node)
        for job_id in running:
            db.catch_retracting_job(conn, job_id, node, deadline)
    # Signalled AFTER the transaction commits: a Spawn that exits the instant it's TERM'd must not race
    # the tick's next pass into seeing a `retracted_jobs` row that doesn't exist yet.
    for job_id in running:
        slurm.signal(job_id, "TERM")


def unretract(node: str) -> None:
    with db.connect() as conn:
        db.clear_arm_retraction(conn, node)
