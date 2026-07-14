"""CCC Head enrollment API.

Phase 1 ③ ships enrollment. This app stays **enrollment-only**: the read/Spawn views the SPA consumes
live in a *separate* Head API app (`ccc_head_api.main`), because they have opposite exposure needs —
enrollment hands out the munge key and must stay mesh-only, while the Head API must reach the LAN.
Splitting them keeps the secret physically off every non-mesh interface.

**The first half of the security model is the bind address.** uvicorn listens on the Head's *mesh*
address only, an address that exists solely on the `ccc0` interface. Nothing on the LAN, on the
operator's personal tailnet, or on the internet has a route to it. There is no password and no TLS —
routability is the first gate, and only an enrollment token gets a machine onto the mesh at all.

**The second half is the pre-declared name** (Phase 3a slice 5). `cuttle arm authorize` writes the
Arm's row — name and owning User — into the registry *before* its token is handed out, and `/enroll`
refuses a name that isn't there. So mesh membership alone no longer buys the munge key: an
unauthorized machine that somehow reached the mesh still gets nothing. Both gates fall out of the
same single admin decision.

**The service is unprivileged.** It runs as `User=munge` — the one identity that can read 0400
munge:munge /etc/munge/munge.key without loosening the file or running a network listener as root.
It cannot write /etc/slurm or run scontrol. `POST /enroll` only updates a munge-owned registry row an
operator already created; the operator turns that into a live Slurm node with `cuttle arm admit`. So
no request body has a path to root, and the Head API inherits an unprivileged service, not a root one.

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

app = FastAPI(title="CCC Enroll API", version="0.1.0")


class EnrollRequest(BaseModel):
    node_name: str
    gpu_device: str  # /dev/nvidia0 on native Linux, /dev/dxg under WSL2
    mesh_ip: str
    vram_mb: int  # the card's memory — becomes its `vramN` Slurm feature, which `--vram` matches


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
    """Record an Arm's hardware and hand back everything it needs — none of which it could get before
    joining the mesh. Recording is all this does: the Arm is inert until `cuttle arm admit` runs on the
    Head (or the `cuttle arm authorize` that is still blocked, waiting for exactly this POST).

    **An Arm must have been authorized by name.** `cuttle arm authorize` pre-declares the row before
    the enrollment token is ever handed out, so a machine on the mesh that no Admin ever named is
    refused here — and the munge key is what it is being refused. This is the second of enrollment's
    two gates, and like the first it traces back to one admin decision. Mesh membership alone is no
    longer enough to obtain the cluster's shared secret.

    An UPDATE, never a create. The pre-declared fields (owner, above all) must survive the donor's
    POST: this endpoint is the one thing a donor's machine gets to write, and it does not get to say
    who owns it.
    """
    if not NODE_NAME_RE.match(req.node_name):
        raise HTTPException(422, f"bad node name {req.node_name!r}")
    if not GPU_DEVICE_RE.match(req.gpu_device):
        raise HTTPException(422, f"unsupported GPU device {req.gpu_device!r}")

    reg = _load()
    if req.node_name not in reg:
        raise HTTPException(403, f"{req.node_name!r} was not authorized to join this cluster — "
                                 f"ask the Head's operator to run `cuttle arm authorize`")

    entry = reg[req.node_name]
    entry["gpu_device"] = req.gpu_device
    entry["mesh_ip"] = req.mesh_ip
    # Hardware profile (Phase 4 slice 0). VRAM is *declared* here; Power is *measured* later, by a
    # benchmark the Head submits once the node is live — a donor could inflate either, and the trust
    # story is unchanged (an Arm can already fabricate results outright, and donors are never
    # strangers), but only one of the two can be taken on the donor's word without it also being wrong.
    entry["vram_mb"] = req.vram_mb
    # Re-enrolling an already-admitted Arm (e.g. after the Head's mesh IP moved) stays admitted.
    entry.setdefault("admitted", False)
    _save(reg)

    return {
        "munge_key": base64.b64encode(MUNGE_KEY.read_bytes()).decode(),
        "ctld_addr": os.environ["CCC_CTLD_ADDR"],
        "cluster": os.environ["CCC_CLUSTER"],
        "partition": os.environ["CCC_PARTITION"],
        "admitted": entry["admitted"],
    }
