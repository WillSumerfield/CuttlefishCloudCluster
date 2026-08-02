"""Single source of truth for MVP constants: pinned versions, paths, cluster identity.

Pins mirrored in docs/TODO.md — bump both together at full release.
"""

import os
from pathlib import Path

# --- pinned system stack (Ubuntu 24.04 noble / amd64) ---
SLURM_PKG = "slurm-wlm=23.11.4-1.2ubuntu5"
SLURMD_PKG = "slurmd=23.11.4-1.2ubuntu5"  # Arms need the node daemon only, not slurmctld
NVIDIA_CTK_PKG = "nvidia-container-toolkit=1.19.1-1"  # NVIDIA's repo retains old versions — safe
MUNGE_PKG = "munge"  # auth; distro version fine
# HEAD ONLY. `setfacl` is how the Mantle lends ONE **Guise** read access to ONE uploaded project
# without any privilege at all (it already owns the directory) — see ccc_head_api.spawns.submit. Distro
# version: the ACL syscalls are kernel/ext4, and this package is just the CLI over them.
ACL_PKG = "acl"

# The archive publishes ONE version of a package per pocket, so the day Ubuntu supersedes a pin above
# it stops being installable and every future setup dies at `apt-get install`. snapshot.ubuntu.com
# serves the archive *as it was* at a timestamp, so setup adds it as an EXTRA apt source (never a
# replacement — see setup.add_repos) and the pinned versions stay fetchable however long noble lives.
# The timestamp is the archive state these pins were verified against; bump it with them.
UBUNTU_SNAPSHOT = "20260729T000000Z"
UBUNTU_SNAPSHOT_URL = "https://snapshot.ubuntu.com/ubuntu"
UBUNTU_CODENAME = "noble"
# Signed by the ordinary Ubuntu archive keys — the snapshot serves the original Release/InRelease
# untouched, so the keyring every 24.04 box already ships verifies it with no key of ours to install.
UBUNTU_ARCHIVE_KEYRING = "/usr/share/keyrings/ubuntu-archive-keyring.gpg"
UBUNTU_SNAPSHOT_LIST = Path("/etc/apt/sources.list.d/ccc-ubuntu-snapshot.list")

# apptainer is NOT a version pin, because there is nothing durable to pin to: it comes from a Launchpad
# PPA, which keeps only its newest build (measured 2026-07-29: 1.5.3-1~noble, and nothing else). So the
# reproducibility on offer is fleet-internal — the HEAD downloads the .deb once and serves that exact
# file to every Arm forever (setup.pin_apptainer_deb). We cannot resurrect a build Launchpad pruned;
# we can guarantee every node in the cluster runs the same one. The Arm side gains a second win: a
# donor's box never has a third-party repo added to it.
APPTAINER_PKG = "apptainer"
APPTAINER_PPA = "ppa:apptainer/ppa"  # added on the HEAD only

# --- pinned base Pod image (built from a def: pytorch image + pip wandb) ---
# By DIGEST, not by tag: `pytorch/pytorch:2.13.0-cuda13.0-cudnn9-runtime` is mutable and upstream
# re-pushes it, so a tag would silently hand two nodes different CUDA/torch builds of "the same" image.
# The digest below is that tag as of 2026-07-29 (linux/amd64). `docker://` accepts a digest reference.
BASE_IMAGE_DOCKER = ("pytorch/pytorch@sha256:"
                     "db80a41f8428644cebcb3d75b0b62df334ab6c0e75785951eb25f48bfbd42407")
WANDB_PKG = "wandb==0.28.1"  # PyPI is immutable per version — this pin cannot rot

# --- pinned Environment build tools (Head only) ---
# uv and micromamba build a Spawn's declared Environment (ccc_head_api.environments). Pinned by
# version AND sha256, checked here rather than against the sidecar .sha256 each project publishes: a
# sidecar fetched from the same host as the file it vouches for proves only that the download wasn't
# truncated. These hashes are in the repo, so bumping a tool is a reviewed commit — which matters more
# for these two than for apptainer, because a uv release can change what `uv sync` resolves and every
# Spawn's dependencies would move underneath their Users with nothing to point at.
#
# Installed on the HEAD ONLY, so `uname -m` is the Head's own arch. Both are static binaries with no
# runtime deps; uv ships a tarball, micromamba a bare binary.
ENV_TOOLS_DIR = Path("/opt/ccc/tools")
UV_VERSION = "0.12.1"
UV_URL = ("https://github.com/astral-sh/uv/releases/download/{v}/"
          "uv-{arch}-unknown-linux-gnu.tar.gz")
