"""Arm enrollment (Phase 1 ③).

Two gates, both operator decisions, no bearer secrets anywhere:

1. **Mesh admission.** The Arm's `tailscale up` presents no key and blocks, printing an auth-id. The
   operator runs `cuttle arm approve <name> --auth-id <id>`. The auth-id is NOT a secret — it names
   a pending request from one specific machine, and only the Head can grant it.
2. **Slurm admission.** Once meshed, the Arm `POST`s /enroll, recording its GPU device in a registry
   the (unprivileged) API owns. The same `approve` command then renders gres.conf and reconfigures
   Slurm. Nothing from the network reaches root unvalidated.

The **invite** carries only public facts — the coordinator URL and the Head's mesh IP — so it can be
posted anywhere. The munge key never leaves the mesh: it is served by /enroll, which is unroutable
until gate 1 has been passed.

Intended path only — no fallbacks (test box; failures decided live).
"""

import base64
import json
import re
import tempfile
import time
import urllib.request
from pathlib import Path

from . import config, headapi, mesh, settings, templates
from .system import run


# --- invite (public, not a credential) --------------------------------------

def mint_invite() -> str:
    """Head-side. Contains no secret — publish it anywhere. Holding it lets a machine *ask* to join;
    it does not let it join. Admission is `cuttle arm approve`.

    The mesh IP is read live rather than from config.yaml: the Head always knows its own current
    address, and a persisted one would hand out invites pointing at a stale Head if it ever moved.
    """
    payload = {
        "coordinator": settings.load()["mesh"]["coordinator"],  # reachable BEFORE the mesh exists
        "mesh_ip": mesh.tailscale_ip(),                          # reachable only AFTER
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def decode_invite(invite: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(invite.encode()))


# --- /enroll ----------------------------------------------------------------

def gpu_device() -> str:
    """This box's GPU device node. WSL2 has no /dev/nvidiaN — CUDA reaches the GPU through /dev/dxg
    with the driver shimmed in from /usr/lib/wsl/lib."""
    if Path("/dev/dxg").exists():
        return "/dev/dxg"
    return config.GPU_DEVICE


def fetch_enrollment(head_mesh_ip: str, node_name: str, my_mesh_ip: str) -> dict:
    """Record this Arm with the Head and pull the munge key + ctld address. Only routable from
    inside the mesh — which is exactly why the munge key is safe to send here and nowhere else."""
    body = json.dumps({"node_name": node_name, "gpu_device": gpu_device(),
                       "mesh_ip": my_mesh_ip}).encode()
    req = urllib.request.Request(f"{headapi.enroll_url(head_mesh_ip)}/enroll", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def wait_for_admit(head_mesh_ip: str, node_name: str, timeout_s: int = 900) -> None:
    """Block until an operator runs `cuttle arm admit` on the Head. Until then the Head has no
    gres.conf line for us, so starting slurmd would register a node with no GPU."""
    print(f"\ncuttle: waiting to be admitted. On the Head, run:  cuttle arm admit {node_name}")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with urllib.request.urlopen(f"{headapi.enroll_url(head_mesh_ip)}/arms/{node_name}", timeout=10) as r:
            if json.load(r)["admitted"]:
                print("cuttle: admitted.")
                return
        time.sleep(5)
    raise TimeoutError(f"not admitted within {timeout_s}s — run `cuttle arm admit {node_name}`")


def check_head_can_reach_us(head_mesh_ip: str) -> None:
    """Ask the Head to connect back to our slurmd. Run *after* slurmd starts, or nothing listens.

    `fetch_enrollment` proved Arm->Head. This proves Head->Arm, the direction slurmctld needs and the
    only one nothing else exercises: a node that registers but can't be reached simply drops to DOWN
    a few minutes later, with no hint that a firewall did it.

    Deliberately reports the observation, not a diagnosis — a firewall is only one of the causes.
    """
    with urllib.request.urlopen(f"{headapi.enroll_url(head_mesh_ip)}/probe", timeout=20) as r:
        res = json.load(r)
    if res["reachable"]:
        print(f"cuttle: Head can reach this Arm at {res['peer']}:{res['port']} — good")
        return
    raise RuntimeError(
        f"the Head cannot reach this Arm at {res['peer']}:{res['port']} ({res.get('error')}).\n"
        f"  slurmd registered, but slurmctld will mark this node DOWN within minutes.\n"
        f"  The mesh itself is fine — this Arm just reached the Head API to run the check.\n"
        f"  Common causes:\n"
        f"    - a firewall other than ufw (firewalld, nftables, an endpoint agent)\n"
        f"    - a ufw deny rule ahead of ours (`sudo ufw status numbered`)\n"
        f"    - slurmd died after starting (`systemctl status slurmd`)"
    )


def write_munge_key(b64_key: str) -> None:
    """The cluster-wide shared secret. 0400 munge:munge, same as the Head's."""
    with tempfile.NamedTemporaryFile("wb", suffix=".ccc", delete=False) as f:
        f.write(base64.b64decode(b64_key))
        tmp = f.name
    run(["install", "-D", "-m", "0400", "-o", "munge", "-g", "munge",
         tmp, str(config.MUNGE_KEY)], sudo=True)
    run(["systemctl", "restart", "munge"], sudo=True)


# --- slurmd (dynamic + configless) ------------------------------------------

def slurmd_defaults(node_name: str, ctld_addr: str) -> str:
    """`-Z` = register as a dynamic node; `--conf-server` = fetch slurm.conf from slurmctld instead
    of having one installed. Gres must be declared here: slurm-wlm ships no NVML, so the node cannot
    introspect its own GPUs (Phase 1 = one GPU per Arm)."""
    return (
        "# Rendered by `cuttle setup --arm`. Do not hand-edit.\n"
        f'SLURMD_OPTIONS="-Z -N {node_name} --conf Gres=gpu:1 --conf-server {ctld_addr}"\n'
    )


# --- Arm registry + admission (Head only) -----------------------------------

def load_registry() -> dict:
    return json.loads(config.ARMS_REGISTRY.read_text()) if config.ARMS_REGISTRY.exists() else {}


def save_registry(reg: dict) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".ccc", delete=False) as f:
        f.write(json.dumps(reg, indent=2, sort_keys=True) + "\n")
        tmp = f.name
    run(["install", "-D", "-m", "0644", "-o", config.ENROLL_API_USER, "-g", config.ENROLL_API_USER,
         tmp, str(config.ARMS_REGISTRY)], sudo=True)


def render_gres(reg: dict, write_root_file) -> None:
    """gres.conf = the Head + every ADMITTED Arm. Re-validated here, root-side: the API is
    unprivileged and its registry is only as trustworthy as the mesh."""
    nodes = [templates.head_gres_node()]
    for name, e in sorted(reg.items()):
        if not e["admitted"]:
            continue
        assert re.match(config.NODE_NAME_RE, name), f"bad node name in registry: {name!r}"
        assert re.match(config.GPU_DEVICE_RE, e["gpu_device"]), f"bad device: {e['gpu_device']!r}"
        nodes.append((name, e["gpu_device"]))
    write_root_file(config.GRES_CONF, templates.render_gres_conf(nodes))
    run(["scontrol", "reconfigure"], sudo=True)


def _await_enrollment(name: str, timeout_s: int = 300) -> dict:
    """After mesh admission the Arm POSTs /enroll within seconds. Wait for it, because only that
    POST tells us the GPU device we must write into gres.conf."""
    print(f"cuttle: waiting for {name} to report its hardware...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        reg = load_registry()
        if name in reg:
            return reg
        time.sleep(2)
    raise TimeoutError(f"{name} never reached POST /enroll — is its slurmd host on the mesh?")


def approve_arm(name: str, auth_id: str, write_root_file) -> None:
    """The cluster's front door. One operator decision, two subsystems:

    1. grant the pending mesh-admission request (Headscale), unblocking the Arm's `tailscale up`;
    2. once the Arm reports in, give it a GRES line and reconfigure Slurm.

    Deliberately the *only* privileged step. The API cannot do either half.

    A re-enrolling Arm wipes its own state but cannot delete its Headscale node (no `headscale`
    binary), so approving would otherwise leave a superseded node holding a live mesh address — and
    mesh membership is what authorizes fetching the munge key. Approving a name means "this machine,
    now": anything older under that name is dead by definition.

    Order matters. We reap *after* registering, never before: a mistyped or already-consumed auth-id
    then fails harmlessly instead of evicting a healthy Arm. The cost is that Headscale, seeing the
    old node still holding `name`, calls the new one `name-1` — so we rename it back.
    """
    stale = set(mesh.node_ids_named(name))
    mesh.register_node(auth_id)  # fails here if the auth-id is bad — nothing deleted yet

    if stale:
        # Whatever Headscale called the new node (`name-1`, or another suffix), it is the one that
        # wasn't there a moment ago. Don't guess the suffix; diff against the snapshot.
        fresh = [n for n in mesh.list_nodes() if str(n["id"]) not in stale
                 and n.get("given_name", "").startswith(name)]
        for node_id in stale:
            print(f"cuttle: reaping superseded mesh node {node_id} ({name})")
            mesh.delete_node_id(node_id)
        for n in fresh:
            if n["given_name"] != name:
                mesh.rename_node(str(n["id"]), name)

    reg = _await_enrollment(name)
    reg[name]["admitted"] = True
    save_registry(reg)
    render_gres(reg, write_root_file)
    print(f"cuttle: {name} approved ({reg[name]['gpu_device']}) — its slurmd can now register")


def list_arms() -> None:
    reg = load_registry()
    if not reg:
        print("no Arms have enrolled")
        return
    for name, e in sorted(reg.items()):
        state = "admitted" if e["admitted"] else "PENDING (cuttle arm admit)"
        print(f"{name:20} {e['mesh_ip']:12} {e['gpu_device']:14} {state}")


# --- node removal (Head only) -----------------------------------------------

def remove_arm(name: str, write_root_file) -> None:
    """Reap an Arm from ALL THREE places. A half-reap leaves a departed donor on the mesh, and mesh
    membership is what authorizes `POST /enroll` — i.e. the munge key."""
    run(["scontrol", "delete", f"nodename={name}"], sudo=True, check=False)
    mesh.delete_node(name)
    reg = load_registry()
    if reg.pop(name, None) is not None:
        save_registry(reg)
        render_gres(reg, write_root_file)
    print(f"cuttle: {name} removed from Slurm, the registry, and evicted from the mesh")
