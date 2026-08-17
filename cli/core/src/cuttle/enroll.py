"""Arm enrollment (③).

**The Admin decides first, and the donor redeems.** One admin command mints an **enrollment token**
(glossary) naming the Arm and its owning **User**; the donor redeems it and declares nothing.

Two gates guard the cluster, and both now fall out of that *single* admin decision:

1. **Mesh admission.** The token carries a one-time, 1-hour Headscale preauth key. `tailscale up`
   redeems it and joins unattended — there is nothing left to approve, because minting the key *was*
   the approval.
2. **Slurm admission.** `cuttle arm authorize` pre-declares the Arm's name in the registry before the
   token is ever handed out, so the mesh-only Enroll API can refuse a name no Admin ever named. Being
   on the mesh is no longer sufficient to fetch the munge key. The Arm then `POST`s /enroll to report
   its GPU devices, and `authorize` — still blocked, waiting — renders gres.conf and reconfigures Slurm.
   Nothing from the network reaches root unvalidated.

**The token is a secret**: it admits one machine to the mesh. It crosses the LAN to reach the donor,
which is acceptable only because CCC is LAN-scoped (item #8 is deferred) and its donors are never
strangers.

Intended path only — no fallbacks (test box; failures decided live).
"""

import base64
import hashlib
import json
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import auth, config, headapi, mesh, settings, system, templates, ui
from . import features as features_mod   # `features` is a parameter name throughout this module
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


def enrollment_from_state() -> dict:
    """What a token would have said, recovered from a box that already redeemed one.

    A re-setup is routine — new Features to declare, a rebuilt Environment, a CCC upgrade — and none
    of it is re-enrollment. But the token is printed once and never logged (it is a secret), so
    needing it again meant going back to the Admin to mint a second one, and minting one *revokes
    nothing*: the old preauth key stays live for its hour. Asking for a new secret to do a job that
    needs no secret is the wrong shape.

    Everything is already here. The Head's mesh IP and this Arm's name are in settings, written at
    enrollment; the coordinator is in tailscaled's own state, because it has to be. The preauth key is
    the one thing that is NOT recoverable — and the one thing this path never uses, since `up_arm`
    skips the join outright for a box already on this coordinator's mesh, leaving even a spent key
    unspent.

    Refused rather than half-attempted when this box is not on the mesh: `up_arm` would then wipe the
    local identity before discovering it has no key to re-join with, which is strictly worse than
    where it started. That case is what `--re-enroll` and a fresh token are for.
    """
    arm = (settings.load().get("arm") or {})
    node_name, head_mesh_ip = arm.get("node_name"), arm.get("head_mesh_ip")
    if not node_name or not head_mesh_ip:
        raise ui.abort("this box has never enrolled as an Arm — there is nothing to reuse.\n"
                       "  Ask the cluster's admin for a token: cuttle arm authorize <name> --user <you>")
    coordinator = mesh.current_coordinator()
    if not coordinator or not mesh.joined_to(coordinator):
        raise ui.abort(
            f"{node_name} is not on the mesh any more, so its enrollment cannot be reused.\n"
            f"  Rejoining needs a fresh preauth key, which only a new token carries. Ask the "
            f"cluster's admin for one:\n"
            f"    cuttle arm authorize {node_name} --user <you>\n"
            f"  then: cuttle setup --arm --token <token> --re-enroll")
    return {"coordinator": coordinator, "mesh_ip": head_mesh_ip, "node_name": node_name,
            "authkey": ""}   # never redeemed on this path — see up_arm


# --- /enroll ----------------------------------------------------------------

