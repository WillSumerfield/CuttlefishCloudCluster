"""Single source of truth for MVP constants: pinned versions, paths, cluster identity."""

import os
from pathlib import Path

# --- pinned system stack (Ubuntu 24.04 noble / amd64) ---
# One version, two packages: the Head takes the full suite, an Arm takes the node daemon only (not
# slurmctld). Derived rather than written twice — a Head and an Arm on different Slurm versions is a
# cluster that fails at the protocol level, and two literals are one careless edit away from it.
SLURM_VERSION = "23.11.4-1.2ubuntu5"
SLURM_PKG = f"slurm-wlm={SLURM_VERSION}"
SLURMD_PKG = f"slurmd={SLURM_VERSION}"
# An Arm's ccc-launch reads its job back with `scontrol` (slurm-wlm carries it on a Head).
SLURM_CLIENT_PKG = f"slurm-client={SLURM_VERSION}"
NVIDIA_CTK_VERSION = "1.20.0-1"  # NVIDIA's repo retains old versions — safe
# The toolkit Depends on its siblings at EXACTLY its version, and apt will not pick an older-than-newest
# sibling on its own ("Depends: … (= 1.20.0-1) but 1.20.1-1 is to be installed") — so all four are pinned.
NVIDIA_CTK_PKGS = [f"{p}={NVIDIA_CTK_VERSION}" for p in
                   ("nvidia-container-toolkit", "nvidia-container-toolkit-base",
                    "libnvidia-container-tools", "libnvidia-container1")]
MUNGE_PKG = "munge"  # auth; distro version fine
# Every node: rootful Podman runs every container (cluster jobs, Dives, Tidepools) — see podman.py.
PODMAN_PKG = "podman"
# `podman run --init` (ccc-launch): PID 1 that passes Slurm's SIGTERM on. Only a Recommends of podman.
INIT_PKG = "catatonit"
# HEAD only: `htpasswd -B` writes the registry's bcrypt credentials (podman.install_registry).
HTPASSWD_PKG = "apache2-utils"

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

# HEAD ONLY. Node exists on a Head for exactly one reason — `vite` builds the Dashboard SPA
# (setup.build_spa) — and a donor Arm, which never builds it, never gets Node or this repo. Noble's
# own nodejs is 18.19 and Tailwind v4 wants >=20, so it comes from NodeSource. Pinned like the rest:
# node_22.x retains every 22.x build back to 22.0.0, so the pin stays fetchable.
# The ccc- prefix on both paths matters: teardown removes only ours. A box with its own NodeSource
# source gets none of ours (setup._box_has_nodesource) — same repo, two keyrings is fatal to apt.
NODE_PKG = "nodejs=22.22.2-1nodesource1"
NODESOURCE_REPO = "https://deb.nodesource.com/node_22.x"
NODESOURCE_KEY_URL = "https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key"
NODESOURCE_KEYRING = "/usr/share/keyrings/ccc-nodesource-keyring.gpg"
NODESOURCE_LIST = Path("/etc/apt/sources.list.d/ccc-nodesource.list")

# --- container images (podman.py) ---
# The base image's FROM. By DIGEST, not tag: `ubuntu:24.04` is re-pushed monthly. 24.04 to match
# UBUNTU_CODENAME — one Ubuntu release fleet-wide.
BASE_IMAGE_DOCKER = ("docker.io/library/ubuntu@sha256:"
                     "561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea")
# The base + benchmark images' tag. **Frozen per release**: a Dive's User layer is only valid on the base
# it was committed from, so the base never changes inside a release (upgrade = clean install).
IMAGE_TAG = "2.0"
# CUDA compiler + headers for building extensions; the runtime libs come in wheels, the driver via CDI.
CUDA_REPO_KEYRING = ("https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/"
                     "cuda-keyring_1.1-1_all.deb")
CUDA_PKGS = ("cuda-nvcc-12-8 cuda-cudart-dev-12-8 cuda-cccl-12-8 cuda-nvrtc-dev-12-8 cuda-nvtx-12-8 "
             "cuda-profiler-api-12-8")
CUDA_HOME = "/usr/local/cuda-12.8"
# The registry itself, pinned by index digest (registry:2 = 2.8.3).
REGISTRY_IMAGE = ("docker.io/library/registry:2.8.3@sha256:"
                  "a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373")

# --- pinned Environment build tools (every node) ---
# uv and micromamba build a Dive's declared Environment (ccc_head_api.environments). Pinned by
# version AND sha256, checked here rather than against the sidecar .sha256 each project publishes: a
# sidecar fetched from the same host as the file it vouches for proves only that the download wasn't
# truncated. These hashes are in the repo, so bumping a tool is a reviewed commit — a uv release can change what `uv sync` resolves and every
# Dive's dependencies would move underneath their Users with nothing to point at.
#
# Installed on EVERY node, Head and Arm alike, because every node builds: an Environment is built by
# the Arm that runs it, at job start, from its own bandwidth. A Head-built env had to cross the mesh —
# measured at 2.82 MB/s to a remote Arm, which is ~29 minutes for a 4.6 GB torch env, against a
# benchmark's 10-minute wall. The manifest is kilobytes and PyPI is fast from everywhere.
#
# Pinned by `uname -m` on the node doing the installing, so a mixed-arch fleet is fine. Both are static
# binaries with no runtime deps; uv ships a tarball, micromamba a bare binary.
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
SLURM_CLUSTER = "ccc"      # Slurm's ClusterName — not the Cluster name below
PARTITION = "ccc"
# The **Cluster name** (CONTEXT.md): a short name chosen at `setup --head` (default: the Head's
# hostname). The domain every machine CCC installs on uses to reach the Head is `ccc.<name>`; the
# User's ssh aliases are `<name>` (Tidepool) / `<name>-<id>` (Dive). Changing it = teardown + setup:
# it is baked into the cert, pins and installer bundles.
#
# Stored box-wide in CLUSTER_NAME_FILE (Head: setup --head; Arm: enrollment; root helpers read it
# too), else per-user in settings["cluster"] (a client: `setup --client` / `cuttle login`).
#
# Nothing resolves the domain. CCC has no DNS: the mesh runs no resolver and there is no router to
# configure, so `setup` writes it into /etc/hosts on every box it touches (system.write_hosts_entry),
# pointed at whichever address THAT box can reach the Head on — 127.0.0.1 on the Head, the Head's mesh
# IP on an Arm, the configured address on a client. A device CCC was never installed on has no way to
# learn it and reaches the Dashboard by IP instead.
CLUSTER_NAME_FILE = Path("/etc/ccc/cluster-name")   # 0644; ccc_launch/ccc_tidepool/Head API read it
CLUSTER_NAME_RE = r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"


def cluster_name() -> str:
    try:
        return CLUSTER_NAME_FILE.read_text().strip()
    except FileNotFoundError:
        pass
    from . import settings
    if name := settings.load().get("cluster"):
        return name
    raise RuntimeError("this box does not know its cluster's name — run `cuttle login`")


def domain() -> str:
    return f"ccc.{cluster_name()}"


# The marker that makes our /etc/hosts line ours: rewritten in place on re-setup, removed by teardown.
HOSTS_MARKER = "# cuttle"