UV_SHA256 = {
    "x86_64": "90b2f223fb69d19db49e117da601f64978593417988530aa733d456141b4bcbb",
    "aarch64": "769d373e146692c639b5fbaae33b331c297a32e03d30448772051902df52bbf4",
}
MICROMAMBA_VERSION = "2.8.1-1"
MICROMAMBA_URL = ("https://github.com/mamba-org/micromamba-releases/releases/download/{v}/"
                  "micromamba-{arch}")
MICROMAMBA_ARCH = {"x86_64": "linux-64", "aarch64": "linux-aarch64"}
MICROMAMBA_SHA256 = {
    "x86_64": "77b7790ec97f64581118f103585b175df4306f95829b0fa6bfe4a19cc88a1182",
    "aarch64": "49aa29008cafa6cd6027ebd643fc43ca32c20740e60b0a05378c4e5bb837c217",
}

# --- cluster identity ---
CLUSTER_NAME = "ccc"
PARTITION = "ccc"

# --- GPU devices (distro Slurm lacks NVML; map GRES to the device nodes explicitly) ---
# There is no constant here on purpose: a node's device list is DETECTED (system.gpu_devices()), not
# assumed. CCC does its own hardware **Registration** at enrollment, which is exactly the job
# `AutoDetect=nvml` would have done — so rebuilding slurm-wlm against NVML would buy a Slurm fork and
# an `apt-mark hold` blocking security updates, to autodetect what we already detect ourselves.

# --- Karma substrate: priority, Power, VRAM features (Phase 4 slice 0) ---
# Nice is how Karma reaches the queue: the Head lowers the priority of a poor-standing User's pending
# Spawns. It can do that with ZERO privilege, because every Spawn runs as the Mantle — so the Head is
# the *owner* of every job in the queue, and lowering your own job's priority is an owner's right.
# (Only NEGATIVE nice — a boost — needs an operator; that's the admin Queue bump, which goes through
# sudo. See docs/DesignDoc.md §5.)
#
# Treat a Spawn's priority as a SIGNED int32 whose zero sits at 2^31. Slurm's priority is a `uint32_t`
# and it clamps: with no base, a *fresh* Spawn (age factor 0) computes `-nice`, which underflows to the
# floor — so every new Spawn from every User in less than perfect standing would tie there, and Karma
# would rank correctly, push nice successfully, and change nothing. The flat base is what keeps the
# subtraction off the rail. PriorityWeightPartition is the only base available without slurmdbd, and
# with a single partition Slurm normalizes the partition factor to exactly 1.0 for every job — so it
# adds a constant, reorders nobody, and exists purely to lift the range clear of zero.
PRIORITY_BASE = 2**31          # PriorityWeightPartition — the flat base under every Spawn
NICE_MAX = 2**16               # nice = (1 - Share) * NICE_MAX, so standing spans [0, NICE_MAX]
# Equal ranges for nice and age, so poor standing costs a Spawn AT MOST a day of apparent queue age —
# Slurm treats it as though it arrived later than it did. Bounded and intelligible. It is NOT a
# starvation bound (age saturates at PRIORITY_MAX_AGE; a rival can max its age out too) — that bound is
# economic: spending Karma is what makes you poor. See temp/KARMA.md "the priority floor".
PRIORITY_WEIGHT_AGE = NICE_MAX
PRIORITY_MAX_AGE = "1-0"       # 1 day — the ramp over which waiting earns back a full nice range
# One admin Queue bump. Bigger than age + nice combined, so a bumped Spawn beats anything the economy
# can produce; negative nice is operator-only, which is what makes it a flag needing no state of its own
# (the tick skips any job with nice < 0 — only an operator could have set it).
BUMP_STEP = 2**17

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


