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
            "submit_time": _num(j.get("submit_time")),
            "start_time": _num(j.get("start_time")),
        })
    return sorted(out, key=lambda s: s["id"] or 0)
