"""Single source of truth for MVP constants: pinned versions, paths, cluster identity.

Pins mirrored in docs/TODO.md — bump both together at full release.
"""

from pathlib import Path

# --- pinned system stack (Ubuntu 24.04 noble / amd64) ---
SLURM_PKG = "slurm-wlm=23.11.4-1.2ubuntu5"
APPTAINER_PKG = "apptainer=1.5.2-1~noble"
NVIDIA_CTK_PKG = "nvidia-container-toolkit=1.19.1-1"
MUNGE_PKG = "munge"  # auth; distro version fine

# --- pinned base Pod image (built from a def: pytorch image + pip wandb) ---
BASE_IMAGE_DOCKER = "pytorch/pytorch:2.12.1-cuda13.0-cudnn9-runtime"
WANDB_PKG = "wandb==0.28.0"

# --- cluster identity ---
CLUSTER_NAME = "ccc"
PARTITION = "ccc"

# --- GPU device (distro Slurm lacks NVML; map GRES to the device node explicitly) ---
GPU_DEVICE = "/dev/nvidia0"

# --- Slurm FHS paths (distro-standard) ---
SLURM_CONF = Path("/etc/slurm/slurm.conf")
GRES_CONF = Path("/etc/slurm/gres.conf")
SLURM_STATE_DIR = Path("/var/spool/slurmctld")
SLURMD_SPOOL_DIR = Path("/var/spool/slurmd")
SLURM_LOG_DIR = Path("/var/log/slurm")

# --- CCC user artifacts ---
CCC_HOME = Path.home() / ".ccc"
IMAGES_DIR = CCC_HOME / "images"
SPAWNS_DIR = CCC_HOME / "spawns"
CONFIG_DIR = CCC_HOME / "config"
BASE_SIF = IMAGES_DIR / "base.sif"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
# Packages newly installed by setup (before→after diff) — teardown purges exactly this set.
APT_MANIFEST = CONFIG_DIR / "apt-installed.txt"
