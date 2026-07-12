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
# slurmctld dials every slurmd here. Inbound on each Arm — see system.allow_mesh_iface.
SLURMD_PORT = 6818
# Ubuntu's slurmd.service does `ExecStart=/usr/sbin/slurmd --systemd $SLURMD_OPTIONS` with
# `EnvironmentFile=-/etc/default/slurmd` — the supported hook for an Arm's `-Z --conf-server` flags.
SLURMD_DEFAULTS = Path("/etc/default/slurmd")
MUNGE_KEY = Path("/etc/munge/munge.key")  # 0400 munge:munge — cluster-wide, never expires

# --- Shared API package venv (Phase 1) ---
# Both head-side FastAPI apps live in one package (`ccc_head_api`) and run from this one venv, kept
# out of the CLI's own venv. The Enroll API app and the Head API app are two ASGI targets off it.
API_VENV = Path("/opt/ccc/api/.venv")

# --- Enroll API (Phase 1 ③: /enroll; mesh-only, serves the munge key) ---
# Bound to the Head's MESH address only: being on the mesh is the entire authorization. Glossary
# (head/CONTEXT.md): the *Enroll API* is the durable, mesh-only trust boundary (holds the munge key).
ENROLL_API_PORT = 8081
ENROLL_API_SERVICE = "ccc-enroll-api"
ENROLL_API_UNIT = Path("/etc/systemd/system/ccc-enroll-api.service")
# Runs as munge: the only user that can read munge.key without chmod'ing it or running as root.
# It is otherwise UNPRIVILEGED — it records enrolling Arms into the registry below, and an operator
# admits them with `cuttle arm approve`. No sudoers rule, so no root path out of a POST body.
ENROLL_API_USER = "munge"
CCC_LIB = Path("/var/lib/ccc")          # munge-owned; the Enroll API's only writable location
ARMS_REGISTRY = CCC_LIB / "arms.json"   # {name: {gpu_device, mesh_ip, admitted}}

# An Arm's GPU device node. Native Linux exposes /dev/nvidiaN; WSL2 exposes /dev/dxg with CUDA
# shimmed via /usr/lib/wsl/lib. Re-validated root-side before it ever reaches gres.conf.
GPU_DEVICE_RE = r"^/dev/(nvidia[0-9]+|dxg)$"

# --- Head API (Phase 1 ⑥: read board + SPA; Phase 3a: auth + submission): LAN+mesh ---
# The LAN+mesh service backing both the web Dashboard and the CLI (glossary: *Head API*). Split from
# the Enroll API on purpose: /enroll hands out the munge key and MUST stay mesh-only, but the Head
# API must reach the LAN too. Bound 0.0.0.0. Reuses API_VENV (same package, different app object). A
# remote Arm sees it over the mesh; a LAN box sees it too (needs a ufw port rule — system.allow_tcp_port).
HEAD_API_PORT = 8082  # 8080 = headscale, 8081 = enroll API
HEAD_API_SERVICE = "ccc-head-api"
HEAD_API_UNIT = Path("/etc/systemd/system/ccc-head-api.service")
# Dedicated `ccc` service account: owns the auth DB (head.db) and (later) submits via the munge
# socket — never reads the munge key. Phase 1 ran this read-only board as `nobody`.
HEAD_API_USER = "ccc"
# The first CCC login account, seeded at `setup --head`. A fixed name (NOT the operator's OS username):
# it's an app-level account in head.db, unrelated to any Unix uid. Spawns show `submitted_by: admin`.
FIRST_ADMIN_USER = "admin"
HEAD_API_WEB_DIR = Path("/opt/ccc/head-api/web")  # built SPA, world-readable
# Auth store (Phase 3a): the User + Session tables behind login. Its own ccc-owned subdir (0700) —
# NOT loose in the munge-owned CCC_LIB, since SQLite writes WAL/journal sidecars beside the db and
# `ccc` can't create files in a munge-owned dir. 0600 db, seeded at `setup --head`.
HEAD_STATE_DIR = CCC_LIB / "head"
HEAD_DB = HEAD_STATE_DIR / "head.db"
# Slice 2 submission (all ccc-owned, 0700, under HEAD_STATE_DIR so `ccc` can create/traverse them):
#   projects/<uuid>/  — an uploaded project tree, unpacked by the Head API from a `cuttle run` tar.
#                       Also the rsync [project] module root (a remote Arm pulls <uuid>/... from here).
#   spawns/<id>/      — a Spawn's out/err/exit/outputs (the rsync [spawns] stage-out target). The CLI
#                       reads these back over the API, never off local disk (jobs run as ccc now).
#   scratch/<id>/     — the co-located Arm's per-Spawn run dir. A job NEVER runs in the served
#                       projects tree (that would mutate the rsync source); it's copied here first.
HEAD_PROJECTS_DIR = HEAD_STATE_DIR / "projects"
HEAD_SPAWNS_DIR = HEAD_STATE_DIR / "spawns"
HEAD_SCRATCH_DIR = HEAD_STATE_DIR / "scratch"
# base.sif for ccc-run jobs on the Head. The operator's ~/.ccc/images copy is unreadable to the
# home-less `ccc` account, and post-slice-2 the operator never runs jobs directly — so the Head builds
# a world-readable copy here (same /opt/ccc tree as the venv + SPA). Remote Arms keep BASE_SIF.
HEAD_IMAGES_DIR = Path("/opt/ccc/images")
HEAD_SIF = HEAD_IMAGES_DIR / "base.sif"

