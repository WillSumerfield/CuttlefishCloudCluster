"""Render single-node slurm.conf + gres.conf."""

import base64
import json

from . import config, system

SLURM_CONF_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
# Served to Arms over configless (they fetch it from slurmctld), so every address here must be one
# an Arm can route to: the Head's mesh IP, never its LAN address.
ClusterName={cluster}
SlurmctldHost={host}({mesh_ip})

# Arms fetch this file from slurmctld instead of having it installed.
SlurmctldParameters=enable_configless
# Ceiling on static+dynamic nodes (restart to change). Donor Arms register via `slurmd -Z`.
MaxNodeCount={max_nodes}
# Contact each node directly rather than routing messages through a fan-out tree of donor boxes.
TreeWidth={tree_width}

AuthType=auth/munge
# Both cgroup plugins, together, are what makes a GRES allocation REAL. Without them Slurm counts
# `--gres=gpu:1` for scheduling and then hands the step a machine with every card visible: measured,
# `SLURM_JOB_GPUS` comes back EMPTY without both. task/cgroup is what puts the step in a device
# cgroup and tells it which GPU it holds; proctrack/cgroup is its prerequisite
# (task/cgroup needs cgroup-based process tracking, not linuxproc's pid-scanning). See cgroup.conf.
ProctrackType=proctrack/cgroup
TaskPlugin=task/cgroup
# How long a Spawn has between the TERM that ends it and the KILL that enforces it — the window its own
# EXIT trap uses to send work home (ccc_head_api.spawns). 600 was sized for the (since-dropped) Recovery
# Window's cold rsync; kept until the Dive mirror-back decides its own grace (2.0 phase 4).
#
# A CEILING, not a wait: the job leaves COMPLETING the moment its trap returns, which is seconds in the
# ordinary case.
#
# It is paid for in queue latency, not Karma: a COMPLETING Spawn still holds its node and card but
# accrues nothing (the tick only bills RUNNING ones), so a slow copy stalls the next placement. Accepted
# — the stall needs a multi-GB scratch AND a User who let the clock run out, and this is a handful of
# boxes rather than a contended facility.
#
# It does NOT extend a Retract: retraction.GRACE_SECONDS (180) force-KILLs first, so a donor still gets
# their machine back on the 3 minutes they were promised.
KillWait=600
SwitchType=switch/none
MpiDefault=none
ReturnToService=2

SlurmUser=slurm
SlurmdUser=root
# Reclaim a Spawn's node-local scratch after it ends, HOWEVER it ended (the job's own trap misses
# SIGKILL). Served configless like the rest of this file; the script itself is written on every node by
# `cuttle setup`. Runs as root (SlurmdUser), at job end, on the node that ran the Spawn.
Epilog={epilog}
{prolog}
{tmpfs}
StateSaveLocation={state}
SlurmdSpoolDir={spool}
SlurmctldPidFile=/run/slurmctld.pid
SlurmdPidFile=/run/slurmd.pid
SlurmctldLogFile={logdir}/slurmctld.log
SlurmdLogFile={logdir}/slurmd.log

SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_Core

# --- priority ---
# Karma ranks Spawns by lowering the *nice* of the queued jobs of Users in poor standing, and only the
# job's OWNER may do that — which the Head is, for every Spawn in the queue (they all run as the
# Mantle). But nice is only *read* by priority/multifactor: the default priority/basic is pure FIFO and
# ignores it entirely, so without this line Karma would have no effect on the queue at all.
#
# priority = PriorityWeightPartition*1.0 + PriorityWeightAge*age_factor - nice, and NOTHING else:
#
#   PriorityWeightPartition is a FLAT BASE, not a factor. Slurm's priority is a uint32_t and it clamps
#   at zero, so without a base a *fresh* Spawn (age_factor 0) computes -nice and lands on the floor —
#   as would every other fresh Spawn from every User below perfect standing, all tied. With one
#   partition Slurm normalizes the partition factor to exactly 1.0 for every job, so this adds a
#   constant, reorders nobody, and exists only to keep the subtraction off the rail. (It is therefore
#   load-bearing on there being exactly ONE partition — a second one with a higher PriorityJobFactor
#   would silently scale the base down. Guarded by a test.)
#
#   PriorityWeightAge == NICE_MAX means nice and age span the same range, so poor standing makes Slurm
#   treat a Spawn as though it ARRIVED UP TO A DAY LATER than it did. That is the whole handicap, and
#   it is bounded. It is NOT a starvation bound: age saturates at PriorityMaxAge, so past the cap a
#   maximally-niced Spawn ties a brand-new best-standing rival but loses to one that has waited a
#   minute. The bound on starvation is ECONOMIC — spending Karma is what makes you poor, so nobody can
#   hog the cluster and stay ahead of the queue. Deliberate; see temp/KARMA.md "the priority floor".
#
# (Fairshare stays 0 — it needs slurmdbd, and we compute standing ourselves. See docs/Architecture.md,
#  "Scheduling: Slurm, and no accounting database".)
PriorityType=priority/multifactor
PriorityWeightPartition={base}
PriorityWeightAge={age_weight}
PriorityMaxAge={max_age}
PriorityWeightFairshare=0
PriorityWeightJobSize=0
PriorityWeightQOS=0

GresTypes=gpu
# The Head's co-located Arm is a *static* node — always in sinfo, even with slurmd down.
# Donor Arms are dynamic and appear here at runtime; Nodes=ALL is what they auto-join.
# Feature=vramN is what `--vram` matches against (Slurm constraints are set-membership, so the Head
# OR-expands a request into `--constraint=vram24|vram48`).
# Weight (∝ 1/Power) IS here, and has to be: `scontrol reconfigure` re-reads this file and resets any
# node attribute the file does not state, so a Weight set only by `scontrol update` is silently wiped
# by the next `arm authorize`/`admit`/`remove` (verified: `scontrol update` sets it, `reconfigure`
# resets it to 1).
# On a first setup Power is unknown here — it is benchmarked after the node is live — so this renders
# 1 and `scontrol update` carries it until the next render. Donor Arms are dynamic, so they are not in
# this file and cannot be fixed this way; enroll.reapply_weights() re-pushes them after a reconfigure.
NodeName={host} Sockets={sockets} CoresPerSocket={cores} ThreadsPerCore={threads} \
RealMemory={mem} Gres=gpu:{gpus} Feature={vram_feature} Weight={weight} State=UNKNOWN
PartitionName={partition} Nodes=ALL Default=YES MaxTime=INFINITE State=UP
"""

# Distro slurm-wlm is built without NVML → AutoDetect=nvml is fatal. Map each node's devices
# explicitly, per node. Served to Arms over configless, so the Head authors all of it.
#
# **One line per Device**, rather than the `File=/dev/nvidia[0-3]` range form or `MultipleFiles`
# (man 5 gres.conf offers all three):
#   * `MultipleFiles` is wrong outright — it exists for MIG, and declares ONE generic resource backed
#     by several device files. That is the opposite of what we want here: N whole, separately
#     schedulable cards.
#   * The bracket range is Slurm hostlist syntax, so it would have to survive a root-side re-validation
#     (enroll.render_gres re-checks every path against config.GPU_DEVICE_RE before writing a file as
#     root, because the registry's contents arrived over the network). Widening that regex to accept
#     `[0-3]` would widen the one validator standing between a POST body and /etc/slurm — and it also
#     assumes the cards are contiguous, which a box with a card removed is not.
#   * Separate lines need no bracket parsing, describe a gappy set (`nvidia0`, `nvidia2`) exactly, and
#     keep the per-path validation as narrow as it is today.
# Slurm gives each of a node's GRES an index in the order its File records are read, so **line order
# here is the index order** a running Spawn's `gres_detail` (IDX:n) reports back — which is what
# Settlement maps to a Power. system.gpu_devices() sorts numerically to make that order stable.
GRES_CONF_LINE = "NodeName={node} Name=gpu File={device}\n"

# Served to Arms over configless alongside slurm.conf, so the Head authors it and every node gets the
# SAME file — configless has no per-node override, which is why this is deliberately minimal.
#
# ConstrainDevices is the entire point: it puts each Spawn in a device cgroup holding exactly the GPUs
# Slurm allocated it, and denying the rest is what finally makes `SLURM_JOB_GPUS` and
# CUDA_VISIBLE_DEVICES mean something. It is also the prerequisite for ever subdividing a card
# (docs/TODO.md, automatic GPU partitioning): you cannot safely share what you cannot constrain.
#
# ConstrainRAMSpace/ConstrainCores stay OFF (their default). Nothing drives them yet — a Spawn declares
# --time and --vram, not a RAM or core budget — and turning on a limit no one sets would OOM-kill jobs
# for hitting a number they never asked for.
CGROUP_CONF_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
# Detect cgroup v1 vs v2 rather than pinning either — this one file serves every node.
CgroupPlugin=autodetect
# Confine a Spawn to the GPUs it was allocated, and no others.
ConstrainDevices=yes
"""

