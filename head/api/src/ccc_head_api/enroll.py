"""CCC Head enrollment API.

Stays **enrollment-only**: the read/Spawn views the SPA consumes live in a separate Head API app
(`ccc_head_api.main`), because they have opposite exposure needs — enrollment hands out the munge key
and must stay mesh-only, while the Head API must reach the LAN. Splitting them keeps the secret
physically off every non-mesh interface.

**First gate: the bind address.** uvicorn listens on the Head's *mesh* address only (`ccc0`) — nothing
on the LAN, the operator's personal tailnet, or the internet has a route to it. No password, no TLS:
routability is the first gate, and only an enrollment token gets a machine onto the mesh at all.

**Second gate: the pre-declared name.** `cuttle arm authorize` writes the Arm's row (name + owning User)
into the registry *before* its token is handed out, and `/enroll` refuses a name that isn't there —
mesh membership alone no longer buys the munge key.

**The two gates are tied together.** `/enroll` asks Headscale who is actually registered at
`request.client.host` (the one field a caller cannot forge) and refuses a mismatch — see
`_node_named_at`. Without this, an admitted Arm's owner could claim a different, already-admitted
node_name and be handed that node's key.

**Unprivileged, with one narrow exception.** Runs as `User=munge` — able to read 0400 munge:munge
/etc/munge/munge.key without loosening the file or running a network listener as root. It cannot write
/etc/slurm or run scontrol; `POST /enroll` only updates a munge-owned registry row an operator already
created. Its one sudo grant (`headscale nodes list`, nothing else) takes no argument from the request,
so no request body has a path to root even through it.

Accepted consequence: an Arm, once admitted, holds the munge key on disk forever, and munge is
cluster-wide and authenticates as any node or user. Only the *join token* is short-lived; per-Arm
revocable credentials are still open. `cuttle munge rotate` + `cuttle arm sync-key` are the coarser,
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
from pydantic import BaseModel

# stdlib logging -> stdout -> journald, same shape as the Head API and the Karma tick. Targeted event
# logs (who was handed the munge key, who was refused) rather than per-request middleware.
log = logging.getLogger("ccc.enroll")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(os.environ.get("CCC_LOG_LEVEL", "INFO").upper())
    log.propagate = False

MUNGE_KEY = Path("/etc/munge/munge.key")
REGISTRY = Path(os.environ.get("CCC_ARMS_REGISTRY", "/var/lib/ccc/arms.json"))
# The base + benchmark images' digest refs (`cuttle setup --head` built and pushed them), and each Arm's
# own registry pull password (minted at `cuttle arm authorize`, 0600 munge). Mirror cuttle.config's
# IMAGES_RECORD and REGISTRY_CREDS.
IMAGES_RECORD = Path(os.environ.get("CCC_IMAGES_RECORD", "/opt/ccc/images.json"))
REGISTRY_CREDS = Path(os.environ.get("CCC_REGISTRY_CREDS", "/var/lib/ccc/registry-creds.json"))
# This cluster's CA (public half), handed to an Arm as part of enrolling. Mirrors
# cuttle.config.HEAD_CA_CERT and ccc_head_api.main.CA_CERT.
CA_CERT = Path(os.environ.get("CCC_CA_CERT", "/opt/ccc/tls/ccc-ca.crt"))
SLURMD_PORT = 6818  # mirrors cuttle.config.SLURMD_PORT
# The catalog of Arm Features. Built into CCC, not curated and not state — CCC ships the runtime for
# each one, so a name outside it could only ever be a Slurm token with nothing behind it. A donor's
# declared features are validated against it below.
#
# Mirrors cuttle.config.FEATURE_NAMES and ccc_head_api.slurm.FEATURE_NAMES (neither app can import the
# others). All three must agree.
FEATURE_NAMES = ("vlearn", "isaaclab")

# Mirrors cuttle.config; re-validated root-side by `cuttle arm admit` before reaching gres.conf.
# LOWERCASE ONLY, and the two ends must agree on that or this check is theatre: Headscale lowercases
# the `given_name` it derives from a node's hostname, and `_node_named_at` below compares that against
# the name the donor claims. An uppercase name therefore cannot pass `/enroll` however it got here, so
# it is refused with an explanation rather than left to fail as an unexplained 403 two checks later.
NODE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
GPU_DEVICE_RE = re.compile(r"^/dev/nvidia[0-9]+$")
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
    # written — one /dev/nvidiaN per card.
    gpu_devices: list[str]
    mesh_ip: str
    # The memory of one card. One number for the whole box, because `setup --arm` refuses to enroll a
    # machine whose GPUs are not all the same — a Slurm Feature is per-node, so a mixed box could not
    # advertise both sizes honestly (cuttle.system.gpu_profile).
    vram_mb: int
    # Arm Features this box declares, each validated against FEATURE_NAMES below. Optional and defaults
    # empty — most Arms have none, and older CLIs never sent the field at all.
    features: list[FeatureDecl] = []
    # What Arm-side files this box has WRITTEN. Configless Slurm serves slurm.conf but not the scripts
    # it names, so a `Prolog=` naming a file an Arm doesn't have fails that Arm's jobs and DRAINS it —
    # and a drained node stays drained across a slurmd restart, so fixing the file after doesn't heal
    # it. The Head refuses to render `Prolog=` until every admitted Arm has reported it here. Defaults
    # empty: an older CLI hasn't written the prolog, and saying so is exactly right.
    provides: list[str] = []


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


def _node_named_at(peer: str) -> str | None:
    """Whichever node Headscale has registered at mesh address `peer`, by its Headscale name — or None
    if no node holds it. Lets `/enroll` check the claimed name against a source neither a spoofed field
    nor a spoofed source IP can move (a forged source gets no TCP handshake, same reasoning as `probe`).

    Shells to `sudo -n headscale nodes list` — the ONE privilege this service has; `munge` cannot reach
    Headscale's own unix socket (0750 headscale:headscale) any other way. `-n`: this runs inside a
    request handler with no tty to prompt, so a broken sudoers rule must fail fast, not hang the request.
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


