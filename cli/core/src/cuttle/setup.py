"""`cuttle setup` — provision a node in one of two roles, on a native-Linux box.

`--head`: self-hosts the Headscale coordinator + the Head API, runs slurmctld, and carries a
co-located Arm as a *static* Slurm node. `--local` is an alias (a single box is just a Head).

`--arm`: joins an existing cluster with a join token, pulls the munge key from the Head API over the
mesh, and registers as a *dynamic* Slurm node (`slurmd -Z`) fetching its config from slurmctld.

Drives apt+sudo directly. Intended path only — no fallbacks (test box; failures decided live).
"""

import getpass
import tempfile
from pathlib import Path

from . import auth, config, enroll, headapi, mesh, settings, system, templates
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
    print(f"host={system.hostname()} os={rel.get('PRETTY_NAME')} "
          f"cpus={system.cpu_count()} mem={system.real_memory_mb()}MB")
    assert system.is_native_linux_x86(), "MVP supports Linux x86_64 only"
    assert rel.get("VERSION_ID") == "24.04", "MVP pinned to Ubuntu 24.04"
    if system.is_wsl():
        assert role == "arm", "WSL2 can host an Arm, but not the Head (headscale/slurmctld unproven)"
        print("cuttle: WSL2 detected. Supported as an Arm, with one permanent limit: /dev/dxg is a\n"
              "        single node for ALL GPUs, so Slurm can never isolate GPUs per-Spawn here.")
    run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])


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
    print(f"cuttle: WSL Arm — Apptainer now binds {config.WSL_LIB_ROOT} and injects "
          f"{config.WSL_EXTRA_NVLIB}; `--nv` behaves as on native Linux")


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
    print(f"recorded {len(added)} newly-installed packages → {config.APT_MANIFEST}")


def write_slurm_config(mesh_ip: str) -> None:
    for d, owner in ((config.SLURM_STATE_DIR, "slurm"),
                     (config.SLURM_LOG_DIR, "slurm"),
                     (config.SLURMD_SPOOL_DIR, "root")):
        run(["install", "-d", "-o", owner, "-g", owner, str(d)], sudo=True)
    _write_root_file(config.SLURM_CONF, templates.render_slurm_conf(mesh_ip))
    # The Head plus any Arms already admitted (a re-run must not un-admit them). The Head's own row is
    # skipped: head_gres_node() already supplies its line (see enroll.render_gres).
    reg = enroll.load_registry()
    nodes = [templates.head_gres_node()]
    nodes += [(n, e["gpu_device"]) for n, e in sorted(reg.items())
              if e["admitted"] and not e.get("head")]
    _write_root_file(config.GRES_CONF, templates.render_gres_conf(nodes))


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
    auth.seed_admin(admin_user)  # prints a one-time temp password (or 'already exists' on re-run)
    print(f"cuttle: auth store {config.HEAD_DB}; first admin '{admin_user}' — log in with "
          f"`cuttle login --head <url>` and change the temp password shown above")


def start_daemons() -> None:
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
        print(f"base.sif exists ({config.CCC_SIF}) — skipping build")
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
    print(f"cuttle: WSL dashboard forward up — open https://localhost:{config.HEAD_API_PORT}/ "
          f"in your Windows browser (self-signed cert: click through the warning once)")


def make_user_dirs() -> None:
    """The operator's own ~/.ccc — settings + credentials only. No images/ or spawns/ here: Pod images
    live in the world-readable CCC_IMAGES_DIR (the home-less Mantle must read them) and Spawn results
    live on the Head, fetched over the API."""
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def verify() -> None:
    run(["sinfo", "-N", "-o", "%N %t %C %G"])


def mesh_up_head(address: str | None) -> str:
    """Self-host Headscale + join our own tailnet. `address` = coordinator's advertised host
    (default: primary outbound IP; pass a public/DDNS addr so remote Arms can reach it).
    Returns the Head's mesh IP.

    Two addresses, two jobs: `address` is where Arms reach the coordinator *before* they're on the
    mesh (public/DDNS); the returned mesh IP is where they reach slurmctld *after*.
    """
    addr = address or system.primary_ip()
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
    make_user_dirs()
    settings.prompt_wandb()  # up front, so the rest runs unattended
    preflight("head")
    # Mesh first, before any apt work: (a) slurm.conf can't be rendered without the Head's mesh IP,
    # (b) if a mesh change ever breaks host DNS, a re-run still reaches the code that repairs it
    # rather than dying at `add-apt-repository`.
    mesh_ip = mesh_up_head(address)
    system.allow_mesh_iface(config.CCC_TS_TUN)
    add_repos()
    install_packages()
    write_slurm_config(mesh_ip)
    start_daemons()
    make_registry_dir()
    headapi.install_enroll(_write_root_file, mesh_ip)  # after munge: the Enroll API serves munge.key
    provision_head_auth()                        # ccc account + head.db + submission dirs (needs venv)
    enroll.register_head_arm(mesh_ip)            # our co-located Arm is admin-owned (needs the admin)
    install_rsyncd(mesh_ip)                      # stage-in/out, mesh-bound; needs ccc + its dirs first
    build_spa()                                  # build + stage the Dashboard SPA (Node, Head-only)
    install_tls(mesh_ip)                         # self-signed cert; must exist before uvicorn starts
    headapi.install_head(_write_root_file, mesh_ip)  # Head API (board + SPA + auth + submit) LAN+mesh
    headapi.install_karma(_write_root_file)      # Karma tick — ranks the queue by standing (same venv)
    build_base_sif()                             # Mantle-readable Pod image at the cluster-wide path
    # The Head's own Arm is an Arm like any other, so it is priced like one. Last, because the
    # benchmark is a real Spawn: it needs slurmd up, the Pod image built, and rsyncd listening.
    enroll.benchmark_arm(system.hostname())
    verify()
    lan = system.primary_ip()
    print("\ncuttle: Head up (control plane + co-located Arm). Node should read 'idle' above.")
    print(f"cuttle: add an Arm with `cuttle arm authorize <name> --user <owner>` "
          f"(coordinator {mesh_ip})")
    print(f"cuttle: dashboard at {headapi.head_url(lan)} (LAN) / "
          f"{headapi.head_url(mesh_ip)} (mesh)")
    print("\ncuttle: the Head signs its own TLS certificate (no public domain = no CA can sign it).\n"
          "        BROWSER — the first visit warns 'your connection is not private'. That is EXPECTED.\n"
          "        Click Advanced -> Proceed; the browser remembers, and you see it once per browser.\n"
          "        CLI     — `cuttle login` shows the certificate's fingerprint once and asks you to\n"
          "        confirm it. After that it is pinned, and a changed certificate is refused.")


