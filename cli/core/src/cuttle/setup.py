"""`cuttle setup` — provision a node in one of two roles, on a native-Linux box.

`--head`: self-hosts the Headscale coordinator + the Head API, runs slurmctld, and carries a
co-located Arm as a *static* Slurm node. `--local` is an alias (a single box is just a Head).

`--arm`: joins an existing cluster with a join token, pulls the munge key from the Head API over the
mesh, and registers as a *dynamic* Slurm node (`slurmd -Z`) fetching its config from slurmctld.

Drives apt+sudo directly. Intended path only — no fallbacks (test box; failures decided live).
"""

import getpass
import ipaddress
import tempfile
from pathlib import Path

from . import auth, backup, config, enroll, headapi, mesh, monitor, settings, system, templates, ui
from .system import run, sh


def _write_root_file(path: Path, content: str, mode: str = "644") -> None:
    """Write `content` to a root-owned path via a temp file + sudo install."""
    with tempfile.NamedTemporaryFile("w", suffix=".ccc", delete=False) as f:
        f.write(content)
        tmp = f.name
    run(["install", "-D", "-m", mode, tmp, str(path)], sudo=True)


def preflight(role: str) -> None:
    """`role` is "head" or "arm". WSL2 is a proven Arm platform (mesh, slurmd, and CUDA inside a Pod
    all verified on an RTX 4090) but an unproven Head: headscale and slurmctld have never run there,
    and a Head is expected to stay up."""
    rel = system.os_release()
    # ui.say, not print: inside a section this is a row of the box. print goes to stdout, which
    # `ui.fullscreen` holds back until the whole flow is over — so the box would sit empty while the
    # facts about this machine waited to be replayed at the end.
    ui.say(f"  [muted]host={system.hostname()} os={rel.get('PRETTY_NAME')} "
           f"cpus={system.cpu_count()} mem={system.real_memory_mb()}MB[/muted]")
    # Gating a supported platform, not an internal invariant — so ui.abort (a clean ✗ + a next step),
    # not `assert` (which prints a bare one-liner and vanishes entirely under `python -O`).
    if not system.is_native_linux_x86():
        raise ui.abort("this MVP supports Linux x86_64 only",
                       next_step="run setup on a native x86_64 Ubuntu 24.04 box")
    if rel.get("VERSION_ID") != "24.04":
        raise ui.abort(f"this MVP is pinned to Ubuntu 24.04 (this box is {rel.get('VERSION_ID')!r})",
                       next_step="use Ubuntu 24.04")
    if system.is_wsl():
        if role != "arm":
            raise ui.abort("WSL2 can host an Arm, but not the Head (headscale/slurmctld unproven here)",
                           next_step="run `cuttle setup --arm` on this box, and host the Head elsewhere")
        ui.say("  [muted]WSL2 — supported as an Arm, with one permanent limit: /dev/dxg is a single "
               "device node for ALL GPUs, so Slurm can never isolate GPUs per-Spawn here[/muted]")
    run([system.nvidia_smi(), "--query-gpu=name,driver_version", "--format=csv,noheader"])


def configure_apptainer_wsl() -> None:
    """Make `apptainer exec --nv` work on a WSL Arm, so nothing above the node ever learns what WSL
    is. Contains the platform quirk at the platform boundary: `cuttle run`, Tide Pools, and any
    future API-launched job all keep emitting one portable exec line.

    Two additive lines (see config.WSL_LIB_ROOT for the measurements). Idempotent: `setup --arm`
    re-runs, and a duplicate `bind path` would be applied twice.
    """
    sh(f"grep -qxF 'bind path = {config.WSL_LIB_ROOT}' {config.APPTAINER_CONF} || "
       f"echo 'bind path = {config.WSL_LIB_ROOT}' >> {config.APPTAINER_CONF}", sudo=True)
    sh(f"grep -qxF '{config.WSL_EXTRA_NVLIB}' {config.APPTAINER_NVLIBLIST} || "
       f"echo '{config.WSL_EXTRA_NVLIB}' >> {config.APPTAINER_NVLIBLIST}", sudo=True)
    ui.say(f"  [muted]WSL Arm — Apptainer binds {config.WSL_LIB_ROOT} and injects "
           f"{config.WSL_EXTRA_NVLIB}; --nv now behaves as on native Linux[/muted]")


