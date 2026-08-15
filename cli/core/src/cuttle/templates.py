"""Render single-node slurm.conf + gres.conf."""

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
# EXIT trap uses to send work home (ccc_head_api.spawns): a Workspace's final mirror, a Tide Pool's
# Recovery Window copy. Slurm's default 30s is not enough for the latter, which is a COLD rsync of up to
# RECOVERY_MAX_BYTES (16 GB) made once, at the end, with no periodic pass behind it — so at 30s the
# day-long window quietly did not exist for exactly the Spawns with the most to lose.
#
# A CEILING, not a wait: the job leaves COMPLETING the moment its trap returns, which is seconds in the
# ordinary case. What 600 buys is that a big tree finishes instead of being truncated — and a truncated
# copy is worth nothing, since `recovery.size` never lands, so `register_new` never registers it and
# `sweep_orphans` deletes it (ccc_head_api.recovery).
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
# explicitly. Per-node lines are unavoidable once Arms disagree about the device path: native Linux
# has /dev/nvidia0, WSL2 has /dev/dxg. Served to Arms over configless, so the Head authors all of it.
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
#
# WSL Arms: unverified. WSL2 presents a fake cgroup-v1 `tmpfs`, and what slurmd does with it here is
# not known — this file ships cluster-wide anyway and the WSL box is a live test. A WSL Arm cannot be
# GPU-isolated regardless: /dev/dxg is ONE device node for all its GPUs.
CGROUP_CONF_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
# Detect cgroup v1 vs v2 rather than pinning either — the fleet is mixed (native Ubuntu is v2).
CgroupPlugin=autodetect
# Confine a Spawn to the GPUs it was allocated, and no others.
ConstrainDevices=yes
"""

# --- the Power benchmark ------------------------------------------------------
# What a device can *do*, measured — never a spec sheet. It runs **inside the Pod**, submitted by the
# Head down the ordinary Slurm path and pinned to the node, so the number reflects the machine as
# Spawns will really meet it: a WSL GPU reached through /dev/dxg, a laptop card that thermally
# throttles, a stale driver, a starved PCIe link. None of that is on a spec sheet, and all of it is
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
# ascending order, so torch's device i is the i'th card of this node's gres.conf lines — the same
# index `gres_detail` reports at Settlement. The benchmark holds every card (--gres=gpu:N), so the
# allocation is the whole node and the two orders coincide exactly.
BENCH_PY = """\
import json, os, time, torch
ndev = torch.cuda.device_count()
assert ndev, "CUDA not visible in Pod"
props = torch.cuda.get_device_properties(0)   # homogeneous by enrollment (system.gpu_profile)
powers = []
for i in range(ndev):
    with torch.cuda.device(i):                # scopes the allocator and synchronize() to this card
        a = torch.randn({n}, {n}, device="cuda")
        b = torch.randn({n}, {n}, device="cuda")
        for _ in range({warmup}):    # the first matmul pays cuBLAS init — never time it
            c = a @ b
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        for _ in range({iters}):
            c = a @ b
        torch.cuda.synchronize()
        elapsed = time.perf_counter() - t0
        powers.append(round({iters} * 2 * {n}**3 / elapsed / 1e12, 2))  # fp32 TFLOP/s
        del a, b, c                  # a card's buffers are freed before the next one is filled
        torch.cuda.empty_cache()