def node_weight(powers: list[float]) -> int:
    """A node's Slurm Weight, from its per-**Device** Powers. Takes the list and reduces to the MEAN.

    Weight is inherently node-level — Slurm has one per node, not one per GRES — so a multi-GPU box's
    cards must be summarised somehow. The mean is the honest summary *here specifically*, and only
    here: Weight is a **preference ordering among nodes**, never a price. It decides which free box
    Slurm reaches for first; it never multiplies a second of held time. A Spawn's bill is still
    settled against the Power of the Device it actually held (tick._meter), so the reduction cannot
    reach anyone's Balance. Max would over-prefer a box whose one fast card is already busy; the mean
    ranks a box by what it is typically worth to land on.
    """
    return max(1, round(WEIGHT_SCALE / (sum(powers) / len(powers))))


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
# What task/cgroup enforces. Head-authored like the other two: configless serves slurm.conf AND its
# siblings (cgroup.conf, gres.conf) to every Arm, and there is no per-node override — one file, whole
# cluster. See templates.CGROUP_CONF_TMPL.
CGROUP_CONF = Path("/etc/slurm/cgroup.conf")
# slurmd runs this on the node after every Spawn, however it ended — the reclaim the job script's own
# `trap ... EXIT` can't do because SIGKILL (OOM, `scancel -9`, a Phase-2 Retract) bypasses the trap.
SLURM_EPILOG = Path("/etc/slurm/ccc-epilog.sh")
# slurmd runs this on the node BEFORE every Spawn, as root: it creates the **Guise** the Spawn will run
# as, if this node has never run one of that User's before. Like the epilog it is served by neither
# configless nor any push — every node writes its own at `cuttle setup` (see templates.PROLOG_TMPL).
SLURM_PROLOG = Path("/etc/slurm/ccc-prolog.sh")
# job_container/tmpfs's settings. The plugin looks for this beside slurm.conf and nowhere else ("the
# file will always be located in the same directory as the slurm.conf", man 5 job_container.conf), and
# like the two hook scripts it is served by neither configless nor any push — every node writes its own.
JOB_CONTAINER_CONF = Path("/etc/slurm/job_container.conf")
# Where the plugin builds each job's private /tmp. slurmd creates it (AutoBasePath) and holds it mounted
# `private` for its whole life, and does NOT remove it at shutdown — so teardown does, which is why the
# ROOT is a constant of its own: `%h` is expanded by slurmd, not by us, so removing the tree means
# removing the whole `jobcontainer/` directory rather than guessing which hostname is inside it.
# `%h` is required of any BasePath that might sit on a shared filesystem; ours does not, but the
# expansion costs nothing and the alternative silently collides if one ever does.
JOB_CONTAINER_ROOT = Path("/var/lib/slurm/jobcontainer")
JOB_CONTAINER_BASE = f"{JOB_CONTAINER_ROOT}/%h"
# What an Arm reports having written, in its /enroll POST, so the Head can refuse to name a file the
# fleet does not have yet (see enroll.arms_missing and the Rollout section of temp/spawn-isolation.md).
#
# Two names, not one, and they are gated independently: an Arm enrolled during the Phase 3 rollout
# reports only "prolog", so folding tmpfs into that name would either hold `Prolog=` back on an Arm
# that has it, or claim tmpfs of a node with no job_container.conf — which drains it.
ARM_CAP_PROLOG = "prolog"
ARM_CAP_TMPFS = "tmpfs"
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
# The Head mints that key with this (see setup.ensure_munge_key). munge 0.5.15 (noble) ships
# `mungekey`; the older `create-munge-key` is gone, and we are pinned to 24.04.
MUNGEKEY_BIN = "/usr/sbin/mungekey"

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
# created, and the operator admits the Arm with `cuttle arm admit`. Its one sudo grant (Phase 9 #2,
# SUDOERS_MUNGE_HEADSCALE below) is a single parameterless command with nothing a POST body can steer —
# no wildcard, no argument taken from the request, nothing else headscale's CLI can do.
SUDOERS_MUNGE_HEADSCALE = Path("/etc/sudoers.d/ccc-munge-headscale")
ENROLL_API_USER = "munge"
CCC_LIB = Path("/var/lib/ccc")          # munge-owned; the Enroll API's only writable location
# The Arm registry. `cuttle arm authorize` CREATES each row (owner + name) before the donor's token is
# issued — /enroll refuses a name that isn't here, so mesh membership alone can't fetch the munge key.
# The donor's POST only fills in gpu_devices + mesh_ip; it never gets to say who owns the machine.
# `head: true` marks the Head's own co-located Arm (admin-owned, never enrolls, GRES lines come from
# templates.head_gres_node() — so gres rendering skips it).
# `gpu_devices` and `powers` are parallel lists, index-aligned, and that alignment is load-bearing:
# it is what turns a Slurm GRES index back into the Power a Spawn is settled at. There is deliberately
# no node-level `power` beside them — a summary stored next to the list it came from is a second
# number that can silently disagree with it. Where one is needed it is derived at the point of use.
ARMS_REGISTRY = CCC_LIB / "arms.json"   # {name: {owner, gpu_devices, powers, mesh_ip, admitted, ...}}