def _images() -> dict:
    """What every Arm pulls. Absent means `setup --head` never finished building them — a loud 503,
    since an Arm without the base image can run nothing."""
    if not IMAGES_RECORD.exists():
        raise HTTPException(503, "this Head has not built its images — its operator must re-run "
                                 "`cuttle setup --head`")
    return json.loads(IMAGES_RECORD.read_text())


def _pull_password(node_name: str) -> str:
    creds = json.loads(REGISTRY_CREDS.read_text()) if REGISTRY_CREDS.exists() else {}
    if node_name not in creds:
        raise HTTPException(503, f"no registry credential for {node_name!r} — the Head's operator "
                                 f"must re-run `cuttle arm authorize`")
    return creds[node_name]


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
    Head.

    **An Arm must have been authorized by name.** `cuttle arm authorize` pre-declares the row before the
    enrollment token is ever handed out, so a machine on the mesh that no Admin ever named is refused
    here — the munge key is what it's being refused. Mesh membership alone is not enough to obtain the
    cluster's shared secret.

    **The claimed name must match who Headscale says is actually calling.** `request.client.host` is the
    one thing in this request a caller cannot forge (a forged source gets no TCP handshake — same
    reasoning `probe` relies on), so it — not the request body's `req.mesh_ip` — is what gets checked
    against Headscale. Without this, any admitted Arm's owner could POST a different, already-admitted
    node_name and be handed that node's munge key.

    An UPDATE, never a create. The pre-declared fields (owner, above all) must survive the donor's POST:
    this endpoint is the one thing a donor's machine gets to write, and it does not get to say who owns
    it.

    The response also carries the image digest refs and this Arm's own registry pull credential, and
    this cluster's CA, so the Arm trusts the Head (and its registry) without a fingerprint ceremony it
    has already earned the right to skip.
    """
    if not NODE_NAME_RE.match(req.node_name):
        raise HTTPException(422, f"invalid Arm name {req.node_name!r} — must be 1-32 characters, "
                                 f"LOWERCASE letters/digits/hyphens, starting with a letter or digit. "
                                 f"Headscale lowercases every node name, so an uppercase one could "
                                 f"never match on the mesh")
    if not req.gpu_devices:
        raise HTTPException(422, "an Arm must report at least one GPU device")
    for device in req.gpu_devices:
        if not GPU_DEVICE_RE.match(device):
            raise HTTPException(422, f"unsupported GPU device {device!r} — expected /dev/nvidiaN")
    # Hard-validate every declared Arm Feature against the catalog — no free typing. An unknown name
    # means the donor's CCC is NEWER than this Head's (both ends carry the same constant), so the
    # remedy names that, not an operator command.
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

    # Read before the row is touched: a Head with no images cannot enroll anyone, so it fails here
    # rather than after the munge key has left.
    images = _images()

    reg = _load()
    if req.node_name not in reg:
        # A machine on the mesh reaching for the munge key under a name no Admin ever declared. Worth a
        # WARNING: this firing at all is the signal to watch for.
        log.warning("enroll REFUSED undeclared node=%r from %s", req.node_name, request.client.host)
        raise HTTPException(403, f"{req.node_name!r} was not authorized to join this cluster — "
                                 f"ask the Head's operator to run `cuttle arm authorize`")

    peer = request.client.host
    actual_name = _node_named_at(peer)
    if actual_name != req.node_name:
        # Declared (it's in the registry) but not WHO IT SAYS IT IS. Worth a WARNING for the same reason
        # as the undeclared case: this firing at all is the signal, whether it's a stale Headscale
        # re-registration racing this request or someone actually trying it.
        log.warning("enroll REFUSED node=%r claimed by %s, which Headscale calls %r",
                   req.node_name, peer, actual_name)
        raise HTTPException(403, f"{peer} is not registered to {req.node_name!r} on the mesh")

    entry = reg[req.node_name]
    entry["gpu_devices"] = req.gpu_devices
    # The VERIFIED address, never the request body's: `req.mesh_ip` stays on the wire only because
    # dropping it would break EnrollRequest, but nothing below this line reads it.
    entry["mesh_ip"] = peer
    # VRAM is *declared* here; Power is *measured* later by a benchmark. A donor could inflate either,
    # but only VRAM can be taken on the donor's word without also being wrong.
    entry["vram_mb"] = req.vram_mb
    # An UPDATE, not a setdefault: a re-enroll resends whatever this Arm currently has, so a genuine
    # removal (owner un-picks a feature, re-runs setup) actually takes.
    entry["features"] = [f.model_dump() for f in req.features]
    # An UPDATE for the same reason, and load-bearing: this is what the Head gates `Prolog=` on. A stale
    # "yes, I have the prolog" surviving a downgrade is precisely the claim that would drain a node.
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
        "images": images,  # {base, bench}: digest refs on the Head registry
        "registry": {"user": f"arm-{req.node_name}", "password": _pull_password(req.node_name)},
        # The cluster's CA, so the Arm's owner never sees a fingerprint prompt: this response already
        # crossed a channel authenticated by the join token and checked against Headscale's own view of
        # the caller, which is a far stronger thing than the trust-on-first-use ceremony a client with
        # nothing but an address has to fall back on.
        "ca_pem": CA_CERT.read_text() if CA_CERT.exists() else None,
    }
