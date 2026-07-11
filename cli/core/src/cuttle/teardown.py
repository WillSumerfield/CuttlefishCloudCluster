"""`cuttle teardown` — reverse `setup`. `--local` (Head+Arm) or `--arm` (remote Arm).

Default: full uninstall (daemons + packages + repos + Slurm config/state), but keep ~/.ccc
(preserves the multi-GB base.sif). Flags: --keep-packages (leave apt pkgs + repos),
--all (also remove ~/.ccc). Tolerant of partial state — safe to re-run.
"""

import shutil

from . import config, system
from .system import run

_SERVICES = [config.HEAD_API_SERVICE, config.DASHBOARD_SERVICE, config.RSYNCD_SERVICE,
             "munge", "slurmctld", "slurmd"]
_ARM_SERVICES = ["slurmd", "munge"]  # an Arm runs no slurmctld / head-api / rsyncd / headscale
_PACKAGES = ["slurm-wlm", "slurmd", "apptainer", "nvidia-container-toolkit", "munge"]
_NVIDIA_REPO_FILES = [
    "/etc/apt/sources.list.d/nvidia-container-toolkit.list",
    "/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg",
]


def stop_daemons() -> None:
    run(["systemctl", "disable", "--now", *_SERVICES], sudo=True, check=False)


