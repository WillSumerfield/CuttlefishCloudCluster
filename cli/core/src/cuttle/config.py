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

# --- Karma substrate: priority, Power, VRAM features (Phase 4 slice 0) ---
# Nice is how Karma reaches the queue: the Head lowers the priority of a poor-standing User's pending
# Spawns. It can do that with ZERO privilege, because every Spawn runs as the Mantle — so the Head is
# the *owner* of every job in the queue, and lowering your own job's priority is an owner's right.
# (Only NEGATIVE nice — a boost — needs an operator; that's the admin Queue bump, which goes through
# sudo. See docs/DesignDoc.md §5.)
NICE_MAX = 10000  # nice = (1 - Share) * NICE_MAX, so standing spans [0, NICE_MAX]
# Age must be able to out-run the worst nice, or a poor-standing User could starve. Equal weights mean
# a maximally-niced Spawn overtakes a fresh un-niced one after PriorityMaxAge — Karma decides who goes
# first, never who goes at all.
PRIORITY_WEIGHT_AGE = NICE_MAX
PRIORITY_MAX_AGE = "1-0"  # 1 day

# **Power**: measured throughput, in fp32 TFLOP/s, of one device — benchmarked once, at enroll, by a
# Head-submitted Spawn running inside the Pod (templates.BENCH_PY). Intrinsic to the card; the other
# half of a device-second's price is Demand, which is live and extrinsic.
BENCH_MATRIX_N = 4096
BENCH_WARMUP = 5
BENCH_ITERS = 50
BENCH_TIME_MINUTES = 10
BENCH_DIR_NAME = "bench"  # HEAD_SPAWNS_DIR/bench/<node>/power — where a benchmark stages its result
BENCH_TIMEOUT_S = 600     # from sbatch to the result landing (a cold node may still be booting slurmd)

# Slurm schedules the LOWEST-weight eligible node first, so Weight must run inverse to Power: the
# fastest free card that fits wins. It is a preference among *free* nodes, not a reservation — Slurm
# will not hold a Spawn back waiting for a better card, which is exactly the "first one that fits, else
# queue" behaviour we want.
WEIGHT_SCALE = 10000


def node_weight(power: float) -> int:
    return max(1, round(WEIGHT_SCALE / power))