def add_repos() -> None:
    run(["add-apt-repository", "-y", "ppa:apptainer/ppa"], sudo=True)
    sh(
        # --yes: clobber the keyring on re-run instead of prompting (public repo key; setup is idempotent).
        "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey "
        "| gpg --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && "
        "curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/"
        "nvidia-container-toolkit.list "
        "| sed 's#deb https://#deb [signed-by=/usr/share/keyrings/"
        "nvidia-container-toolkit-keyring.gpg] https://#g' "
        "> /etc/apt/sources.list.d/nvidia-container-toolkit.list",
        sudo=True,
    )


def install_packages(slurm_pkg: str = config.SLURM_PKG) -> None:
    """`slurm_pkg` is the full suite on a Head, just the node daemon on an Arm."""
    run(["apt-get", "update"], sudo=True)
    before = system.installed_packages()
    run(
        ["apt-get", "install", "-y",
         slurm_pkg, config.APPTAINER_PKG, config.NVIDIA_CTK_PKG, config.MUNGE_PKG],
        sudo=True,
    )
    # Record exactly what we added (incl. pulled deps) so teardown removes only these.
    added = sorted(system.installed_packages() - before)
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.APT_MANIFEST.write_text("\n".join(added) + "\n" if added else "")
    ui.say(f"  [muted]recorded {len(added)} newly-installed packages → {config.APT_MANIFEST}[/muted]")


def write_slurm_config(mesh_ip: str) -> None:
    for d, owner in ((config.SLURM_STATE_DIR, "slurm"),
                     (config.SLURM_LOG_DIR, "slurm"),
                     (config.SLURMD_SPOOL_DIR, "root")):
        run(["install", "-d", "-o", owner, "-g", owner, str(d)], sudo=True)
    # The Head plus any Arms already admitted (a re-run must not un-admit them). The Head's own row is
    # skipped: head_gres_node() already supplies its line (see enroll.render_gres).
    reg = enroll.load_registry()
    # The Head's Weight goes in the file, not just `scontrol update`, or the next reconfigure wipes it.
    # Unknown on a first setup (the benchmark runs later, and re-renders this) -> node_weight's floor.
    power = (reg.get(system.hostname()) or {}).get("power")
    _write_root_file(config.SLURM_CONF,
                     templates.render_slurm_conf(mesh_ip, config.node_weight(power) if power else 1))
    nodes = [templates.head_gres_node()]
    nodes += [(n, e["gpu_device"]) for n, e in sorted(reg.items())
              if e["admitted"] and not e.get("head")]
    _write_root_file(config.GRES_CONF, templates.render_gres_conf(nodes))
    # What task/cgroup enforces. Configless serves it to every Arm with slurm.conf, so it is written
    # here only — an Arm never authors one.
    _write_root_file(config.CGROUP_CONF, templates.render_cgroup_conf())
    # The Head's own slurmd (its co-located Arm) runs Spawns too, so the scratch-reclaim epilog its
    # slurm.conf now references must exist here as well as on every Arm (setup_arm writes it there).
    _write_root_file(config.SLURM_EPILOG, templates.render_epilog(), mode="755")


def make_registry_dir() -> None:
    """The Enroll API's only writable location — it runs as munge and owns nothing else."""
    run(["install", "-d", "-m", "0755", "-o", config.ENROLL_API_USER, "-g", config.ENROLL_API_USER,
         str(config.CCC_LIB)], sudo=True)


def provision_mantle() -> None:
    """Create the **Mantle** — the one identity every Spawn executes as — on THIS node, Head or Arm.

    The uid is pinned (config.CCC_UID), not distro-allocated, and that is the whole point: Slurm runs a
    job as the *submitting* uid, so slurmctld records the Head API's uid at sbatch and every slurmd
    setuid()s to it. A dynamically-allocated `ccc` would get a different uid on each box and a Spawn
    scheduled onto a remote Arm would setuid() to a user that doesn't exist there. One pinned uid,
    cluster-wide, is what makes a Spawn the same Spawn on any node.

    Home-less on purpose: the Mantle owns no per-node state anywhere. It reads base.sif from the
    world-readable CCC_IMAGES_DIR and scratches in /tmp (see the API's JOB_SCRIPT), so a donor box is
    left with nothing. `nologin` is fine — slurmd execs the batch script by its shebang, not a shell.

    Idempotent (re-runs are non-fatal), but a uid/gid COLLISION is fatal by design: useradd fails,
    setup halts, and we decide live rather than silently landing on a mismatched uid.
    """
    run(["groupadd", "--gid", str(config.CCC_GID), config.HEAD_API_USER], sudo=True, check=False)
    run(["useradd", "--uid", str(config.CCC_UID), "--gid", str(config.CCC_GID), "--system",
         "--no-create-home", "--shell", "/usr/sbin/nologin", config.HEAD_API_USER],
        sudo=True, check=False)  # non-fatal if the account already exists
    # useradd's failure is non-fatal above (re-run), so verify the uid we actually ended up with —
    # a pre-existing `ccc` at some OTHER uid would silently break remote Spawns.
    uid = run(["id", "-u", config.HEAD_API_USER], capture_output=True, text=True).stdout.strip()
    assert uid == str(config.CCC_UID), (
        f"the `{config.HEAD_API_USER}` account on this box has uid {uid}, but CCC pins it to "
        f"{config.CCC_UID}. Every node must agree or Slurm cannot run a Spawn here. Remove the "
        f"conflicting account (`sudo userdel {config.HEAD_API_USER}`) and re-run setup.")