# --- the Power benchmark ------------------------------------------------------
# What a device can *do*, measured — never a spec sheet. It runs **inside the Pod**, submitted by the
# Head down the ordinary Slurm path and pinned to the node, so the number reflects the machine as
# Spawns will really meet it: a laptop card that thermally throttles, a stale driver, a starved PCIe
# link. None of that is on a spec sheet, and all of it is
# what an hour on the card is actually worth.
#
# Power is the term that stops the meter paying an owner MORE for being SLOWER. Bill seconds alone and
# the same matmul costs 4x on a card a quarter the speed — so donating your worst GPU would pay best,
# which is precisely backwards from what Karma exists to encourage.
#
# **One number per Device, not one per node.** Identical models genuinely differ in practice — an x4
# slot, a card that throttles because it sits under another — and a Spawn holds a *card*, not a box.
# So every card is measured, in one job holding all of them: N sbatch jobs would queue behind each
# other, each pay Slurm scheduling latency, and could land while another Spawn holds a sibling card.
#
# Device i here IS GRES index i. Slurm sets CUDA_VISIBLE_DEVICES to the allocated GRES indices in
# ascending order, so the CUDA device i is the i'th card of this node's gres.conf lines — the same
# index `gres_detail` reports at Settlement. The benchmark holds every card (--gres=gpu:N), so the
# allocation is the whole node and the two orders coincide exactly.
BENCH_PY = """\
import json, os, subprocess, sys

out = os.environ.get("CCC_BENCH_OUT", "/job")

# ccc-launch hands the job's GPUs in CCC_GPUS (GPU or MIG UUIDs, GRES order); the job holds every card,
# so position i IS GRES index i. One process per Device: a MIG instance is the only device its process
# can address. Then fold them into the ONE `power` file: powers[n] is the card Slurm reports as IDX:n.
devs = os.environ["CCC_GPUS"].split(",")
parts = []
for dev in devs:
    r = subprocess.run([{bench_bin!r}], check=True, capture_output=True, text=True,
                       env={{**os.environ, "CUDA_VISIBLE_DEVICES": dev}})
    parts.append(json.loads(r.stdout))
json.dump({{"powers": [p["power"] for p in parts], "vram_mb": parts[0]["vram_mb"], "gpu": parts[0]["gpu"]}},
          open(out + "/power", "w"))
"""

# The measurement itself: fp32 SGEMM through cuBLAS — the exact kernel torch's fp32 `a @ b` dispatches
# to (TF32 off, cuBLAS's default math mode), without the 5 GB of CUDA stack a torch wheel bundles.
# N/WARMUP/ITERS come in as -D at build time (BENCH_CONTAINERFILE_TMPL). No kernels of its own, so no
# GPU arch to target: cuBLAS carries the per-arch code.
BENCH_CU = r"""
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <cublas_v2.h>
#include <cuda_runtime.h>

#define CK(x) do { if ((x) != 0) { fprintf(stderr, "%s failed\n", #x); return 1; } } while (0)

int main() {
    int count = 0;
    CK(cudaGetDeviceCount(&count));
    if (count != 1) { fprintf(stderr, "want exactly 1 visible device, got %d\n", count); return 1; }
    const size_t n = BENCH_N, bytes = n * n * sizeof(float);
    std::vector<float> h(n * n);
    for (auto &v : h) v = rand() / (float)RAND_MAX - 0.5f;
    float *a, *b, *c;
    CK(cudaMalloc(&a, bytes)); CK(cudaMalloc(&b, bytes)); CK(cudaMalloc(&c, bytes));
    CK(cudaMemcpy(a, h.data(), bytes, cudaMemcpyHostToDevice));
    CK(cudaMemcpy(b, h.data(), bytes, cudaMemcpyHostToDevice));
    cublasHandle_t hd;
    CK(cublasCreate(&hd));
    const float one = 1.f, zero = 0.f;
    for (int i = 0; i < BENCH_WARMUP; i++)  // the first SGEMM pays cuBLAS init — never time it
        CK(cublasSgemm(hd, CUBLAS_OP_N, CUBLAS_OP_N, n, n, n, &one, a, n, b, n, &zero, c, n));
    CK(cudaDeviceSynchronize());
    auto t0 = std::chrono::steady_clock::now();
    for (int i = 0; i < BENCH_ITERS; i++)
        CK(cublasSgemm(hd, CUBLAS_OP_N, CUBLAS_OP_N, n, n, n, &one, a, n, b, n, &zero, c, n));
    CK(cudaDeviceSynchronize());
    double s = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
    cudaDeviceProp p;
    CK(cudaGetDeviceProperties(&p, 0));  // under MIG this is the INSTANCE's memory, not the card's
    printf("{\"power\": %.2f, \"vram_mb\": %zu, \"gpu\": \"%s\"}\n",
           BENCH_ITERS * 2.0 * n * n * n / s / 1e12, p.totalGlobalMem / (1024 * 1024), p.name);  // fp32 TFLOP/s
    return 0;
}
"""

# The **Benchmark** job. Submitted as the **cluster's own Guise** (`sudo -u ccc-u60001 sbatch`), pinned
# to the node being priced, holding ALL its cards so one job prices every Device. The container is
# started by `ccc-launch` from the spec line below (the benchmark image, `bench.py all`); its /job is
# config.JOBS_DIR/<jid>, handed back to the Guise when the container exits, so the Power file goes home
# over the [bench] rsync module from the host. Same on the Head's co-located Arm (rsync to itself).
BENCH_SBATCH_TMPL = """\
#!/bin/bash
#SBATCH --job-name=ccc-bench
#SBATCH --nodelist={node}
#SBATCH --gres=gpu:{gpus}
#SBATCH --time={minutes}
#SBATCH --chdir=/tmp
#SBATCH --output=/tmp/ccc-bench-%j.log
#SBATCH --error=/tmp/ccc-bench-%j.log
{spec_line}
set -e
sink=/tmp/ccc-bench-$SLURM_JOB_ID.log
trap '[ -s "$sink" ] || rm -f "$sink"' EXIT
sudo -n {launch} "$SLURM_JOB_ID"
# The cluster-lifetime [bench] secret — a Benchmark never goes through the Head API, so there is no
# **Siphon** for it. The module is rooted at the bench subtree: this reaches the Power files only.
export RSYNC_PASSWORD={bench_secret}
rsync -a "{jobs_dir}/$SLURM_JOB_ID/power" "rsync://{bench_user}@{head_ip}/{bench_mod}/{node}/"
"""


