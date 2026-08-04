"""Arm Retract/Return: an owner's self-service pause of their own machine.

App-layer, not real Slurm DRAIN (that needs Slurm-operator privilege on a node) — every other
admin-level command (admit, benchmark, remove, karma bump) stays Head-CLI-only forever. Retract needs no
privilege: every Spawn already runs as the Mantle, so ending one is the owner's right, and steering NEW
placement away from a node is just `--exclude=` at submit time. Two callers: the owner-scoped Head API
route, and the Admin's `--any` escape hatch (Head-local).

Retract fires immediately: SIGTERM to every Spawn RUNNING on the node (cgroup delivers it to the whole
job, so the EXIT trap still reclaims scratch and stages out). GRACE_SECONDS later the Karma tick
force-kills anything still alive; either way it settles at ZERO — Retract must never let a donor farm
Karma by yanking a card back mid-run.

Return only reopens the node for NEW placement; it does not reprieve a Spawn already caught (see db.py's
`retracted_jobs`).
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