def provision_head_auth() -> None:
    """Head-side identity spine: the Mantle's Head state + auth store (head.db). On the Head the Mantle
    also runs the Head API and owns head.db plus the submission dirs (uploaded projects, Spawn results,
    scratch). Called after the API venv is built (seeding the admin runs from it) and before the Head
    API unit and rsyncd start (both need User=ccc + these dirs). Idempotent on re-run."""
    provision_mantle()
    for d in (config.HEAD_STATE_DIR, config.HEAD_PROJECTS_DIR, config.HEAD_SPAWNS_DIR,
              config.HEAD_SCRATCH_DIR):
        run(["install", "-d", "-m", "0700", "-o", config.HEAD_API_USER, "-g", config.HEAD_API_USER,
             str(d)], sudo=True)
    admin_user = config.FIRST_ADMIN_USER
    auth.seed_admin(admin_user)  # registers the one-time temp password as a result


def ensure_munge_key() -> None:
    """Mint the cluster's munge key if this Head has none. **Head only** — an Arm is *given* this key
    over /enroll (enroll.write_munge_key); a second one would be a second cluster.

    Setup used to leave this entirely to the munge package's postinst, which creates a key on a FRESH
    install and never again. That assumption survives exactly one lifecycle: `cuttle teardown` deletes
    the key (correctly — a departed node must not keep the cluster secret) but leaves munge installed
    whenever the package predates CCC, since the apt manifest only records what setup itself added. The
    next `apt-get install munge` is then a no-op, no postinst runs, and munge dies on a missing keyfile,
    taking slurmctld and slurmd with it — three failed units for one absent file.

    `test -s` under sudo rather than Path.exists(): /etc/munge is `drwx------ munge munge`, and pathlib
    raises PermissionError instead of answering False when it cannot traverse (see monitor.py).
    """
    if run(["test", "-s", str(config.MUNGE_KEY)], sudo=True, check=False).returncode == 0:
        return
    run([config.MUNGEKEY_BIN, "--create", "--keyfile", str(config.MUNGE_KEY)], sudo=True)
    run(["chown", "munge:munge", str(config.MUNGE_KEY)], sudo=True)
    run(["chmod", "0400", str(config.MUNGE_KEY)], sudo=True)
    ui.say(f"  [muted]minted a new cluster munge key → {config.MUNGE_KEY}[/muted]")


def start_daemons() -> None:
    ensure_munge_key()  # munge cannot start without it, and slurmctld/slurmd cannot start without munge
    # enable = start on boot; restart = start if down, else reload new config (idempotent re-run).
    run(["systemctl", "enable", "munge", "slurmctld", "slurmd"], sudo=True)
    run(["systemctl", "restart", "munge", "slurmctld", "slurmd"], sudo=True)


def install_rsyncd(mesh_ip: str) -> None:
    """Stage-in/out daemon, bound to the Head's mesh address only. Idle until a remote Arm uses it;
    started on every Head so admitting the first Arm needs no reconfigure. Runs as root (873 is
    privileged) but serves each module as `ccc`, so files stay ccc-owned. Both module roots (projects,
    spawns) are created ccc-owned by provision_head_auth, which must run first."""
    _write_root_file(config.RSYNCD_CONF, templates.render_rsyncd_conf(mesh_ip))
    _write_root_file(config.RSYNCD_UNIT, templates.render_rsyncd_unit())
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", config.RSYNCD_SERVICE], sudo=True)
    run(["systemctl", "restart", config.RSYNCD_SERVICE], sudo=True)


