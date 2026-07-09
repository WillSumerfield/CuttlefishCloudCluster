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
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MUNGE_KEY = Path("/etc/munge/munge.key")
REGISTRY = Path(os.environ.get("CCC_ARMS_REGISTRY", "/var/lib/ccc/arms.json"))

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