# An EXTRA apt source pointing at the Ubuntu archive frozen at config.UBUNTU_SNAPSHOT, so the exact
# versions CCC pins stay installable after the live archive has moved past them.
#
# **Additive, and that is load-bearing.** CCC installs on donor boxes. Rewriting or replacing the box's
# own sources.list would freeze that donor's *entire system* at our timestamp — no security updates, no
# upgrades — which is not ours to do. An extra file only ever ADDS availability: `apt upgrade` still
# sees the live archive's newer versions and prefers them (apt takes the highest version on offer), and
# the snapshot is reached only because our exact-version pins name a version nothing else can supply.
#
# noble-updates as well as noble: a pin that later moves to the updates pocket (a security fix) would
# not be in the release pocket's snapshot at all.
UBUNTU_SNAPSHOT_LIST_TMPL = """\
# Rendered by `cuttle setup`. Do not hand-edit.
# ADDITIVE — this box's own apt sources are untouched, and a later `apt upgrade` still prefers the live
# archive. This source exists so the exact versions CCC pins stay fetchable after the archive rotates.
deb [signed-by={keyring}] {url}/{snapshot} {codename} main universe
deb [signed-by={keyring}] {url}/{snapshot} {codename}-updates main universe
"""


def render_ubuntu_snapshot_list() -> str:
    return UBUNTU_SNAPSHOT_LIST_TMPL.format(
        keyring=config.UBUNTU_ARCHIVE_KEYRING,
        url=config.UBUNTU_SNAPSHOT_URL,
        snapshot=config.UBUNTU_SNAPSHOT,
        codename=config.UBUNTU_CODENAME,
    )