def build_base_sif() -> None:
    """Build the base Pod image at CCC_SIF — the SAME path on every node, Head or Arm.

    It lands in a world-readable /opt tree rather than the operator's ~/.ccc because Spawns run as the
    home-less **Mantle**, which cannot traverse a 0750 home. One path cluster-wide means the rendered
    job script names the sif identically wherever Slurm places the Spawn.

    Built locally on each node (apptainer build) — deliberately NOT shipped from the Head, which would
    push a multi-GB image over the mesh at every enrollment. Skipped if already present (a re-run must
    not trigger a multi-GB rebuild; teardown keeps it unless --all).
    """
    # Root-owned tree; make it operator-writable to build in place, then world-readable to run.
    run(["install", "-d", "-o", getpass.getuser(), "-g", getpass.getuser(),
         str(config.CCC_IMAGES_DIR)], sudo=True)
    if config.CCC_SIF.exists():
        ui.say(f"  [muted]base.sif exists ({config.CCC_SIF}) — skipping build[/muted]")
        return
    def_file = config.CCC_IMAGES_DIR / "base.def"
    def_file.write_text(templates.render_base_def())
    run(["apptainer", "build", str(config.CCC_SIF), str(def_file)])
    run(["chmod", "-R", "a+rX", str(config.CCC_IMAGES_DIR)], sudo=True)


def install_tls(mesh_ip: str) -> None:
    """Generate the Head's self-signed TLS cert, so the LAN path stops carrying passwords in the clear.

    Every address a client might dial the Head at must be in the cert's SANs, or TLS rejects the
    connection with a name mismatch *on top of* the self-signed warning:
      - the LAN IP + hostname  — a browser or CLI on the local network,
      - the mesh IP            — a remote Arm's owner browsing over the mesh,
      - localhost/127.0.0.1    — a WSL Arm's dashboard forward, which surfaces the Head at
                                 https://localhost:8082 in the Windows browser.

    Generated only if absent: regenerating would change the cert's fingerprint and invalidate the pin
    every client stored (see cuttle/tls.py), so a routine teardown+setup must not churn it. The key is
    readable only by the Mantle, which is the user the Head API runs as.
    """
    if config.HEAD_CERT.exists():
        print(f"cuttle: TLS cert exists ({config.HEAD_CERT}) — keeping it (clients have pinned it)")
        return
    host, lan = system.hostname(), system.primary_ip()
    san = (f"DNS:{host},DNS:localhost,IP:{lan},IP:{mesh_ip},IP:127.0.0.1")
    run(["install", "-d", "-m", "0755", str(config.CCC_TLS_DIR)], sudo=True)
    run(["openssl", "req", "-x509", "-newkey", "rsa:4096", "-nodes",
         "-days", str(config.CERT_DAYS),
         "-subj", f"/CN={host}", "-addext", f"subjectAltName={san}",
         "-keyout", str(config.HEAD_KEY), "-out", str(config.HEAD_CERT)], sudo=True)
    run(["chown", f"{config.HEAD_API_USER}:{config.HEAD_API_USER}", str(config.HEAD_KEY)], sudo=True)
    run(["chmod", "0600", str(config.HEAD_KEY)], sudo=True)  # the private half: Mantle-only
    run(["chmod", "0644", str(config.HEAD_CERT)], sudo=True)  # the public ID card: anyone may read
    print(f"cuttle: TLS cert for {san}")


def build_spa() -> None:
    """Build the Dashboard SPA and stage it where the Head API service reads it. Node is needed only
    on the Head; a donor Arm never builds this. Staged OUTSIDE the user's 0750 home — the service
    user can't traverse there — into a world-readable /opt path."""
    web_src = headapi.REPO_ROOT / "head" / "web"
    run(["npm", "ci"], cwd=str(web_src))
    run(["npm", "run", "build"], cwd=str(web_src))
    run(["rm", "-rf", str(config.HEAD_API_WEB_DIR)], sudo=True)
    run(["install", "-d", str(config.HEAD_API_WEB_DIR.parent)], sudo=True)
    run(["cp", "-r", str(web_src / "dist"), str(config.HEAD_API_WEB_DIR)], sudo=True)
    run(["chmod", "-R", "a+rX", str(config.HEAD_API_WEB_DIR.parent)], sudo=True)
    system.allow_tcp_port(config.HEAD_API_PORT, "ccc head api (LAN+mesh)")
    print(f"cuttle: Dashboard SPA built → {config.HEAD_API_WEB_DIR}")


