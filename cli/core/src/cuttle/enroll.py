"""Arm enrollment (Phase 1 ③; reversed in Phase 3a slice 5).

**The Admin decides first, and the donor redeems.** One admin command mints an **enrollment token**
(glossary) naming the Arm and its owning **User**; the donor redeems it and declares nothing. This
replaces the old model, in which the donor asked to join and the operator approved out-of-band by
relaying an auth-id back and forth.

Two gates guard the cluster, and both now fall out of that *single* admin decision:

1. **Mesh admission.** The token carries a one-time, 1-hour Headscale preauth key. `tailscale up`
   redeems it and joins unattended — there is nothing left to approve, because minting the key *was*
   the approval.
2. **Slurm admission.** `cuttle arm authorize` pre-declares the Arm's name in the registry before the
   token is ever handed out, so the mesh-only Enroll API can refuse a name no Admin ever named. Being
   on the mesh is no longer sufficient to fetch the munge key. The Arm then `POST`s /enroll to report
   its GPU device, and `authorize` — still blocked, waiting — renders gres.conf and reconfigures Slurm.
   Nothing from the network reaches root unvalidated.

Unlike the invite it replaces, **the token is a secret**: it admits one machine to the mesh. It
crosses the LAN to reach the donor, which is acceptable only because CCC is LAN-scoped (item #8 is
deferred) and its donors are never strangers.

Intended path only — no fallbacks (test box; failures decided live).
"""

import base64
import json
import re
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import auth, config, headapi, mesh, settings, system, templates, ui
from .system import run


# --- enrollment token (a secret: it admits one machine) ---------------------

def warn_if_coordinator_is_lan_only() -> None:
    """Say so when the coordinator only answers on this LAN, before a token is minted against it.

    Second line of defence: setup already warns when this address is CHOSEN (setup.mesh_up_head),
    which is the moment the operator can still change it cheaply. This one catches a Head set up
    before that check existed, and the case where the fleet grew a remote donor afterwards.

    Every token carries this address, and it is the first thing the donor's box dials — before the
    mesh exists, so there is no other route to fall back on. A remote donor handed a LAN address does
    not fail fast: the packets look routable, leave, and die, so `setup --arm` hangs until it times
    out and the donor reports that enrollment is broken.

    A warning, not a refusal: a donor *on* this LAN is the ordinary case and this address is right for
    them. Only the admin knows where the box is.
    """
    coordinator = settings.load().get("mesh", {}).get("coordinator", "")
    host = urllib.parse.urlparse(coordinator).hostname or ""
    if not mesh.lan_only(host):
        return
    ui.warn(f"the coordinator is {coordinator} — an address only this LAN can reach.")
    ui.hint("The donor's box dials this before it is on the mesh, so a box off this network will "
            "hang until it times out. Fine if they are on this LAN. If they are not, re-point the "
            "Head at an address they can reach (e.g. its Tailscale IP) with "
            "`cuttle setup --head --address=<addr>`, then authorize again.")


