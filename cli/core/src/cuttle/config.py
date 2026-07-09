"""Single source of truth for MVP constants: pinned versions, paths, cluster identity.

Pins mirrored in docs/TODO.md — bump both together at full release.
"""

from pathlib import Path

# --- pinned system stack (Ubuntu 24.04 noble / amd64) ---
SLURM_PKG = "slurm-wlm=23.11.4-1.2ubuntu5"
SLURMD_PKG = "slurmd=23.11.4-1.2ubuntu5"  # Arms need the node daemon only, not slurmctld
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
# One line, no NodeName= → applies to every node. Any single-GPU Arm matches it unchanged.
# Multi-GPU Arms need per-node lines (deferred; see docs/TODO.md NVML entry).
GPU_DEVICE = "/dev/nvidia0"

# --- Slurm dynamic nodes (donor Arms self-register via `slurmd -Z`) ---
# MaxNodeCount is a ceiling on static+dynamic nodes; raising it later needs a slurmctld restart.
MAX_NODE_COUNT = 64
# Talk to every node directly instead of fanning messages out through a tree of donor boxes.
TREE_WIDTH = 65533

# --- Slurm FHS paths (distro-standard) ---
SLURM_CONF = Path("/etc/slurm/slurm.conf")
GRES_CONF = Path("/etc/slurm/gres.conf")
SLURM_STATE_DIR = Path("/var/spool/slurmctld")
SLURMD_SPOOL_DIR = Path("/var/spool/slurmd")
SLURM_LOG_DIR = Path("/var/log/slurm")
SLURMCTLD_PORT = 6817
# Ubuntu's slurmd.service does `ExecStart=/usr/sbin/slurmd --systemd $SLURMD_OPTIONS` with
# `EnvironmentFile=-/etc/default/slurmd` — the supported hook for an Arm's `-Z --conf-server` flags.
SLURMD_DEFAULTS = Path("/etc/default/slurmd")
MUNGE_KEY = Path("/etc/munge/munge.key")  # 0400 munge:munge — cluster-wide, never expires

# --- Head API (Phase 1 ③: /enroll; ⑤ grows it into the slurmrestd-backed API) ---
# Bound to the Head's MESH address only: being on the mesh is the entire authorization.
HEAD_API_PORT = 8081
HEAD_API_SERVICE = "ccc-head-api"
HEAD_API_UNIT = Path("/etc/systemd/system/ccc-head-api.service")
HEAD_API_VENV = Path("/opt/ccc/head-api/.venv")
# Runs as munge: the only user that can read munge.key without chmod'ing it or running as root.
# It is otherwise UNPRIVILEGED — it records enrolling Arms into the registry below, and an operator
# admits them with `cuttle arm admit`. No sudoers rule, so no root path out of a POST body.
HEAD_API_USER = "munge"
CCC_LIB = Path("/var/lib/ccc")          # munge-owned; the API's only writable location
ARMS_REGISTRY = CCC_LIB / "arms.json"   # {name: {gpu_device, mesh_ip, admitted}}

# An Arm's GPU device node. Native Linux exposes /dev/nvidiaN; WSL2 exposes /dev/dxg with CUDA
# shimmed via /usr/lib/wsl/lib. Re-validated root-side before it ever reaches gres.conf.
GPU_DEVICE_RE = r"^/dev/(nvidia[0-9]+|dxg)$"
NODE_NAME_RE = r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,31}$"

# --- mesh (Phase 1 substrate: self-hosted Headscale coordinator + Tailscale client) ---
# Headscale isn't in apt — install the newest GitHub-release .deb (version recorded, pinned later).
HEADSCALE_RELEASES = "https://api.github.com/repos/juanfont/headscale/releases/latest"
HEADSCALE_USER = "ccc"  # the Headscale user/namespace Arms enroll under
HEADSCALE_PORT = 8080
HEADSCALE_CONF = Path("/etc/headscale/config.yaml")
HEADSCALE_LIB = Path("/var/lib/headscale")
# Tailscale client ships from its own apt repo (noble). Keyring + list installed by setup.
TAILSCALE_REPO_KEY = "https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg"
TAILSCALE_REPO_LIST = "https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list"
TAILSCALE_KEYRING = Path("/usr/share/keyrings/tailscale-archive-keyring.gpg")
TAILSCALE_REPO_FILE = Path("/etc/apt/sources.list.d/tailscale.list")

# CCC runs its OWN isolated tailscaled instance (separate socket/state/tun/port) so it never
# disturbs a machine's default/personal tailscaled — critical: the Head is administered over its
# personal tailnet. All `cuttle` mesh commands target CCC_TS_SOCKET explicitly.
CCC_TS_SERVICE = "tailscaled-ccc"
CCC_TS_UNIT = Path("/etc/systemd/system/tailscaled-ccc.service")
CCC_TS_RUNDIR = Path("/run/tailscale-ccc")
CCC_TS_SOCKET = CCC_TS_RUNDIR / "tailscaled.sock"
CCC_TS_STATEDIR = Path("/var/lib/tailscale-ccc")
CCC_TS_STATE = CCC_TS_STATEDIR / "tailscaled.state"
CCC_TS_TUN = "ccc0"
CCC_TS_PORT = 41642  # default tailscaled uses 41641 — keep them distinct
# Mesh IP ranges — deliberately OUTSIDE Tailscale's 100.64.0.0/10 CGNAT so CCC routes never
# collide with a personal tailnet on the same host (v6 = a distinct ULA from fd7a:115c:a1e0::/48).
MESH_V4_PREFIX = "10.99.0.0/16"
MESH_V6_PREFIX = "fdcc:cc::/48"

# --- CCC user artifacts ---
CCC_HOME = Path.home() / ".ccc"
IMAGES_DIR = CCC_HOME / "images"
SPAWNS_DIR = CCC_HOME / "spawns"
CONFIG_DIR = CCC_HOME / "config"
BASE_SIF = IMAGES_DIR / "base.sif"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
# Packages newly installed by setup (before→after diff) — teardown purges exactly this set.
APT_MANIFEST = CONFIG_DIR / "apt-installed.txt"