# --- Arm Features (docs/TODO.md "Arm Features") ---
# Named, admin-curated capabilities an Arm may report having: heavy/hardware-licence-gated software
# (VSim, IsaacSim) that is not installed everywhere. The catalog lives beside the registry it enriches
# — munge-owned JSON under CCC_LIB, not head.db — because /enroll (which validates a donor's declared
# features against it) runs as `munge` and cannot read head.db (0600, ccc-owned). Mutated only by
# `cuttle feature add/remove` (Head-local, operator + sudo, same `install` pattern as save_registry);
# read by both the Enroll API (validation) and the Head API (GET /features, for the Dashboard/wizard).
FEATURES_CATALOG = CCC_LIB / "features.json"   # [name, ...]
# Also becomes a Slurm Feature= token and (uppercased) an env var name, so it is validated with the
# same shape the codebase already uses for a shell-safe identifier (config.py's OUTPUT_NAME_RE analog).
FEATURE_NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
# Where `cuttle setup --arm` records this box's declared features + install paths, so a Spawn's payload
# can find e.g. $VSIM_PATH without knowing whether this node has it — see
# ccc_head_api.spawns.JOB_SCRIPT, which sources this unconditionally (a no-op where it's absent).
ARM_FEATURES_ENV = Path("/etc/ccc/features.env")
# The apptainer .deb the Head serves to every Arm, and the record that pins it:
# {version, filename, sha256}. Under CCC_LIB rather than HEAD_STATE_DIR because the Enroll API is what
# serves the file, and it runs as `munge` — HEAD_STATE_DIR is 0700 ccc and it could not even traverse
# it. Both are root-owned and world-readable; the Enroll API only ever READS them, so it needs no
# write access here (unlike arms.json).
HEAD_DEBS_DIR = CCC_LIB / "debs"
APPTAINER_RECORD = CCC_LIB / "apptainer.json"

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
# --- Guises (temp/spawn-isolation.md Phase 3): a per-User Unix identity on every node ---
# A **Guise** is what a Spawn runs as. The Mantle above is what the *cluster* is — after Phase 3 no
# Spawn runs as it, and one that does is a bug. Every Guise is `ccc-u<uid>`, named after its uid and
# nothing else: the Prolog that creates the account on a donor's Arm has `SLURM_JOB_UID` and no other
# input at all (no network, no head.db), so the name must be derivable from the uid alone. It also
# cannot collide with a donor's own accounts, and needs no CCC-username regex.
GUISE_PREFIX = "ccc-u"
# Reserved for the cluster itself — the enrollment benchmark, the one Spawn no User submits. It is a
# Guise like any other, so that "a Spawn owned by `ccc` is a bug" has no exceptions.
GUISE_CLUSTER_UID = 60001
GUISE_UID_BASE = 60002   # Users allocate from here: monotonic, never reused (see db.allocate_guise)
GUISE_UID_MAX = 61182    # same reserved block as CCC_UID, below systemd's DynamicUser (61184+)
# The shared SECONDARY group every Guise joins, and the only thing the Head's sudoers rule names: `ccc`
# may run Slurm's client commands as any member of it (never as root). Each Guise's PRIMARY group is
# its own (gid == uid), so nothing is ever group-owned by this one — it is a sudo Runas list, not a
# sharing mechanism. gid at the top of the reserved block, above every allocatable Guise uid.
GUISE_GROUP = "ccc-guises"
GUISE_GROUP_GID = 61183
# Stamped into GECOS so `teardown --arm` can find exactly the accounts CCC created on a donor's box and
# nothing else. Matched as a prefix, never parsed.
GUISE_GECOS = "CCC Guise"
# The Head's one unattended sudo grant: sbatch/scancel/scontrol as a Guise. Not root — see templates.
SUDOERS_CCC_SLURM = Path("/etc/sudoers.d/ccc-slurm")