def install_wsl_dashboard_forward(head_mesh_ip: str) -> None:
    """WSL-only: forward a local port to the Head API so the Arm's owner can open the Dashboard from
    their Windows browser at https://localhost:<HEAD_API_PORT>/. The CCC mesh lives inside WSL and
    Windows can't reach the Head's mesh IP, but WSL2 forwards Windows localhost to WSL listeners —
    so a listener here bridges the gap. Socket-activated proxy (systemd-socket-proxyd, no package).
    Idempotent: `enable --now` on re-run is a no-op.

    HTTPS, not HTTP: the proxy is raw TCP so it carries TLS untouched, and the Head speaks nothing but
    TLS now. `localhost`/127.0.0.1 are in the cert's SANs for exactly this path (see provision_tls)."""
    socket_unit = f"""\
[Unit]
Description=CCC dashboard forward (WSL->Windows localhost bridge)

[Socket]
ListenStream={config.HEAD_API_PORT}

[Install]
WantedBy=sockets.target
"""
    # No [Install]: this is started on demand by the .socket (names match), never enabled directly.
    service_unit = f"""\
[Unit]
Description=CCC dashboard forward: proxy to the Head API over the mesh
Requires={config.DASH_FORWARD_SERVICE}.socket
After={config.DASH_FORWARD_SERVICE}.socket {config.CCC_TS_SERVICE}.service

[Service]
ExecStart={config.SOCKET_PROXYD} {head_mesh_ip}:{config.HEAD_API_PORT}
"""
    _write_root_file(config.DASH_FORWARD_SOCKET_UNIT, socket_unit)
    _write_root_file(config.DASH_FORWARD_SERVICE_UNIT, service_unit)
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", "--now", f"{config.DASH_FORWARD_SERVICE}.socket"], sudo=True)
    ui.result("dashboard (Windows)", f"https://localhost:{config.HEAD_API_PORT}/")
    ui.say("  [muted]self-signed cert — click through the browser warning once[/muted]")


def make_user_dirs() -> None:
    """The operator's own ~/.ccc — settings + credentials only. No images/ or spawns/ here: Pod images
    live in the world-readable CCC_IMAGES_DIR (the home-less Mantle must read them) and Spawn results
    live on the Head, fetched over the API."""
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def verify() -> None:
    run(["sinfo", "-N", "-o", "%N %t %C %G"])


def _pick_coordinator_address() -> str:
    """First run, no --address: which of this box's addresses will Arms dial the coordinator at?

    `primary_ip()` answers a different question — where traffic leaves — and on a Head that is also on
    a tailnet it silently picks the LAN address, which excludes every remote Arm. Ask only when the
    choice is real: one candidate is not a decision, and a non-interactive run has no one to ask.

    The mesh's own addresses are never offered: an Arm dials this BEFORE it is on the mesh, so a mesh
    address is the one answer that cannot work.
    """
    default = system.primary_ip()
    mesh_net = ipaddress.ip_network(config.MESH_V4_PREFIX)
    others = [ip for ip in system.local_ipv4s()
              if ip != default and ipaddress.ip_address(ip) not in mesh_net]
    if not others or not ui.interactive():
        return default
    ui.say("  [muted]An Arm dials this before it is on the mesh, so it must be an address the "
           "donor's box can already reach.[/muted]")
    return ui.ask_select("Which address will Arms reach this Head's coordinator at?",
                         [default, *others], default=default)


def resolve_coordinator_address(address: str | None) -> str:
    """Settle the coordinator address, and confirm anything that would change it. No side effects.

    Called BEFORE the mesh step so its prompts are not buried inside a step frame, and so the operator
    answers before any daemon has been touched.

    The stored address outranks `primary_ip()`: this is a durable property of the Head, not a per-run
    argument. Without that fallback a bare `cuttle setup --head` — a routine re-run — silently reverts
    the coordinator to the LAN IP, invalidating every enrollment token and stranding every remote Arm.
    Observed 2026-07-28; that is the whole reason this function exists.
    """
    stored = settings.load().get("mesh", {})
    addr = address or stored.get("head_address") or _pick_coordinator_address()
    old = stored.get("coordinator")
    new = f"http://{addr}:{config.HEADSCALE_PORT}"

    # Re-pointing the coordinator is a cluster-wide event, not a config tweak, so it is confirmed
    # rather than announced. Unreachable unless --address names something new: an unflagged re-run now
    # resolves to the stored address and compares equal.
    if old and old != new and ui.interactive():
        ui.warn(f"this re-points the mesh: {old} -> {new}")
        ui.hint("Every Arm re-registers against the new address, and every enrollment token already "
                "handed out names the old one and will stop working.")
        if not ui.ask_confirm("Re-point the coordinator?", False):
            raise ui.abort("coordinator left unchanged")
    # Warned where the address is CHOSEN, not where a token later carries it — this is the last moment
    # it costs nothing to change. Never a refusal: a fleet of LAN donors is the ordinary case.
    if mesh.lan_only(addr):
        ui.warn(f"{addr} is an address only this LAN can reach.")
        ui.hint("Arms on other networks will hang at `setup --arm` until it times out. If any donor "
                "is remote, re-run with --address=<addr> — this box's Tailscale IP, say.")
    return addr


