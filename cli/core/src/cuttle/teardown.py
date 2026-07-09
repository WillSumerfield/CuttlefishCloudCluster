"""`cuttle teardown --local` — reverse `setup --local`.

Default: full uninstall (daemons + packages + repos + Slurm config/state), but keep ~/.ccc
(preserves the multi-GB base.sif). Flags: --keep-packages (leave apt pkgs + repos),
--all (also remove ~/.ccc). Tolerant of partial state — safe to re-run.
"""

import shutil

from . import config
from .system import run

_SERVICES = ["munge", "slurmctld", "slurmd"]
_PACKAGES = ["slurm-wlm", "apptainer", "nvidia-container-toolkit", "munge"]
_NVIDIA_REPO_FILES = [
    "/etc/apt/sources.list.d/nvidia-container-toolkit.list",
    "/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg",
]


def stop_daemons() -> None:
    run(["systemctl", "disable", "--now", *_SERVICES], sudo=True, check=False)


def remove_mesh(keep_packages: bool = False) -> None:
    """Unwind the CCC mesh layer. Only ever touches CCC's OWN isolated tailscaled instance +
    Headscale — never the default/personal tailscaled or the shared `tailscale` package (the box is
    administered over its personal tailnet). Headscale is CCC-only, so it's safe to purge."""
    # CCC's isolated tailscaled instance (targeted by socket) — personal daemon left running.
    run(["tailscale", f"--socket={config.CCC_TS_SOCKET}", "down"], sudo=True, check=False)
    run(["systemctl", "disable", "--now", config.CCC_TS_SERVICE], sudo=True, check=False)
    run(["rm", "-f", str(config.CCC_TS_UNIT)], sudo=True, check=False)
    run(["rm", "-rf", str(config.CCC_TS_STATEDIR)], sudo=True, check=False)
    run(["systemctl", "daemon-reload"], sudo=True, check=False)
    # Headscale coordinator (CCC-only).
    run(["systemctl", "disable", "--now", "headscale"], sudo=True, check=False)
    run(["rm", "-rf", str(config.HEADSCALE_CONF.parent), str(config.HEADSCALE_LIB)],
        sudo=True, check=False)
    if not keep_packages:
        run(["apt-get", "purge", "-y", "headscale"], sudo=True, check=False)
        # NOTE: `tailscale` package + repo left in place — shared with the personal daemon.


def remove_slurm_config() -> None:
    run(["rm", "-rf",
         str(config.SLURM_CONF.parent),  # /etc/slurm
         str(config.SLURM_STATE_DIR),
         str(config.SLURMD_SPOOL_DIR),
         str(config.SLURM_LOG_DIR)],
        sudo=True, check=False)


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


def teardown_local(keep_packages: bool = False, all_: bool = False) -> None:
    stop_daemons()
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
