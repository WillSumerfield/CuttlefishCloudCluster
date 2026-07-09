"""`cuttle setup --local` — provision a single native-Linux box (Head+Arm combined).

Drives apt+sudo directly, renders Slurm config, builds the base Pod image, starts daemons.
Intended path only — no fallbacks (this is a test box; failures decided live).
"""

import tempfile
from pathlib import Path

from . import config, settings, system, templates
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


def install_packages() -> None:
    run(["apt-get", "update"], sudo=True)
    before = system.installed_packages()
    run(
        ["apt-get", "install", "-y",
         config.SLURM_PKG, config.APPTAINER_PKG, config.NVIDIA_CTK_PKG, config.MUNGE_PKG],
        sudo=True,
    )
    # Record exactly what we added (incl. pulled deps) so teardown removes only these.
    added = sorted(system.installed_packages() - before)
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.APT_MANIFEST.write_text("\n".join(added) + "\n" if added else "")
    print(f"recorded {len(added)} newly-installed packages → {config.APT_MANIFEST}")


def write_slurm_config() -> None:
    for d, owner in ((config.SLURM_STATE_DIR, "slurm"),
                     (config.SLURM_LOG_DIR, "slurm"),
                     (config.SLURMD_SPOOL_DIR, "root")):
        run(["install", "-d", "-o", owner, "-g", owner, str(d)], sudo=True)
    _write_root_file(config.SLURM_CONF, templates.render_slurm_conf())
    _write_root_file(config.GRES_CONF, templates.render_gres_conf())


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


def setup_local() -> None:
    make_user_dirs()
    settings.prompt_wandb()  # up front, so the rest runs unattended
    preflight()
    add_repos()
    install_packages()
    write_slurm_config()
    start_daemons()
    build_base_sif()
    verify()
    print("\ncuttle: local cluster up. Node should read 'idle' above.")