def vram_feature(vram_mb: int) -> str:
    """The Slurm feature a device advertises: `vram24` for a 24 GB card.

    Rounded, not floored: a 4090 reports 24564 MiB, and flooring would advertise it as `vram23` — so
    `--vram 24` would find no card that plainly has 24 GB.
    """
    return f"vram{round(vram_mb / 1024)}"


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
# An Arm's slurmd flags (`-Z --conf ... --conf-server ...`). A systemd DROP-IN, not the distro's
# `/etc/default/slurmd`, because that hook cannot carry them. Ubuntu's unit is
# `ExecStart=/usr/sbin/slurmd --systemd $SLURMD_OPTIONS`, and systemd splits a variable's value at
# whitespace **without honouring quotes inside it** — so `--conf "Gres=gpu:1 Feature=vram24"` would
# reach slurmd as three mangled arguments the moment `--conf` grew a second key (which Phase 4 needs:
# Feature=vramN is what a Spawn's --vram matches). Quotes written literally in a unit file ARE honoured,
# so the drop-in states the whole command and keeps the space-separated --conf intact.
SLURMD_DROPIN = Path("/etc/systemd/system/slurmd.service.d/ccc.conf")
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
# It is otherwise UNPRIVILEGED — it fills in the hardware fields of a registry row an operator already
# created, and the operator admits the Arm with `cuttle arm admit`. No sudoers rule, so no root path
# out of a POST body.
ENROLL_API_USER = "munge"
CCC_LIB = Path("/var/lib/ccc")          # munge-owned; the Enroll API's only writable location
# The Arm registry. `cuttle arm authorize` CREATES each row (owner + name) before the donor's token is
# issued — /enroll refuses a name that isn't here, so mesh membership alone can't fetch the munge key.
# The donor's POST only fills in gpu_device + mesh_ip; it never gets to say who owns the machine.
# `head: true` marks the Head's own co-located Arm (admin-owned, never enrolls, GRES line comes from
# templates.head_gres_node() — so gres rendering skips it).
ARMS_REGISTRY = CCC_LIB / "arms.json"   # {name: {owner, gpu_device, mesh_ip, admitted, head?}}

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
# The **Mantle** (glossary): the single service identity every Spawn executes as, on every node. On the
# Head it also runs the Head API, owns the auth DB (head.db), and submits via the munge socket — never
# reading the munge key. Phase 1 ran the read-only board as `nobody`.
HEAD_API_USER = "ccc"
# Slurm runs a job as the *submitting uid*: slurmctld records the Head API's uid at sbatch, and every
# slurmd setuid()s to it before launching. So that uid MUST resolve to the same account on every node —
# a cluster-wide pin, not a per-box `useradd --system` (which allocates dynamically). 60000 sits in
# Debian's reserved block AND below systemd's DynamicUser range (61184–65519), so no transient service
# can have taken it. Collision ⇒ useradd fails and setup halts (no fallback; decide live).
CCC_UID = 60000
CCC_GID = 60000
# The first CCC login account, seeded at `setup --head`. A fixed name (NOT the operator's OS username):
# it's an app-level account in head.db, unrelated to any Unix uid. Spawns show `submitted_by: admin`.
FIRST_ADMIN_USER = "admin"
HEAD_API_WEB_DIR = Path("/opt/ccc/head-api/web")  # built SPA, world-readable
# LAN TLS. The Head API carries passwords and session tokens on `0.0.0.0`, so the LAN path must be
# encrypted. No CA will sign a cert for a box with no public domain (that's item #8), so the Head signs
# its OWN — which gives encryption but no identity: anyone can self-sign a cert claiming to be the
# Head. Identity comes back from PINNING (see cuttle/tls.py): the CLI remembers this exact cert the
# first time it sees it, exactly like SSH's known_hosts. Browsers can't pin, so they show the
# "not private" warning once and the user adds an exception (setup prints that this is expected).
# Lives in /opt (not CCC_LIB) so a plain teardown preserves it, like base.sif — regenerating the cert
# would invalidate every client's pin.
CCC_TLS_DIR = Path("/opt/ccc/tls")
HEAD_CERT = CCC_TLS_DIR / "head.crt"   # 0644, the public ID card clients pin
HEAD_KEY = CCC_TLS_DIR / "head.key"    # 0600 ccc — only the Head API may read it
CERT_DAYS = 3650                       # effectively never expires; a pin outliving the cert is worse
# Cap an upload rather than buffering whatever arrives: the Head API streams the body to disk, and the
# Head is the one box that must stay up. A browser folder-picker makes a huge accidental selection easy.
MAX_UPLOAD_BYTES = 4 * 1024**3  # 4 GiB
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
# base.sif, at the SAME path on every node (Head and every Arm) — Spawns run as the home-less Mantle,
# which can't read an operator's 0750 home, so the image lives in a world-readable /opt tree (same
# /opt/ccc as the venv + SPA) instead. One constant path cluster-wide means the rendered job script
# names the sif identically wherever Slurm places it. Each node BUILDS its own (apptainer build at
# setup); nothing ships a multi-GB image over the mesh.
CCC_IMAGES_DIR = Path("/opt/ccc/images")
CCC_SIF = CCC_IMAGES_DIR / "base.sif"

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
# The OPERATOR's own dir — settings + credentials only. It holds no Spawn machinery: images live in
# CCC_IMAGES_DIR (world-readable, so the home-less Mantle can read them) and Spawn results live on the
# Head under HEAD_SPAWNS_DIR, fetched over the API. A submitter never needs a local .ccc/spawns.
CCC_HOME = Path.home() / ".ccc"
CONFIG_DIR = CCC_HOME / "config"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
# Pinned Head certificates, one PEM per host (trust-on-first-use; see tls.py). Keyed by host because a
# client may one day talk to more than one cluster, and a pin is only meaningful against the Head that
# presented it.
PINS_DIR = CCC_HOME / "pins"
# CLI session bearer (Phase 3a): the token `cuttle login` stores, 0600, kept out of config.yaml so a
# submitter who never ran setup still has just this one secret file. The Head URL lives in config.yaml.
CREDENTIALS_FILE = CCC_HOME / "credentials"
# Packages newly installed by setup (before→after diff) — teardown purges exactly this set.
APT_MANIFEST = CONFIG_DIR / "apt-installed.txt"