# The **base image** — frozen per release, built once on the Head (podman.build_images), pulled by
# every Arm by digest. Everything a User's Tidepool and Dives start from; Users add to it with
# `apt install` as container root (an unprivileged host id range), and the diff is their own layer.
#
# No Python packages, on purpose: every library comes from a Dive's dependency file, so a User's env
# never inherits defaults. Python 3.12 (Ubuntu's) + the pinned uv only.
# CUDA: the driver arrives via CDI and the runtime libs in wheels; only nvcc + headers live here, for
# compiling extensions. build-essential for the same reason (Triton/Inductor shell out to a C compiler).
#
# The dpkg excludes file goes first so man pages survive, and the already-unpacked core is reinstalled
# to get its pages back. The zprofile line makes zsh logins read /etc/profile.d (Ubuntu's zsh doesn't).
BASE_CONTAINERFILE_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
FROM {image}
ENV DEBIAN_FRONTEND=noninteractive LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 \\
    CUDA_HOME={cuda_home} PATH={cuttle}/path:{cuda_home}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN rm -f /etc/dpkg/dpkg.cfg.d/excludes \\
 && apt-get update \\
 && apt-get install -y --no-install-recommends {apt} \\
 && apt-get install -y --reinstall --no-install-recommends \\
      coreutils bash grep sed findutils procps tar \\
 && sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen && locale-gen \\
 && curl -fsSL -o /tmp/cuda-keyring.deb {cuda_keyring} && dpkg -i /tmp/cuda-keyring.deb \\
 && rm /tmp/cuda-keyring.deb && apt-get update \\
 && apt-get install -y --no-install-recommends {cuda_pkgs} \\
 && rm -rf /var/lib/apt/lists/*
RUN printf 'export CUDA_HOME={cuda_home}\\nexport PATH={cuda_home}/bin:$PATH\\n' > /etc/profile.d/ccc-cuda.sh \\
 && echo "emulate sh -c 'source /etc/profile'" >> /etc/zsh/zprofile \\
 && userdel -r ubuntu
COPY uv micromamba /usr/local/bin/
COPY oh-my-zsh {omz}
RUN chown -R root:root {omz} && chmod -R go-w {omz} \\
 && chmod 0755 /usr/local/bin/uv /usr/local/bin/micromamba {omz}/custom/themes/powerlevel10k/gitstatus/usrbin/gitstatusd-linux-x86_64
"""

BASE_APT_PKGS = (
    "sudo openssh-server openssh-client rsync ca-certificates zsh locales procps iproute2 git "
    "git-lfs curl wget tmux vim nano less htop nvtop tree jq file unzip zip zstd xz-utils "
    "bash-completion man-db ncurses-term build-essential python3 python3.12-dev python-is-python3"
)

# The **benchmark image**: base + cuBLAS (runtime only) + the compiled SGEMM + bench.py — ~0.5 GB new on
# an Arm. Users never see it. Two stages: the -dev package (headers, and a static lib bigger than the
# whole result) exists only to compile, and stays out of what Arms pull. FROM the local base tag:
# exactly the base just built. rpath, because the CUDA libs aren't on the default loader path.
BENCH_CONTAINERFILE_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
FROM {base} AS build
RUN apt-get update && apt-get install -y --no-install-recommends {cublas_dev} && rm -rf /var/lib/apt/lists/*
COPY bench.cu /tmp/bench.cu
RUN nvcc -O2 -DBENCH_N={n} -DBENCH_WARMUP={warmup} -DBENCH_ITERS={iters} -Xlinker -rpath={cuda_home}/lib64 \\
      -o /tmp/bench /tmp/bench.cu -lcublas

FROM {base}
RUN apt-get update && apt-get install -y --no-install-recommends {cublas} && rm -rf /var/lib/apt/lists/*
COPY --from=build /tmp/bench {bench_bin}
COPY bench.py {bench_py}
"""


def render_base_containerfile() -> str:
    return BASE_CONTAINERFILE_TMPL.format(
        image=config.BASE_IMAGE_DOCKER, apt=BASE_APT_PKGS, cuda_home=config.CUDA_HOME,
        cuda_keyring=config.CUDA_REPO_KEYRING, cuda_pkgs=config.CUDA_PKGS, omz=config.POD_OMZ_DIR,
        cuttle=config.CONTAINER_CUTTLE)


def render_bench_containerfile() -> str:
    return BENCH_CONTAINERFILE_TMPL.format(
        base=f"{config.registry()}/ccc/base:{config.IMAGE_TAG}", cublas=config.BENCH_CUBLAS_PKG, cublas_dev=config.BENCH_CUBLAS_DEV_PKG,
        n=config.BENCH_MATRIX_N, warmup=config.BENCH_WARMUP, iters=config.BENCH_ITERS,
        cuda_home=config.CUDA_HOME, bench_bin=config.BENCH_BIN, bench_py=config.BENCH_PY_PATH)


def render_bench_py() -> str:
    return BENCH_PY.format(bench_bin=config.BENCH_BIN)


# One `registry:2` config per instance (podman.install_registry). Push: 127.0.0.1, read-write. Pull:
# the mesh IP, read-only. Same store, same TLS leaf (the Head API's — its SANs carry the Cluster name).
REGISTRY_CONFIG_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
version: 0.1
log:
  level: warn
storage:
  filesystem:
    rootdirectory: /var/lib/registry
  delete:
    enabled: {delete}
  maintenance:
    uploadpurging:            # 2.8 panics if enabled without age/interval/dryrun
      enabled: {purge}
      age: 168h
      interval: 24h
      dryrun: false
    readonly:
      enabled: {readonly}
http:
  addr: {addr}:{port}
  tls:
    certificate: /certs/head.crt
    key: /certs/head.key
auth:
  htpasswd:
    realm: ccc
    path: /auth/htpasswd
"""

REGISTRY_QUADLET_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
[Unit]
Description=CCC image registry ({role})
After=network-online.target {ts_service}.service
Wants=network-online.target

[Container]
Image={image}
ContainerName={service}-{role}
Network=host
Volume={data}:/var/lib/registry
Volume={etc}/{role}.yml:/etc/docker/registry/config.yml:ro
Volume={etc}/{role}.htpasswd:/auth/htpasswd:ro
Volume={cert}:/certs/head.crt:ro
Volume={key}:/certs/head.key:ro

[Service]
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def render_registry_config(addr: str, readonly: bool) -> str:
    return REGISTRY_CONFIG_TMPL.format(addr=addr, port=config.REGISTRY_PORT,
                                       readonly=str(readonly).lower(),
                                       delete=str(not readonly).lower(),
                                       purge=str(not readonly).lower())


def render_registry_quadlet(role: str) -> str:
    return REGISTRY_QUADLET_TMPL.format(
        role=role, image=config.REGISTRY_IMAGE, service=config.REGISTRY_SERVICE,
        ts_service=config.CCC_TS_SERVICE, data=config.REGISTRY_DATA, etc=config.REGISTRY_ETC,
        cert=config.HEAD_CERT, key=config.HEAD_KEY)


CDI_PATCH_SERVICE_TMPL = """\
[Unit]
Description=CCC: make NVIDIA's CDI spec readable by Podman 4.9

[Service]
Type=oneshot
ExecStart={bin} {spec}
"""

CDI_PATCH_PATH_TMPL = """\
[Unit]
Description=CCC: re-patch NVIDIA's CDI spec whenever it is regenerated

[Path]
PathChanged={spec}

[Install]
WantedBy=multi-user.target
"""


def render_cdi_patch_service() -> str:
    return CDI_PATCH_SERVICE_TMPL.format(bin=config.CDI_PATCH_BIN, spec=config.CDI_SPEC)


def render_cdi_patch_path() -> str:
    return CDI_PATCH_PATH_TMPL.format(spec=config.CDI_SPEC)


# Every node. A Guise may run ccc-launch as root with ONE argument, a job id (sudo's ^…$ regex form);
# ccc-launch itself checks the caller is inside that job and owns it.
LAUNCH_SUDOERS_TMPL = """\
# Managed by `cuttle setup` — do not hand-edit, re-run setup instead.
%{group} ALL=(root) NOPASSWD: {bin} ^[0-9]+$
"""


def render_launch_sudoers() -> str:
    return LAUNCH_SUDOERS_TMPL.format(group=config.GUISE_GROUP, bin=config.CCC_LAUNCH_BIN)


# --- Tidepool (ccc_tidepool.py makes the per-User pieces; these are shared) ------------------------

# One template unit, instance = username. The container was created once (`ccc-tidepool create`);
# this only starts and stops it, so its writable layer (apt installs) survives restarts and reboots.
# Its cgroup is `--cgroup-parent` the slice, with the per-User cap on the container itself.
TIDEPOOL_UNIT_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
[Unit]
Description=CCC Tidepool (%i)
After=network-online.target
Wants=network-online.target
RequiresMountsFor={mnt}/%i
StartLimitIntervalSec=0

[Service]
Slice={slice}
ExecStart={podman} start --attach tp-%i
# A fresh scope each start: GPU denied again (unless a Dive holds it), main-folder locks put back.
ExecStartPost=-{helper} started %i
ExecStop={podman} stop -t 10 tp-%i
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
"""

# Every Tidepool together. A ceiling (cgroup v2 max), not a reservation; the Head's Slurm node is
# shrunk by the same amount (render_slurm_conf) so the scheduler does not count it twice.
TIDEPOOL_SLICE_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
[Unit]
Description=CCC Tidepools (all Users)

[Slice]
CPUQuota={cpu_pct}%
MemoryMax={mem_gb}G
"""

# The Head API may restart or reset a Tidepool by username — the API checks it is the caller's own —
# and prepare/open/close a Dive (token = 32 hex the API minted).
TIDEPOOL_SUDOERS_TMPL = """\
# Managed by `cuttle setup --head` — do not hand-edit, re-run setup instead.
{user} ALL=(root) NOPASSWD: {bin} ^(restart|reset) [a-z][a-z0-9_-]{{0,31}}$
{user} ALL=(root) NOPASSWD: {bin} ^dive-(prep|snap|open) [a-z][a-z0-9_-]{{0,31}} [0-9a-f]{{32}}$
{user} ALL=(root) NOPASSWD: {bin} ^dive-close [0-9a-f]{{32}}$
"""

# Container-side boot, at every start, as container root under catatonit (`--init`). Read from the
# host (/etc/ccc/tidepool, ro), so a change lands at the next restart with no image rebuild.
TIDEPOOL_INIT = """\
#!/bin/sh
# Rendered by `cuttle setup --head`. The Tidepool's boot: account, sudo, zsh, then sshd as PID 2.
set -eu
u="$CCC_USER"
if ! id -u "$u" >/dev/null 2>&1; then
  useradd --uid {uid} --user-group --no-create-home --home-dir "/home/$u" --shell /bin/zsh \\
    --groups sudo --password '*' "$u"
fi
printf '%s ALL=(ALL) NOPASSWD:ALL\\n' "$u" > /etc/sudoers.d/ccc-tidepool
chmod 0440 /etc/sudoers.d/ccc-tidepool
cp /etc/ccc/tidepool/zshrc /etc/zsh/zshrc
cp /etc/ccc/tidepool/p10k.zsh /etc/zsh/ccc-p10k.zsh
# zsh's new-user wizard fires in a home with no rc file at all; a stub is the User's to edit.
h="/home/$u"
if [ ! -e "$h/.zshrc" ] && [ ! -e "$h/.zshenv" ] && [ ! -e "$h/.zprofile" ] && [ ! -e "$h/.zlogin" ]; then
  printf '# Yours. Read after the cluster default (/etc/zsh/zshrc), so anything here wins.\\n' > "$h/.zshrc"
  chown "$u:$u" "$h/.zshrc"
fi
mkdir -p /run/sshd
exec /usr/sbin/sshd -D -e -f /etc/ccc/tidepool/sshd_config -o "AllowUsers=$u"
"""

# Key-only, the User alone. StrictModes off: the CCC keys file is owned by the Head API's `ccc`,
# which is unmapped (nobody) inside. No PAM: pam_loginuid fails in a user namespace, and with PAM off
# a '*' password is not "locked", so pubkey login works.
TIDEPOOL_SSHD_CONFIG = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
Port 22
HostKey /etc/ccc/hostkeys/ssh_host_ed25519_key
AuthorizedKeysFile .ssh/authorized_keys /etc/ccc/keys/authorized_keys
StrictModes no
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
UsePAM no
X11Forwarding no
AllowAgentForwarding yes
AllowTcpForwarding yes
AllowStreamLocalForwarding yes
StreamLocalBindUnlink yes
AcceptEnv CCC_FWD
PrintMotd no
SetEnv LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
ClientAliveInterval 60
PidFile /run/sshd.pid
Subsystem sftp internal-sftp
"""

# The cluster default shell. Global rc, so a User's own ~/.zshrc is read after it and wins. State in
# $HOME: the home is the User's own and persistent now.
TIDEPOOL_ZSHRC = """\
# Rendered by `cuttle setup --head`, copied in at every Tidepool start — edit ~/.zshrc instead.
typeset -g POWERLEVEL9K_INSTANT_PROMPT=off
path=({cuttle}/path $path)
export ZSH={omz}
ZSH_THEME="powerlevel10k/powerlevel10k"
zstyle ':omz:update' mode disabled
export ZSH_CACHE_DIR=$HOME/.cache/oh-my-zsh
mkdir -p $ZSH_CACHE_DIR
HISTFILE=$HOME/.zsh_history
HISTSIZE=50000
SAVEHIST=50000
plugins=(git sudo zsh-syntax-highlighting zsh-autosuggestions you-should-use colored-man-pages extract)
source $ZSH/oh-my-zsh.sh
source /etc/zsh/ccc-p10k.zsh
"""


def tidepool_podman() -> str:
    """The unit's podman prefix — must equal ccc_tidepool.podman() with `%i` for the user."""
    return (f"/usr/bin/podman --root {config.TIDEPOOL_MNT_ROOT}/%i/storage --runroot "
            f"/run/ccc-tidepools/%i --storage-opt additionalimagestore=/var/lib/containers/storage")


def render_tidepool_unit() -> str:
    return TIDEPOOL_UNIT_TMPL.format(mnt=config.TIDEPOOL_MNT_ROOT, slice=config.TIDEPOOL_SLICE,
                                     podman=tidepool_podman(), helper=config.TIDEPOOL_HELPER_BIN)


# Nightly: the registry frees blobs no manifest references — each ended Dive's snapshot layer (its
# manifest is deleted at the Dive's end).
REGISTRY_GC_SERVICE_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
[Unit]
Description=CCC registry garbage collection (ended Dives' snapshots)

[Service]
Type=oneshot
ExecStart={helper} registry-gc
"""
REGISTRY_GC_TIMER_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
[Unit]
Description=CCC registry garbage collection, nightly

[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true

[Install]
WantedBy=timers.target
"""


def render_registry_gc_service() -> str:
    return REGISTRY_GC_SERVICE_TMPL.format(helper=config.TIDEPOOL_HELPER_BIN)


def render_tidepool_slice(total_cpus: int, total_mem_gb: int) -> str:
    return TIDEPOOL_SLICE_TMPL.format(cpu_pct=total_cpus * 100, mem_gb=total_mem_gb)


def render_tidepool_sudoers() -> str:
    return TIDEPOOL_SUDOERS_TMPL.format(user=config.HEAD_API_USER, bin=config.TIDEPOOL_HELPER_BIN)


def render_tidepool_init() -> str:
    return TIDEPOOL_INIT.replace("{uid}", "1000")


def render_tidepool_zshrc() -> str:
    return TIDEPOOL_ZSHRC.format(omz=config.POD_OMZ_DIR, cuttle=config.CONTAINER_CUTTLE)


# rsync daemon for stage-in/out. `address` pins it to the Head's mesh IP, so it never listens on the
# LAN or the personal tailnet. Runs as root (873 is privileged) but each module drops to `ccc` (the
# service account that owns the Head state), so files are read/written as ccc. `use chroot = no`: no
# root chroot (simpler; a module's own root is the boundary now).
#
# This file defines NO per-Spawn module. It sets the globals every module inherits and `&include`s the
# directory the Head API mints **Siphon**s into — an &include'd file
# starts parsing with the parent's defaults, so `hosts allow`, the drop to `ccc`, and read-only-by-
# default all apply to a Siphon without the Head API having to know any of them. rsyncd re-reads this
# file, and re-scans that directory, on EVERY connection, so a Siphon opens and closes with no reload.
#
# `hosts allow` is now defence in depth rather than the authorization: the mesh is flat, so it cannot
# tell one Spawn from another on the same Arm. What isolates a Spawn is its module's root plus its own
# secret. `list = false` keeps the live Siphon names out of a bare `rsync rsync://<head>/`.
#
# [bench] is the one module here that is not per-Spawn: the enrollment benchmark stages its Power file
# home and never touches the Head API, so it cannot be handed a Siphon. Rooted at the bench subtree, so
# it reaches the Power files and nothing else. `strict modes` stays TRUE — its secrets file is
# root-owned, unlike a Siphon's, which must be writable by the Head API.
#
# It is WRITABLE (it is where a Power file lands) and every Arm holds the one secret, so nothing that
# must not be overwritten may live under it.
RSYNCD_CONF_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
address = {mesh_ip}
port = {port}
use chroot = no
uid = {user}
gid = {group}
read only = true
list = false
hosts allow = {v4} {v6}
strict modes = false
[{bench_mod}]
    path = {bench_dir}
    read only = false
    auth users = {bench_user}
    secrets file = {bench_secrets}
    strict modes = true
&include {rsyncd_dir}
&include {dive_dir}
"""

RSYNCD_UNIT_TMPL = """\
[Unit]
Description=CCC stage-in/out rsync daemon (mesh-bound)
# ccc0 must exist before we can bind the mesh address.
After=network-online.target {ts_service}.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/rsync --daemon --no-detach --config={conf}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
"""


def render_rsyncd_conf(mesh_ip: str) -> str:
    return RSYNCD_CONF_TMPL.format(
        mesh_ip=mesh_ip, port=config.RSYNCD_PORT,
        user=config.HEAD_API_USER, group=config.HEAD_API_USER,
        bench_mod=config.RSYNC_MODULE_BENCH,
        bench_dir=config.HEAD_SPAWNS_DIR / config.BENCH_DIR_NAME,
        bench_user=config.RSYNC_BENCH_USER, bench_secrets=config.RSYNCD_BENCH_SECRETS,
        rsyncd_dir=config.RSYNCD_DIR, dive_dir=config.DIVE_RSYNCD,
        v4=config.MESH_V4_PREFIX, v6=config.MESH_V6_PREFIX,
    )


def render_rsyncd_unit() -> str:
    return RSYNCD_UNIT_TMPL.format(ts_service=config.CCC_TS_SERVICE, conf=config.RSYNCD_CONF)


# The `Prolog=` line, or the reason there isn't one. Rendering the line is the moment Guises become
# mandatory fleet-wide, and it is the ONE fleet change in this project that punishes being early: an
# Arm named by it without the script drains, and stays drained across a slurmd restart. So the Head
# states, in the file itself, that it is deliberately absent and what would turn it on.
_PROLOG_LINE = """\
# Creates the **Guise** each Spawn runs as, on whatever node Slurm picked, before the job step. Runs as
# root (SlurmdUser). Written on every node by `cuttle setup`, exactly like the epilog above.
Prolog={prolog}"""
_PROLOG_HELD = """\
# NO Prolog= — {n} admitted Arm(s) have not reported having the prolog script: {missing}.
# Naming it here would fail their jobs and DRAIN them, and a drained node stays drained across a slurmd
# restart. Re-run `cuttle setup --arm` on each, then `cuttle setup --head` here."""

# A private /tmp and /dev/shm per job (temp/spawn-isolation.md Phase 4) — the wall that contains the
# WRAPPER, not just the container inside it. Without it a remote Spawn's scratch sits in the
# host's shared /tmp beside every concurrent Spawn's.
#
# The two lines are one setting: "'PrologFlags=Contain' must be set to use this plugin" (man 5
# slurm.conf), so they are held back together and there is no state where one is on without the other.
# Each node's own job_container.conf supplies BasePath; this file cannot, since configless has no
# per-node override and a BasePath must be unique per node.
_TMPFS_LINE = """\
JobContainerType=job_container/tmpfs
PrologFlags=Contain"""
_TMPFS_HELD = """\
# NO JobContainerType= — {n} admitted Arm(s) have not reported having job_container.conf: {missing}.
# The plugin refuses to start without that file, so naming it here DRAINS them, and a drained node
# stays drained across a slurmd restart. Re-run `cuttle setup --arm` on each, then this."""


def render_slurm_conf(mesh_ip: str, weight: int = 1, features: list[str] | None = None,
                      missing_prolog: list[str] | None = None,
                      missing_tmpfs: list[str] | None = None,
                      reserve: tuple[int, int] = (0, 0)) -> str:
    """`mesh_ip` = the Head's address on the CCC mesh. Baked in because MagicDNS is off: nothing in
    this system can resolve a hostname, so Slurm gets the address literally.

    `weight` = the Head Arm's Weight (∝ 1/Power). Defaults to 1 for the first setup, when the box has
    not been benchmarked yet; thereafter the caller passes it from the registry so a reconfigure
    cannot wipe it. See the NodeName comment in the template.

    `features` = Arm Features the Head's own co-located Arm has, comma-joined onto its
    one vram bucket exactly like a donor Arm's `enroll.slurmd_dropin` — a Slurm Feature is a flat
    per-node set either way. Defaults to none: the Head's own box declares no catalog features today.

    `missing_prolog` / `missing_tmpfs` = admitted Arms that have not reported the prolog script, and
    those that have not reported job_container.conf (enroll.arms_missing, one call per capability).
    Non-empty holds that line back for the WHOLE fleet, including the Arms that are ready — a line here
    is one setting for every node, so it is on for all of them or none. The two are tracked separately
    because an Arm can legitimately have the prolog and not the other.

    `reserve` = (cpus, mem_mb) the Tidepools' slice holds (tidepool.slurm_reserve): Slurm is told about
    that much less — bookkeeping only, nothing is fenced (no ConstrainCores). CPUs come off
    CoresPerSocket, rounded up to whole cores across every socket.
    """
    sockets, cores, threads = system.cpu_topology()
    cores -= -(-reserve[0] // (sockets * threads))
    feature = ",".join([config.vram_feature(system.gpu_vram_mb()), *sorted(features or [])])
    prolog = (_PROLOG_HELD.format(n=len(missing_prolog), missing=", ".join(missing_prolog))
              if missing_prolog else _PROLOG_LINE.format(prolog=config.SLURM_PROLOG))
    tmpfs = (_TMPFS_HELD.format(n=len(missing_tmpfs), missing=", ".join(missing_tmpfs))
             if missing_tmpfs else _TMPFS_LINE)
    return SLURM_CONF_TMPL.format(
        prolog=prolog,
        tmpfs=tmpfs,
        weight=weight,
        cluster=config.SLURM_CLUSTER,
        partition=config.PARTITION,
        host=system.hostname(),
        mesh_ip=mesh_ip,
        max_nodes=config.MAX_NODE_COUNT,
        tree_width=config.TREE_WIDTH,
        epilog=str(config.SLURM_EPILOG),
        base=config.PRIORITY_BASE,
        age_weight=config.PRIORITY_WEIGHT_AGE,
        max_age=config.PRIORITY_MAX_AGE,
        sockets=sockets,
        cores=cores,
        threads=threads,
        mem=system.real_memory_mb() - reserve[1],
        gpus=len(system.gpu_devices()),
        vram_feature=feature,
        state=config.SLURM_STATE_DIR,
        spool=config.SLURMD_SPOOL_DIR,
        logdir=config.SLURM_LOG_DIR,
    )


# MIG, node side — the half that needs root, so the prolog and epilog share it and nothing else does.
# Inserted verbatim into both (never `.format`ed), hence ordinary braces; `$CCC_GUISE_GROUP` is set by
# whichever script embeds it, since only those know the rendered group name.
#
# **CCC never carves for placement.** There is no Beak (CONTEXT.md, "Slice"): Slurm is told about whole
# cards, priced per whole card, and the resting state of a MIG-mode card is therefore ONE full-size
# instance, so that a Spawn asking for a card gets a card. What a Spawn does to the card it already
# holds is its own business, and the epilog puts it back.
MIG_ADMIN_SH = """\
# `4g.96gb` on an RTX PRO 6000 — the whole card. `nvidia-smi mig -lgip` lists the profiles and their IDs;
# the full-card one is 0 there. Note the `+me`/`-me`/`+gfx` variants exist only on the 1g and 2g
# profiles, so there is no suffixed spelling of this one to choose between.
CCC_MIG_FULL_PROFILE=0

# The GPU UUID of a device minor, out of the one file that carries both (gres.conf is
# written in device-minor order, so the minor is what everything here is keyed by). Used as nvidia-smi's `-i`, so no index can drift.
ccc_gpu_uuid() {
    awk -v m="$1" '
        /^GPU UUID:/     { u = $NF }
        /^Device Minor:/ { if ($NF == m) { print u; exit } }
    ' /proc/driver/nvidia/gpus/*/information
}