def mesh_up_head(addr: str) -> str:
    """Self-host Headscale + join our own tailnet. `addr` = the coordinator's advertised host, already
    settled by resolve_coordinator_address. Returns the Head's mesh IP.

    Two addresses, two jobs: `addr` is where Arms reach the coordinator *before* they're on the
    mesh (public/DDNS); the returned mesh IP is where they reach slurmctld *after*.
    """
    server_url = f"http://{addr}:{config.HEADSCALE_PORT}"

    old = settings.load().get("mesh", {}).get("coordinator")
    if old and old != server_url:
        print(f"cuttle: coordinator changed ({old} -> {server_url}); re-registering on the mesh")

    # NB: forgetting stale mesh state is mesh.up_head's job, not ours — a changed address is only one
    # of the ways the daemon's state goes stale, and it was never the dangerous one. See its docstring.
    hs_ver, mesh_ip = mesh.up_head(server_url, _write_root_file)
    # Persist for later Arm enrollment (join token) + reproducibility.
    data = settings.load()
    data["mesh"] = {"head_address": addr, "coordinator": server_url,
                    "headscale_version": hs_ver, "mesh_ip": mesh_ip}
    settings.save(data)
    print(f"cuttle: Head on mesh — mesh IP {mesh_ip}, coordinator {server_url}")
    return mesh_ip


def setup_head(address: str | None = None) -> None:
    with ui.section("Setting up the Head", lambda: "control plane + co-located Arm"):
        system.prime_sudo()   # sudo as the box's first row, before any step rows grow the frame
        _setup_head(address)


def _setup_head(address: str | None = None) -> None:
    make_user_dirs()
    settings.prompt_wandb()  # up front, so the rest runs unattended
    preflight("head")
    address = resolve_coordinator_address(address)  # may prompt — keep it out of the step frames
    # Mesh first, before any apt work: (a) slurm.conf can't be rendered without the Head's mesh IP,
    # (b) if a mesh change ever breaks host DNS, a re-run still reaches the code that repairs it
    # rather than dying at `add-apt-repository`.
    with ui.step("Joining the mesh"):
        mesh_ip = mesh_up_head(address)
        system.allow_mesh_iface(config.CCC_TS_TUN)
    with ui.step("Adding package repositories"):
        add_repos()
    with ui.step("Installing Slurm, munge and Apptainer"):
        install_packages()
    with ui.step("Configuring Slurm"):
        write_slurm_config(mesh_ip)
        start_daemons()
        make_registry_dir()
    with ui.step("Installing the Enroll API"):
        headapi.install_enroll(_write_root_file, mesh_ip)  # after munge: it serves munge.key
    with ui.step("Provisioning the Mantle and the auth store"):
        provision_head_auth()                    # ccc account + head.db + submission dirs (needs venv)
        enroll.register_head_arm(mesh_ip)        # our co-located Arm is admin-owned (needs the admin)
    with ui.step("Installing stage-in/out"):
        install_rsyncd(mesh_ip)                  # mesh-bound; needs ccc + its dirs first
    with ui.step("Building the Dashboard"):
        build_spa()                              # build + stage the Dashboard SPA (Node, Head-only)
    with ui.step("Issuing the Head certificate"):
        install_tls(mesh_ip)                     # must exist before uvicorn starts
    with ui.step("Starting the Head API and the Karma tick"):
        headapi.install_head(_write_root_file, mesh_ip)  # board + SPA + auth + submit, LAN+mesh
        headapi.install_karma(_write_root_file)  # ranks the queue by standing (same venv)
    with ui.step("Installing the nightly backup timer"):
        backup.install_timer(_write_root_file)   # head.db + arms.json + headscale db, see backup.py
    with ui.step("Installing the Head-box monitor"):
        monitor.install(_write_root_file)        # disk/services/rogue-usage, see monitor.py
        monitor.prompt_smtp(_write_root_file)     # SMTP creds for alerting; skippable
    with ui.step("Building the Pod image"):
        build_base_sif()                         # Mantle-readable, at the cluster-wide path
    # The Head's own Arm is an Arm like any other, so it is priced like one. Last, because the
    # benchmark is a real Spawn: it needs slurmd up, the Pod image built, and rsyncd listening.
    enroll.benchmark_arm(system.hostname())
    verify()
    lan = system.primary_ip()
    # Results, not `ui.kv` — kv writes straight to stdout, which inside a box is both uncounted (so
    # the erase lands in the wrong place) and unreadable by the time eleven phases have scrolled by.
    ui.result("dashboard (LAN)", headapi.head_url(lan))
    ui.result("dashboard (mesh)", headapi.head_url(mesh_ip))
    ui.result("coordinator", mesh_ip)
    ui.result("add an Arm", "cuttle arm authorize <name> --user <owner>")
    print("\ncuttle: the Head signs its own TLS certificate (no public domain = no CA can sign it).\n"
          "        BROWSER — the first visit warns 'your connection is not private'. That is EXPECTED.\n"
          "        Click Advanced -> Proceed; the browser remembers, and you see it once per browser.\n"
          "        CLI     — `cuttle login` shows the certificate's fingerprint once and asks you to\n"
          "        confirm it. After that it is pinned, and a changed certificate is refused.")