# --- GPU devices (distro Slurm lacks NVML; map GRES to the device nodes explicitly) ---
# There is no constant here on purpose: a node's device list is DETECTED (system.gpu_devices()), not
# assumed. CCC does its own hardware **Registration** at enrollment, which is exactly the job
# `AutoDetect=nvml` would have done — so rebuilding slurm-wlm against NVML would buy a Slurm fork and
# an `apt-mark hold` blocking security updates, to autodetect what we already detect ourselves.

# --- Karma substrate: priority, Power, VRAM features ---
# Nice is how Karma reaches the queue: the Head lowers the priority of a poor-standing User's pending
# Dives. It can do that with ZERO privilege, because every Dive runs as the Mantle — so the Head is
# the *owner* of every job in the queue, and lowering your own job's priority is an owner's right.
# (Only NEGATIVE nice — a boost — needs an operator; that's the admin Queue bump, which goes through
# sudo. See docs/Architecture.md, "Scheduling: Slurm, and no accounting database".)
#
# Treat a Dive's priority as a SIGNED int32 whose zero sits at 2^31. Slurm's priority is a `uint32_t`
# and it clamps: with no base, a *fresh* Dive (age factor 0) computes `-nice`, which underflows to the
# floor — so every new Dive from every User in less than perfect standing would tie there, and Karma
# would rank correctly, push nice successfully, and change nothing. The flat base is what keeps the
# subtraction off the rail. PriorityWeightPartition is the only base available without slurmdbd, and
# with a single partition Slurm normalizes the partition factor to exactly 1.0 for every job — so it
# adds a constant, reorders nobody, and exists purely to lift the range clear of zero.
PRIORITY_BASE = 2**31          # PriorityWeightPartition — the flat base under every Dive
NICE_MAX = 2**16               # nice = (1 - Share) * NICE_MAX, so standing spans [0, NICE_MAX]
# Equal ranges for nice and age, so poor standing costs a Dive AT MOST a day of apparent queue age —
# Slurm treats it as though it arrived later than it did. Bounded and intelligible. It is NOT a
# starvation bound (age saturates at PRIORITY_MAX_AGE; a rival can max its age out too) — that bound is
# economic: spending Karma is what makes you poor. See temp/KARMA.md "the priority floor".
PRIORITY_WEIGHT_AGE = NICE_MAX
PRIORITY_MAX_AGE = "1-0"       # 1 day — the ramp over which waiting earns back a full nice range
# One admin Queue bump. Bigger than age + nice combined, so a bumped Dive beats anything the economy
# can produce; negative nice is operator-only, which is what makes it a flag needing no state of its own
# (the tick skips any job with nice < 0 — only an operator could have set it).
BUMP_STEP = 2**17

# **Power**: measured throughput, in fp32 TFLOP/s, of one device — benchmarked once, at enroll, by the
# Head-submitted **Benchmark** job, in the benchmark image via ccc-launch (templates.BENCH_PY/BENCH_CU). Intrinsic to the card; the other
# half of a device-second's price is Demand, which is live and extrinsic.
BENCH_MATRIX_N = 4096
BENCH_WARMUP = 5
BENCH_ITERS = 50
BENCH_TIME_MINUTES = 10
BENCH_TIMEOUT_S = 600     # from sbatch to the result landing (a cold node may still be booting slurmd)

# Pinned: Power is the divisor in every Settlement, so every card must be measured by the same cuBLAS
# (the base's CUDA minor). Baked into the benchmark image (podman.build_images), pulled by digest.
BENCH_CUBLAS_PKG = "libcublas-12-8"
BENCH_CUBLAS_DEV_PKG = "libcublas-dev-12-8"   # build stage only
BENCH_BIN = "/opt/ccc/bench"         # inside the benchmark image: the compiled SGEMM (templates.BENCH_CU)
BENCH_PY_PATH = "/opt/ccc/bench.py"  # inside the benchmark image
BENCH_ARGV = ["python3", BENCH_PY_PATH]

# Slurm schedules the LOWEST-weight eligible node first, so Weight must run inverse to Power: the
# fastest free card that fits wins. It is a preference among *free* nodes, not a reservation — Slurm
# will not hold a Dive back waiting for a better card, which is exactly the "first one that fits, else
# queue" behaviour we want.
WEIGHT_SCALE = 10000


def node_weight(powers: list[float]) -> int:
    """A node's Slurm Weight, from its per-**Device** Powers. Takes the list and reduces to the MEAN.

    Weight is inherently node-level — Slurm has one per node, not one per GRES — so a multi-GPU box's
    cards must be summarised somehow. The mean is the honest summary *here specifically*, and only
    here: Weight is a **preference ordering among nodes**, never a price. It decides which free box
    Slurm reaches for first; it never multiplies a second of held time. A Dive's bill is still
    settled against the Power of the Device it actually held (tick._meter), so the reduction cannot
    reach anyone's Balance. Max would over-prefer a box whose one fast card is already busy; the mean
    ranks a box by what it is typically worth to land on.
    """
    return max(1, round(WEIGHT_SCALE / (sum(powers) / len(powers))))