ccc_mig_enabled() {
    [ "$(nvidia-smi -i "$1" --query-gpu=mig.mode.current --format=csv,noheader 2>/dev/null)" = Enabled ]
}

# How many MIG devices this card currently offers. The resting state is exactly 1. Zero means MIG mode
# was switched on and nothing was ever carved, or a GPU instance exists with no compute instance under
# it — which is what an interrupted `-cgi` leaves, and is indistinguishable from "no GPU" to CUDA.
# Several means a Spawn carved the card and died before the epilog put it back.
ccc_mig_count() {
    nvidia-smi -L | awk -v g="$1" '
        /^GPU /      { u = $NF; gsub(/[()]/, "", u); cur = (u == g); next }
        cur && /MIG/ { n++ }
        END          { print n + 0 }
    '
}

# Put one card back to its resting state. Compute instances must go before the GPU instances holding
# them, and both are no-ops on a card that has neither.
ccc_mig_reset() {
    nvidia-smi mig -i "$1" -dci >/dev/null 2>&1
    nvidia-smi mig -i "$1" -dgi >/dev/null 2>&1
    nvidia-smi mig -i "$1" -cgi $CCC_MIG_FULL_PROFILE -C >/dev/null 2>&1
}

# The capability nodes a MIG device is reached through, opened to the Guise group for the job's life.
#
# **This is not the carving feature — it is what makes MIG work at all.** Creating an instance as root
# leaves its capability node 0400 root, so a Guise cannot open the very MIG device Slurm allocated it,
# and every Spawn on a MIG-mode Arm dies with no CUDA device. Measured: as a non-root uid with
# nvidia-cap1 readable, `-cgi` succeeds and `-C` fails "Insufficient Permissions", leaving a GPU instance
# nothing can use.
#
# `/proc/driver/nvidia-caps/mig-minors` is a STATIC, complete map — gpu0..31 / gi0..14 / ci0..7, all
# present on a box with MIG switched off entirely — so every node this card could ever need is knowable
# before any instance exists. All of them are created, which is what lets a Spawn re-carve its own card
# and still open what it made.
#
# ⚠ The per-GPU gi/ci caps are the only per-card gate here. `nvidia-cap1` (config) is global, so a Spawn
# holding one card can still ISSUE config calls against another card on the same box. What actually
# stops it is ConstrainDevices=yes denying the Pod that card's /dev/nvidiaN, so NVML never finds it to
# name. UNVERIFIED on hardware: confirm `nvidia-smi -L` inside a one-card Pod on a two-card MIG Arm
# lists only the allocated card.
ccc_mig_cap_node() {
    local m="$1" mode="$2" major
    major=$(awk '$2 == "nvidia-caps" { print $1 }' /proc/devices)
    [ -n "$major" ] || return 0
    mkdir -p /dev/nvidia-caps
    [ -e "/dev/nvidia-caps/nvidia-cap$m" ] || mknod "/dev/nvidia-caps/nvidia-cap$m" c "$major" "$m"
    chgrp "$CCC_GUISE_GROUP" "/dev/nvidia-caps/nvidia-cap$m"
    chmod "$mode" "/dev/nvidia-caps/nvidia-cap$m"
}