# `--local` is just the single-box Head (Head + its co-located Arm).
setup_local = setup_head


def setup_arm(token: str, re_enroll: bool = False) -> None:
    """Enroll this box as an Arm, redeeming the **enrollment token** an Admin minted for it.

    The donor declares nothing. The token names the Arm and carries the one-time preauth key that
    admits it to the mesh — so there is no node-name prompt and no waiting for a human to approve a
    relayed auth-id. The Admin already decided, when they ran `cuttle arm authorize`.

    `re_enroll` forces a fresh mesh identity even if this box looks already-joined.
    """
    make_user_dirs()
    t = enroll.decode_token(token)
    node_name = t["node_name"]
    settings.prompt_wandb()
    preflight("arm")

    mesh_ip = mesh.up_arm(t["coordinator"], node_name, t["authkey"], _write_root_file,
                          re_enroll=re_enroll)
    print(f"cuttle: Arm on mesh as {node_name} — mesh IP {mesh_ip}")
    system.allow_mesh_iface(config.CCC_TS_TUN)  # before slurmd: slurmctld dials us on 6818

    # Enroll IMMEDIATELY, before the long apt run. Two reasons, both about timing:
    #   1. `cuttle arm authorize` is blocked on the Head waiting for exactly this POST. Doing it after
    #      apt would hold the Admin's terminal hostage for our entire package install.
    #   2. If the Head is unroutable over the mesh, we learn now — not five minutes into an apt we
    #      would then have to throw away.
    # The POST needs nothing but the mesh (gpu_device() is a path check). It reports our GPU device —
    # /dev/nvidia0 natively, /dev/dxg under WSL2 — which the Head needs to author our gres.conf line,
    # and returns the munge key. We hold that key in memory: writing it needs the `munge` user, which
    # apt has not created yet.
    info = enroll.fetch_enrollment(t["mesh_ip"], node_name, mesh_ip)

    add_repos()
    install_packages(config.SLURMD_PKG)
    if system.is_wsl():
        configure_apptainer_wsl()  # after apptainer is installed, before base.sif is built

    enroll.write_munge_key(info["munge_key"])  # now that `munge` exists to own it

    data = settings.load()
    data["arm"] = {"node_name": node_name, "mesh_ip": mesh_ip, "head_mesh_ip": t["mesh_ip"],
                   "ctld_addr": info["ctld_addr"], "gpu_device": enroll.gpu_device()}
    settings.save(data)

    # The Mantle must exist here BEFORE slurmd can accept a Spawn: slurmd setuid()s to the submitting
    # uid, which is the Head API's pinned CCC_UID. Without this account a Spawn scheduled onto this Arm
    # would run as an unknown numeric uid. Its Pod image must be readable by it, at the same path the
    # Head renders into the job script.
    provision_mantle()
    build_base_sif()

    # Mesh admission (the token) and Slurm admission are separate. The Admin's blocked `arm authorize`
    # sees our /enroll above and writes our gres.conf line within seconds — but if they weren't at
    # their desk and it timed out, they finish with `cuttle arm admit`. Either way, starting slurmd
    # before that lands would register a node the Head has no GRES for.
    if not info["admitted"]:
        enroll.wait_for_admit(t["mesh_ip"], node_name)

    # No slurm.conf here — slurmd fetches it from slurmctld (configless) and self-registers (-Z).
    for d, owner in ((config.SLURM_LOG_DIR, "slurm"), (config.SLURMD_SPOOL_DIR, "root")):
        run(["install", "-d", "-o", owner, "-g", owner, str(d)], sudo=True)
    _write_root_file(config.SLURMD_DROPIN,
                     enroll.slurmd_dropin(node_name, info["ctld_addr"], system.gpu_vram_mb()))
    run(["systemctl", "daemon-reload"], sudo=True)  # the drop-in rewrites slurmd's ExecStart
    run(["systemctl", "enable", "slurmd"], sudo=True)
    run(["systemctl", "restart", "slurmd"], sudo=True)

    # Only now is there something on 6818 to reach. Raises if the Head can't get back to us.
    enroll.check_head_can_reach_us(t["mesh_ip"])

    if system.is_wsl():
        install_wsl_dashboard_forward(t["mesh_ip"])  # let Windows browse the dashboard via localhost

    print(f"\ncuttle: Arm '{node_name}' enrolled. Check `sinfo` on the Head.")