def guise_name(uid: int) -> str:
    """The Unix account a Spawn owned by this uid runs as. The ONLY place the name is formed."""
    return f"{GUISE_PREFIX}{uid}"
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

# --- Karma tick (Phase 4 slice 1): the service that ranks the queue by standing ---
# A THIRD unit off the same venv and the same head.db (`ccc_head_api.tick`). Deliberately not a task
# inside the Head API: the economy must not be able to wedge the API, nor the API the economy — and
# slice 2's meter (sample Demand, settle ended Spawns) is this same tick. Runs as the Mantle, which
# owns every job in the queue and so may re-nice any of them with no privilege at all.
KARMA_SERVICE = "ccc-karma"
KARMA_UNIT = Path("/etc/systemd/system/ccc-karma.service")

# --- Tide Pool (docs/TODO.md, now built): interactive Spawn relay ---
# The Head-side TCP relay's listen-port range (ccc_head_api.tidepool.PORT_BASE/PORT_RANGE) — MUST match
# those constants exactly; duplicated here rather than shared because the CLI and the head-side API
# package do not import each other (same reason FEATURES_CATALOG's path is duplicated between
# ccc_head_api.enroll and ccc_head_api.slurm). Opened once, as a whole range, at `setup --head`
# (system.allow_tcp_port_range) — a per-Tide-Pool ufw rule would mean firewall churn on every submit.
#
# BELOW Linux's default ephemeral range (net.ipv4.ip_local_port_range = 32768-60999): these ports are
# LISTENED on, by the relay on the Head and by the Pod's sshd on an Arm, so a range inside the ephemeral
# one is racing the kernel's own allocator on every box — any outbound connection can be handed the port
# a Tide Pool is about to want, and the bind then fails with EADDRINUSE. Was 41000; moved 2026-08-02.
TIDEPOOL_PORT_BASE = 31000
TIDEPOOL_PORT_RANGE = 200

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
# nvidia-smi on WSL lives here, and this dir is on PATH only in an interactive login shell — so a
# non-interactive caller (an srun step, a service) can't find it by name. system.nvidia_smi() falls
# back to this absolute path so the GPU checks don't depend on who invoked them.
WSL_NVIDIA_SMI = f"{WSL_LIB_ROOT}/lib/nvidia-smi"
NODE_NAME_RE = r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,31}$"

# --- mesh (Phase 1 substrate: self-hosted Headscale coordinator + Tailscale client) ---
# Headscale isn't in apt — install the newest GitHub-release .deb (version recorded, pinned later).
HEADSCALE_RELEASES = "https://api.github.com/repos/juanfont/headscale/releases/latest"
# Deterministic asset URL for a known tag — no API call, so no unauthenticated rate limit.
HEADSCALE_DEB = ("https://github.com/juanfont/headscale/releases/download/"
                 "{tag}/headscale_{ver}_linux_amd64.deb")
HEADSCALE_USER = "ccc"  # the Headscale TAILNET user/namespace Arms enroll under — NOT the unix
                        # account below; two unrelated things this project happens to both call "user"
HEADSCALE_PORT = 8080
HEADSCALE_CONF = Path("/etc/headscale/config.yaml")
HEADSCALE_LIB = Path("/var/lib/headscale")
HEADSCALE_POLICY = Path("/etc/headscale/policy.hujson")  # empty policy == deny-all; see mesh._POLICY
# The coordinator's own TLS identity (Phase 9 #3) — distinct from the Head API's (setup.install_tls /
# CCC_TLS_DIR): different service, different reader. This one lives INSIDE HEADSCALE_LIB (0750
# headscale:headscale, set by the package — cuttle never loosens it) so the `headscale` unix account
# can read it with no ownership dance; anything else that needs these bytes goes through `sudo cat`
# (see mesh.trust_own_headscale_cert), the same way mesh.py already reads CCC_TS_STATE.
HEADSCALE_CERT = HEADSCALE_LIB / "headscale.crt"   # 0644 headscale:headscale
HEADSCALE_KEY = HEADSCALE_LIB / "headscale.key"    # 0600 headscale:headscale
HEADSCALE_SERVICE_USER = "headscale"  # the unix account systemd runs headscale as (package-created)
# Where a box that trusts the coordinator's cert installs it (mesh.trust_headscale_cert): the OS CA
# store, because `tailscaled` — not cuttle's own client — is what verifies it, and that store is the
# only lever it reads (see mesh.py's "trusting the coordinator's cert" section). Every node that has
# ever joined this mesh, Head included, has a copy here; teardown removes it (teardown._leave_ccc_mesh).
HEADSCALE_CA_PATH = Path("/usr/local/share/ca-certificates/ccc-headscale.crt")
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
HEADSCALE_DB = HEADSCALE_LIB / "db.sqlite"  # node keys, preauth keys — never touched directly except
                                            # by cuttle backup (see backup.py); everywhere else goes
                                            # through the `headscale` binary.