def point_at_head() -> str:
    """Ask which Head this box talks to, and record it — what makes the box **pointed**.

    A **baked** bundle already wrote this (build-installer bakes config.yaml + the matching cert pin),
    so the prompt arrives pre-filled and is one Enter. A **generic** bundle leaves the box knowing
    nothing, and this is the only place that asks: `auth.login` reads the stored URL and, finding
    none, can only abort telling you to pass `--head` — which is a usable message for a typed `cuttle
    login` but a dead end for the wizard that is supposed to be doing the asking.

    Accepts a bare host as well as a full URL: `--head` takes a URL because it is a flag, but a person
    answering "which Head?" types an address.
    """
    data = settings.load()
    # Re-ask on blank rather than storing it: the prompt is pre-filled with the baked URL, so an empty
    # answer means the field was cleared — and every branch below would turn "" into a syntactically
    # valid, permanently broken URL (`https://:8082`) that only surfaces at the next login.
    while True:
        answer = ui.ask_text("Head address?", default=data.get("head") or "").strip().rstrip("/")
        if answer:
            break
        ui.warn("a Head address is required — the host this cluster's Head API answers on")
    # A scheme means they gave a URL and we take it as given (including a deliberate http://). A bare
    # `host:port` only needs the scheme; a bare host gets the Head API's default form, port and all.
    if "://" in answer:
        url = answer
    elif ":" in answer:
        url = f"https://{answer}"
    else:
        url = headapi.head_url(answer)
    data["head"] = url
    settings.save(data)
    return url


def setup_client() -> None:
    """A submitter's box: point it at a Head, then log in. Nothing else.

    No apt, no mesh, no Slurm, no sudo — a client only ever talks to the Head API over HTTP. Kept
    separate from `setup --head/--arm` precisely so that submitting from a laptop never requires
    provisioning it.
    """
    pointed: dict[str, str] = {}
    with ui.section("Set up this client", lambda: pointed.get("head", "")):
        make_user_dirs()
        pointed["head"] = point_at_head()
    who: dict[str, str] = {}
    with ui.section("Log in", lambda: who.get("as", "")):
        who["as"] = auth.login(None)
    ui.ok("Client ready. `cuttle run <script> --time <min> --vram <GB>` to submit.")


# `--local` is just the single-box Head (Head + its co-located Arm).
setup_local = setup_head


def setup_arm(token: str, re_enroll: bool = False) -> None:
    """Enroll this box as an Arm, redeeming the **enrollment token** an Admin minted for it.

    The donor declares nothing. The token names the Arm and carries the one-time preauth key that
    admits it to the mesh — so there is no node-name prompt and no waiting for a human to approve a
    relayed auth-id. The Admin already decided, when they ran `cuttle arm authorize`.

    `re_enroll` forces a fresh mesh identity even if this box looks already-joined.
    """
    with ui.section("Setting up this Arm", lambda: "enrolled"):
        system.prime_sudo()   # sudo as the box's first row, before any step rows grow the frame
        _setup_arm(token, re_enroll=re_enroll)


