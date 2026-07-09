"""`cuttle setup` — provision a node in one of two roles, on a native-Linux box.

`--head`: self-hosts the Headscale coordinator + the Head API, runs slurmctld, and carries a
co-located Arm as a *static* Slurm node. `--local` is an alias (a single box is just a Head).

`--arm`: joins an existing cluster with a join token, pulls the munge key from the Head API over the
mesh, and registers as a *dynamic* Slurm node (`slurmd -Z`) fetching its config from slurmctld.

Drives apt+sudo directly. Intended path only — no fallbacks (test box; failures decided live).
"""

import tempfile
from pathlib import Path

from . import config, enroll, headapi, mesh, settings, system, templates
from .system import run, sh


def _write_root_file(path: Path, content: str, mode: str = "644") -> None:
    """Write `content` to a root-owned path via a temp file + sudo install."""
    with tempfile.NamedTemporaryFile("w", suffix=".ccc", delete=False) as f:
        f.write(content)
        tmp = f.name
    run(["install", "-D", "-m", mode, tmp, str(path)], sudo=True)


def preflight() -> None:
    rel = system.os_release()
    print(f"host={system.hostname()} os={rel.get('PRETTY_NAME')} "
          f"cpus={system.cpu_count()} mem={system.real_memory_mb()}MB")
    assert system.is_native_linux_x86(), "MVP supports native Linux x86_64 only"
    assert rel.get("VERSION_ID") == "24.04", "MVP pinned to Ubuntu 24.04"
    run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])


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
    # The Head plus any Arms already admitted (a re-run must not un-admit them).
    reg = enroll.load_registry()
    nodes = [templates.head_gres_node()]
    nodes += [(n, e["gpu_device"]) for n, e in sorted(reg.items()) if e["admitted"]]
    _write_root_file(config.GRES_CONF, templates.render_gres_conf(nodes))


def make_registry_dir() -> None:
    """The API's only writable location — it runs as munge and owns nothing else."""
    run(["install", "-d", "-m", "0755", "-o", config.HEAD_API_USER, "-g", config.HEAD_API_USER,
         str(config.CCC_LIB)], sudo=True)


def start_daemons() -> None:
    # enable = start on boot; restart = start if down, else reload new config (idempotent re-run).
    run(["systemctl", "enable", "munge", "slurmctld", "slurmd"], sudo=True)
    run(["systemctl", "restart", "munge", "slurmctld", "slurmd"], sudo=True)


def build_base_sif() -> None:
    config.IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    if config.BASE_SIF.exists():
        print(f"base.sif exists ({config.BASE_SIF}) — skipping build")
        return
    def_file = config.IMAGES_DIR / "base.def"
    def_file.write_text(templates.render_base_def())
    run(["apptainer", "build", str(config.BASE_SIF), str(def_file)])


def make_user_dirs() -> None:
    for d in (config.IMAGES_DIR, config.SPAWNS_DIR, config.CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)


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

    # Re-pointing at a different coordinator requires forgetting the old one first, or `tailscale up`
    # hangs forcing HTTPS:443 at a plain-HTTP Headscale. See mesh.forget_state().
    old = settings.load().get("mesh", {}).get("coordinator")
    if old and old != server_url:
        print(f"cuttle: coordinator changed ({old} -> {server_url}); re-registering on the mesh")
        mesh.forget_state()

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
    preflight()
    # Mesh first, before any apt work: (a) slurm.conf can't be rendered without the Head's mesh IP,
    # (b) if a mesh change ever breaks host DNS, a re-run still reaches the code that repairs it
    # rather than dying at `add-apt-repository`.
    mesh_ip = mesh_up_head(address)
    add_repos()
    install_packages()
    write_slurm_config(mesh_ip)
    start_daemons()
    make_registry_dir()
    headapi.install(_write_root_file, mesh_ip)  # after munge: the API serves munge.key
    build_base_sif()
    verify()
    print("\ncuttle: Head up (control plane + co-located Arm). Node should read 'idle' above.")
    print(f"cuttle: mint an Arm's join token with `cuttle token` (coordinator {mesh_ip})")


# `--local` is just the single-box Head (Head + its co-located Arm).
setup_local = setup_head


def prompt_node_name() -> str:
    """Required, no default. The Arm's identity in both Slurm and the mesh, fixed thereafter.
    A collision with an existing Arm is rejected by slurmctld at registration."""
    name = input("Node name for this Arm (unique in the cluster): ").strip()
    assert name, "a node name is required"
    return name


def setup_arm(invite: str) -> None:
    """Enroll this box as an Arm of the cluster named by `invite`.

    Order is the security model. We ask to join the mesh and BLOCK until an operator approves; only
    then can we route to the Head API to collect the munge key. No secret ever crosses the internet:
    the invite is public, and the auth-id we print authorizes nothing by itself.
    """
    make_user_dirs()
    node_name = prompt_node_name()
    settings.prompt_wandb()
    preflight()

    t = enroll.decode_invite(invite)
    mesh_ip = mesh.up_arm(t["coordinator"], node_name, _write_root_file)  # blocks on approval
    print(f"cuttle: Arm on mesh as {node_name} — mesh IP {mesh_ip}")

    add_repos()
    install_packages(config.SLURMD_PKG)

    # Now on the mesh: the Head API is routable, so the munge key can be fetched. This POST also
    # records our GPU device — /dev/nvidia0 natively, /dev/dxg under WSL2 — which the Head needs to
    # author our gres.conf line.
    info = enroll.fetch_enrollment(t["mesh_ip"], node_name, mesh_ip)
    enroll.write_munge_key(info["munge_key"])

    data = settings.load()
    data["arm"] = {"node_name": node_name, "mesh_ip": mesh_ip, "head_mesh_ip": t["mesh_ip"],
                   "ctld_addr": info["ctld_addr"], "gpu_device": enroll.gpu_device()}
    settings.save(data)

    build_base_sif()

    # Mesh admission (above) and Slurm admission are separate: `cuttle arm approve` grants the first
    # immediately, then waits for this POST before writing our gres.conf line. Starting slurmd early
    # would register a node the Head has no GRES for.
    if not info["admitted"]:
        enroll.wait_for_admit(t["mesh_ip"], node_name)

    # No slurm.conf here — slurmd fetches it from slurmctld (configless) and self-registers (-Z).
    for d, owner in ((config.SLURM_LOG_DIR, "slurm"), (config.SLURMD_SPOOL_DIR, "root")):
        run(["install", "-d", "-o", owner, "-g", owner, str(d)], sudo=True)
    _write_root_file(config.SLURMD_DEFAULTS, enroll.slurmd_defaults(node_name, info["ctld_addr"]))
    run(["systemctl", "enable", "slurmd"], sudo=True)
    run(["systemctl", "restart", "slurmd"], sudo=True)

    print(f"\ncuttle: Arm '{node_name}' enrolled. Check `sinfo` on the Head.")
