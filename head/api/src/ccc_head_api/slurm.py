"""Slurm read-side: shell out to `squeue`/`sinfo --json` and project into the CCC-domain contract.

Shared by the dashboard app (⑥). Shells out to the CLI's `--json` rather than running slurmrestd —
identical data model, no daemon/JWT/bind (slurmrestd deferred to Phase 3 with job submission). Every
response is a hand-picked projection: Slurm job -> Spawn, node -> Arm, so the SPA never couples to
Slurm's ~120-field, versioned schema. Read-only and safe as the unprivileged `nobody` dashboard user;
nothing here touches secrets or the Head user's home.
"""

import json
import os
import re
import subprocess
from pathlib import Path

from fastapi import HTTPException

REGISTRY = Path(os.environ.get("CCC_ARMS_REGISTRY", "/var/lib/ccc/arms.json"))
PARTITION = os.environ.get("CCC_PARTITION", "ccc")


def load_registry() -> dict:
    """The enrollment registry (written by the enroll app). World-readable (0644), so the dashboard's
    `nobody` user reads it for the mesh_ip/admitted facts Slurm never sees."""
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}


def _slurm_json(*cmd: str) -> dict:
    """Run a Slurm CLI with --json and parse it. Surfaces failures as 503 rather than a 500 stack —
    the likely cause is slurmctld being down, which the SPA should show as 'cluster unreachable'."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        raise HTTPException(503, f"slurm unreachable ({' '.join(cmd)}): {e}")
    return json.loads(r.stdout)


def _num(v):
    """Unwrap Slurm's `{set,infinite,number}` scalar. Returns the int when set+finite, else None."""
    if isinstance(v, dict):
        if not v.get("set", True) or v.get("infinite"):
            return None
        return v.get("number")
    return v


def _gpu_count(gres: str) -> int:
    """Sum GPU counts from any Slurm gres string form: 'gpu:1', 'gres/gpu:1' (tres_per_node),
    'gres/gpu=2', or typed 'gpu:a100:2'. Returns 0 when there's no gpu term."""
    return sum(int(n) for n in re.findall(r"gpu(?::[a-z0-9_]+)?[:=](\d+)", gres or "", re.I))


def arms() -> list[dict]:
    """Every compute node's live Slurm status, enriched with enrollment facts the scheduler never
    sees: mesh address, admitted, and the **owner** — the User who donated the machine, named by the
    Admin when they authorized it (and what Karma will pay passive income to). `sinfo` is the
    authoritative node list; the Head's own co-located Arm is in it and carries an admin-owned
    registry row despite never enrolling."""
    reg = load_registry()
    out = []
    for rec in _slurm_json("sinfo", "--json").get("sinfo", []):
        state = "+".join(s.lower() for s in rec.get("node", {}).get("state", []))
        gpus = _gpu_count(rec.get("gres", {}).get("total", ""))
        for name in rec.get("nodes", {}).get("nodes", []):
            e = reg.get(name, {})
            out.append({
                "name": name,
                "state": state,
                "gpus": gpus,
                "owner": e.get("owner"),
                "mesh_ip": e.get("mesh_ip"),
                "gpu_device": e.get("gpu_device"),
                "admitted": e.get("admitted", True),
                # The hardware profile Karma prices from: what the card can DO (Power, measured at
                # enroll by a benchmark Spawn) and how much it can HOLD (the thing --vram matches).
                "gpu": e.get("gpu"),
                "vram_mb": e.get("vram_mb"),
                "power": e.get("power"),
            })
    return sorted(out, key=lambda a: a["name"])


def vram_constraint(vram_gb: int) -> str:
    """Turn `--vram 20` into a Slurm constraint over the fleet's actual cards: `vram24|vram48|vram80`.

    Slurm constraints are string **set-membership** — there is no `>=`. So an arbitrary integer can
    only be honoured by whoever knows every distinct card in the cluster, and expands the request into
    the OR of the ones that qualify. That is the Head, here. The alternative (fixed buckets baked into
    the Arms) would cap what a User may ask for at whatever list we guessed at design time.
    """
    sizes = sorted({round(e["vram_mb"] / 1024) for e in load_registry().values()
                    if e.get("admitted") and e.get("vram_mb")})
    eligible = [g for g in sizes if g >= vram_gb]
    if not eligible:
        have = ", ".join(f"{g}GB" for g in sizes) or "none"
        raise HTTPException(422, f"no Arm has {vram_gb}GB of VRAM — the cluster has: {have}")
    return "|".join(f"vram{g}" for g in eligible)