# `nvidia-cap1` is the *config* capability, and it is GLOBAL — one node for the whole box, not one per
# card. So it is opened and never closed: closing it when one Spawn ends would take carving away from a
# CONCURRENT Spawn still running on the box's other card, which is a bug two cards make reachable and a
# one-card box hides completely.
#
# Safe to leave open because config alone reaches no card. A MIG device is opened through the per-GPU
# access caps below, which ARE closed per job, and ConstrainDevices still decides which /dev/nvidiaN a
# Pod may touch at all — so a process holding only this cannot find a card to name.
ccc_mig_cap_config() { ccc_mig_cap_node 1 0440; }

# The per-GPU access caps: what a MIG device on THIS card is actually opened through, and the only
# per-card gate there is. Held for exactly the life of the Spawn that was allocated the card.
ccc_mig_caps() {
    local minor="$1" mode="$2" m
    for m in $(awk -v g="^gpu$minor/" '$1 ~ g { print $2 }' /proc/driver/nvidia-caps/mig-minors); do
        ccc_mig_cap_node "$m" "$mode"
    done
}

# The cards THIS job holds, as device minors. SLURM_JOB_GPUS is the allocated GRES indices, and a GRES
# index is a position in gres.conf — which CCC writes from /dev/nvidiaN in numeric order, so `sort -V`
# reproduces that list exactly.
ccc_held_minors() {
    local devs held g
    [ -n "${SLURM_JOB_GPUS:-}" ] || return 0
    [ -d /proc/driver/nvidia-caps ] || return 0
    compgen -G '/dev/nvidia[0-9]*' >/dev/null || return 0   # WSL: /dev/dxg only, no MIG
    mapfile -t devs < <(printf '%s\\n' /dev/nvidia[0-9]* | sort -V)
    IFS=, read -ra held <<< "$SLURM_JOB_GPUS"
    for g in "${held[@]}"; do printf '%s\\n' "${devs[$g]#/dev/nvidia}"; done
}
"""


EPILOG_TMPL = """\
#!/bin/bash
# CCC Spawn epilog. Rendered by `cuttle setup`; slurm.conf's Epilog= points here on every node.
# Reclaims a Spawn's node-local scratch after it ends, HOWEVER it ended. The job script's own
# `trap ... EXIT` fires on normal exit and ordinary signals, but NOT SIGKILL (OOM-killer, `scancel -9`,
# a Phase-2 Retract) — which would otherwise orphan the scratch until reboot. slurmd runs this at job
# end regardless. The two roots cover both branches of the job script (a remote Arm scratches in /tmp;
# the Head's co-located Arm under its state dir); the absent one is a harmless no-op on the other node.
# ALWAYS exit 0: a non-zero epilog drains the node, and a stale/missing dir must never do that.
rm -rf "/tmp/ccc-$SLURM_JOB_ID" "{head_scratch}/$SLURM_JOB_ID"
# THE kill for a ccc-launch container: Slurm never signals it (its cgroup is Podman's, not a task's),
# so after scancel / time limit it is still running here, holding its GPU. slurmd hands the node on only
# after this epilog, so the next job never shares the card. Host mount ns, as ccc-launch ran it.
timeout 60 nsenter --mount=/proc/1/ns/mnt -- podman rm -f -t 0 "{job_ctr}$SLURM_JOB_ID" >/dev/null 2>&1
# A Dive on the Head's own GPU lent it to the User's Tidepool; ccc-launch takes it back at the end, and
# this is the backstop if ccc-launch was killed first. No-op on an Arm (no helper) or any other job.
[ -x {helper} ] && timeout 60 {helper} dive-gpu revoke-job "$SLURM_JOB_ID" >/dev/null 2>&1
rm -rf "{jobs_dir}/$SLURM_JOB_ID"
# Podman's cgroup dirs (libpod-*, conmon) block Slurm's own rmdir of the job's cgroup tree. rmdir only
# takes empty dirs, so anything still in use is left alone.
for cg in /sys/fs/cgroup/system.slice/*slurmstepd.scope/job_"$SLURM_JOB_ID"; do
    [ -d "$cg" ] && find "$cg" -depth -type d -exec rmdir {{}} \\; 2>/dev/null
done
# sbatch's own stdout/stderr sinks. The wrapper's own
# `trap ... EXIT` already reaps these when it runs — but that's the same trap that misses SIGKILL, so a
# hard-killed job leaves one behind forever without this. Same "keep only if non-empty" rule as the
# trap: a sink that caught real stderr (the shell died before out/err existed) is worth keeping.
# A glob, because the name carries a per-submission tag the epilog cannot derive (job id alone is not
# unique across teardowns — see ccc_head_api.spawns' `sink=` line). A glob that matches nothing expands
# to itself, and `rm -f` on a path that does not exist is a no-op, so the loop is safe either way.
for s in "/tmp/ccc-$SLURM_JOB_ID.slurm" "/tmp/ccc-$SLURM_JOB_ID-"*.slurm; do
    [ -s "$s" ] || rm -f "$s"
done
[ -s "/tmp/ccc-bench-$SLURM_JOB_ID.log" ] || rm -f "/tmp/ccc-bench-$SLURM_JOB_ID.log"
# Take the Spawn dir back off the **Guise** the prolog lent it to.
# The prolog takes, the epilog returns, so the loan lasts exactly the job's lifetime — and unlike the
# job script's own EXIT trap, this still runs after a SIGKILL. Without it a User's LATER Spawn could
# rewrite an earlier one's recorded `exit` code and outputs, since both run as the same Guise.
# Head's co-located Arm only; the directory does not exist on a donor Arm, so this no-ops there.
[ -d "{head_spawns}/$SLURM_JOB_ID" ] && chown -R {mantle}:{mantle} "{head_spawns}/$SLURM_JOB_ID"

CCC_GUISE_GROUP={guise_group}
{mig_admin}
# Put every MIG-mode card this Spawn held back to one full-size instance. THE reset point: the prolog
# only repairs what this never got to (a rebooted node, a killed slurmd), so a card is normally correct
# from the moment a Spawn ends rather than from the moment the next one starts — which is what keeps an
# idle box honest about what it holds.
#
# Caps are closed FIRST, so nothing left running can carve the card while it is being put back. Like
# everything else here this must never exit non-zero: a drained node over a stale instance would cost
# the cluster the whole Arm.
for minor in $(ccc_held_minors); do
    ccc_mig_caps "$minor" 0400
    uuid=$(ccc_gpu_uuid "$minor")
    ccc_mig_enabled "$uuid" && ccc_mig_reset "$uuid"
done
exit 0
"""


def render_epilog() -> str:
    return EPILOG_TMPL.format(head_scratch=config.HEAD_SCRATCH_DIR, helper=config.TIDEPOOL_HELPER_BIN,
                              job_ctr=config.JOB_CONTAINER_PREFIX, jobs_dir=config.JOBS_DIR,
                              head_spawns=config.HEAD_SPAWNS_DIR,
                              mantle=config.HEAD_API_USER,
                              guise_group=config.GUISE_GROUP,
                              mig_admin=MIG_ADMIN_SH)


# --- the Prolog ----------------------------------------------------------------
# Where a **Guise** comes into existence on a machine, and the only place it can be: it runs as root
# (SlurmdUser=root), before the job step, on whatever node Slurm picked, with no network and no head.db.
# `SLURM_JOB_UID` is its ONLY input — which is why a Guise is named `ccc-u<uid>` after its uid and
# nothing else. A Slurm job LAUNCHES fine as a uid with no local account, but `sudo ccc-launch` (the
# Guise group's one sudoers rule) needs the account and its group membership. So this is mandatory,
# and it lands in time: it runs before the job step that calls ccc-launch.
#
# Written on every node by `cuttle setup` exactly as the epilog is (setup._start_slurmd): configless
# Slurm serves slurm.conf but NOT the scripts it names, and a `Prolog=` pointing at a file a node does
# not have fails that node's jobs and DRAINS it — which does not self-heal when the file is fixed. See
# the rollout gate in enroll.fleet_is_prolog_ready.
PROLOG_TMPL = """\
#!/bin/bash
# CCC Spawn prolog. Rendered by `cuttle setup`; slurm.conf's Prolog= points here on every node.
# Creates the **Guise** this Spawn runs as, if this node has never run one of that User's Spawns
# before, and lends it the two directories it needs to work in. Runs as root, before the job step.
uid="$SLURM_JOB_UID"
jid="$SLURM_JOB_ID"
guise="{prefix}$uid"
# Anything outside the reserved Guise block is not ours to touch: the Mantle at {mantle_uid},
# root, slurm, a donor's own accounts. They already exist, and
# creating an account for one would be a bug. Nothing to do — and NEVER a non-zero exit, which would
# drain the node over a job we simply have no opinion about.
if [ "$uid" -lt {uid_min} ] || [ "$uid" -gt {uid_max} ]; then exit 0; fi
existing=$(getent passwd "$uid" | cut -d: -f1)
if [ -z "$existing" ]; then
    # First Spawn of this User's ever to land on this box. The shared group exists only so the HEAD's
    # sudoers rule has a Runas list (sudo never runs on an Arm); created here too so a Guise is the
    # same account everywhere. Home-less: nothing reads it, and the Pod makes its own in its overlay.
    groupadd --gid "$uid" "$guise" 2>/dev/null
    groupadd --gid {group_gid} {group} 2>/dev/null
    useradd --uid "$uid" --gid "$uid" --groups {group} --no-create-home \\
            --shell /usr/sbin/nologin --comment "{gecos}" "$guise" || exit 1
elif [ "$existing" != "$guise" ]; then
    # The uid is taken by an account CCC did not create. Refusing is the whole point: running a
    # stranger's Spawn as a donor's own login account is worse than draining this node, which is what
    # this exit does. Loud, and in the node's slurmd log.
    echo "ccc-prolog: uid $uid is '$existing' on this node, not '$guise' — refusing to run a Spawn" \\
         "as an account CCC does not own" >&2
    exit 1
fi

# Head's co-located Arm ONLY: the two directories a Spawn writes in are under the Head's state dir, and
# they are ccc-owned. Lend them to the Guise for exactly this job; the epilog takes them back. Both are
# keyed by the JOB ID, which is why this needs nothing the prolog cannot already see. Absent on a donor
# Arm (a remote Spawn scratches in /tmp and rsyncs its results home), so both branches no-op there.
#
# `install -d` rather than mkdir+chown: it is idempotent and it applies the ownership either way, so it
# does not matter whether this or the API's own mkdir got here first.
if [ -d "{head_spawns}" ]; then
    # group {mantle}: the Head API still reads out/err back and rsyncd still stages results in as the
    # Mantle. 0770 gives both sides everything they need and everyone else nothing.
    install -d -o "$guise" -g {mantle} -m 0770 "{head_spawns}/$jid" "{head_spawns}/$jid/outputs"
fi
if [ -d "{head_scratch}" ]; then
    install -d -o "$guise" -g "$guise" -m 0700 "{head_scratch}/$jid"
fi

CCC_GUISE_GROUP={group}
{mig_admin}
# Hand this Spawn the MIG-mode cards it was allocated, in a state it can actually use.
#
# REPAIR, not reset — the epilog is what normally restores a card. This catches the cases it never ran
# for (a rebooted node, a hard-killed slurmd) and the first Spawn ever to land on a card whose owner
# switched MIG mode on and never carved it, which is exactly the state that produces "CUDA not visible
# in Pod" with a perfectly healthy GPU sitting there.
#
# The caps are opened for the allocated cards only, and the epilog closes them again, so a Guise holds
# this for precisely the life of its own Spawn.
for minor in $(ccc_held_minors); do
    uuid=$(ccc_gpu_uuid "$minor")
    ccc_mig_enabled "$uuid" || continue
    [ "$(ccc_mig_count "$uuid")" = 1 ] || ccc_mig_reset "$uuid"
    ccc_mig_cap_config
    ccc_mig_caps "$minor" 0440
done
exit 0
"""


# --- job_container/tmpfs -------------------------------------------------------
# Written on every node by `cuttle setup`, exactly as the two hook scripts are: the plugin reads it from
# beside slurm.conf, and configless serves slurm.conf but NOT its siblings that are per-node. A
# `JobContainerType=` naming a plugin whose conf is absent drains the node, which is what the rollout
# gate in enroll.arms_missing exists to prevent (config.ARM_CAP_TMPFS).
#
# `Dirs` is deliberately absent: it defaults to "/tmp,/dev/shm", which is exactly what we want, and
# /dev/shm "has special handling, and instead of a bind mount is always a fresh tmpfs filesystem" (man
# 5 job_container.conf). So shared-memory IPC — a torch DataLoader's workers, above all — keeps a real
# RAM-backed filesystem. Only /tmp is a bind mount off BasePath's disk.
JOB_CONTAINER_CONF_TMPL = """\
# Rendered by `cuttle setup`. Do not hand-edit.
# A private /tmp and /dev/shm per Spawn. slurmd creates BasePath itself (AutoBasePath) and holds it
# mounted `private` from start to shutdown; it does NOT remove it, so teardown does.
AutoBasePath=true
BasePath={base}
"""


def render_job_container_conf() -> str:
    return JOB_CONTAINER_CONF_TMPL.format(base=config.JOB_CONTAINER_BASE)


def render_prolog() -> str:
    return PROLOG_TMPL.format(
        prefix=config.GUISE_PREFIX,
        mantle=config.HEAD_API_USER,
        mantle_uid=config.CCC_UID,
        uid_min=config.GUISE_CLUSTER_UID,
        uid_max=config.GUISE_UID_MAX,
        group=config.GUISE_GROUP,
        group_gid=config.GUISE_GROUP_GID,
        gecos=config.GUISE_GECOS,
        head_spawns=config.HEAD_SPAWNS_DIR,
        head_scratch=config.HEAD_SCRATCH_DIR,
        mig_admin=MIG_ADMIN_SH,
    )


def render_bench_sbatch(node: str, head_mesh_ip: str, bench_secret: str, bench_image: str,
                        gpus: int = 1) -> str:
    """`gpus` = how many Devices this node registered. The job holds all of them and returns one Power
    each, so a re-benchmark of a 4-card box is still a single job."""
    spec = {"image": bench_image, "argv": config.BENCH_ARGV,
            "env": {"CCC_BENCH_OUT": "/job"}}
    return BENCH_SBATCH_TMPL.format(
        node=node, gpus=gpus, minutes=config.BENCH_TIME_MINUTES,
        spec_line=launch_spec_line(spec), launch=config.CCC_LAUNCH_BIN, jobs_dir=config.JOBS_DIR,
        head_ip=head_mesh_ip, bench_mod=config.RSYNC_MODULE_BENCH,
        bench_user=config.RSYNC_BENCH_USER, bench_secret=bench_secret)


def launch_spec_line(spec: dict) -> str:
    """The `# ccc-launch-spec:` line ccc-launch reads back from slurmctld's copy of the batch script."""
    return "# ccc-launch-spec: " + base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()


def render_cgroup_conf() -> str:
    """No parameters, and that is the design: configless serves ONE cgroup.conf to the whole fleet."""
    return CGROUP_CONF_TMPL


def render_gres_conf(nodes: list[tuple[str, list[str]]]) -> str:
    """`nodes` = [(node_name, [gpu_device, ...])], the Head plus every admitted Arm.

    One line per Device, in list order — see GRES_CONF_LINE for why that form and why the order is
    load-bearing.
    """
    header = "# Rendered by `cuttle setup --head` / `cuttle arm admit`. Do not hand-edit.\n"
    return header + "".join(GRES_CONF_LINE.format(node=n, device=d)
                            for n, devices in nodes for d in devices)


def head_gres_node() -> tuple[str, list[str]]:
    """The Head's own co-located Arm. Detected, not assumed: the Head is the box most likely to hold
    several cards, and it never enrolls, so nothing else would ever report its hardware."""
    return system.hostname(), system.gpu_devices()
