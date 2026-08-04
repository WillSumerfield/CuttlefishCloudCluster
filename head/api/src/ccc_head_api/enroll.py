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

**The two gates are now tied together** (Phase 9 #2). Until this, they never actually met: the bind
address proved the caller was ON the mesh, and the registry check proved the CLAIMED name was
declared, but nothing checked that the two were the same machine — `req.mesh_ip` was trusted
straight from the request body. Any admitted Arm's owner could claim a different, already-admitted
node_name and be handed that node's key. `/enroll` now asks Headscale who is actually registered at
`request.client.host` (the one field in this request a caller cannot forge) and refuses a mismatch —
see `_node_named_at`.

**The service is unprivileged, with one narrow exception.** It runs as `User=munge` — the identity
that can read 0400 munge:munge /etc/munge/munge.key without loosening the file or running a network
listener as root. It cannot write /etc/slurm or run scontrol, and `POST /enroll` only updates a
munge-owned registry row an operator already created — the operator turns that into a live Slurm node
with `cuttle arm admit`. Its one sudo grant (`headscale nodes list`, nothing else headscale's CLI can
do — see `cuttle.headapi.install_enroll_sudoers`) takes no argument from the request, so no request
body has a path to root even through it.

Accepted Phase-1 consequence, narrowed but not closed by the above: an Arm, once legitimately
admitted, still holds the munge key on disk forever, and munge is cluster-wide and authenticates as
any node or any user. Only the *join token* is short-lived. See `docs/TODO.md` Phase 9 #1 — per-Arm
revocable credentials, still open; `cuttle munge rotate` + `cuttle arm sync-key` are the coarser,
cluster-wide lever that exists today.
"""

import base64
import json
import os
import logging
import re
import socket
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

# stdlib logging -> stdout -> journald, same shape as the Head API and the Karma tick. This service is
# small and mesh-only, so it gets targeted event logs (who was handed the munge key, who was refused)
# rather than the Head API's per-request middleware — the munge-key grant is the one event that matters.
log = logging.getLogger("ccc.enroll")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(os.environ.get("CCC_LOG_LEVEL", "INFO").upper())
    log.propagate = False

MUNGE_KEY = Path("/etc/munge/munge.key")
REGISTRY = Path(os.environ.get("CCC_ARMS_REGISTRY", "/var/lib/ccc/arms.json"))
# The one apptainer build this cluster runs: `cuttle setup --head` downloaded the .deb once and
# recorded {version, filename, sha256}, and this service hands that same file to every Arm forever.
# Both live under /var/lib/ccc because this service runs as `munge` and that is the tree it can read.
DEBS_DIR = Path(os.environ.get("CCC_DEBS_DIR", "/var/lib/ccc/debs"))
APPTAINER_RECORD = Path(os.environ.get("CCC_APPTAINER_RECORD", "/var/lib/ccc/apptainer.json"))
SLURMD_PORT = 6818  # mirrors cuttle.config.SLURMD_PORT
# The catalog of Arm Features (docs/TODO.md "Arm Features", temp/FEATURES_BUILTIN.md). Built into CCC,
# not curated and not state — CCC ships the runtime for each one, so this is the set it knows how to
# arrange for a Spawn, and a name outside it could only ever be a Slurm token with nothing behind it.
# A donor's declared features are validated against it below.
#
# Mirrors cuttle.config.FEATURE_NAMES and ccc_head_api.slurm.FEATURE_NAMES (neither app can import the
# others; see the note in slurm.py). All three must agree.
FEATURE_NAMES = ("vlearn", "isaaclab")

# Mirrors cuttle.config; re-validated root-side by `cuttle arm admit` before reaching gres.conf.
NODE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,31}$")
GPU_DEVICE_RE = re.compile(r"^/dev/(nvidia[0-9]+|dxg)$")
# Same shape as cuttle.config.FEATURE_NAME_RE — it also becomes a Slurm Feature= token and an env var.
FEATURE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

app = FastAPI(title="CCC Enroll API", version="0.1.0")


class FeatureDecl(BaseModel):
    """One Arm Feature this box claims to have, and where it lives — `cuttle setup --arm`'s wizard
    output, picked from `GET /features` (never free-typed) and prompted for an install path."""
    name: str
    path: str


class EnrollRequest(BaseModel):
    node_name: str
    # Every GPU **Device** the Arm can hand to a Spawn, in the order its gres.conf lines must be
    # written — /dev/nvidiaN per card natively, and exactly ONE /dev/dxg under WSL2 however many
    # physical cards the box has (one device node fronts them all; a cgroup cannot split it).
    gpu_devices: list[str]
    mesh_ip: str
    # The memory of one card. One number for the whole box, because `setup --arm` refuses to enroll a
    # machine whose GPUs are not all the same — a Slurm Feature is per-node, so a mixed box could not
    # advertise both sizes honestly (cuttle.system.gpu_profile).
    vram_mb: int
    # Arm Features (docs/TODO.md) this box declares, each validated against FEATURE_NAMES below.
    # Optional and defaults empty — most Arms have none, and older CLIs never sent the field at all.
    features: list[FeatureDecl] = []
    # What Arm-side files this box has WRITTEN (temp/spawn-isolation.md, Rollout). Configless Slurm
    # serves slurm.conf but not the scripts it names, so a `Prolog=` naming a file an Arm does not have
    # fails that Arm's jobs and DRAINS it — and a drained node stays drained across a slurmd restart,
    # so fixing the file afterwards does not heal it. The Head therefore refuses to render `Prolog=`
    # until every admitted Arm has reported it here. Defaults empty: an Arm running an older CLI has
    # not written the prolog, and saying so is exactly right.
    provides: list[str] = []


def _load() -> dict:
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}


def _save(reg: dict) -> None:
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/features")
def features_catalog() -> list[str]:
    """The Arm Features CCC supports. Kept as an endpoint although the answer is now a constant every
    caller already has: it is how a client learns what the HEAD supports, which matters the moment a
    donor's CCC is a different version from the Head's."""
    return sorted(FEATURE_NAMES)


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


def _node_named_at(peer: str) -> str | None:
    """Whichever node Headscale has registered at mesh address `peer`, by its Headscale name — or
    None if no node holds it. Phase 9 #2: `POST /enroll` used to take the caller's word for both
    its address (`req.mesh_ip`) and its name (`req.node_name`); this is what lets it check the
    first against a source neither a spoofed field nor a spoofed source IP can move (a forged
    source gets no TCP handshake, same reasoning as `probe` above).

    Shells to `sudo -n headscale nodes list` — the ONE privilege this service has (see
    `cuttle.headapi.install_enroll_sudoers`); `munge` cannot reach Headscale's own unix socket
    (0750 headscale:headscale) to ask any other way. `-n`: this runs inside a request handler with
    no tty to prompt, so a broken sudoers rule must fail fast, not hang the request.
    """
    try:
        r = subprocess.run(["sudo", "-n", "headscale", "nodes", "list", "--output", "json"],
                           capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise HTTPException(503, "timed out asking Headscale for its node list")
    if r.returncode != 0:
        raise HTTPException(503, "could not read Headscale's node list — the Head's operator should "
                                 "check the ccc-enroll-api service log and "
                                 "/etc/sudoers.d/ccc-munge-headscale")
    for node in json.loads(r.stdout) or []:  # `null`, not `[]`, when Headscale knows no nodes yet
        if peer in (node.get("ip_addresses") or []):
            return node.get("given_name") or node.get("name")
    return None


def _apptainer_pin() -> dict:
    """What the Head pinned. Absent means `setup --head` predates the pin (or never finished), and an
    Arm cannot install apptainer at all — so it is a loud 503 rather than a fallback to the PPA, which
    is the exact divergence this mechanism exists to prevent."""
    if not APPTAINER_RECORD.exists():
        raise HTTPException(503, "this Head has not pinned an apptainer .deb — its operator must "
                                 "re-run `cuttle setup --head`")
    return json.loads(APPTAINER_RECORD.read_text())


@app.get("/apptainer.deb")
def apptainer_deb() -> FileResponse:
    """Serve the pinned .deb. Unauthenticated like the rest of this app (being on the mesh is the
    authorization) and, unlike the munge key, it is no secret — it is a public Launchpad artifact.
    What matters is that it is always the SAME one."""
    return FileResponse(DEBS_DIR / _apptainer_pin()["filename"],
                        media_type="application/vnd.debian.binary-package")


@app.get("/arms/{name}")
def arm_status(name: str) -> dict:
    """An enrolling Arm polls this until the operator admits it."""
    entry = _load().get(name)
    if entry is None:
        raise HTTPException(404, f"no Arm named {name!r}")
    return {"name": name, "admitted": entry["admitted"]}


@app.post("/enroll")
def enroll(req: EnrollRequest, request: Request) -> dict:
    """Record an Arm's hardware and hand back everything it needs — none of which it could get before
    joining the mesh. Recording is all this does: the Arm is inert until `cuttle arm admit` runs on the
    Head (or the `cuttle arm authorize` that is still blocked, waiting for exactly this POST).

    **An Arm must have been authorized by name.** `cuttle arm authorize` pre-declares the row before
    the enrollment token is ever handed out, so a machine on the mesh that no Admin ever named is
    refused here — and the munge key is what it is being refused. This is the second of enrollment's
    two gates, and like the first it traces back to one admin decision. Mesh membership alone is no
    longer enough to obtain the cluster's shared secret.

    **The claimed name must match who Headscale says is actually calling** (Phase 9 #2). Before this,
    `req.node_name` was checked against the registry but `req.mesh_ip` was trusted outright — so any
    admitted Arm's owner (or a machine that had ever redeemed ANY still-valid preauth key) could POST
    a DIFFERENT node_name it knew was already admitted and be handed that node's munge key, gres
    identity be damned; the mesh gate and the pre-declaration gate never actually met each other.
    `request.client.host` is the one thing in this request a caller cannot forge (a forged source
    gets no TCP handshake — same reasoning `probe` above already relies on), so it — not the body —
    is what gets checked against Headscale.

    An UPDATE, never a create. The pre-declared fields (owner, above all) must survive the donor's
    POST: this endpoint is the one thing a donor's machine gets to write, and it does not get to say
    who owns it.

    The response also carries the apptainer pin, so the Arm knows what `/apptainer.deb` should hand it
    before it downloads it.
    """
    if not NODE_NAME_RE.match(req.node_name):
        raise HTTPException(422, f"invalid Arm name {req.node_name!r} — must be 1-32 characters, "
                                 f"letters/digits/hyphens, starting with a letter or digit")
    if not req.gpu_devices:
        raise HTTPException(422, "an Arm must report at least one GPU device")
    for device in req.gpu_devices:
        if not GPU_DEVICE_RE.match(device):
            raise HTTPException(422, f"unsupported GPU device {device!r} — expected /dev/nvidiaN "
                                     f"(native Linux) or /dev/dxg (WSL2)")
    # Hard-validate every declared Arm Feature against the catalog — no free typing, on the same
    # philosophy as the output-name / GPU-device checks above: a name outside it is refused outright,
    # not warned about. An unknown name now means the donor's CCC is NEWER than this Head's, since both
    # ends carry the same constant — so the remedy names that, not an operator command.
    if req.features:
        for f in req.features:
            if not FEATURE_NAME_RE.match(f.name):
                raise HTTPException(422, f"invalid feature name {f.name!r} — letters/digits/'.'/'_'/"
                                         f"'-' only, starting with a letter or digit")
            if f.name not in FEATURE_NAMES:
                raise HTTPException(
                    422, f"unknown feature {f.name!r} — this Head's CCC supports: "
                        f"{', '.join(sorted(FEATURE_NAMES))}. A newer feature means this Head needs "
                        f"upgrading, not that your box is wrong.")
            if not f.path.strip():
                raise HTTPException(422, f"feature {f.name!r} needs a non-empty install path")

    # Read before the row is touched: a Head with no pinned .deb cannot enroll anyone (the Arm would
    # have no apptainer to install), so it fails here rather than after the munge key has left.
    pin = _apptainer_pin()

    reg = _load()
    if req.node_name not in reg:
        # A machine on the mesh reaching for the munge key under a name no Admin ever declared. Refused,
        # and worth a WARNING: it is the enrollment gate doing its job, and the signal if it ever fires
        # for a name the operator didn't expect.
        log.warning("enroll REFUSED undeclared node=%r from %s", req.node_name, request.client.host)
        raise HTTPException(403, f"{req.node_name!r} was not authorized to join this cluster — "
                                 f"ask the Head's operator to run `cuttle arm authorize`")

    peer = request.client.host
    actual_name = _node_named_at(peer)
    if actual_name != req.node_name:
        # Declared (it's in the registry) but not WHO IT SAYS IT IS — the gate the mesh-membership
        # check and the pre-declaration check never actually shared until now. Worth a WARNING for the
        # same reason the undeclared case is: this firing at all is the signal, whether it's a stale
        # Headscale re-registration racing this request or someone actually trying it.
        log.warning("enroll REFUSED node=%r claimed by %s, which Headscale calls %r",
                   req.node_name, peer, actual_name)
        raise HTTPException(403, f"{peer} is not registered to {req.node_name!r} on the mesh")

    entry = reg[req.node_name]
    entry["gpu_devices"] = req.gpu_devices
    # The VERIFIED address, never the request body's: `req.mesh_ip` is only still on the wire because
    # dropping it would be a breaking change to EnrollRequest, but nothing below this line reads it.
    entry["mesh_ip"] = peer
    # Hardware profile (Phase 4 slice 0). VRAM is *declared* here; Power is *measured* later, by a
    # benchmark the Head submits once the node is live — a donor could inflate either, and the trust
    # story is unchanged (an Arm can already fabricate results outright, and donors are never
    # strangers), but only one of the two can be taken on the donor's word without it also being wrong.
    entry["vram_mb"] = req.vram_mb
    # An UPDATE like every other declared field above: a re-enroll (mesh IP moved, `sync-key`) resends
    # whatever this Arm currently has, so a genuine removal (the owner un-picks a feature and re-runs
    # setup) actually takes — a setdefault here would let a stale declaration outlive its removal.
    entry["features"] = [f.model_dump() for f in req.features]
    # An UPDATE for the same reason, and a load-bearing one: this is what the Head gates `Prolog=` on
    # (temp/spawn-isolation.md, Rollout). A stale "yes, I have the prolog" surviving a downgrade is
    # precisely the claim that would drain a node, so the answer is always this run's.
    entry["provides"] = list(req.provides)
    # Re-enrolling an already-admitted Arm (e.g. after the Head's mesh IP moved) stays admitted.
    entry.setdefault("admitted", False)
    _save(reg)

    # The security event: the cluster's shared secret just left the Head for this node. Logged with the
    # owner the Admin bound to it, so the journal answers "who holds the munge key, and on whose say-so".
    log.info("munge key -> node %r owner=%s mesh_ip=%s", req.node_name, entry.get("owner", "?"), peer)
    return {
        "munge_key": base64.b64encode(MUNGE_KEY.read_bytes()).decode(),
        "ctld_addr": os.environ["CCC_CTLD_ADDR"],
        "cluster": os.environ["CCC_CLUSTER"],
        "partition": os.environ["CCC_PARTITION"],
        "admitted": entry["admitted"],
        "apptainer": pin,  # {version, filename, sha256} — what GET /apptainer.deb will hand over
    }