# --- backup/restore (Phase 8 loose end) ---
# head.db + arms.json + Headscale's db.sqlite are the Head's ENTIRE durable state. Losing all three to
# a Head-disk failure evicts the whole fleet (every Arm must physically re-enroll). Bundled into one
# timestamped tarball; getting it off this box (scp/rsync over the personal Tailscale) is a manual
# admin step, not cuttle's job — see backup.py.
BACKUP_DIR = Path("/var/backups/ccc")  # root:root 0700 — the bundle holds live secrets (session
                                       # tokens, Headscale preauth/node keys)
BACKUP_RETAIN = 3                     # the nightly timer prunes to this many newest tarballs
BACKUP_SCRIPT = Path("/opt/ccc/bin/ccc-backup.py")  # standalone (no cuttle import) so the timer's
                                                     # oneshot service runs it with a bare python3
BACKUP_SERVICE = "ccc-backup"
BACKUP_SERVICE_UNIT = Path("/etc/systemd/system/ccc-backup.service")
BACKUP_TIMER_UNIT = Path("/etc/systemd/system/ccc-backup.timer")

# --- stage-in/out (Phase 1 ④): rsync daemon on the Head, bound to the mesh ---
# A Spawn on a remote Arm has none of the submitter's files. The Head runs an rsync daemon on its
# mesh address ONLY (never 0.0.0.0 / the personal tailnet); the job pulls the project in and pushes
# results back. rsync 873 is privileged, so the daemon runs as root and each module drops to the Head
# user via uid=/gid=.
#
# Mesh membership is NO LONGER the authz (temp/spawn-isolation.md Phase 2). The two shared modules it
# used to serve — [project] over every upload, [spawns] over every Spawn's results — are gone: a
# payload on any Arm could name either URL and read every other User's code or overwrite any Spawn's
# `exit` marker, and nothing about a uid or a namespace closes a network service. What the daemon
# serves now is one pair of modules per Spawn, rooted at that Spawn's own upload and its own results
# dir, minted at submit and revoked when its transfers end — a **Siphon** (ccc_head_api.siphons).
# Those live in RSYNCD_DIR, which the Head API writes and rsyncd `&include`s; this file's job is the
# globals they inherit (mesh-only `hosts allow`, the drop to `ccc`) plus [bench], below.
RSYNCD_CONF = Path("/etc/ccc-rsyncd.conf")
RSYNCD_SERVICE = "ccc-rsyncd"
RSYNCD_UNIT = Path("/etc/systemd/system/ccc-rsyncd.service")
RSYNCD_PORT = 873
# Per-Spawn module files (+ their secrets), written by the Head API as `ccc`, re-read by rsyncd on
# every connection — so opening and closing a Siphon needs no reload, no signal, and no root.
RSYNCD_DIR = HEAD_STATE_DIR / "rsyncd.d"
# The one module that is NOT per-Spawn: the enrollment benchmark's stage-out target, rooted at the
# bench subtree so a benchmark can reach the Power files and nothing else. It cannot borrow a Siphon —
# it never goes through the Head API at all (enroll.py sbatches it directly) — so it carries a
# cluster-lifetime secret instead. Both the module and its secret stay in the ROOT-owned conf above,
# out of RSYNCD_DIR: that directory is writable by the Head API by necessity, and this is the one part
# of the daemon's config a compromised Head API should not be able to rewrite.
RSYNC_MODULE_BENCH = "bench"
RSYNC_BENCH_USER = "ccc-bench"
RSYNCD_BENCH_SECRETS = Path("/etc/ccc-rsyncd.bench.secrets")