# --- WSL Arm dashboard forward (⑥ convenience) ---
# A WSL Arm's owner can't browse the Head Dashboard from Windows: the CCC mesh lives INSIDE WSL, and
# Windows has no route to the Head's mesh IP. So on a WSL Arm we forward a local port to the Head
# API — WSL2's localhost-forwarding then surfaces it at http://localhost:HEAD_API_PORT/ in the
# Windows browser. Socket-activated proxy: no extra package (systemd-socket-proxyd ships with
# systemd). WSL-only — a native-Linux Arm reaches the mesh Dashboard directly.
DASH_FORWARD_SERVICE = "ccc-dash-forward"  # the .socket/.service basename (they must match to pair)
DASH_FORWARD_SOCKET_UNIT = Path("/etc/systemd/system/ccc-dash-forward.socket")
DASH_FORWARD_SERVICE_UNIT = Path("/etc/systemd/system/ccc-dash-forward.service")
SOCKET_PROXYD = "/usr/lib/systemd/systemd-socket-proxyd"

# --- WSL2 Arms (see setup.configure_apptainer_wsl) ---
# `apptainer exec --nv` alone yields torch.cuda.is_available() == False on WSL. Measured: --nv finds
# libcuda.so.1 (ldconfig knows /usr/lib/wsl/lib) and injects it, but WSL's libcuda is a shim that
# loads libdxcore.so, which is absent from nvliblist AND enumerates GPUs from /usr/lib/wsl/DRIVERS —
# a directory of Windows driver files no library list can supply. cuInit then returns 100
# (CUDA_ERROR_NO_DEVICE). Binding the parent /usr/lib/wsl + listing libdxcore.so gives cuInit 0.
APPTAINER_CONF = Path("/etc/apptainer/apptainer.conf")
APPTAINER_NVLIBLIST = Path("/etc/apptainer/nvliblist.conf")
WSL_LIB_ROOT = "/usr/lib/wsl"  # holds both lib/ and drivers/ — bind the parent, not lib/
WSL_EXTRA_NVLIB = "libdxcore.so"
NODE_NAME_RE = r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,31}$"

# --- mesh (Phase 1 substrate: self-hosted Headscale coordinator + Tailscale client) ---
# Headscale isn't in apt — install the newest GitHub-release .deb (version recorded, pinned later).
HEADSCALE_RELEASES = "https://api.github.com/repos/juanfont/headscale/releases/latest"
# Deterministic asset URL for a known tag — no API call, so no unauthenticated rate limit.
HEADSCALE_DEB = ("https://github.com/juanfont/headscale/releases/download/"
                 "{tag}/headscale_{ver}_linux_amd64.deb")