json.dump({{
    "powers": powers,
    "vram_mb": props.total_memory // 1024**2,
    "gpu": props.name,
}}, open(os.environ["CCC_BENCH_OUT"] + "/power", "w"))
"""

# Submitted as the **cluster's own Guise** (`sudo -u ccc-u60001 sbatch`), like every Spawn is submitted
# as somebody's — it must be, or slurmd on a remote Arm would setuid() to an operator uid that does not
# exist there. The uid is reserved (config.GUISE_CLUSTER_UID) and the prolog creates the account on the
# Arm being priced, exactly as it would for any User. --nodelist pins it to the node being
# priced, and --gres=gpu:N takes ALL of its cards at once so one job measures every Device. Results come home the way every remote Spawn's do: rsync to the Head's [spawns] module, which
# the Head then reads. That path is identical on the Head's own co-located Arm (it rsyncs to itself over
# the mesh), so there is no is-this-the-Head branch here.
BENCH_SBATCH_TMPL = """\
#!/bin/bash
#SBATCH --job-name=ccc-bench
#SBATCH --nodelist={node}
#SBATCH --gres=gpu:{gpus}
#SBATCH --time={minutes}
#SBATCH --chdir=/tmp
#SBATCH --output=/tmp/ccc-bench-%j.log
#SBATCH --error=/tmp/ccc-bench-%j.log
set -e
out=/tmp/ccc-bench-$SLURM_JOB_ID
# The .log is sbatch's own sink and stays empty unless the shell itself failed — kept only then, or
# every benchmark would leave a 0-byte file behind (same rule as a Spawn's /tmp/ccc-<jid>.slurm).
sink=/tmp/ccc-bench-$SLURM_JOB_ID.log
trap 'rm -rf "$out"; [ -s "$sink" ] || rm -f "$sink"' EXIT
mkdir -p "$out"
cat > "$out/bench.py" <<'CCC_BENCH_PY'
{bench_py}
CCC_BENCH_PY
# The stage-out uses the cluster-lifetime secret from RSYNCD_BENCH_SECRETS — a benchmark never goes
# through the Head API, so there is no **Siphon** for it to be handed. The module is rooted at the bench
# subtree, so this URL reaches the Power files and nothing else.
export RSYNC_PASSWORD={bench_secret}
# The torch this measurement needs, which base.sif no longer carries. Built by THIS node at
# `cuttle setup` (setup.build_bench_env) into config.BENCH_ENV_DIR, so nothing is fetched here — the
# env was 4.6 GB and pulling it from the Head measured 29 minutes against this job's 10-minute wall.
#
# CCC_BENCH_OUT is the benchmark's OWN channel and has nothing to do with a Spawn's declared outputs
# (there is no CCC_OUT in a Spawn's Pod any more — see ccc_head_api.spawns' PAYLOAD_SCRIPT). This is
# CCC's own script writing to CCC's own path, so it may name it whatever it likes.
#
# /tmp is auto-mounted by Apptainer, so it needs no explicit --bind (unlike a Spawn's, which lives
# under /var/lib/ccc). The env is bound at the ONE path it was built at and cannot be moved from
# (config.BENCH_ENV_MOUNT), and its own interpreter is what runs — the image has none.
apptainer exec --nv --env CCC_BENCH_OUT="$out" --bind "{bench_env_dir}:{env_mount}" \\
  {sif} {env_mount}/venv/bin/python "$out/bench.py"
rsync -a "$out/power" "rsync://{bench_user}@{head_ip}/{bench_mod}/{node}/"
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


# Base Pod image: plain Ubuntu and the four things a Pod cannot bring with it. NO Python, no torch, no
# CUDA — every Spawn declares an **Environment** and that Environment carries its own interpreter and its
# own torch (ccc_head_api.environments), so anything supplied here would be shadowed by what the Spawn
# brought and paid for in durable disk on every donated box regardless. ~150 MB instead of 2.9 GB.
#
# `--nv` still injects the host's driver at exec, and PyPI's torch wheels bundle their own CUDA runtime,
# so removing the CUDA image costs a GPU Spawn nothing.
#
# * `openssh-server` — sshd runs INSIDE the Pod, unprivileged, as the Mantle (see
#   ccc_head_api.spawns.PAYLOAD_TIDEPOOL). It never setuid()s: the "login" is to the SAME uid already
#   running the Pod, so there is no privilege drop and no per-User account to provision on the Arm.
#   It pulls in openssh-client, which is where `ssh-keygen -A` comes from.
# * `ca-certificates` — an Environment is built in a Pod off THIS image, and uv fetches every package
#   (and the standalone CPython itself) over TLS. A stock Ubuntu image carries no trust store, so
#   without this every build fails at the first HTTPS connection. The PyTorch image happened to have
#   one; nothing here may depend on happening to.
# * `build-essential` — a toolchain, because Python packages COMPILE AT RUNTIME. Triton is the case
#   that forced it: the first `torch.compile`d kernel makes Inductor instantiate Triton's CUDA driver,
#   which builds a `cuda_utils` C shim by shelling out to `$CC`, else to whatever `gcc`/`clang` is on
#   PATH. No wheel on PyPI ships a compiler, so an **Environment** cannot supply one however it is
#   declared, and `--contain` (spawns.PAYLOAD_SCRIPT) hides the host's — a box with gcc in /usr/bin and
#   none in its Pods was the observed failure. Without it every Spawn that reaches torch.compile dies
#   at the WORST moment: the card is already held and the Environment already built, so it is a
#   full-price failed Spawn rather than a refusal at submit.
#
#   **The metapackage, deliberately, rather than the minimal closure.** Naming packages one at a time
#   cost two fleet-wide `teardown --all` cycles to learn what a toolchain is: `gcc` alone missed
#   `libc6-dev` (a *Recommends*, dropped by `--no-install-recommends`, and the owner of stdio.h,
#   stdlib.h and the crt objects), and `gcc` + `libc6-dev` still had no C++ compiler for Inductor's CPU
#   kernels, `torch.utils.cpp_extension`, or any sdist that has to build a wheel. Each wrong guess is
#   another teardown of every enrolled box, to save ~100 MB. build-essential is what every one of those
#   packages already expects to find, and it ends the guessing.
#
#   It is the single largest thing in this image — roughly +250 MB, against ~150 MB for everything else
#   put together — and that is a real cost paid in durable disk on somebody's own machine. Named here
#   as a deliberate trade, not an oversight.
# * a baked-in `mantle` passwd/group/home entry, at exactly CCC_UID/CCC_GID — the ONE thing unprivileged
#   sshd still needs that a stock image does not have: the connecting uid has to resolve via
#   getpwuid, or sshd cannot find a home dir for authorized_keys or set up a session at all. Written
#   directly rather than via useradd/groupadd (not guaranteed present in every base image) — three text
#   appends and a directory, nothing that can fail for a missing tool.
BASE_DEF_TMPL = """\
Bootstrap: docker
From: {image}

%post
    # Runs inside the (disposable) build container only — never the host.
    apt-get update && apt-get install -y --no-install-recommends openssh-server ca-certificates \
      build-essential \
      && rm -rf /var/lib/apt/lists/*
    echo "mantle:x:{uid}:{gid}:CCC Mantle:/home/mantle:/bin/bash" >> /etc/passwd
    echo "mantle:x:{gid}:" >> /etc/group
    mkdir -p /home/mantle && chown {uid}:{gid} /home/mantle
"""


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
# must not be overwritten may live under it. Nothing does: the benchmark's torch env is built by each
# node at setup (config.BENCH_ENV_DIR) rather than served from here.
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
        rsyncd_dir=config.RSYNCD_DIR,
        v4=config.MESH_V4_PREFIX, v6=config.MESH_V6_PREFIX,
    )