def card_gb(vram_mb: int) -> int:
    """How many whole GB a card counts as — **rounded up**.

    A card almost never reports the round number it is sold as. A 4090 reports 24564 MiB (over), and a
    16 GB card with ECC on, or one handed through a virtualization layer, reports well under 16384
    (under) — ECC alone reserves several percent. Flooring advertises the second as `vram15`; rounding
    advertises anything below 15.5 GB the same way. Either leaves a User looking at a 16 GB card that
    refuses `--vram 16`, which is the confusion this exists to stop.

    The cost is real and bounded: a card counts as up to 1 GB larger than it is, so a Dive that asks
    for exactly what the card claims may OOM instead of being refused at submit. That is the trade
    deliberately made — a number a User can predict from the box's spec sheet beats a number only the
    driver knows.

    **Mirrored in `ccc_head_api.slurm.card_gb`**, which cannot import this module (see the note at the
    bottom of this file). The Arm advertises with this one and the Head selects with that one, so the
    two must agree exactly or a Dive is placed on a card the Head never priced.
    """
    return -(-vram_mb // 1024)


def vram_feature(vram_mb: int) -> str:
    """The Slurm feature a device advertises: `vram24` for a 24 GB card."""
    return f"vram{card_gb(vram_mb)}"


def node_features(vram_mb: int, feature_names: list[str] | None = None) -> str:
    """Every Slurm Feature a node carries, as the one comma-joined set Slurm stores.

    The vram bucket and the declared Arm Features are one flat set to Slurm — there is no structural
    difference between them once rendered (see `enroll.slurmd_dropin`). Written once here because two
    places state it and `scontrol update` takes the whole set at a time: a re-push that named only the
    Arm Features would silently drop the node out of `--vram` matching, and one that named only the
    bucket would undo the Features it was issued to restore.
    """
    return ",".join([vram_feature(vram_mb), *sorted(feature_names or [])])


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
# slurmd runs this on the node after every Dive, however it ended — the reclaim the job script's own
# `trap ... EXIT` can't do because SIGKILL (OOM, `scancel -9`, a Phase-2 Retract) bypasses the trap.
SLURM_EPILOG = Path("/etc/slurm/ccc-epilog.sh")
# slurmd runs this on the node BEFORE every Dive, as root: it creates the **Guise** the Dive will run
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
# fleet does not have yet (see enroll.arms_missing).
#
# Two names, not one, and they are gated independently: an Arm enrolled before job_container/tmpfs
# shipped reports only "prolog", so folding tmpfs into that name would either hold `Prolog=` back on
# an Arm that has it, or claim tmpfs of a node with no job_container.conf — which drains it.
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
# reach slurmd as three mangled arguments the moment `--conf` grew a second key (Feature=vramN is what
# a Dive's --vram matches). Quotes written literally in a unit file ARE honoured,
# so the drop-in states the whole command and keeps the space-separated --conf intact.
SLURMD_DROPIN = Path("/etc/systemd/system/slurmd.service.d/ccc.conf")
MUNGE_KEY = Path("/etc/munge/munge.key")  # 0400 munge:munge — cluster-wide, never expires
# The Head mints that key with this (see setup.ensure_munge_key). munge 0.5.15 (noble) ships
# `mungekey`; the older `create-munge-key` is gone, and we are pinned to 24.04.
MUNGEKEY_BIN = "/usr/sbin/mungekey"

# --- Shared API package venv ---
# Both head-side FastAPI apps live in one package (`ccc_head_api`) and run from this one venv, kept
# out of the CLI's own venv. The Enroll API app and the Head API app are two ASGI targets off it.
API_VENV = Path("/opt/ccc/api/.venv")
# The interpreter that venv is built ON, named explicitly for two reasons — leave either out and the
# build succeeds and the SERVICE fails:
#   * version — ccc_head_api declares requires-python >=3.12, and `uv venv` with no version picks
#     whatever it likes best. Under sudo it does not see the operator's UV_PYTHON_INSTALL_DIR, so it
#     reaches for root's own uv-managed set and can land on an older one. 24.04 ships 3.12 and
#     `preflight` refuses any other release, so this is the platform's version, not a preference.
#   * system, not uv-managed — a uv-managed interpreter lives under `/root/.local/share/uv/` when uv
#     runs as root, and /root is 0700. The venv would build fine and then be unexecutable by the
#     accounts that actually run these services (munge, ccc), which `chmod -R a+rX /opt/ccc` cannot
#     fix because the interpreter is not under /opt/ccc at all.
API_PYTHON = "3.12"

# --- Enroll API (③: /enroll; mesh-only, serves the munge key) ---
# Bound to the Head's MESH address only: being on the mesh is the entire authorization. Glossary
# (head/CONTEXT.md): the *Enroll API* is the durable, mesh-only trust boundary (holds the munge key).
ENROLL_API_PORT = 8081
ENROLL_API_SERVICE = "ccc-enroll-api"
ENROLL_API_UNIT = Path("/etc/systemd/system/ccc-enroll-api.service")
# Runs as munge: the only user that can read munge.key without chmod'ing it or running as root.
# It is otherwise UNPRIVILEGED — it fills in the hardware fields of a registry row an operator already
# created, and the operator admits the Arm with `cuttle arm admit`. Its one sudo grant
# (SUDOERS_MUNGE_HEADSCALE below) is a single parameterless command with nothing a POST body can steer —
# no wildcard, no argument taken from the request, nothing else headscale's CLI can do.
SUDOERS_MUNGE_HEADSCALE = Path("/etc/sudoers.d/ccc-munge-headscale")
ENROLL_API_USER = "munge"
CCC_LIB = Path("/var/lib/ccc")          # munge-owned; the Enroll API's only writable location
# The package download cache that makes a per-Dive build affordable — NOT an env cache. Node-local,
# never staged anywhere. Without it every Dive re-downloads its whole dependency set; with it, uv and
# micromamba hard-link a rebuild out of what is already on disk. Under CCC_LIB so both teardowns already
# sweep it (remove_arm_state on an Arm, remove_enroll_api on the Head).
#
# **A PARENT, not the cache.** Each **Guise** gets its own subtree, created by the job script as that
# Guise, by the job script's Environment build (uncalled since 2.0). Only this directory is 0777, and that is all it is for: a
# mkdir point. Two reasons it is not one shared tree:
#
#   * Permissions do not work. uv creates its whole subtree with the CREATOR's ownership and umask
#     (0755), so the first uid to build owns it and every later one gets EACCES. 0777 here does not
#     propagate. Found on hardware 2026-08-08: `cuttle setup` built the benchmark env as the operator
#     and locked every Guise out of `uv/sdists-v9`.
#   * A shared writable cache is a supply chain between Users. One could plant a tampered wheel that
#     another's build hard-links straight into their Environment. Donors are trusted (they own the
#     hardware); two Users of the same cluster are not each other's.
#
# The cap below is therefore PER GUISE. A box's total is that times the number of Users who have ever
# built here, which on a donated box is a handful — the honest trade for isolation.
#
# Whole-subtree pruning at the cap, not per-entry LRU: both tools hard-link OUT of these trees, so
# evicting one entry needs their own bookkeeping to be safe. Emptying costs re-downloads and nothing
# else — it holds no state any Dive depends on.
NODE_ENV_CACHE_DIR = CCC_LIB / "envcache"
NODE_ENV_CACHE_MAX_GB = 20
# The Arm registry. `cuttle arm authorize` CREATES each row (owner + name) before the donor's token is
# issued — /enroll refuses a name that isn't here, so mesh membership alone can't fetch the munge key.
# The donor's POST only fills in gpu_devices + mesh_ip; it never gets to say who owns the machine.
# `head: true` marks the Head's own co-located Arm (admin-owned, never enrolls, GRES lines come from
# templates.head_gres_node() — so gres rendering skips it).
# `gpu_devices` and `powers` are parallel lists, index-aligned, and that alignment is load-bearing:
# it is what turns a Slurm GRES index back into the Power a Dive is settled at. There is deliberately
# no node-level `power` beside them — a summary stored next to the list it came from is a second
# number that can silently disagree with it. Where one is needed it is derived at the point of use.
ARMS_REGISTRY = CCC_LIB / "arms.json"   # {name: {owner, gpu_devices, powers, mesh_ip, admitted, ...}}

# --- Arm Features (temp/FEATURES_BUILTIN.md) ---
# Named capabilities an Arm may report having: heavy, hardware- or licence-gated software that is not
# installed everywhere. BUILT IN rather than admin-curated, because CCC knows what each name MEANS.
# At minimum that is placement plus `$<NAME>_PATH`; a name may also carry a runtime profile (see
# ccc_head_api.features), in which case CCC arranges the env vars, binds, cache and licence a Dive
# needs. A name CCC has never heard of would be a Slurm `Feature=` token with neither: an Arm could
# advertise it and nothing on the other end would know what to do with it. So the catalog is a
# constant, and an Arm picks FROM it at `cuttle setup`.
#
# Mirrored in ccc_head_api.enroll and .slurm, which cannot import this module (see the note at the
# bottom of this file on why the CLI and API packages stay separate). They must agree.
FEATURE_NAMES = ("vlearn", "isaaclab")
# Each name also becomes a Slurm Feature= token and (uppercased) an env var name. Kept as a check on
# what arrives over the wire at /enroll — the CLI no longer takes a typed feature name anywhere.
FEATURE_NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
# Where `cuttle setup --arm` records this box's declared features + install paths, so a Dive's payload
# can find e.g. $VSIM_PATH without knowing whether this node has it — the
# Dive job script sources this unconditionally (a no-op where it's absent).
ARM_FEATURES_ENV = Path("/etc/ccc/features.env")
# This Arm's per-Arm enrollment secret (F1): a high-entropy token minted at `cuttle arm authorize`,
# carried to the Arm inside the enrollment token, and persisted here 0600 root. Presented as an
# `Authorization: Bearer` on every /enroll, so being on the mesh (which a Dive can forge by NATing as
# the Arm's IP) no longer buys the munge key — only a host that can read this 0600 file can. The Head
# stores only its sha256; see head/api enroll.py.
ENROLL_KEY = Path("/etc/ccc/enroll.key")
# This Arm's **Licence** directory, exported into that same file. ONE per Arm regardless of how many
# Features it offers: a licence is locked to the machine, and an Arm is one machine — so two Features
# can never collide over it. The Dive job script binds it at the Guise's home inside the container
# (the only place a licence manager looks), and only for a Dive that requested a Feature.
LICENCE_HOME_VAR = "CCC_LICENCE_HOME"
# The base + benchmark images' digest refs, written by `setup --head` (podman.build_images) and handed
# to every Arm at /enroll. In /opt, kept by a plain teardown with the images it names.
IMAGES_RECORD = Path("/opt/ccc/images.json")
# An Arm refuses setup with less free than this on Podman's store (and, on WSL, the Windows drive the
# vhdx lives on): the images, Dive layers and job scratch all land there.
CONTAINERS_STORE = Path("/var/lib/containers")
WSL_WINDOWS_DRIVE = Path("/mnt/c")
DONOR_DISK_FLOOR_GB = 25
# Per-Arm registry pull passwords {arm: password}, 0600 munge — /enroll hands each Arm its own.
REGISTRY_CREDS = CCC_LIB / "registry-creds.json"

# An Arm's GPU device node, one /dev/nvidiaN per card — or, on a WSL Arm, the one /dev/dxg that fronts
# every GPU (so a WSL Arm has exactly one card; setup.preflight). Re-validated root-side before it ever
# reaches gres.conf.
GPU_DEVICE_RE = r"^/dev/(nvidia[0-9]+|dxg)$"
WSL_GPU_DEVICE = "/dev/dxg"
# What a WSL donor runs in Windows so the distro (and slurmd) stays up without a terminal open.
WSL_KEEPALIVE_TASK = "CCC Arm keep-alive"

# --- Head API (⑥: read board + SPA + auth + submission): LAN+mesh ---
# The LAN+mesh service backing both the web Dashboard and the CLI (glossary: *Head API*). Split from
# the Enroll API on purpose: /enroll hands out the munge key and MUST stay mesh-only, but the Head
# API must reach the LAN too. Bound 0.0.0.0. Reuses API_VENV (same package, different app object). A
# remote Arm sees it over the mesh; a LAN box sees it too (needs a ufw port rule — system.allow_tcp_port).
# 443, so the **Cluster name** is the whole address: `https://ccc.<name>`, nothing to append. The CLI
# could always default a port (it takes a bare host and fills one in); a browser cannot be taught one,
# and the Dashboard is a browser surface. Costs a capability — the Head API runs as HEAD_API_USER, and
# a non-root process cannot bind <1024 without CAP_NET_BIND_SERVICE (see headapi._head_unit) — and
# claims the box's one 443, which is why the Head is a box CCC owns outright.
HEAD_API_PORT = 443  # 8080 = headscale, 8081 = enroll API
HEAD_API_SERVICE = "ccc-head-api"
HEAD_API_UNIT = Path("/etc/systemd/system/ccc-head-api.service")
# The **Mantle** (glossary): the single service identity every Dive executes as, on every node. On the
# Head it also runs the Head API, owns the auth DB (head.db), and submits via the munge socket — never
# reading the munge key.
HEAD_API_USER = "ccc"
# Slurm runs a job as the *submitting uid*: slurmctld records the Head API's uid at sbatch, and every
# slurmd setuid()s to it before launching. So that uid MUST resolve to the same account on every node —
# a cluster-wide pin, not a per-box `useradd --system` (which allocates dynamically). 60000 sits in
# Debian's reserved block AND below systemd's DynamicUser range (61184–65519), so no transient service
# can have taken it. Collision ⇒ useradd fails and setup halts (no fallback; decide live).
CCC_UID = 60000
CCC_GID = 60000
# --- Guises: a per-User Unix identity on every node ---
# A **Guise** is what a Dive runs as. The Mantle above is what the *cluster* is — no Dive runs as
# it, and one that does is a bug. Every Guise is `ccc-u<uid>`, named after its uid and
# nothing else: the Prolog that creates the account on a donor's Arm has `SLURM_JOB_UID` and no other
# input at all (no network, no head.db), so the name must be derivable from the uid alone. It also
# cannot collide with a donor's own accounts, and needs no CCC-username regex.
GUISE_PREFIX = "ccc-u"
# Reserved for the cluster itself — the enrollment benchmark, the one Dive no User submits. It is a
# Guise like any other, so that "a Dive owned by `ccc` is a bug" has no exceptions.
GUISE_CLUSTER_UID = 60001
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

# --- the Licence account (temp/FEATURES_LICENCE.md) --------------------------------------------
# An account that exists for ONE reason: its home directory is this Arm's **Licence** directory, so
# that running a vendor's activation as it writes the activation state THERE instead of into a human's
# home. TurboActivate resolves its state directory through getpwuid(getuid())->pw_dir and ignores
# $HOME, so the only way to choose where a licence lands is to choose which uid activates it.
# Hand-copying the files instead does not work for long — they are named
# from an obfuscated per-release token (absent from both TurboActivate.dat and the vlearn wheels, so
# underivable), and every version bump renames all of them.
#
# NOT a pinned uid, unlike the Mantle above. That pin exists because Slurm setuid()s to the submitting
# uid on every node, so the Mantle must be the same uid cluster-wide. This account never runs a Dive,
# never appears to Slurm and never leaves the box that holds the licence — a locally-allocated system
# uid is correct, and staying out of the reserved 60000+ block means it can never shadow a Guise.
LICENCE_USER = "ccc-licence"
LICENCE_GECOS = "CCC Licence"   # matched as a prefix by teardown, exactly like GUISE_GECOS


def guise_name(uid: int) -> str:
    """The Unix account a Dive owned by this uid runs as. The ONLY place the name is formed."""
    return f"{GUISE_PREFIX}{uid}"
# The first CCC login account, seeded at `setup --head`. A fixed name (NOT the operator's OS username):
# it's an app-level account in head.db, unrelated to any Unix uid. Dives show `submitted_by: admin`.
FIRST_ADMIN_USER = "admin"
HEAD_API_WEB_DIR = Path("/opt/ccc/head-api/web")  # built SPA, world-readable
HEAD_API_WIKI_DIR = Path("/opt/ccc/head-api/wiki")  # built wiki (Quartz), served at /wiki/ behind sign-in
# LAN TLS. The Head API carries passwords and session tokens on `0.0.0.0`, so the LAN path must be
# encrypted. No public CA will sign a cert for a box with no public domain, so the Head is its OWN CA:
# it mints a root once and signs its serving cert (the "leaf") with it.
#
# A CA rather than a bare self-signed leaf, for two reasons that are really one:
#   * every box CCC installs on TRUSTS the CA — OS store for curl/python, NSS + Firefox policy for
#     browsers (system.trust_ccc_ca*) — so the Dashboard loads clean, no "not private" warning;
#   * the leaf can then be REISSUED (a new SAN, a changed LAN IP, expiry) without invalidating anything,
#     because what is trusted and pinned is the CA, not the leaf. A bare self-signed cert had to be
#     kept forever or every client broke at once.
#
# Identity still comes from PINNING (cuttle/tls.py): cuttle verifies the Head against this CA alone,
# never the public bundle. A client that has no CA yet gets it on first contact, SSH-known_hosts style.
#
# Lives in /opt (not CCC_LIB) so a plain teardown preserves it, like the images — a NEW CA is the one
# thing that would still invalidate every client, so it is minted once and kept (`--all` clears it).
CCC_TLS_DIR = Path("/opt/ccc/tls")
HEAD_CA_CERT = CCC_TLS_DIR / "ccc-ca.crt"  # 0644, the trust anchor every box installs and cuttle pins
HEAD_CA_KEY = CCC_TLS_DIR / "ccc-ca.key"   # 0600 ccc — signs leaves; never leaves the Head
HEAD_CERT = CCC_TLS_DIR / "head.crt"   # 0644, the serving cert; reissued freely under the CA above
HEAD_KEY = CCC_TLS_DIR / "head.key"    # 0600 ccc — only the Head API may read it
CERT_DAYS = 3650                       # effectively never expires; a pin outliving the cert is worse
# Where a box that trusts this cluster installs the CA. Same store, same mechanism and the same reasons
# as HEADSCALE_CA_PATH below — a distinct file so each can be removed without disturbing the other.
HEAD_CA_TRUST_PATH = Path("/usr/local/share/ca-certificates/ccc-head.crt")
HEAD_CA_NICKNAME = "CCC Head CA"       # the name it goes in (and comes out) of an NSS db under
# Chromium-family browsers (Chrome, Chromium, Edge, Brave) share ONE per-user NSS db on Linux and
# ignore the OS store entirely, so trusting the CA there is a separate act from update-ca-certificates.
# Per-user by construction: a second login account on the same box does its own import.
NSSDB_DIR = ".pki/nssdb"               # relative to $HOME
NSS_TOOLS_PKG = "libnss3-tools"        # provides `certutil`

# --- the OS around a guest install (cuttle/guest.py) ------------------------
# Three stores above cover every browser on THIS box. On a guest — Crostini under ChromeOS — the
# browser its owner actually uses is on none of them: it runs on the host OS, which has neither the
# **Cluster name** nor this CA, and which CCC cannot install onto. So the host OS's owner is handed an
# artifact instead, by name — where it lands is the host OS's business.
GUEST_CA_NAME = "ccc-ca.crt"           # the CA, dropped where the host OS can import it by hand
# ChromeOS's own Downloads, as Crostini sees it — but ONLY once its owner has shared that folder with
# Linux (Files app -> right-click Downloads -> Share with Linux). It is not shared by default, which is
# why anything dropped there falls back to $HOME: the home directory is always visible to the Files app
# as "Linux files", so it is the one drop point that cannot be missing.
CHROMEOS_DOWNLOADS = Path("/mnt/chromeos/MyFiles/Downloads")

# Firefox ignores both stores above and reads an enterprise policy file instead. Cuttle merges its
# keys into an existing file rather than clobbering whatever else is configured there.
FIREFOX_POLICIES = Path("/etc/firefox/policies/policies.json")
# The CA, copied where FIREFOX CAN READ IT. `Certificates.Install` takes absolute paths, but Ubuntu
# ships Firefox as a snap whose confinement grants read on exactly one host tree — `/etc/firefox`
# (`snap connections firefox` -> the etc-firefox system-files plug). HEAD_CA_TRUST_PATH is outside it,
# so pointing the policy at that path silently installs nothing.
#
# `Certificates.Install` rather than `ImportEnterpriseRoots`: the latter is a Windows/macOS policy —
# on Linux it parses, applies, and trusts nothing, which looks exactly like success.
FIREFOX_CA = Path("/etc/firefox/ccc-head.crt")
# Auth store: the User + Session tables behind login. Its own ccc-owned subdir (0700) —
# NOT loose in the munge-owned CCC_LIB, since SQLite writes WAL/journal sidecars beside the db and
# `ccc` can't create files in a munge-owned dir. 0600 db, seeded at `setup --head`.
HEAD_STATE_DIR = CCC_LIB / "head"
HEAD_DB = HEAD_STATE_DIR / "head.db"
# bench/<node>/power — where an enrollment benchmark stages its result (the rsync [bench] module).
HEAD_BENCH_DIR = HEAD_STATE_DIR / "bench"
CCC_OPT = Path("/opt/ccc")              # every /opt path below hangs off this; teardown rmdirs it last

# --- Updates from the Head (dist.py, rollout.py) ---
# The Head builds the `cuttle` wheel from its checkout at every setup/update and serves it: to laptops
# through the Head API (they update themselves), to Arms through the Enroll API (the Head pushes the
# update as a root Slurm job). build.json = {"build": <hash>, "wheel": <filename>}. World-readable:
# the code is not secret, and both APIs run as service users.
CUTTLE_DIST_DIR = CCC_OPT / "cuttle-dist"
CUTTLE_DIST_META = CUTTLE_DIST_DIR / "build.json"
# F3: the Head signs the served wheel with an ed25519 key, so a Head impersonator on the mesh cannot
# push forged code to an Arm. The keypair lives in CCC_TLS_DIR beside the CA key, for the same reason
# — an ordinary teardown KEEPS it (only `--all` wipes TLS), so a Head redeploy keeps the same key and
# every Arm that pinned its public half stays valid. Private 0600 ccc; public PEM 0644.
ROLLOUT_SIGN_KEY = CCC_TLS_DIR / "rollout-sign.key"
ROLLOUT_SIGN_PUB = CCC_TLS_DIR / "rollout-sign.pub"
# The wheel's detached signature sits beside the wheel in CUTTLE_DIST_DIR as `<wheel>.sig`, served at
# /cuttle/wheel.sig. On an Arm, the Head's pubkey is PINNED here at `cuttle arm authorize` (carried in
# the enrollment token like the F1 enroll secret) and checked in the update job before every install.
# 0644 root — the pubkey is public, but only root writes it.
ROLLOUT_PUBKEY_ARM = Path("/etc/ccc/rollout-sign.pub")
# Written on each Arm at setup, as its owner: where their cuttle and uv live, so the root update job
# can reinstall and re-run cuttle as they would.
ARM_UPDATER_RECORD = Path("/etc/ccc/arm-updater.json")
ROLLOUT_SCRIPT = CCC_OPT / "bin" / "ccc-arm-rollout.py"   # standalone, root, on a timer
ROLLOUT_STATE = CCC_LIB / "arm-updates.json"              # {arm: {target, attempts, status, error}}, 0644
ROLLOUT_SERVICE = "ccc-arm-rollout"
ROLLOUT_SERVICE_UNIT = Path("/etc/systemd/system/ccc-arm-rollout.service")
ROLLOUT_TIMER_UNIT = Path("/etc/systemd/system/ccc-arm-rollout.timer")
ROLLOUT_ATTEMPTS = 3          # the first try plus two retries
ROLLOUT_PREFIX = "ccc-update-"   # each Arm's reservation and job are named this + the Arm

# --- Podman (podman.py) ---
REGISTRY_PORT = 5000


def registry() -> str:
    """`ccc.<name>:5000` — Head → 127.0.0.1 (push), Arm → mesh (pull)."""
    return f"{domain()}:{REGISTRY_PORT}"


REGISTRY_SERVICE = "ccc-registry"         # Quadlets ccc-registry-push / ccc-registry-pull
REGISTRY_ETC = Path("/etc/ccc/registry")  # configs + htpasswd files, 0700 root
REGISTRY_PUSH_HTPASSWD = REGISTRY_ETC / "push.htpasswd"
REGISTRY_PULL_HTPASSWD = REGISTRY_ETC / "pull.htpasswd"
REGISTRY_PUSH_USER = "ccc-push"
REGISTRY_DATA = CCC_OPT / "registry"      # the store; kept unless --all, like the images
# This node's registry credential (push on the Head, its own pull user on an Arm). Must equal
# ccc_launch.AUTH_FILE.
REGISTRY_AUTH = Path("/etc/ccc/registry-auth.json")
QUADLET_DIR = Path("/etc/containers/systemd")
# NVIDIA's own nvidia-cdi-refresh.service writes CDI_SPEC; ccc-cdi-patch makes Podman 4.9 read it.
CDI_SPEC = Path("/var/run/cdi/nvidia.yaml")
CDI_PATCH_BIN = Path("/usr/local/sbin/ccc-cdi-patch")
CDI_PATCH_SERVICE = "ccc-cdi-patch"
CDI_PATCH_SERVICE_UNIT = Path("/etc/systemd/system/ccc-cdi-patch.service")
CDI_PATCH_PATH_UNIT = Path("/etc/systemd/system/ccc-cdi-patch.path")
# The root launcher (ccc_launch.py) and the one sudoers rule letting a Guise run it. JOBS_DIR and
# JOB_CONTAINER_PREFIX must equal ccc_launch's.
CCC_LAUNCH_BIN = Path("/usr/local/sbin/ccc-launch")
SUDOERS_CCC_LAUNCH = Path("/etc/sudoers.d/ccc-launch")
JOBS_DIR = Path("/var/lib/ccc/jobs")
JOB_CONTAINER_PREFIX = "ccc-job-"

# --- Tidepool: each User's fixed-size image + always-on container on the Head (ccc_tidepool.py) ---
# The helper's constants MUST equal these (tests/test_tidepool.py checks). Images live OUTSIDE
# CCC_LIB on purpose: teardown `rm -rf`s CCC_LIB, and keeps images unless --all.
TIDEPOOL_HELPER_BIN = Path("/usr/local/sbin/ccc-tidepool")
SUDOERS_CCC_TIDEPOOL = Path("/etc/sudoers.d/ccc-tidepool")
TIDEPOOL_SETTINGS = Path("/etc/ccc/tidepools.json")   # setup --head's answers, 0644 (API reads size)
TIDEPOOL_RECORDS = Path("/etc/ccc/tidepools.d")       # <user>.json {uid}, root
# Written by a teardown that KEPT the images: their paths (JSON list), 0644. Outside /etc/ccc so it
# outlives the teardown; it marks the box as a torn-down Head, so a later `teardown --all` deletes them.
TIDEPOOL_KEPT = Path("/etc/ccc-tidepools-kept.json")
TIDEPOOL_MNT_ROOT = CCC_LIB / "tidepools"              # <user>/ = the loop-mounted image
TIDEPOOL_KEYS_ROOT = HEAD_STATE_DIR / "tidepool-keys"  # <user>/authorized_keys (ccc) + host key .pub
TIDEPOOL_FILES_DIR = Path("/usr/local/lib/ccc/tidepool")  # init, sshd_config, zshrc → /etc/ccc/tidepool
TIDEPOOL_SERVICE = "ccc-tidepool"                      # ccc-tidepool@<user>.service
TIDEPOOL_UNIT = Path("/etc/systemd/system/ccc-tidepool@.service")
TIDEPOOL_SLICE = "ccc-tidepools.slice"
TIDEPOOL_SLICE_UNIT = Path("/etc/systemd/system") / TIDEPOOL_SLICE
# Head-side, User-triggered build work (Dive snapshot commit/push, apt replay) runs here, off the
# Tidepool cgroup. CPUWeight-only (no quota): it yields to slurm jobs + Tidepools under load, uses
# idle cores otherwise. A weight slice reserves nothing, so the Slurm node is not shrunk for it.
BUILD_SLICE = "ccc-build.slice"
BUILD_SLICE_UNIT = Path("/etc/systemd/system") / BUILD_SLICE
# podman's default pool hands each new bridge network the next /24 of this range (the first 256 of
# them) — every User's isolate=true network included. ufw lets it reach the host's DNS (allow_dns_from).
TIDEPOOL_BRIDGE_SUBNETS = "10.89.0.0/16"
TIDEPOOL_SSH_PORT_BASE = 32000                         # port = base + (uid - CCC_UID)
TIDEPOOL_DEFAULT_IMAGE_DIR = "/var/lib/ccc-tidepools"
TIDEPOOL_DEFAULT_SIZE_GB = 200
TIDEPOOL_DEFAULT_CPUS = 4
TIDEPOOL_DEFAULT_MEM_GB = 16
TIDEPOOL_DEFAULT_TOTAL_FRACTION = 4                    # total slice = 1/4 of the box
# Client side: the pinned host key's alias (the Tidepool's own alias is cluster_name()).
TIDEPOOL_HOSTKEY_ALIAS = "ccc-tidepool"
# `cuttle` inside every Tidepool and Dive: a self-contained env built in a throwaway base container
# (incontainer.py), bind-mounted ro at the same path; PATH gets its `path/` (not `bin/`: that holds a
# `python`). Plus what lets it talk to the Head with no login (an **ambient** session): the Cluster
# name, the CA, and a narrow token the API mints — per User in the Tidepool, per Dive in a Dive.
CONTAINER_CUTTLE = Path("/usr/local/lib/ccc/cuttle")
CONTAINER_CA = Path("/etc/ccc/ca.crt")                 # inside: the Head CA (Arm: its trusted copy)
AMBIENT_SESSIONS = (Path("/etc/ccc/keys/session"),     # inside a Tidepool (tidepool-keys/<user>/)
                    Path("/etc/ccc/dive/session"))     # inside an Arm Dive (<tok>/sys/)


def ambient_session() -> Path | None:
    """Inside a Tidepool/Dive: the token file the Head minted for it (None anywhere else)."""
    return next((p for p in AMBIENT_SESSIONS if os.access(p, os.R_OK)), None)


# Dives — the helper's per-Dive state (ccc_tidepool.DIVE_*; must match).
DIVE_RECORDS = Path("/etc/ccc/dives")                  # <token>.json, root
DIVE_RSYNCD = Path("/etc/ccc/rsyncd-dives.d")          # per-Dive rsync modules, root; rsyncd &includes
DIVE_STATE = CCC_LIB / "dives"                         # <token>/{home,sys}: dotfiles + host key bundle
DIVE_MAX_MINUTES = 14 * 24 * 60
REGISTRY_GC_SERVICE = "ccc-registry-gc"                # nightly: blobs of ended Dives' snapshots
REGISTRY_GC_UNIT = Path("/etc/systemd/system/ccc-registry-gc.service")
REGISTRY_GC_TIMER = Path("/etc/systemd/system/ccc-registry-gc.timer")


# --- the interactive shell a Dive hands its User ---
# The oh-my-zsh tree, powerlevel10k and its plugins, vendored INTO this package and COPYed into the
# base image (podman.build_images) — no upstream clone at build time, so no drift.
#
# The tree carries powerlevel10k's gitstatusd at `custom/themes/powerlevel10k/gitstatus/usrbin/`, which
# is upstream's own slot for a prebuilt daemon and is where its installer looks first. That binary is
# x86_64-only, deliberately: it is per-architecture and this fleet is x86_64. An aarch64 Arm would need
# its own copy dropped in beside it under that release's name.
ZSH_DATA_DIR = Path(__file__).resolve().parent / "zsh"
# Where the above lands INSIDE the image.
POD_OMZ_DIR = "/opt/ccc/oh-my-zsh"

# --- Karma tick: the service that ranks the queue by standing ---
# A THIRD unit off the same venv and the same head.db (`ccc_head_api.tick`). Deliberately not a task
# inside the Head API: the economy must not be able to wedge the API, nor the API the economy — and
# the meter (sample Demand, settle ended Dives) is this same tick. Runs as the Mantle, which
# owns every job in the queue and so may re-nice any of them with no privilege at all.
KARMA_SERVICE = "ccc-karma"
KARMA_UNIT = Path("/etc/systemd/system/ccc-karma.service")

# --- Dive: interactive Dive relay ---
# The Head-side TCP relay's listen-port range (ccc_head_api.dive.PORT_BASE/PORT_RANGE) — MUST match
# those constants exactly; duplicated here rather than shared because the CLI and the head-side API
# package do not import each other (same reason FEATURE_NAMES is duplicated between
# ccc_head_api.enroll and ccc_head_api.slurm). Opened once, as a whole range, at `setup --head`
# (system.allow_tcp_port_range) — a per-Tide-Pool ufw rule would mean firewall churn on every submit.
#
# BELOW Linux's default ephemeral range (net.ipv4.ip_local_port_range = 32768-60999): these ports are
# LISTENED on, by the relay on the Head and by the container's sshd on an Arm, so a range inside the ephemeral
# one is racing the kernel's own allocator on every box — any outbound connection can be handed the port
# a Dive is about to want, and the bind then fails with EADDRINUSE.
TIDEPOOL_PORT_BASE = 31000
TIDEPOOL_PORT_RANGE = 200

# **Lowercase only, and that is Headscale's rule rather than ours.** An Arm's name is one string in
# three systems (`mesh.join_mesh`: the Slurm node name IS the Headscale hostname IS the registry key),
# and Headscale normalizes a node's `given_name` to DNS rules — which lowercases it, unconditionally and
# silently. So an uppercase name is one CCC accepts, writes to the registry, hands to Headscale, and then
# fails to recognise coming back: `/enroll` compares the registry's spelling against Headscale's and
# refuses the Arm 403 (ccc_head_api.enroll._node_named_at). Observed on `willPC`, which enrolled five
# times and was refused five times, each attempt stranding another mesh node.
#
# Refused at INPUT rather than quietly lowercased, so the operator names the box and the cluster does not
# rename it behind them — the same rule an Environment declaration follows (CCC never guesses which file
# you meant either).
NODE_NAME_RE = r"^[a-z0-9][a-z0-9-]{0,31}$"
# What may safely be WRITTEN into root-owned gres.conf, which is a charset question and not a policy one:
# a mixed-case name is wrong for the mesh but perfectly safe in a Slurm config file. Kept permissive so
# tightening the rule above cannot break a cluster that admitted an uppercase-named Arm before it existed
# — `enroll.render_gres` asserts against THIS one, and an upgrade must not turn an old row into a crash
# in the command that would remove it.
NODE_NAME_SAFE_RE = r"^[a-zA-Z0-9][a-zA-Z0-9-]{0,31}$"

# --- mesh (self-hosted Headscale coordinator + Tailscale client) ---
# Headscale isn't in apt, so it comes from a GitHub release .deb — PINNED, like every other version
# here. Tracking "latest" meant two Heads built weeks apart ran different coordinators with nothing
# recording how, and the coordinator owns the database mapping every node to its mesh IP. Upgrading is
# editing this line.
HEADSCALE_VERSION = "v0.29.3"
# Deterministic asset URL for a known tag — no API call, so no unauthenticated rate limit.
HEADSCALE_DEB = ("https://github.com/juanfont/headscale/releases/download/"
                 "{tag}/headscale_{ver}_linux_amd64.deb")
HEADSCALE_USER = "ccc"  # the Headscale TAILNET user/namespace Arms enroll under — NOT the unix
                        # account below; two unrelated things this project happens to both call "user"
HEADSCALE_PORT = 8080
HEADSCALE_CONF = Path("/etc/headscale/config.yaml")
HEADSCALE_LIB = Path("/var/lib/headscale")
HEADSCALE_POLICY = Path("/etc/headscale/policy.hujson")  # empty policy == deny-all; see mesh._POLICY
# The coordinator's own TLS identity — distinct from the Head API's (setup.install_tls /
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
# Tailscale client ships from its own apt repo, one path per Ubuntu release. Keyring + list installed
# by setup. Built off UBUNTU_CODENAME rather than a second hardcoded "noble": the release these two
# URLs name and the release the snapshot pins are the same fact, and they must move together.
TAILSCALE_REPO_KEY = f"https://pkgs.tailscale.com/stable/ubuntu/{UBUNTU_CODENAME}.noarmor.gpg"
TAILSCALE_REPO_LIST = (f"https://pkgs.tailscale.com/stable/ubuntu/"
                       f"{UBUNTU_CODENAME}.tailscale-keyring.list")
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
# daemons fight over it either way.
#
# And NOT 100.115.92.0/23, which looks like the perfect home and is a trap. A personal tailscaled's
# anti-spoof rule RETURNs that range instead of dropping it (it is Tailscale's ChromeOS-VM carve-out),
# so a mesh addressed from it would need no firewall exemption at all — see mesh.ensure_ts_exemption.
# But it is the SAME predicate on both sides: tsaddr.IsTailscaleIPv4 is
# `CGNATRange().Contains(ip) && !ChromeOSVMRange().Contains(ip)` (read at v1.102.3), so exempt from the
# DROP and rejected by the peer-route check are one condition. It reproduces the 10.99.0.0/16 failure
# above exactly.
MESH_V4_PREFIX = "100.99.0.0/16"
MESH_V6_PREFIX = "fd7a:115c:a1e0:cc::/64"

# --- coexisting with a personal Tailscale's anti-spoof DROP ---
# A personal tailscaled blackholes CCC unless exempted; mesh.ensure_ts_exemption carries the full
# story. Its rule dies on every graceful tailscaled restart, so a timer re-asserts it on every
# provisioned box (Head and Arm — a Client joins no Mesh and has no ccc0 to be dropped).
MESH_FW_SCRIPT = Path("/opt/ccc/bin/ccc-mesh-firewall.py")
MESH_FW_SERVICE = "ccc-mesh-firewall"
MESH_FW_SERVICE_UNIT = Path("/etc/systemd/system/ccc-mesh-firewall.service")
MESH_FW_TIMER_UNIT = Path("/etc/systemd/system/ccc-mesh-firewall.timer")
TS_INPUT_CHAIN = "ts-input"  # tailscaled's own chain name, on both its netfilter backends
# 60s worst-case blackhole after a tailscaled restart. Far inside Slurm's default SlurmdTimeout (300s,
# and templates.py sets ReturnToService=2), so this never costs a job — it costs an `srun` a stall.
MESH_FW_INTERVAL_S = 60
MESH_FW_BOOT_S = 5  # first tick after boot; tailscaled builds the chain asynchronously, so the
                    # interval — not this — is what actually catches it. See the timer unit.

# --- fencing container egress off the mesh (white-hat F1 remediation) ---
# A Dive/Tidepool NATs out as the box's mesh IP and so can dial enroll/slurm/registry on the mesh — or
# the Head's push registry on loopback. DROP that at FORWARD on every node. Raw iptables (netavark owns
# FORWARD, so ufw route rules fight it; iptables matches `mesh.allow_dnat_forward`'s own FORWARD rule).
# A timer re-asserts after netavark rebuilds FORWARD. Full story in mesh.ensure_dive_egress.
DIVE_EGRESS_SCRIPT = Path("/opt/ccc/bin/ccc-dive-egress.py")
DIVE_EGRESS_SERVICE = "ccc-dive-egress"
DIVE_EGRESS_SERVICE_UNIT = Path("/etc/systemd/system/ccc-dive-egress.service")
DIVE_EGRESS_TIMER_UNIT = Path("/etc/systemd/system/ccc-dive-egress.timer")
# Sources to fence: stock podman's base `podman` net (10.88/16) AND its created-net pool
# (TIDEPOOL_BRIDGE_SUBNETS, 10.89/16 — every Tidepool's isolate net and the Dive `private` net draw
# from it). Both /16 so the fence holds whatever /24 podman handed a given container.
DIVE_EGRESS_SRC_SUBNETS = ("10.88.0.0/16", TIDEPOOL_BRIDGE_SUBNETS)
# Destinations a container has no business reaching: the mesh, and the host's own loopback (the Head's
# push registry binds 127.0.0.1). v4 only — matches the mesh FW; the v6 ULA carries no services.
DIVE_EGRESS_DST_SUBNETS = (MESH_V4_PREFIX, "127.0.0.0/8")
DIVE_EGRESS_INTERVAL_S = 60  # re-assert cadence after a netavark FORWARD rebuild (same math as MESH_FW)
DIVE_EGRESS_BOOT_S = 5

HEADSCALE_DB = HEADSCALE_LIB / "db.sqlite"  # node keys, preauth keys — never touched directly except
                                            # by cuttle backup (see backup.py); everywhere else goes
                                            # through the `headscale` binary.

# --- backup/restore ---
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

# --- stage-in/out (④): rsync daemon on the Head, bound to the mesh ---
# A Dive on a remote Arm pulls its folders from, and mirrors them back to, the Head's rsync daemon on
# its mesh address ONLY (never 0.0.0.0 / the personal tailnet). 873 is privileged, so the daemon runs
# as root; each module drops to its own uid. The per-Dive modules are DIVE_RSYNCD (ccc-tidepool writes
# them); this file holds the globals they inherit (mesh-only `hosts allow`) plus [bench], below.
RSYNCD_CONF = Path("/etc/ccc-rsyncd.conf")
RSYNCD_SERVICE = "ccc-rsyncd"
RSYNCD_UNIT = Path("/etc/systemd/system/ccc-rsyncd.service")
RSYNCD_PORT = 873
# The one module that is not per-Dive: the enrollment benchmark's stage-out target, rooted at
# HEAD_BENCH_DIR so a benchmark can reach the Power files and nothing else. It never goes through the
# Head API (enroll.py sbatches it directly), so it carries a cluster-lifetime secret. Module and secret
# stay in the root-owned conf.
RSYNC_MODULE_BENCH = "bench"
RSYNC_BENCH_USER = "ccc-bench"
RSYNCD_BENCH_SECRETS = Path("/etc/ccc-rsyncd.bench.secrets")

# --- CCC user artifacts ---
# The OPERATOR's own dir — settings + credentials only. Images live in CCC_IMAGES_DIR (world-readable,
# so the home-less Mantle can read them).
# `$CCC_HOME` overrides the location, so a test (or a second cluster) can point cuttle at an isolated
# home instead of the operator's real `~/.ccc`. Without it a pty test inherits the live box's
# credentials and its menu state depends on whether the real Head is reachable — flaky by construction.
CCC_HOME = Path(os.environ.get("CCC_HOME") or Path.home() / ".ccc")
CONFIG_DIR = CCC_HOME / "config"


def ensure_ccc_home() -> Path:
    """~/.ccc, at 0700. Every writer of a child directory calls this first.

    Called rather than trusting `mkdir(mode=...)`, which applies its mode to the LEAF only — so a
    child created 0700 under a home that the umask made 0755 is not private at all. The chmod is
    unconditional so a directory created by an older cuttle is tightened on the next run.
    """
    CCC_HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    CCC_HOME.chmod(0o700)
    return CCC_HOME
CONFIG_FILE = CONFIG_DIR / "config.yaml"
# Pinned Head certificates, one PEM per host (trust-on-first-use; see tls.py). Keyed by host because a
# client may one day talk to more than one cluster, and a pin is only meaningful against the Head that
# presented it.
PINS_DIR = CCC_HOME / "pins"
# Every run's full transcript. The screen is a live frame now — progress scrolls past inside the box
# and is gone when it closes — so the log is the only place the detail survives. One file per run,
# named for when it started, so a failure days later still has its own.
LOG_DIR = CCC_HOME / "logs"
# Cap on the run logs. One file per invocation with no reaper is a leak
# on a CLI built to be scripted — and every one of them holds a transcript, which is why this is a cap
# and not a policy of keeping them all (ui._prune_logs).
MAX_LOGS = 500
# CLI session bearer: the token `cuttle login` stores, 0600, kept out of config.yaml so a
# submitter who never ran setup still has just this one secret file. The Head URL lives in config.yaml.
SESSION_FILE = CCC_HOME / "session"
DIVE_DIR = CCC_HOME / "dives"                    # <id>.json per live Dive → <name>-<id> aliases
# The User's OWN ssh directory. cuttle writes exactly one file in it (`ccc.config`, wholly generated)
# and adds exactly one `Include` line to `config` — it never touches an entry anyone else wrote.
SSH_DIR = Path(os.environ.get("CCC_SSH_DIR") or Path.home() / ".ssh")
SSH_CONFIG = SSH_DIR / "config"
SSH_CCC_CONFIG = SSH_DIR / "ccc.config"
TIDEPOOL_KEY = SSH_DIR / "ccc_ed25519"          # made by `cuttle login`; the pubkey goes to the Head
TIDEPOOL_SSH_META = CCC_HOME / "tidepool-ssh.json"   # {host, port, user} for the alias block
TIDEPOOL_KNOWN_HOSTS = CCC_HOME / "known_hosts"      # the Tidepool's host key, pinned
# Packages newly installed by setup (before→after diff) — teardown purges exactly this set.
APT_MANIFEST = CONFIG_DIR / "apt-installed.txt"

# --- Head-box health monitoring ---
# Disk/process-liveness/rogue-GPU-CPU checks on the Head itself, scoped Head-only (an Arm is a donor's
# machine, not ours to watch the same way). One standalone script (no cuttle import) on a systemd
# timer, run as `ccc` — nothing here needs root.
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
                    HEAD_API_SERVICE, KARMA_SERVICE, RSYNCD_SERVICE,
                    # The only `.timer` in this list, and it has to be: the service behind it is a
                    # oneshot, which reads `inactive` whenever it isn't mid-tick. If this stops, the
                    # Head silently loses its mesh exemption the next time tailscaled restarts.
                    f"{MESH_FW_SERVICE}.timer"]