def queue() -> list[dict]:
    """Every live Spawn — PENDING and RUNNING — and the Karma tick's whole view of Slurm. One poll per
    pass, because the tick needs both halves and they must agree with each other:

    * **PENDING** Spawns are what it re-ranks (by `nice`) and what generates queued **Demand**.
    * **RUNNING** Spawns are what it meters — `node` says which Arm's Power and Demand to bill, and
      `start_time` is where the very first accrual bills *from* (a Spawn that started between two ticks
      still owes for the seconds before the tick noticed).

    Absence from a *successful* poll is the only signal a Spawn has **ended** — it is what triggers
    Settlement. There is no other: a killed Spawn writes no end-time, Slurm forgets jobs after
    `MinJobAge`, and `sacct` would need the slurmdbd we deliberately don't run. If slurmctld is down
    `_slurm_json` raises, the tick logs and skips, and nothing settles — which is the point.

    `nice` is read back rather than remembered: it is the one piece of state the tick shares with an
    operator, and Slurm is where they meet. A **negative** nice is a Queue bump — only an operator can
    set one (Slurm refuses a negative nice from a job's owner, and every Spawn is owned by the Mantle),
    so its mere presence is the bump flag. No bump table, no coordination state.
    """
    out = []
    for j in _slurm_json("squeue", "--json").get("jobs", []):
        state = j.get("job_state") or []
        if j.get("partition") != PARTITION:
            continue
        if "PENDING" in state:
            live = "PENDING"
        elif "RUNNING" in state:
            live = "RUNNING"
        else:
            continue  # COMPLETING/COMPLETED/FAILED — on its way out; the next poll settles it
        gres = ",".join(j.get("gres_detail") or []) or j.get("tres_per_node", "")
        out.append({
            "id": _num(j.get("job_id")),
            "state": live,
            "nice": _num(j.get("nice")) or 0,
            "node": (j.get("nodes") or None) if live == "RUNNING" else None,
            "gpus": _gpu_count(gres),
            "start_time": _num(j.get("start_time")),
        })
    return out


def set_nice(job_id: int, nice: int) -> None:
    """Lower a queued Spawn's priority. Needs no privilege at all: every Spawn runs as the Mantle, so
    the Head *owns* every job in the queue, and setting your own job's nice is an owner's right (only a
    negative nice — a boost — needs a Slurm operator)."""
    subprocess.run(["scontrol", "update", f"JobId={job_id}", f"Nice={nice}"],
                   capture_output=True, text=True, timeout=15, check=True)


def spawns() -> list[dict]:
    """The live Spawn queue — pending/running jobs in the CCC partition. Completed Spawns drop out of
    squeue after MinJobAge; durable history waits for Phase 3 slurmdbd (see module docstring)."""
    out = []
    for j in _slurm_json("squeue", "--json").get("jobs", []):
        if j.get("partition") != PARTITION:
            continue
        # tres_per_node ('gres/gpu:1') is the reliable GPU source — the *allocated* TRES omits gpu
        # (no gres accounting), and gres_detail is empty until/unless the step runs.
        gres = ",".join(j.get("gres_detail") or []) or j.get("tres_per_node", "")
        out.append({
            "id": _num(j.get("job_id")),
            "name": j.get("name"),
            "state": "+".join(s.lower() for s in j.get("job_state", [])),
            "node": j.get("nodes") or None,
            "gpus": _gpu_count(gres),
            "user": j.get("user_name"),
            # The declared duration (`--time`), in minutes — what a waiting Spawn is priced against and
            # the one declared need squeue carries cleanly. `--vram` becomes a Slurm *constraint*
            # string, which is not worth parsing back out here.
            "time_limit": _num(j.get("time_limit")),
            "submit_time": _num(j.get("submit_time")),
            "start_time": _num(j.get("start_time")),
        })
    return sorted(out, key=lambda s: s["id"] or 0)


def cancel(job_id: int) -> None:
    """End a live Spawn now. `scancel` needs no privilege here — every Spawn runs as the Mantle, so the
    Head owns every job in the queue.

    Settlement is NOT done here: the Karma tick settles a Spawn on its *disappearance from squeue*
    (slurm.queue()), which is exactly what this causes, so a cancel bills for the seconds actually held
    with no second code path. Idempotent — scancel on an already-gone job is a no-op, not an error."""
    subprocess.run(["scancel", str(job_id)], capture_output=True, text=True, timeout=15, check=True)