# --- CCC user artifacts ---
# The OPERATOR's own dir — settings + credentials only. It holds no Spawn machinery: images live in
# CCC_IMAGES_DIR (world-readable, so the home-less Mantle can read them) and Spawn results live on the
# Head under HEAD_SPAWNS_DIR, fetched over the API. A submitter never needs a local .ccc/spawns.
# `$CCC_HOME` overrides the location, so a test (or a second cluster) can point cuttle at an isolated
# home instead of the operator's real `~/.ccc`. Without it a pty test inherits the live box's
# credentials and its menu state depends on whether the real Head is reachable — flaky by construction.
CCC_HOME = Path(os.environ.get("CCC_HOME") or Path.home() / ".ccc")
CONFIG_DIR = CCC_HOME / "config"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
# Pinned Head certificates, one PEM per host (trust-on-first-use; see tls.py). Keyed by host because a
# client may one day talk to more than one cluster, and a pin is only meaningful against the Head that
# presented it.
PINS_DIR = CCC_HOME / "pins"
# Every run's full transcript. The screen is a live frame now — progress scrolls past inside the box
# and is gone when it closes — so the log is the only place the detail survives. One file per run,
# named for when it started, so a failure days later still has its own.
LOG_DIR = CCC_HOME / "logs"
# CLI session bearer (Phase 3a): the token `cuttle login` stores, 0600, kept out of config.yaml so a
# submitter who never ran setup still has just this one secret file. The Head URL lives in config.yaml.
CREDENTIALS_FILE = CCC_HOME / "credentials"
# Auto-download bookkeeping (cuttle.watch): one `<id>.json` per Spawn whose results this box is still
# owed, plus its `<id>.log`. The FILE is the durable intent and the sweeper process is only the
# current attempt at it — which is what lets a reboot, a logout or a `pkill` cost nothing but time.
WATCH_DIR = CCC_HOME / "watch"
# Where an auto-download lands when the directory it was submitted from no longer exists. Results are
# the User's, never CCC's: nothing under here is ever expired or swept, unlike the Head's own copy.
OUTPUTS_DIR = CCC_HOME / "outputs"
# Packages newly installed by setup (before→after diff) — teardown purges exactly this set.
APT_MANIFEST = CONFIG_DIR / "apt-installed.txt"

# --- Head-box health monitoring (Phase 8 loose end) ---
# Disk/process-liveness/rogue-GPU-CPU checks on the Head itself, scoped Head-only (an Arm is a donor's
# machine, not ours to watch the same way — see docs/TODO.md). One standalone script (no cuttle
# import) on a systemd timer, run as `ccc` — nothing here needs root.
MONITOR_SCRIPT = Path("/opt/ccc/bin/ccc-monitor.py")
MONITOR_SERVICE = "ccc-monitor"
MONITOR_SERVICE_UNIT = Path("/etc/systemd/system/ccc-monitor.service")
MONITOR_TIMER_UNIT = Path("/etc/systemd/system/ccc-monitor.timer")
MONITOR_INTERVAL_MIN = 5
MONITOR_CHECKS = HEAD_STATE_DIR / "monitor_checks.json"  # thresholds + service list, ccc:ccc 0600
MONITOR_CONF = HEAD_STATE_DIR / "monitor.yaml"            # SMTP creds + recipient, ccc:ccc 0600
MONITOR_STATE = HEAD_STATE_DIR / "monitor_state.json"     # per-check last-alert time, ccc:ccc 0600
MONITOR_DISK_PATH = "/"
MONITOR_DISK_PCT = 85
# telemetry/CONTEXT.md's rogue-actor threshold (">10% util, 0 jobs"), applied to the Head's own node.
# Sustained over MONITOR_ROGUE_STREAK checks so one transient blip doesn't fire.
MONITOR_ROGUE_UTIL_PCT = 10
MONITOR_ROGUE_STREAK = 3
MONITOR_COOLDOWN_S = 24 * 3600  # at most one email per check-type per day
# Every daemon _setup_head installs — liveness-checked each run.
MONITOR_SERVICES = ["slurmctld", "munge", "headscale", CCC_TS_SERVICE, ENROLL_API_SERVICE,
                    HEAD_API_SERVICE, KARMA_SERVICE, RSYNCD_SERVICE]
