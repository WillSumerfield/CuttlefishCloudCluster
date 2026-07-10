"""CCC Head enrollment API.

Phase 1 ③ ships enrollment. This app stays **enrollment-only**: the read/Spawn views the SPA consumes
live in a *separate* dashboard app (`ccc_head_api.dashboard`), because they have opposite exposure
needs — enrollment hands out the munge key and must stay mesh-only, while the dashboard must reach the
LAN. Splitting them keeps the secret physically off every non-mesh interface.

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
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

MUNGE_KEY = Path("/etc/munge/munge.key")
REGISTRY = Path(os.environ.get("CCC_ARMS_REGISTRY", "/var/lib/ccc/arms.json"))
SLURMD_PORT = 6818  # mirrors cuttle.config.SLURMD_PORT

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