HEADSCALE_USER = "ccc"  # the Headscale user/namespace Arms enroll under
HEADSCALE_PORT = 8080
HEADSCALE_CONF = Path("/etc/headscale/config.yaml")
HEADSCALE_LIB = Path("/var/lib/headscale")
HEADSCALE_POLICY = Path("/etc/headscale/policy.hujson")  # empty policy == deny-all; see mesh._POLICY
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
# Mesh IP ranges. These MUST sit inside Tailscale's own ranges (v4 100.64.0.0/10, v6
# fd7a:115c:a1e0::/48): tailscaled runs every *peer* address through tsaddr.IsTailscaleIP() before
# installing a route, and silently drops the rest. A node accepts its own out-of-range address, so
# a one-node mesh looks healthy — then no peer is ever routable. Measured on v1.98.8 with
# 10.99.0.0/16: WireGuard configured the peer, `ip route show dev ccc0` was empty, and pings left
# via the default gateway.
#
# So isolation from a personal tailnet is by *sub-range*, not by disjoint space. Tailscale installs
# per-peer /32s (never one route for the whole /10), so both daemons coexist unless they claim the
# same address. Headscale allocates sequentially from the bottom of this prefix; Tailscale assigns
# pseudo-randomly across the /10. A collision needs an unlucky draw inside this /16, and would
# break one /32 rather than the mesh.
#
# Avoids 100.100.0.0/16, which would contain Tailscale's MagicDNS resolver at 100.100.100.100.
# Cosmetic only: measured, tailscaled routes quad100 into its tun whatever the prefix, so both
# daemons fight over it either way (see docs/TODO.md).
MESH_V4_PREFIX = "100.99.0.0/16"
MESH_V6_PREFIX = "fd7a:115c:a1e0:cc::/64"

# --- stage-in/out (Phase 1 ④): rsync daemon on the Head, bound to the mesh ---
# A Spawn on a remote Arm has none of the submitter's files. The Head runs an rsync daemon on its
# mesh address ONLY (never 0.0.0.0 / the personal tailnet); the job pulls the project in and pushes
# results back. Auth == being on the mesh (hosts allow = the mesh prefixes), same model as /enroll.
# Two modules: [project] read-only at the Head user's $HOME (a job can't corrupt the source tree),
# [spawns] read-write at $HOME/.ccc/spawns (the one place a job may write home). rsync 873 is
# privileged, so the daemon runs as root and each module drops to the Head user via uid=/gid=.
RSYNCD_CONF = Path("/etc/ccc-rsyncd.conf")
RSYNCD_SERVICE = "ccc-rsyncd"
RSYNCD_UNIT = Path("/etc/systemd/system/ccc-rsyncd.service")
RSYNCD_PORT = 873
RSYNC_MODULE_PROJECT = "project"  # ro, rooted at HEAD_PROJECTS_DIR (a remote Arm pulls <uuid>/...)
RSYNC_MODULE_SPAWNS = "spawns"    # rw, rooted at HEAD_SPAWNS_DIR — stage-out target

# --- CCC user artifacts ---
CCC_HOME = Path.home() / ".ccc"
IMAGES_DIR = CCC_HOME / "images"
SPAWNS_DIR = CCC_HOME / "spawns"
# Ephemeral per-Spawn staging on a remote Arm (node-local, resolved against the NODE's $HOME). One
# job = one dir, wiped on exit (trap). No persistent cache — leaving multi-GB dirs on donor boxes is
# bad tenancy, so each Spawn copies fresh. Path is relative to the node home; see spawns.JOB_SCRIPT.
SCRATCH_REL = ".ccc/scratch"
CONFIG_DIR = CCC_HOME / "config"
BASE_SIF = IMAGES_DIR / "base.sif"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
# CLI session bearer (Phase 3a): the token `cuttle login` stores, 0600, kept out of config.yaml so a
# submitter who never ran setup still has just this one secret file. The Head URL lives in config.yaml.
CREDENTIALS_FILE = CCC_HOME / "credentials"
# Packages newly installed by setup (before→after diff) — teardown purges exactly this set.
APT_MANIFEST = CONFIG_DIR / "apt-installed.txt"