def render_rsyncd_unit() -> str:
    return RSYNCD_UNIT_TMPL.format(ts_service=config.CCC_TS_SERVICE, conf=config.RSYNCD_CONF)


def render_base_def() -> str:
    return BASE_DEF_TMPL.format(image=config.BASE_IMAGE_DOCKER,
                                uid=config.CCC_UID, gid=config.CCC_GID)


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
# WRAPPER, not just the Apptainer child inside it. Without it a remote Spawn's scratch sits in the
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
                      missing_tmpfs: list[str] | None = None) -> str:
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
    """
    sockets, cores, threads = system.cpu_topology()
    feature = ",".join([config.vram_feature(system.gpu_vram_mb()), *sorted(features or [])])
    prolog = (_PROLOG_HELD.format(n=len(missing_prolog), missing=", ".join(missing_prolog))
              if missing_prolog else _PROLOG_LINE.format(prolog=config.SLURM_PROLOG))
    tmpfs = (_TMPFS_HELD.format(n=len(missing_tmpfs), missing=", ".join(missing_tmpfs))
             if missing_tmpfs else _TMPFS_LINE)
    return SLURM_CONF_TMPL.format(
        prolog=prolog,
        tmpfs=tmpfs,
        weight=weight,
        cluster=config.CLUSTER_NAME,
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
        mem=system.real_memory_mb(),
        gpus=len(system.gpu_devices()),
        vram_feature=feature,
        state=config.SLURM_STATE_DIR,
        spool=config.SLURMD_SPOOL_DIR,
        logdir=config.SLURM_LOG_DIR,
    )


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
exit 0
"""


def render_epilog() -> str:
    return EPILOG_TMPL.format(head_scratch=config.HEAD_SCRATCH_DIR,
                              head_spawns=config.HEAD_SPAWNS_DIR,
                              mantle=config.HEAD_API_USER)


# --- the Prolog ----------------------------------------------------------------
# Where a **Guise** comes into existence on a machine, and the only place it can be: it runs as root
# (SlurmdUser=root), before the job step, on whatever node Slurm picked, with no network and no head.db.
# `SLURM_JOB_UID` is its ONLY input — which is why a Guise is named `ccc-u<uid>` after its uid and
# nothing else. Measured on hardware: a Slurm job LAUNCHES fine as a uid with no local account, but
# `apptainer exec` refuses one outright ("Couldn't determine user account
# information"), and every Spawn is an apptainer exec. So this is mandatory, not an optimisation — and
# it lands in time, because the job step is where apptainer is invoked and this runs before it.
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
    )


def render_bench_sbatch(node: str, head_mesh_ip: str, bench_secret: str, gpus: int = 1) -> str:
    """`gpus` = how many Devices this node registered. The job holds all of them and returns one Power
    each, so a re-benchmark of a 4-card box is still a single Spawn."""
    bench_py = BENCH_PY.format(n=config.BENCH_MATRIX_N, warmup=config.BENCH_WARMUP,
                               iters=config.BENCH_ITERS)
    return BENCH_SBATCH_TMPL.format(
        node=node,
        gpus=gpus,
        minutes=config.BENCH_TIME_MINUTES,
        bench_py=bench_py,
        sif=config.CCC_SIF,
        head_ip=head_mesh_ip,
        bench_mod=config.RSYNC_MODULE_BENCH,
        bench_env_dir=config.BENCH_ENV_DIR,
        env_mount=config.BENCH_ENV_MOUNT,
        bench_user=config.RSYNC_BENCH_USER,
        bench_secret=bench_secret,
    )


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