def _mint_token(node_name: str, authkey: str) -> str:
    """Everything the donor's `setup --arm` needs, and nothing it can choose for itself.

    The coordinator is reachable BEFORE the mesh exists; the Head's mesh IP only AFTER. Both are read
    live rather than from a persisted copy — the Head always knows its own current address, and a
    stale one would hand out tokens pointing at a Head that has moved.
    """
    payload = {
        "coordinator": settings.load()["mesh"]["coordinator"],
        "mesh_ip": mesh.tailscale_ip(),
        "node_name": node_name,   # the Admin names the Arm; the donor is not asked
        "authkey": authkey,       # the secret: one machine, one hour, one use
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def decode_token(token: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(token.encode()))


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
                       "mesh_ip": my_mesh_ip, "vram_mb": system.gpu_vram_mb()}).encode()
    req = urllib.request.Request(f"{headapi.enroll_url(head_mesh_ip)}/enroll", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def wait_for_admit(head_mesh_ip: str, node_name: str, timeout_s: int = 900) -> None:
    """Block until an operator runs `cuttle arm admit` on the Head. Until then the Head has no
    gres.conf line for us, so starting slurmd would register a node with no GPU."""
    # ui.say, not print: this is the one instruction the donor MUST act on, and stdout is held back
    # until the whole flow ends (see ui.fullscreen) — printed, it would surface a quarter of an hour
    # late, next to the timeout it caused.
    ui.say(f"  [muted]on the Head, run:  cuttle arm admit {node_name}[/muted]")
    deadline = time.time() + timeout_s
    # The abort lives INSIDE the step, or timing out would tick the row ✓ on its way past.
    with ui.step("Waiting to be admitted"):
        while time.time() < deadline:
            with urllib.request.urlopen(
                    f"{headapi.enroll_url(head_mesh_ip)}/arms/{node_name}", timeout=10) as r:
                if json.load(r)["admitted"]:
                    return
            time.sleep(5)
        raise ui.abort(f"not admitted within {timeout_s}s",
                       next_step=f"run `cuttle arm admit {node_name}` on the Head to finish enrolling")


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
        ui.say(f"  [muted]Head reaches this Arm at {res['peer']}:{res['port']}[/muted]")
        return
    raise ui.abort(
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

def slurmd_dropin(node_name: str, ctld_addr: str, vram_mb: int) -> str:
    """This Arm's slurmd flags. `-Z` = register as a dynamic node; `--conf-server` = fetch slurm.conf
    from slurmctld instead of having one installed. Gres must be declared here: slurm-wlm ships no
    NVML, so the node cannot introspect its own GPUs (Phase 1 = one GPU per Arm).

    `Feature=vramN` rides along, and `--conf` is the only durable place for it: a dynamic node's config
    is re-sent from `--conf` at *every* registration, so a feature set any other way (`scontrol
    update`) would silently vanish the next time this slurmd restarted — and a Spawn's `--vram`
    constraint would then match nothing. Weight (∝ 1/Power) is deliberately NOT here, and can't be:
    Power is only measurable once the node is live. Losing Weight costs nothing worse than the
    fastest-free-card preference; losing Feature would make the node unschedulable.

    A drop-in rather than `/etc/default/slurmd` (see config.SLURMD_DROPIN): systemd does not honour
    quotes *inside* an expanded variable, so the distro's `$SLURMD_OPTIONS` hook cannot express a
    `--conf` with a space in it. Quotes written literally in a unit file are honoured.
    """
    conf = f"Gres=gpu:1 Feature={config.vram_feature(vram_mb)}"
    return (
        "# Rendered by `cuttle setup --arm`. Do not hand-edit.\n"
        "[Service]\n"
        "ExecStart=\n"  # clear the distro's ExecStart; a bare assignment is systemd's reset syntax
        f'ExecStart=/usr/sbin/slurmd --systemd -Z -N {node_name} --conf "{conf}" '
        f"--conf-server {ctld_addr}\n"
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
    unprivileged and its registry is only as trustworthy as the mesh.

    The Head's own co-located Arm has a registry row too (so it can carry an owner, like any other
    Arm), but it is `head: true` and templates.head_gres_node() already supplies its line — skip it
    or gres.conf names it twice.
    """
    nodes = [templates.head_gres_node()]
    for name, e in sorted(reg.items()):
        if not e["admitted"] or e.get("head"):
            continue
        assert re.match(config.NODE_NAME_RE, name), f"bad node name in registry: {name!r}"
        assert re.match(config.GPU_DEVICE_RE, e["gpu_device"]), f"bad device: {e['gpu_device']!r}"
        nodes.append((name, e["gpu_device"]))
    write_root_file(config.GRES_CONF, templates.render_gres_conf(nodes))
    run(["scontrol", "reconfigure"], sudo=True)
    reapply_weights(reg)


def reapply_weights(reg: dict) -> None:
    """Re-push every benchmarked Arm's Weight after a reconfigure — which resets each node to what the
    config files say, and donor Arms are dynamic, so no file says anything about them.

    Verified 2026-07-14: `scontrol update NodeName=X Weight=276` lands, and the very next `scontrol
    reconfigure` puts it back to 1. Since a reconfigure happens on every authorize/admit/remove, a
    Weight pushed once does not survive the next Arm joining. The Head's own Weight is in slurm.conf
    and needs none of this; it is re-pushed anyway, because one loop is simpler than an exception.

    ⚠ Still lost when a donor's slurmd restarts (their box reboots): a dynamic node re-registers from
    its `--conf`, which cannot carry a Power that had not been measured when it was written. Costs
    card-selection order only — never money, since the Escrow quotes max Power over eligible Arms and
    the meter settles on what actually ran. Closing it needs a privileged loop on the Head (Phase 2).
    """
    for name, e in sorted(reg.items()):
        if e["admitted"] and e.get("power"):
            apply_weight(name, e["power"])


def _await_enrollment(name: str, token: str, timeout_s: int = 300) -> dict:
    """Block until the Arm POSTs /enroll — the only thing that tells us the GPU device we must write
    into gres.conf.

    Waits for `mesh_ip`, not for the row: `authorize` pre-declared the row itself moments ago (that
    pre-declaration is what lets the Enroll API refuse un-named machines), so its mere existence
    proves nothing. `mesh_ip` and `gpu_device` are null until the donor's POST fills them in.

    `token` is here only to keep the copy hotkey live: this wait is precisely the window in which the
    operator still has to *send* the token, so it is where `c` has to work.
    """
    # A step, not a print: this blocks for up to five minutes waiting on another human, and stdout is
    # held back until the flow ends — so a `print` here is a silent screen for the entire wait, which
    # is indistinguishable from a hang. The spinner says it is still waiting, and on whom.
    with ui.step(f"Waiting for {name} to enroll (the donor runs `cuttle setup --arm --token …`)"), \
            ui.copy_hotkey(token) as wait:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            reg = load_registry()
            if reg.get(name, {}).get("mesh_ip"):
                return reg
            wait(2)
        raise ui.abort(
            f"{name} has not enrolled yet. Nothing is broken and the pre-declaration stands",
            next_step=f"when the donor has run `cuttle setup --arm`, finish with "
                      f"`cuttle arm admit {name}`")


# --- the Power benchmark (Head only, operator + sudo) -----------------------
# Price = Power x Demand. This measures Power: what a device can *do*, once, at enroll.
#
# It lives on the Head CLI rather than in the Head API because its two ends both need privilege the
# API deliberately doesn't have — it must submit as the **Mantle** (`sudo -u ccc`), or slurmd on a
# remote Arm would setuid() to an operator uid that doesn't exist there, and it must write the node's
# Weight (`scontrol update`, operator-only). The Head API is unprivileged by design and stays that way.


def _wait_for_node(name: str, timeout_s: int = 300) -> None:
    """Block until the node is registered and idle. `arm authorize` reaches here the moment it has
    written gres.conf — but the donor's `setup --arm` is still polling for that admission and hasn't
    started its slurmd yet, so for a few seconds the node Slurm is about to benchmark does not exist."""
    with ui.step(f"Waiting for {name} to come up in Slurm"):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            r = run(["sinfo", "-h", "-n", name, "-o", "%T"],
                    capture_output=True, text=True, check=False)
            if r.stdout.strip().rstrip("*") in ("idle", "mixed", "allocated"):
                return
            time.sleep(3)
        raise ui.abort(f"{name} never came up in Slurm",
                       next_step=f"benchmark it later with `cuttle arm benchmark {name}`")


def benchmark_arm(name: str) -> dict:
    """Submit the Power benchmark to one Arm and record what it measures.

    The result comes home the way every remote Spawn's does — rsync'd into the Head's [spawns] module —
    so there is one code path for a donor's box and for the Head's own co-located Arm (which simply
    rsyncs to itself over the mesh). The Head pre-creates the landing directory because rsync will not
    invent one.

    Its own box when run as `cuttle arm benchmark` — it waits on Slurm and then on a Spawn, minutes
    with nothing to show otherwise. Reached from `_admit` a box is already open, and a nested
    `section` is transparent (no second frame, and the outer one releases the results).
    """
    with ui.section(f"Benchmarking {name}", lambda: "benchmarked"):
        system.prime_sudo()
        return _benchmark_arm(name)


def _benchmark_arm(name: str) -> dict:
    _wait_for_node(name)
    head_ip = mesh.tailscale_ip()
    dest = config.HEAD_SPAWNS_DIR / config.BENCH_DIR_NAME / name
    run(["install", "-d", "-o", config.HEAD_API_USER, "-g", config.HEAD_API_USER, str(dest)],
        sudo=True)
    run(["rm", "-f", str(dest / "power")], sudo=True)  # a re-benchmark must not read the old number

    with tempfile.NamedTemporaryFile("w", suffix=".sbatch", delete=False) as f:
        f.write(templates.render_bench_sbatch(name, head_ip))
        job_file = f.name
    Path(job_file).chmod(0o644)  # sbatch reads it as ccc, not as the operator

    with ui.step(f"Benchmarking {name} (matmul in the Pod — this is what prices a second on it)"):
        run(["-u", config.HEAD_API_USER, "sbatch", "--parsable", job_file], sudo=True, check=True)
        deadline = time.time() + config.BENCH_TIMEOUT_S
        while time.time() < deadline:
            r = run(["cat", str(dest / "power")], sudo=True,
                    capture_output=True, text=True, check=False)
            if r.returncode == 0 and r.stdout.strip():
                result = json.loads(r.stdout)
                break
            time.sleep(3)
        else:
            raise ui.abort(f"{name}'s benchmark produced no result within {config.BENCH_TIMEOUT_S}s",
                           next_step=f"retry with `cuttle arm benchmark {name}`")

    reg = load_registry()
    reg[name]["power"] = result["power"]
    reg[name]["gpu"] = result["gpu"]
    save_registry(reg)
    apply_weight(name, result["power"])
    ui.result("gpu", f"{result['gpu']} — {result['vram_mb']} MiB VRAM")
    ui.result("power", f"{result['power']} TFLOP/s")
    return result


def apply_weight(name: str, power: float) -> None:
    """Weight ∝ 1/Power, so Slurm hands out the fastest *free* eligible card first.

    Not a reservation: Slurm won't hold a Spawn back waiting for a better GPU to free up — it takes the
    best one available now, which is what "the first one that fits, else queue" means.
    """
    run(["scontrol", "update", f"NodeName={name}", f"Weight={config.node_weight(power)}"], sudo=True)


# --- authorize / admit / chown (Head only, operator + sudo) -----------------
# Being the operator on the Head IS the authorization — no session, no login (mirrors `user add`).

def authorize_arm(name: str, owner: str, write_root_file) -> None:
    """Authorize one machine to become an Arm. The cluster's front door, and the only privileged step.

    Mints the **enrollment token** the donor redeems, then blocks until they do, then admits them to
    Slurm. One admin command; one donor command; no auth-id relayed by hand.

    Order is deliberate:
    1. *Verify the owner exists* before anything else — a typo here would otherwise strand the Arm's
       Karma income (3b) on a User that never existed.
    2. *Refuse if a mesh node already answers to this name.* Re-authorizing would have to evict it,
       and evicting a working donor's box must never be the side-effect of a command you re-ran.
       Removal is its own explicit act. (Checked against the MESH, not the registry: a donor whose
       1-hour key expired before they ever joined leaves a pre-declared row and no node, and re-issuing
       them a token must just work.)
    3. *Pre-declare the row*, before the token exists. This is what the mesh-only Enroll API checks, so
       it must be in place before any machine can possibly reach /enroll.
    4. *Mint the key* (1h, single-use — mesh.mint_preauthkey's defaults) and print the token.
    """
    # A box of its own. Everything below either blocks on another human or shells out with sudo, and
    # `ui.result` is only released when a section closes — outside one the token would be collected
    # and then dropped on the floor.
    with ui.section(f"Authorizing {name}", lambda: "authorized"):
        system.prime_sudo()   # sudo as the box's first row, before any step rows grow the frame
        _authorize_arm(name, owner, write_root_file)


def _authorize_arm(name: str, owner: str, write_root_file) -> None:
    if not auth.user_exists(owner):
        raise ui.abort(f"no such user {owner!r}",
                       next_step=f"create them first with `cuttle user add {owner}`")
    if mesh.node_ids_named(name):
        raise ui.abort(
            f"an Arm named {name!r} is already on the mesh — authorizing it again would evict\n"
            f"  that machine. If you mean to replace it, remove it first:\n"
            f"      cuttle arm remove {name}\n"
            f"  To only change its owner, leave it running and use:\n"
            f"      cuttle arm chown {name} --user <owner>")

    # Before the key is minted, not after: a preauth key is single-use and expires in an hour, so an
    # admin who reads this and re-points the Head should not have burned one first.
    warn_if_coordinator_is_lan_only()

    reg = load_registry()
    # vram_mb arrives with the donor's /enroll; power only after the benchmark, which can't run until
    # the node is live. Both are named here so a row's shape never depends on how far it got.
    reg[name] = {"owner": owner, "gpu_device": None, "mesh_ip": None, "admitted": False,
                 "vram_mb": None, "power": None, "gpu": None}
    save_registry(reg)

    token = _mint_token(name, mesh.mint_preauthkey())
    # The token has to be readable RIGHT NOW: the next line blocks for up to five minutes waiting for
    # the donor to redeem it, and they cannot redeem what they have not been sent. `ui.say` draws it
    # in the box immediately; `ui.data` puts it on stdout so `t=$(cuttle arm authorize …)` still
    # works (held by the alternate screen and replayed when the terminal comes back); `ui.result`
    # carries it onto the real terminal after the screen is discarded.
    ui.say(f"[ok]✓[/ok] authorized [bold]{name}[/bold] (owner: {owner})")
    ui.say("")
    ui.say("  [warn]SECRET[/warn] — send this privately. It admits one machine, once, "
           "within the hour:")
    ui.say(f"  [bold]{token}[/bold]")
    ui.say("  the donor runs:  [accent]cuttle setup --arm --token <token>[/accent]")
    ui.say("")
    ui.data(token)
    # Registered BEFORE the wait, deliberately: a wait that times out is exactly when the operator
    # still needs the token — it stays valid for the hour, and `cuttle arm admit` finishes the job.
    ui.result("arm", f"{name} (owner: {owner})")
    ui.result("token", token)
    # Straight into the wait — no pause to acknowledge. The wait IS the window in which the operator
    # sends the token, and `c` copies it throughout (ui.copy_hotkey), so stopping the flow first to
    # offer that would only have interrupted it.
    reg = _await_enrollment(name, token)
    _admit(name, reg, write_root_file)


def admit_arm(name: str, write_root_file) -> None:
    """Give an enrolled Arm its GRES line and reconfigure Slurm.

    The second half of `authorize`, on its own — for when authorize's wait timed out because the donor
    wasn't at their desk. The token they hold is still good (for its hour), and the pre-declared row is
    still there; this just finishes the job once they've run setup.
    """
    reg = load_registry()
    if name not in reg:
        raise ui.abort(f"{name!r} was never authorized",
                       next_step=f"run `cuttle arm authorize {name}` first")
    if not reg[name].get("mesh_ip"):
        raise ui.abort(f"{name!r} is authorized but has not enrolled yet — it appears once "
                       f"the donor has run `cuttle setup --arm --token ...`")
    # Its own box, for the same reason `authorize` has one: it benchmarks, which blocks for minutes
    # and reports its findings as results.
    with ui.section(f"Admitting {name}", lambda: "admitted"):
        system.prime_sudo()
        ui.result("arm", f"{name} (owner: {reg[name].get('owner')})")
        _admit(name, reg, write_root_file)


def _admit(name: str, reg: dict, write_root_file) -> None:
    with ui.step(f"Admitting {name} to Slurm ({reg[name]['gpu_device']})"):
        reg[name]["admitted"] = True
        save_registry(reg)
        render_gres(reg, write_root_file)
    # An Arm with no Power cannot be priced, so it is benchmarked as part of joining — not as an
    # optional follow-up an operator might forget.
    benchmark_arm(name)


def chown_arm(name: str, owner: str) -> None:
    """Re-point an Arm at a different owning User. Touches neither the mesh nor Slurm — so fixing a
    typo'd owner never costs the donor a re-enrollment."""
    if not auth.user_exists(owner):
        raise ui.abort(f"no such user {owner!r}",
                       next_step=f"create them first with `cuttle user add {owner}`")
    reg = load_registry()
    if name not in reg:
        raise ui.abort(f"no Arm named {name!r}",
                       next_step="run `cuttle arm list` to see the Arms this Head knows")
    was = reg[name].get("owner")
    reg[name]["owner"] = owner
    save_registry(reg)
    print(f"cuttle: {name} owner {was} -> {owner}")


def register_head_arm(mesh_ip: str) -> None:
    """Give the Head's own co-located Arm a registry row, owned by the first admin.

    It never enrolls (it is a *static* Slurm node, and there is no donor), so without this it would be
    the one Arm in the cluster with no owner — and Karma (3b) exempts admin-owned Arms, which it can
    only do if it knows this one is admin-owned. `head: true` keeps render_gres from emitting its GRES
    line twice (templates.head_gres_node() already has it).
    """
    reg = load_registry()
    name = system.hostname()
    reg[name] = {"owner": config.FIRST_ADMIN_USER, "gpu_device": config.GPU_DEVICE,
                 "mesh_ip": mesh_ip, "admitted": True, "head": True,
                 "vram_mb": system.gpu_vram_mb(), "power": None, "gpu": None}
    save_registry(reg)


def _arm_state(name: str, e: dict) -> str:
    if e["admitted"]:
        return "head" if e.get("head") else "admitted"
    if e.get("mesh_ip"):
        return f"enrolled, NOT ADMITTED (cuttle arm admit {name})"
    return "authorized, awaiting the donor"


def list_arms(as_json: bool = False) -> None:
    reg = load_registry()
    payload = [{"name": n, **e, "state": _arm_state(n, e)} for n, e in sorted(reg.items())]
    if as_json:
        ui.emit(payload, True, lambda: None)
        return
    if not reg:
        ui.say("no Arms have enrolled")
        return
    ui.table(
        ["NAME", "OWNER", "MESH IP", "GPU", "VRAM", "POWER", "STATE"],
        [[a["name"], a.get("owner") or "-", a.get("mesh_ip") or "-", a.get("gpu") or "-",
          f"{round(a['vram_mb'] / 1024)}G" if a.get("vram_mb") else "-",
          f"{a['power']} TF" if a.get("power") else "-", a["state"]] for a in payload])


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


# --- Retract / Return (Phase 2 loose end) -----------------------------------
# Unlike everything else in this file, these are NOT Head-local: retracting/returning your own Arm is
# an owner action reachable over the network (any box, session-authed), exactly like `cuttle cancel`.
# `--any` is the operator's escape hatch — Head-only, and writes head.db directly through the API venv
# rather than the network route, mirroring `cuttle user reset` (see auth._admin).

def retract_arm(name: str, any_owner: bool = False) -> None:
    """Pause an Arm: it stops taking new Spawns and whatever is running on it now is ended (TERM now, a
    3-minute grace, then force-killed by the Karma tick). Settles at zero either way."""
    if any_owner:
        auth._admin(["retract", name])
    else:
        auth._api("POST", f"/arms/{name}/retract")
    print(f"cuttle: {name} retracted — running work has 3 minutes to end gracefully")


def return_arm(name: str, any_owner: bool = False) -> None:
    """Reopen a retracted Arm for new Spawns. Does not save a Spawn the Retract already caught."""
    if any_owner:
        auth._admin(["return", name])
    else:
        auth._api("POST", f"/arms/{name}/return")
    print(f"cuttle: {name} returned — accepting new Spawns again")