def fetch_enrollment(head_mesh_ip: str, node_name: str, my_mesh_ip: str,
                     features: list[dict] | None = None,
                     provides: list[str] | None = None) -> dict:
    """Record this Arm with the Head and pull the munge key + ctld address. Only routable from
    inside the mesh — which is exactly why the munge key is safe to send here and nowhere else.

    Reports every **Device** this box has (system.gpu_devices), because the Head authors gres.conf
    for the whole fleet and a card it was never told about is a card no Spawn can ever be placed on.

    `features` = the Arm Features this box's owner picked at `setup --arm`, as
    `[{"name", "path"}, ...]` — the Head validates each name against its catalog and re-sends this on
    every call, so a re-enrollment (mesh IP moved, `arm sync-key`) never silently drops a declaration.

    `provides` = Arm-side files this box has ALREADY WRITTEN, which
    is why the first POST of a `setup --arm` sends none: at that point in the run the prolog is not on
    disk yet, and claiming otherwise is exactly the lie that drains a node. `report_capabilities`
    re-POSTs once it is.
    """
    # Name and path only. A Feature entry may also carry the local path of a **Licence** key
    # (cuttle.features), which is this box's business and nobody else's — it stays in
    # /etc/ccc/features.env, on the Arm, and the Head has no field for it and no use for it.
    reported = [{"name": f["name"], "path": f["path"]} for f in (features or [])]
    body = json.dumps({"node_name": node_name, "gpu_devices": system.gpu_devices(),
                       "mesh_ip": my_mesh_ip, "vram_mb": system.gpu_vram_mb(),
                       "features": reported, "provides": provides or []}).encode()
    req = urllib.request.Request(f"{headapi.enroll_url(head_mesh_ip)}/enroll", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    # The Head's refusals are WRITTEN TO BE READ — "'willPC' is not registered to … on the mesh",
    # "ask the Head's operator to run `cuttle arm authorize`" — and a bare urlopen throws every one of
    # them away, leaving the donor with urllib's "HTTP Error 403: Forbidden" and nothing to act on.
    # The body is a FastAPI error document, so `detail` is where the sentence lives; anything else that
    # comes back is shown raw rather than swallowed a second time.
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace").strip()
        try:
            detail = json.loads(raw).get("detail") or raw
        except json.JSONDecodeError:
            detail = raw
        raise ui.abort(f"the Head refused this Arm's enrollment ({e.code}): {detail}") from e


def fetch_apptainer_deb(head_mesh_ip: str, pin: dict) -> Path:
    """Pull the apptainer .deb the Head pinned for this cluster, and refuse anything else.

    Reproducibility here is fleet-internal by necessity: the Launchpad PPA keeps only its newest build,
    so a version cannot be resurrected once pruned — but the Head downloaded it once and hands every
    Arm those same bytes, so a node enrolled today and one enrolled next year agree. `pin` is the
    {version, sha256, filename} block /enroll returned.

    The sha256 is checked because it is nearly free, not because the mesh is doubted: this file becomes
    root-installed software, and a truncated download (the ordinary failure) would otherwise reach
    `apt-get install` as an unreadable archive.
    """
    dest = Path(tempfile.gettempdir()) / pin["filename"]
    urllib.request.urlretrieve(f"{headapi.enroll_url(head_mesh_ip)}/apptainer.deb", dest)
    got = hashlib.sha256(dest.read_bytes()).hexdigest()
    if got != pin["sha256"]:
        dest.unlink(missing_ok=True)
        raise ui.abort(
            f"the apptainer .deb from the Head does not match what the Head said it would be\n"
            f"  expected sha256 {pin['sha256']}\n"
            f"  got      sha256 {got}",
            next_step="re-run `cuttle setup --arm` — if it happens again, the Head's copy is damaged "
                      "and the operator should delete /var/lib/ccc/apptainer.json and re-run "
                      "`cuttle setup --head`")
    ui.say(f"  [muted]apptainer {pin['version']} from the Head — the build the rest of this cluster "
           f"runs[/muted]")
    return dest


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
    system.install_file(base64.b64decode(b64_key), config.MUNGE_KEY,
                        mode="0400", owner="munge", group="munge")
    run(["systemctl", "restart", "munge"], sudo=True)


def sync_munge_key() -> None:
    """Re-fetch the CURRENT munge key from the Head. `cuttle munge rotate`'s counterpart on an Arm:
    that command mints a fresh key on the Head and can reach no further — a donor's box is the
    donor's, and this is the one thing only they can run.

    Deliberately narrow. `/enroll` already hands back whatever key is live to any admitted node on
    every call (head/api's POST handler doesn't special-case rotation — the key it reads off disk is
    just whatever the Head currently has), so no new endpoint was needed, only a reason to call the
    old one again. Everything else `fetch_enrollment`/`setup --arm` would normally touch — mesh
    identity, gres.conf, slurmd's `--conf-server` — is left exactly as it was; a re-sync is not a
    re-enrollment.
    """
    arm = settings.load().get("arm")
    if not arm:
        raise ui.abort("this box was never enrolled as an Arm",
                       next_step="run `cuttle setup --arm --token <token>` first")
    with ui.section("Syncing the munge key", lambda: "synced"):
        system.prime_sudo()
        # `features` re-sent from what setup last recorded (not re-collected): /enroll is an UPDATE, so
        # omitting them here would read as "this Arm has none" and silently erase a real declaration.
        info = fetch_enrollment(arm["head_mesh_ip"], arm["node_name"], arm["mesh_ip"],
                                features=arm.get("features"))
        write_munge_key(info["munge_key"])


# --- slurmd (dynamic + configless) ------------------------------------------

def slurmd_dropin(node_name: str, ctld_addr: str, vram_mb: int, gpus: int,
                  feature_names: list[str] | None = None) -> str:
    """This Arm's slurmd flags. `-Z` = register as a dynamic node; `--conf-server` = fetch slurm.conf
    from slurmctld instead of having one installed. Gres must be declared here: slurm-wlm ships no
    NVML, so the node cannot introspect its own GPUs — CCC counts them itself at enrollment
    (system.gpu_devices) and states the count here, which is why no NVML-enabled rebuild is needed.

    `gpus` must agree with the number of gres.conf File lines the Head wrote for this node, or
    slurmctld sets the node INVALID: the count claimed here is checked against the devices configured
    there. Both come from the same `gpu_devices` list this Arm reported at /enroll.

    `Feature=vramN[,feature,...]` rides along, and `--conf` is the only durable place for it: a dynamic
    node's config is re-sent from `--conf` at *every* registration, so a feature set any other way
    (`scontrol update`) would silently vanish the next time this slurmd restarted — and a Spawn's
    `--vram`/Arm-Feature constraint would then match nothing. `feature_names` are this Arm's declared
    Arm Features, comma-joined onto the one vram bucket exactly
    like it: Slurm Features are a flat per-node set, so there is no structural difference between the
    two once rendered. Weight (∝ 1/Power) is deliberately NOT here, and can't be: Power is only
    measurable once the node is live. Losing Weight costs nothing worse than the fastest-free-card
    preference; losing a Feature would make the node unschedulable for anything that requires it.

    A drop-in rather than `/etc/default/slurmd` (see config.SLURMD_DROPIN): systemd does not honour
    quotes *inside* an expanded variable, so the distro's `$SLURMD_OPTIONS` hook cannot express a
    `--conf` with a space in it. Quotes written literally in a unit file are honoured.
    """
    feature = ",".join([config.vram_feature(vram_mb), *sorted(feature_names or [])])
    conf = f"Gres=gpu:{gpus} Feature={feature}"
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


def arms_missing(capability: str) -> list[str]:
    """Every ADMITTED Arm that has not reported `capability` in its /enroll POST — the Arms holding up
    a fleet-wide Slurm change.

    Configless Slurm serves slurm.conf but NOT the scripts it names, so turning on `Prolog=` before
    every node has the file fails those nodes' jobs and drains them — and a drained node stays drained
    across a slurmd restart, so putting the file there afterwards does not bring it back. The Head
    therefore will not render the line at all while this list is non-empty.

    The Head's own co-located Arm is excluded: it never enrolls (it has no registry POST to report in),
    and `setup --head` writes its files itself, in the same run that renders the config.

    A retracted Arm is deliberately NOT excluded. Retraction lives in head.db, which this side does not
    read — and a Retract can be Returned at any moment, which would put a node with no prolog straight
    back into the placement pool.
    """
    return sorted(name for name, e in load_registry().items()
                  if e.get("admitted") and not e.get("head")
                  and capability not in (e.get("provides") or []))


def save_registry(reg: dict) -> None:
    system.install_file(json.dumps(reg, indent=2, sort_keys=True) + "\n", config.ARMS_REGISTRY,
                        mode="0644", owner=config.ENROLL_API_USER, group=config.ENROLL_API_USER)


# --- Arm Features -----------------------------------------------------------
# The catalog is config.FEATURE_NAMES, a constant — there is nothing to load, save or curate. See that
# constant for why: CCC ships the runtime for each Feature, so a name it does not know is not a Feature
# CCC can place a Spawn on. What remains Arm-local is which of them THIS box has, and where.

def list_features(as_json: bool = False) -> None:
    names = list(config.FEATURE_NAMES)
    if as_json:
        ui.emit(names, True, lambda: None)
        return
    for n in names:
        ui.say(f"  {n}")
    ui.hint("Declare which of these this box has with `cuttle setup --arm` (or `--head` for the "
            "Head's own Arm); ask for one with `cuttle run --feature <name>`.")


def render_features_env(features: list[dict], licence_home: str = "") -> str:
    """One `export <NAME>_PATH=<path>` line per declared Arm Feature, written to config.ARM_FEATURES_ENV
    by `setup --arm`. `export`, not a bare assignment: ccc_head_api.spawns.JOB_SCRIPT sources this file
    with no `set -a`, and Apptainer's default (non `--cleanenv`) behaviour is to pass the WHOLE calling
    environment into the Pod — so only an exported var actually reaches a Spawn's payload.

    A feature name may contain `.`/`-` (config.FEATURE_NAME_RE) which a shell identifier cannot, hence
    the substitution — `vsim-pro` becomes `VSIM_PRO_PATH`, not a syntax error.

    `licence_home` is this Arm's **Licence** directory (temp/FEATURES_LICENCE.md) — ONE per Arm, not one
    per Feature, because a licence is locked to hardware and an Arm is one machine. It rides this same
    file for the same reason a Feature's path does: the value is Arm-local, so the Head cannot bake it
    into a job script it renders for the whole fleet. The Arm supplies it; JOB_SCRIPT decides whether
    this Spawn may see it.
    """
    # `install_path`, not `f['path']`: for a Feature CCC mirrors, the owner's own directory is not
    # reachable from a Pod (cuttle.features.mirror_install), so what a Spawn is told is CCC's copy.
    lines = [f"export {re.sub(r'[^A-Za-z0-9]', '_', f['name']).upper()}_PATH="
             f"{features_mod.install_path(f)}" for f in features]
    # Whatever else a Feature's runtime needs from THIS box — for one that ships binary wheels, where
    # `cuttle setup --arm` unpacked them (cuttle.features). Emitted from the profile rather than passed
    # in, so this file and the unpacking can never describe different directories.
    lines += features_mod.features_env_extras(features)
    if licence_home:
        lines.append(f"export {config.LICENCE_HOME_VAR}={licence_home}")
    header = "# Rendered by `cuttle setup --arm`. Do not hand-edit.\n"
    return header + "\n".join(lines) + ("\n" if lines else "")


def render_gres(reg: dict, write_root_file) -> None:
    """gres.conf = the Head + every ADMITTED Arm. Re-validated here, root-side: the API is
    unprivileged and its registry is only as trustworthy as the mesh.

    The Head's own co-located Arm has a registry row too (so it can carry an owner, like any other
    Arm), but it is `head: true` and templates.head_gres_node() already supplies its lines — skip it
    or gres.conf names it twice.

    Every device path is re-checked, not just the first: a multi-GPU Arm POSTs a whole list, and each
    entry of it is about to be written into a root-owned file.
    """
    nodes = [templates.head_gres_node()]
    for name, e in sorted(reg.items()):
        if not e["admitted"] or e.get("head"):
            continue
        # SAFE, not the input rule: this guards what reaches a root-owned file, and a row admitted
        # before names were forced lowercase (config.NODE_NAME_RE) is still a row this must render
        # rather than crash on — including for the `arm remove` that would retire it.
        assert re.match(config.NODE_NAME_SAFE_RE, name), f"bad node name in registry: {name!r}"
        for device in e["gpu_devices"]:
            assert re.match(config.GPU_DEVICE_RE, device), f"bad device: {device!r}"
        nodes.append((name, e["gpu_devices"]))
    write_root_file(config.GRES_CONF, templates.render_gres_conf(nodes))
    reconfigure(reg)


def reconfigure(reg: dict | None = None, check: bool = True) -> None:
    """The ONLY way this codebase reconfigures Slurm — because a reconfigure without the re-push that
    follows it silently drops every dynamic node's Weight (see `reapply_weights`).

    Two call sites reached `scontrol reconfigure` directly and only one remembered, so a routine
    `setup --head` degraded scheduling to registration order with no error. One function means a third
    call site cannot reintroduce that.

    `reg` is read from disk when not supplied — `setup --head` has no registry in hand at the point it
    starts the daemons, and on a FIRST setup there is no file at all, which loads as {} and re-pushes
    nothing.
    """
    run(["scontrol", "reconfigure"], sudo=True, check=check)
    reapply_weights(load_registry() if reg is None else reg)


def reapply_weights(reg: dict) -> None:
    """Re-push every benchmarked Arm's Weight after a reconfigure — which resets each node to what the
    config files say, and donor Arms are dynamic, so no file says anything about them.

    Verified: `scontrol update NodeName=X Weight=<n>` lands, and the very next `scontrol
    reconfigure` puts it back to 1. A reconfigure happens on every authorize/admit/remove and on every
    `setup --head`, so a Weight pushed once does not survive the next one — which is why `reconfigure`
    above is the only way to issue one. The Head's own Weight is in slurm.conf
    and needs none of this; it is re-pushed anyway, because one loop is simpler than an exception.

    ⚠ Still lost when a donor's slurmd restarts (their box reboots): a dynamic node re-registers from
    its `--conf`, which cannot carry a Power that had not been measured when it was written. Costs
    card-selection order only — never money, since the Escrow quotes max Power over eligible Arms and
    the meter settles on what actually ran. Closing it needs a privileged loop on the Head (Phase 2).
    """
    for name, e in sorted(reg.items()):
        if e["admitted"] and e.get("powers"):
            apply_weight(name, e["powers"])


def _await_enrollment(name: str, token: str, timeout_s: int = 300) -> dict:
    """Block until the Arm POSTs /enroll — the only thing that tells us the GPU devices we must write
    into gres.conf.

    Waits for `mesh_ip`, not for the row: `authorize` pre-declared the row itself moments ago (that
    pre-declaration is what lets the Enroll API refuse un-named machines), so its mere existence
    proves nothing. `mesh_ip` and `gpu_devices` are null until the donor's POST fills them in.

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


def _bench_secret() -> str:
    """The [bench] rsync module's password, read off the root-owned secrets file `setup --head` minted.

    A benchmark stages its Power file home over rsync like any remote Spawn, but it never goes through
    the Head API, so there is no **Siphon** for it — it uses the one cluster-lifetime credential
    instead. Read at render time rather than held anywhere: this runs
    on the Head, with an operator at a tty who has already primed sudo."""
    r = run(["cat", str(config.RSYNCD_BENCH_SECRETS)], sudo=True, capture_output=True, text=True)
    return r.stdout.strip().split(":", 1)[1]


def _benchmark_arm(name: str) -> dict:
    _wait_for_node(name)
    head_ip = mesh.tailscale_ip()
    reg = load_registry()
    # The job takes every card this Arm registered, so one Spawn returns one Power per **Device**.
    # Read from the registry rather than counted on the Head: these are the donor's cards.
    gpus = len(reg[name]["gpu_devices"])
    dest = config.HEAD_SPAWNS_DIR / config.BENCH_DIR_NAME / name
    run(["install", "-d", "-o", config.HEAD_API_USER, "-g", config.HEAD_API_USER, str(dest)],
        sudo=True)
    run(["rm", "-f", str(dest / "power")], sudo=True)  # a re-benchmark must not read the old numbers

    with tempfile.NamedTemporaryFile("w", suffix=".sbatch", delete=False) as f:
        f.write(templates.render_bench_sbatch(name, head_ip, _bench_secret(), gpus))
        job_file = f.name
    Path(job_file).chmod(0o644)  # sbatch reads it as the cluster Guise, not as the operator

    with ui.step(f"Benchmarking {name} (matmul in the Pod — this is what prices a second on it)"):
        # Unlinked the moment sbatch has read it — it holds no state past submission, and left behind
        # it is one /tmp/tmp*.sbatch per benchmark, forever.
        #
        # As the **cluster's own Guise**, not as the Mantle. This is
        # the one Spawn no User submits, and it was the last thing still running as `ccc` — which would
        # have made "a Spawn owned by the Mantle is a bug" a rule with an exception, and an exception is
        # not a rule you can test. Its uid is reserved, so it collides with no User's.
        try:
            sub = run(["-u", config.guise_name(config.GUISE_CLUSTER_UID), "sbatch", "--parsable",
                       job_file], sudo=True, check=True, capture_output=True, text=True)
        finally:
            Path(job_file).unlink(missing_ok=True)
        bench_job = sub.stdout.strip()
        # The wait below can run for minutes, and an operator who gives up on it presses Ctrl-C. Without
        # this the benchmark Spawn keeps running — holding every card on the Arm being admitted, with
        # nothing left that knows its job id — until it finishes on its own. It self-heals, but it wastes
        # the whole Arm meanwhile, which is the one thing an admit is trying to make available.
        try:
            deadline = time.time() + config.BENCH_TIMEOUT_S
            while time.time() < deadline:
                r = run(["cat", str(dest / "power")], sudo=True,
                        capture_output=True, text=True, check=False)
                if r.returncode == 0 and r.stdout.strip():
                    result = json.loads(r.stdout)
                    break
                time.sleep(3)
            else:
                # The job's own output cannot be shown here: `JobContainerType=job_container/tmpfs`
                # gives every job a PRIVATE /tmp, so the sink the benchmark writes dies with it and no
                # later job — not even on the same node — can read it. So this names the one cause the
                # operator cannot otherwise guess.
                #
                # The Head renders this job (templates.BENCH_SBATCH_TMPL) and binds the node's own
                # benchmark **Environment** at config.BENCH_ENV_DIR. A box set up by a `cuttle` older
                # than that env — one that still expected to pull the env over the mesh — has no such
                # directory, apptainer fails on a bind source that does not exist, and the job dies in
                # seconds while this loop waits out the full timeout with nothing to report. That is a
                # version skew between the Head and the Arm, and it looks exactly like a hung job.
                raise ui.abort(f"{name}'s benchmark produced no result within "
                               f"{config.BENCH_TIMEOUT_S}s",
                               next_step=f"check `{config.BENCH_ENV_DIR}` exists on {name} — an Arm "
                                         f"set up with an older cuttle has not got it, and must "
                                         f"re-run `cuttle setup --arm`. Then `cuttle arm benchmark "
                                         f"{name}`")
        except KeyboardInterrupt:
            run(["scancel", bench_job], sudo=True, check=False)
            raise

    # Re-read: the benchmark blocked for minutes, and `authorize` may have written the row since.
    reg = load_registry()
    if len(result["powers"]) != gpus:
        raise ui.abort(
            f"{name} registered {gpus} GPU(s) but the benchmark priced {len(result['powers'])} — "
            f"the Pod saw different hardware than enrollment did",
            next_step=f"check `scontrol show node {name}` against nvidia-smi on that box, then "
                      f"re-run `cuttle arm benchmark {name}`")
    reg[name]["powers"] = result["powers"]
    reg[name]["gpu"] = result["gpu"]
    save_registry(reg)
    apply_weight(name, result["powers"])
    ui.result("gpu", f"{gpus}x {result['gpu']} — {result['vram_mb']} MiB VRAM each")
    ui.result("power", ", ".join(f"{p} TFLOP/s" for p in result["powers"]))
    return result


def apply_weight(name: str, powers: list[float]) -> None:
    """Weight ∝ 1/Power, so Slurm hands out the fastest *free* eligible card first.

    Not a reservation: Slurm won't hold a Spawn back waiting for a better GPU to free up — it takes the
    best one available now, which is what "the first one that fits, else queue" means.

    A node has ONE Weight however many cards it holds, so config.node_weight reduces the list — see
    there for why the mean is sound here and would not be at Settlement.
    """
    run(["scontrol", "update", f"NodeName={name}", f"Weight={config.node_weight(powers)}"], sudo=True)


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
    # FIRST, before the owner lookup and long before a preauth key exists: this is pure input
    # validation with no side effect either way, and getting it wrong is expensive downstream —
    # Headscale lowercases the name it is given (config.NODE_NAME_RE), so an uppercase Arm enrolls,
    # joins the mesh, and is then refused the munge key by a name comparison it can never win.
    if not re.match(config.NODE_NAME_RE, name):
        hint = name.lower()
        raise ui.abort(
            f"invalid Arm name {name!r} — lowercase letters, digits and hyphens only, starting with\n"
            f"  a letter or digit, at most 32 characters. Headscale lowercases every node name it is\n"
            f"  given, and the mesh name and the registry name must be one string or the Arm is\n"
            f"  refused the munge key at /enroll.",
            next_step=(f"run `cuttle arm authorize {hint} --user {owner}`"
                       if re.match(config.NODE_NAME_RE, hint)
                       else "pick a name matching that rule"))
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
    # gpu_devices + vram_mb arrive with the donor's /enroll; powers only after the benchmark, which
    # can't run until the node is live. All are named here so a row's shape never depends on how far
    # it got. `powers` is index-aligned with `gpu_devices` once both are filled.
    reg[name] = {"owner": owner, "gpu_devices": None, "mesh_ip": None, "admitted": False,
                 "vram_mb": None, "powers": None, "gpu": None}
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
    # `ui.secret`/`data(log=False)`: the token reaches the screen and stdout, but not the run log,
    # which would otherwise accumulate every token this cluster has ever minted in cleartext.
    ui.secret(f"  [bold]{token}[/bold]")
    ui.say("  the donor runs:  [accent]cuttle setup --arm --token <token>[/accent]")
    ui.say("")
    ui.data(token, log=False)
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
    with ui.step(f"Admitting {name} to Slurm ({', '.join(reg[name]['gpu_devices'])})"):
        reg[name]["admitted"] = True
        save_registry(reg)
        render_gres(reg, write_root_file)
    # An Arm with no Power cannot be priced, so it is benchmarked as part of joining — not as an
    # optional follow-up an operator might forget.
    benchmark_arm(name)
    # Same stance, for the same reason: a declared **Feature** that has never been proved to work is a
    # Slurm constraint that sends Spawns somewhere they will fail. An Arm joins with its declarations
    # true, or it does not join.
    activate_arm(name, [f["name"] for f in (reg[name].get("features") or [])])


# The activation Spawn's shape. One card — activation is a licence handshake, not a benchmark, but
# creating a gym needs a real device — and a short wall clock, which is plenty for a gym and a network
# round trip.
ACTIVATION_MINUTES = 20

# How long CCC waits, which is a DIFFERENT budget: Slurm's clock starts when the Spawn is placed, and
# everything before that is ours to wait through — the Environment build (a cold conda solve of the
# profile's libraries, the first time this box ever sees them) and then the queue. Conflating the two
# made a slow first build look exactly like a hung activation.
ACTIVATION_WAIT_S = 45 * 60


def activate_arm(name: str, features: list[str]) -> None:
    """Activate every declared **Feature** on `name` that needs it, as part of admitting the Arm.

    **CCC drives this, not the Arm's owner, and not by hand.** The software is licensed to the machine
    through a vendor library, and the only supported way to activate it is to import the library and
    create a gym — which needs the exact runtime a Spawn gets: the Arm's own wheel, a Python it has a
    build for, the system libraries, and `libcuda.so.1`. CCC already assembles all of that for a Spawn,
    including on WSL, where an out-of-Pod activation would have had to solve the driver stack by hand.
    So the activation IS a Spawn, pinned to the Arm being admitted.

    **The Environment comes from the Feature's own profile** (`GET /features/<name>/environment.yml`),
    which is the same file a submitter is handed by `cuttle feature show`. It carries no vlearn: the
    package comes from the Arm's unpacked wheel on PYTHONPATH, which is the build the licence covers.

    **The key never leaves the Arm.** `--activation` is a Head-local flag with no HTTP equivalent; all
    it does is bind the path the Arm recorded in its own `/etc/ccc/features.env`. No ordinary Spawn
    ever sees it, and nothing here reads it.

    Aborts the admit if an activation fails, which is deliberate — see `_admit`.
    """
    from . import features as feature_profiles
    from . import spawns as spawn_client

    for feat in features:
        try:
            project = feature_profiles.activation_project(feat)
        except LookupError:
            continue    # placement-only Feature: nothing to activate, nothing to prove
        with ui.section(f"Activating {feat} on {name}", lambda: "activated"):
            _activate_one(name, feat, project, spawn_client)


def _activate_one(name: str, feat: str, project: dict[str, str], spawn_client) -> None:
    with ui.step(f"Building {feat}'s environment and activating it on {name} "
                 f"(first run on this box — a few minutes)"):
        tar = spawn_client.tar_from_files(project)
        spawn_id, _ = spawn_client._submit_pinned(
            tar, "activate.py", [], minutes=ACTIVATION_MINUTES, vram=0, gpu=1, node=name,
            wandb_env={}, outputs=[config.ACTIVATION_OUTPUT_DIR], features=[feat],
            manifest="pyproject.toml", activation=True,
            name=config.ACTIVATION_SPAWN_NAME % feat,
            # The first admin, who owns the Head's own Arm. Not "whoever is logged in": this Spawn is
            # CCC's, it runs during `setup --head` when nobody is, and an Admin is outside the Karma
            # economy — so activating a donated box charges no one for the card it briefly holds.
            user=config.FIRST_ADMIN_USER)
        rc, err = spawn_client.wait_for_exit(spawn_id, timeout_s=ACTIVATION_WAIT_S)
    if rc == 0:
        ui.result(feat, f"activated on {name} (Spawn {spawn_id})")
        return
    # The vendor's own message is the useful part and is almost always self-explanatory ("no more
    # activations left on this key", "could not reach the activation server"). Surfaced verbatim rather
    # than summarised, because CCC cannot improve on it and paraphrasing it would only hide it.
    detail = (err or "").strip().splitlines()
    raise ui.abort(
        f"{name} declared the {feat!r} Feature but CCC could not activate it"
        + (f":\n  " + "\n  ".join(detail[-8:]) if detail else " (the Spawn produced no output)"),
        next_step=f"fix it on {name} (its owner's `cuttle setup --arm` recorded the install and the "
                  f"licence key), then re-run `cuttle arm admit {name}`. See `cuttle logs {spawn_id}` "
                  f"for the whole run.")


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


def register_head_arm(mesh_ip: str, features: list[dict] | None = None) -> None:
    """Give the Head's own co-located Arm a registry row, owned by the first admin.

    It never enrolls (it is a *static* Slurm node, and there is no donor), so without this it would be
    the one Arm in the cluster with no owner — and Karma (3b) exempts admin-owned Arms, which it can
    only do if it knows this one is admin-owned. `head: true` keeps render_gres from emitting its GRES
    lines twice (templates.head_gres_node() already has them).
    """
    reg = load_registry()
    name = system.hostname()
    # `features` in the SAME shape a donor Arm reports at /enroll, so slurm.arms_with_features and the
    # Dashboard read one row shape for every Arm — the Head's box is an Arm like any other, and its
    # Features are what setup.write_slurm_config re-reads to keep its Feature= line across reconfigures.
    reg[name] = {"owner": config.FIRST_ADMIN_USER, "gpu_devices": system.gpu_devices(),
                 "mesh_ip": mesh_ip, "admitted": True, "head": True,
                 "vram_mb": system.gpu_vram_mb(), "powers": None, "gpu": None,
                 # Name and path only, as /enroll receives them — a **Licence** key path a Feature
                 # entry may carry locally is Arm business, and the registry is served to the cluster.
                 "features": [{"name": f["name"], "path": f["path"]} for f in (features or [])]}
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
    # POWER lists every Device, never a node average: two 4090s in one box measure differently (an x4
    # slot, the one that throttles under the other) and the operator is the person who wants to see it.
    ui.table(
        ["NAME", "OWNER", "MESH IP", "GPU", "VRAM", "POWER", "STATE"],
        [[a["name"], a.get("owner") or "-", a.get("mesh_ip") or "-", _gpu_label(a),
          f"{round(a['vram_mb'] / 1024)}G" if a.get("vram_mb") else "-",
          "/".join(str(p) for p in a["powers"]) + " TF" if a.get("powers") else "-",
          a["state"]] for a in payload])


def _gpu_label(a: dict) -> str:
    """`4x NVIDIA RTX 4090` — the count comes from the devices (known at /enroll), the model from the
    benchmark (known only once it has run), so either half can be missing on its own."""
    n = len(a.get("gpu_devices") or [])
    if not a.get("gpu"):
        return f"{n}x ?" if n else "-"
    return f"{n}x {a['gpu']}" if n > 1 else a["gpu"]


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
    # What removal does NOT do: this box's copy of the munge key is not invalidated by any of the
    # above (munge has no per-holder revocation). Said every time, not just when it might matter,
    # because "might matter" is a judgment this command has no way to make about a donor it just evicted.
    print(f"cuttle: {name}'s copy of the munge key is still technically valid — "
          f"if you don't trust it to be gone, `cuttle munge rotate`")


# --- munge rotation (Head only, operator + sudo) -----------------------------

def rotate_munge_key() -> None:
    """Mint a fresh cluster-wide munge key and push it live on the Head. The one lever against "an
    Arm's copy never expires on its own": rotating invalidates every existing copy AT ONCE,
    cluster-wide (`setup.ensure_munge_key` only ever mints a FIRST key; nothing rekeys a live one).

    Same generation `setup.ensure_munge_key` uses for the very first key, just unconditional: that one
    refuses to touch a key that already exists (a fresh cluster's one chance to mint one at all), and
    this one's entire job is to replace one that already works.

    Every already-ADMITTED Arm's on-disk copy is stale the instant this returns — this command cannot
    reach a donor's box to fix that, so it only prints who needs `cuttle arm sync-key` and stops.
    Until an Arm re-syncs, its slurmd/sbatch simply fails munge auth against the rotated Head; nothing
    here degrades more gracefully than that; the Head's own co-located Arm needs no such notice, since
    it holds no separate copy — the file this just overwrote IS its copy.
    """
    with ui.section("Rotating the cluster's munge key", lambda: "rotated"):
        system.prime_sudo()
        run(["rm", "-f", str(config.MUNGE_KEY)], sudo=True)
        run([config.MUNGEKEY_BIN, "--create", "--keyfile", str(config.MUNGE_KEY)], sudo=True)
        run(["chown", "munge:munge", str(config.MUNGE_KEY)], sudo=True)
        run(["chmod", "0400", str(config.MUNGE_KEY)], sudo=True)
        # Both authenticate every RPC via munged; a munged that kept its old credential cached (or a
        # slurmctld that did) is exactly the state a plain restart avoids finding out about mid-Spawn.
        run(["systemctl", "restart", "munge"], sudo=True)
        run(["systemctl", "restart", "slurmctld"], sudo=True)
        stale = [name for name, e in sorted(load_registry().items())
                if e["admitted"] and not e.get("head")]
    if stale:
        ui.warn("every Arm below is now carrying a stale key and cannot reach slurmctld until its "
                "owner runs `cuttle arm sync-key` on that box:")
        for name in stale:
            ui.say(f"    {name}")
    else:
        ui.say("  [muted]no admitted Arms yet — nothing to re-sync[/muted]")


# --- Retract / Return --------------------------------------------------------
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
