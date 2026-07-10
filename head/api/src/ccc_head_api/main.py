"""CCC Head API.

Phase 1 ③ ships enrollment. Phase 1 ⑤ grows this into the Slurm/Spawn API (via slurmrestd) that the
SPA reads.

**The security model is the bind address.** uvicorn listens on the Head's *mesh* address only, an
address that exists solely on the `ccc0` interface. Nothing on the LAN, on the operator's personal
tailnet, or on the internet has a route to it. Being on the CCC mesh is therefore the entire
authorization: there is no token, no password, no TLS.

**The service is unprivileged.** It runs as `User=munge` — the one identity that can read 0400
munge:munge /etc/munge/munge.key without loosening the file or running a network listener as root.
It cannot write /etc/slurm or run scontrol. `POST /enroll` only appends to a munge-owned registry;
an operator turns that into a live Slurm node with `cuttle arm admit`. So no request body has a path
to root, and ⑤ inherits an unprivileged service rather than a root one.

Accepted Phase-1 consequence: mesh compromise == Slurm compromise, since any mesh member may fetch
the munge key. Munge is cluster-wide, never expires, and authenticates as any node or any user. Only
the *join token* is short-lived. See `docs/TODO.md` — Phase 2 adds per-Arm revocable credentials.
"""

import base64
import json
import os
import re
import socket
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

MUNGE_KEY = Path("/etc/munge/munge.key")
REGISTRY = Path(os.environ.get("CCC_ARMS_REGISTRY", "/var/lib/ccc/arms.json"))
SLURMD_PORT = 6818  # mirrors cuttle.config.SLURMD_PORT
PARTITION = os.environ.get("CCC_PARTITION", "ccc")

# Mirrors cuttle.config; re-validated root-side by `cuttle arm admit` before reaching gres.conf.
NODE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,31}$")
GPU_DEVICE_RE = re.compile(r"^/dev/(nvidia[0-9]+|dxg)$")

app = FastAPI(title="CCC Head API", version="0.1.0")


class EnrollRequest(BaseModel):
    node_name: str
    gpu_device: str  # /dev/nvidia0 on native Linux, /dev/dxg under WSL2
    mesh_ip: str


def _load() -> dict:
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}


def _save(reg: dict) -> None:
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/probe")
def probe(request: Request) -> dict:
    """Can the Head open a connection back to the caller's slurmd? Only the Head can answer this,
    and an Arm cannot test its own inbound reachability — hence an endpoint.

    Takes **no address**. We dial the source address of this very request, which a caller cannot
    forge (a forged source gets no TCP handshake, so there is no request to serve). An endpoint that
    accepted a target would turn this mesh-privileged host into a connect-anywhere probe.

    Answers the question `POST /enroll` cannot: enrollment proves Arm->Head, while slurmctld needs
    Head->Arm. A node that registers but is unreachable goes DOWN minutes later, far from the cause.
    """
    peer = request.client.host
    try:
        with socket.create_connection((peer, SLURMD_PORT), timeout=3):
            return {"reachable": True, "peer": peer, "port": SLURMD_PORT}
    except OSError as e:
        return {"reachable": False, "peer": peer, "port": SLURMD_PORT, "error": str(e)}


@app.get("/arms/{name}")
def arm_status(name: str) -> dict:
    """An enrolling Arm polls this until the operator admits it."""
    entry = _load().get(name)
    if entry is None:
        raise HTTPException(404, f"no Arm named {name!r}")
    return {"name": name, "admitted": entry["admitted"]}


@app.post("/enroll")
def enroll(req: EnrollRequest) -> dict:
    """Record an Arm and hand back everything it needs — none of which it could get before joining
    the mesh. Recording is all this does: the Arm is inert until `cuttle arm admit` runs on the Head.
    """
    if not NODE_NAME_RE.match(req.node_name):
        raise HTTPException(422, f"bad node name {req.node_name!r}")
    if not GPU_DEVICE_RE.match(req.gpu_device):
        raise HTTPException(422, f"unsupported GPU device {req.gpu_device!r}")

    reg = _load()
    prior = reg.get(req.node_name, {})
    reg[req.node_name] = {
        "gpu_device": req.gpu_device,
        "mesh_ip": req.mesh_ip,
        # Re-enrolling an already-admitted Arm (e.g. after the Head's mesh IP moved) stays admitted.
        "admitted": prior.get("admitted", False),
    }
    _save(reg)

    return {
        "munge_key": base64.b64encode(MUNGE_KEY.read_bytes()).decode(),
        "ctld_addr": os.environ["CCC_CTLD_ADDR"],
        "cluster": os.environ["CCC_CLUSTER"],
        "partition": os.environ["CCC_PARTITION"],
        "admitted": reg[req.node_name]["admitted"],
    }


# --- read API (⑤): the SPA's JSON contract ----------------------------------
# Slurm is read by shelling out to `--json` (identical data model slurmrestd serves; no daemon/JWT).
# Every response is a hand-picked CCC-domain projection — Slurm job -> Spawn, node -> Arm — so the SPA
# never couples to Slurm's ~120-field, versioned schema. Read-only and safe as the unprivileged munge
# user; nothing here touches ~/.ccc/spawns (which munge can't read), so completed-Spawn history/logs
# are out until Phase 3 slurmdbd gives a real accounting source.

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


@app.get("/arms")
def arms() -> list[dict]:
    """The Arms screen: every compute node's live Slurm status, enriched with enrollment facts the
    scheduler never sees (mesh address, admitted). `sinfo` is the authoritative node list — it
    includes the Head's own co-located Arm, which never enrolls, so it simply has no registry row."""
    reg = _load()
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
                # Registry-only facts; the Head's own Arm has no row -> implicitly admitted, no mesh IP.
                "mesh_ip": e.get("mesh_ip"),
                "gpu_device": e.get("gpu_device"),
                "admitted": e.get("admitted", True),
            })
    return sorted(out, key=lambda a: a["name"])


@app.get("/spawns")
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