def _setup_arm(token: str, re_enroll: bool = False) -> None:
    make_user_dirs()
    t = enroll.decode_token(token)
    node_name = t["node_name"]
    settings.prompt_wandb()
    preflight("arm")

    with ui.step("Joining the mesh"):
        mesh_ip = mesh.up_arm(t["coordinator"], node_name, t["authkey"], _write_root_file,
                              re_enroll=re_enroll)
        system.allow_mesh_iface(config.CCC_TS_TUN)  # before slurmd: slurmctld dials us on 6818
    ui.say(f"  [muted]on the mesh as {node_name} — {mesh_ip}[/muted]")

    # Enroll IMMEDIATELY, before the long apt run. Two reasons, both about timing:
    #   1. `cuttle arm authorize` is blocked on the Head waiting for exactly this POST. Doing it after
    #      apt would hold the Admin's terminal hostage for our entire package install.
    #   2. If the Head is unroutable over the mesh, we learn now — not five minutes into an apt we
    #      would then have to throw away.
    # The POST needs nothing but the mesh (gpu_device() is a path check). It reports our GPU device —
    # /dev/nvidia0 natively, /dev/dxg under WSL2 — which the Head needs to author our gres.conf line,
    # and returns the munge key. We hold that key in memory: writing it needs the `munge` user, which
    # apt has not created yet.
    with ui.step("Enrolling with the Head"):
        info = enroll.fetch_enrollment(t["mesh_ip"], node_name, mesh_ip)

    # Mesh admission (the token) and Slurm admission are separate. The Admin's blocked `arm authorize`
    # sees our /enroll above and writes our gres.conf line within seconds — but if they weren't at
    # their desk and it timed out, they finish with `cuttle arm admit`. Resolved here, before the apt/
    # build block below, so that block is never interleaved with a wait on the Head.
    if not info["admitted"]:
        enroll.wait_for_admit(t["mesh_ip"], node_name)

    with ui.step("Adding package repositories"):
        add_repos()
    with ui.step("Installing Slurm, munge and Apptainer"):
        install_packages(config.SLURMD_PKG)
        if system.is_wsl():
            configure_apptainer_wsl()  # after apptainer is installed, before base.sif is built
        enroll.write_munge_key(info["munge_key"])  # now that `munge` exists to own it

    data = settings.load()
    data["arm"] = {"node_name": node_name, "mesh_ip": mesh_ip, "head_mesh_ip": t["mesh_ip"],
                   "ctld_addr": info["ctld_addr"], "gpu_device": enroll.gpu_device()}
    # Point the box at the Head over the MESH — an Arm's owner is a User too, and until now enrolling
    # left them with no Head URL at all, so `cuttle login` on their own Arm said "no Head URL set".
    #
    # The mesh address, not whatever was here before: a remote Arm is off the Head's LAN by definition,
    # and a bundle built on the Head bakes that LAN IP (installer.py). Overwriting is the point — the
    # baked address is the one that cannot work from here, and the mesh IP is in the cert's SANs, so
    # `login` can pin it. On the co-located/LAN case the mesh address works just as well.
    data["head"] = headapi.head_url(t["mesh_ip"])
    settings.save(data)

    # The Mantle must exist here BEFORE slurmd can accept a Spawn: slurmd setuid()s to the submitting
    # uid, which is the Head API's pinned CCC_UID. Without this account a Spawn scheduled onto this Arm
    # would run as an unknown numeric uid. Its Pod image must be readable by it, at the same path the
    # Head renders into the job script.
    with ui.step("Provisioning the Mantle"):
        provision_mantle()
    with ui.step("Building the Pod image"):
        build_base_sif()

    # No slurm.conf here — slurmd fetches it from slurmctld (configless) and self-registers (-Z).
    with ui.step("Starting slurmd"):
        _start_slurmd(node_name, info["ctld_addr"])
    with ui.step("Checking the Head can reach us"):
        # Only now is there something on 6818 to reach. Raises if the Head can't get back to us.
        enroll.check_head_can_reach_us(t["mesh_ip"])

    if system.is_wsl():
        with ui.step("Forwarding the Dashboard to Windows"):
            install_wsl_dashboard_forward(t["mesh_ip"])  # browse the Head via localhost

    ui.result("arm", node_name)
    ui.result("mesh ip", mesh_ip)
    ui.result("check it", "run `sinfo` on the Head")


def _start_slurmd(node_name: str, ctld_addr: str) -> None:
    for d, owner in ((config.SLURM_LOG_DIR, "slurm"), (config.SLURMD_SPOOL_DIR, "root")):
        run(["install", "-d", "-o", owner, "-g", owner, str(d)], sudo=True)
    # Configless serves the conf (which names Epilog=) but NOT the script it points at — that must be on
    # the node, written before slurmd starts, or the first Spawn's epilog fails and drains this Arm.
    _write_root_file(config.SLURM_EPILOG, templates.render_epilog(), mode="755")
    _write_root_file(config.SLURMD_DROPIN,
                     enroll.slurmd_dropin(node_name, ctld_addr, system.gpu_vram_mb()))
    run(["systemctl", "daemon-reload"], sudo=True)  # the drop-in rewrites slurmd's ExecStart
    run(["systemctl", "enable", "slurmd"], sudo=True)
    run(["systemctl", "restart", "slurmd"], sudo=True)