def _leave_ccc_mesh() -> None:
    """Depart the mesh and remove CCC's OWN isolated tailscaled instance (targeted by socket) —
    NEVER the default/personal tailscaled or the shared `tailscale` package, since both Head and Arm
    may be administered over their personal tailnet. Shared by the Head and Arm teardowns."""
    # Drop the ufw rule setup added, before ccc0 disappears. Inert afterwards, but setup's mutations
    # are reversed exactly, the same reason purge_packages works off a recorded manifest.
    system.deny_mesh_iface(config.CCC_TS_TUN)
    run(["tailscale", f"--socket={config.CCC_TS_SOCKET}", "down"], sudo=True, check=False)
    run(["systemctl", "disable", "--now", config.CCC_TS_SERVICE], sudo=True, check=False)
    run(["rm", "-f", str(config.CCC_TS_UNIT)], sudo=True, check=False)
    run(["rm", "-rf", str(config.CCC_TS_STATEDIR)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_mesh(keep_packages: bool = False) -> None:
    """Head mesh teardown: leave the mesh (client) AND tear down the Headscale coordinator (CCC-only,
    so safe to purge). The Arm path calls `_leave_ccc_mesh` only — it runs no Headscale."""
    _leave_ccc_mesh()
    run(["systemctl", "disable", "--now", "headscale"], sudo=True, check=False)
    run(["rm", "-rf", str(config.HEADSCALE_CONF.parent), str(config.HEADSCALE_LIB)],
        sudo=True, check=False)
    if not keep_packages:
        run(["apt-get", "purge", "-y", "headscale"], sudo=True, check=False)
        # NOTE: `tailscale` package + repo left in place — shared with the personal daemon.


def remove_head_api() -> None:
    run(["rm", "-f", str(config.HEAD_API_UNIT)], sudo=True, check=False)
    run(["rm", "-rf", str(config.HEAD_API_VENV.parent), str(config.CCC_LIB)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_dashboard() -> None:
    """Dashboard service (Head-only, CCC-only). Drop its ufw port rule, unit, and the staged SPA.
    Already stopped/disabled by stop_daemons."""
    system.deny_tcp_port(config.DASHBOARD_PORT)
    run(["rm", "-f", str(config.DASHBOARD_UNIT)], sudo=True, check=False)
    run(["rm", "-rf", str(config.DASHBOARD_WEB_DIR.parent)], sudo=True, check=False)  # /opt/ccc/dashboard
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_rsyncd() -> None:
    """Stage-in/out daemon (Head-only, CCC-only — safe to remove wholesale). The service was already
    stopped/disabled by stop_daemons; drop its unit + config."""
    run(["rm", "-f", str(config.RSYNCD_UNIT), str(config.RSYNCD_CONF)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_slurm_config() -> None:
    run(["rm", "-rf",
         str(config.SLURM_CONF.parent),  # /etc/slurm
         str(config.SLURM_STATE_DIR),
         str(config.SLURMD_SPOOL_DIR),
         str(config.SLURM_LOG_DIR)],
        sudo=True, check=False)
    # Arm-only: the configless `-Z --conf-server` flags.
    run(["rm", "-f", str(config.SLURMD_DEFAULTS)], sudo=True, check=False)


def purge_packages() -> None:
    # Purge ONLY what setup recorded as newly installed — never pre-existing packages, and no
    # global `autoremove` (which would sweep unrelated system orphans, e.g. nvidia driver bits).
    if config.APT_MANIFEST.exists():
        pkgs = config.APT_MANIFEST.read_text().split()
        if not pkgs:
            print("apt manifest empty — nothing setup installed; skipping purge")
            return
    else:
        pkgs = _PACKAGES
        print("no apt manifest (pre-manifest setup?) — purging only the 4 explicit packages, "
              "no autoremove; run `setup --local` once to record a manifest for exact teardown")
    run(["apt-get", "purge", "-y", *pkgs], sudo=True, check=False)


def remove_repos() -> None:
    run(["add-apt-repository", "-y", "--remove", "ppa:apptainer/ppa"], sudo=True, check=False)
    run(["rm", "-f", *_NVIDIA_REPO_FILES], sudo=True, check=False)


def remove_user_dirs() -> None:
    shutil.rmtree(config.CCC_HOME, ignore_errors=True)


# --- Arm-only teardown -------------------------------------------------------

def stop_arm_daemons() -> None:
    run(["systemctl", "disable", "--now", *_ARM_SERVICES], sudo=True, check=False)


def undo_apptainer_wsl() -> None:
    """Reverse `setup.configure_apptainer_wsl` — strip the two additive lines. Only meaningful on a
    WSL Arm (guarded), so a native-Linux Arm's apptainer.conf is never touched. Idempotent: the sed
    deletes are no-ops if the lines are already gone."""
    if not system.is_wsl():
        return
    # `\#...#` picks # as the address delimiter so the /usr/lib/wsl path needs no escaping.
    run(["sed", "-i", f"\\#^bind path = {config.WSL_LIB_ROOT}$#d", str(config.APPTAINER_CONF)],
        sudo=True, check=False)
    run(["sed", "-i", f"/^{config.WSL_EXTRA_NVLIB}$/d", str(config.APPTAINER_NVLIBLIST)],
        sudo=True, check=False)
    print(f"cuttle: reverted WSL Apptainer config ({config.WSL_LIB_ROOT} bind + "
          f"{config.WSL_EXTRA_NVLIB})")


def remove_wsl_dashboard_forward() -> None:
    """Reverse install_wsl_dashboard_forward. Tolerant of partial state (a native Arm never had it,
    so these are harmless no-ops) — no is_wsl guard needed, matching the rest of teardown."""
    run(["systemctl", "disable", "--now",
         f"{config.DASH_FORWARD_SERVICE}.socket", f"{config.DASH_FORWARD_SERVICE}.service"],
        sudo=True, check=False)
    run(["rm", "-f", str(config.DASH_FORWARD_SOCKET_UNIT), str(config.DASH_FORWARD_SERVICE_UNIT)],
        sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)


def remove_munge_key() -> None:
    """The cluster-wide shared secret this Arm was seeded with. Purging the munge package would drop
    it too, but --keep-packages must not leave a live key behind on a departed Arm."""
    run(["rm", "-f", str(config.MUNGE_KEY)], sudo=True, check=False)


def teardown_arm(keep_packages: bool = False, all_: bool = False) -> None:
    """Reverse `setup --arm`. Leaves the mesh and wipes local state, so the Arm's Headscale node goes
    stale on the Head — reaped on next `cuttle arm approve` (see enroll.approve_arm), or now with
    `cuttle arm remove <name>`. No Headscale/head-api/rsyncd here; an Arm runs none."""
    stop_arm_daemons()
    _leave_ccc_mesh()
    remove_slurm_config()
    undo_apptainer_wsl()
    remove_wsl_dashboard_forward()
    remove_munge_key()
    if not keep_packages:
        purge_packages()
        remove_repos()
    if all_:
        remove_user_dirs()
    print("\ncuttle: Arm torn down (left the mesh; its Head-side node is now stale — reap with "
          "`cuttle arm remove <name>` or let re-enrollment do it)"
          + (" (packages/repos kept)" if keep_packages else "")
          + (" (~/.ccc removed)" if all_ else " (~/.ccc kept)"))


def teardown_local(keep_packages: bool = False, all_: bool = False) -> None:
    stop_daemons()
    remove_head_api()
    remove_dashboard()
    remove_rsyncd()
    remove_mesh(keep_packages=keep_packages)
    remove_slurm_config()
    if not keep_packages:
        purge_packages()
        remove_repos()
    if all_:
        remove_user_dirs()
    print("\ncuttle: local cluster torn down"
          + (" (packages/repos kept)" if keep_packages else "")
          + (" (~/.ccc removed)" if all_ else " (~/.ccc kept)"))
