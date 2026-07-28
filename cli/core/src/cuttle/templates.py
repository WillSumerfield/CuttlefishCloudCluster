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
# `--gres=gpu:1` for scheduling and then hands the step a machine with every card visible: measured on
# the first remote job, `SLURM_JOB_GPUS` came back EMPTY (docs/TODO.md). task/cgroup is what puts the
# step in a device cgroup and tells it which GPU it holds; proctrack/cgroup is its prerequisite
# (task/cgroup needs cgroup-based process tracking, not linuxproc's pid-scanning). See cgroup.conf.
ProctrackType=proctrack/cgroup
TaskPlugin=task/cgroup
SwitchType=switch/none
MpiDefault=none
ReturnToService=2

SlurmUser=slurm
SlurmdUser=root
# Reclaim a Spawn's node-local scratch after it ends, HOWEVER it ended (the job's own trap misses
# SIGKILL). Served configless like the rest of this file; the script itself is written on every node by
# `cuttle setup`. Runs as root (SlurmdUser), at job end, on the node that ran the Spawn.
Epilog={epilog}
StateSaveLocation={state}
SlurmdSpoolDir={spool}
SlurmctldPidFile=/run/slurmctld.pid
SlurmdPidFile=/run/slurmd.pid
SlurmctldLogFile={logdir}/slurmctld.log
SlurmdLogFile={logdir}/slurmd.log

SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_Core

# --- priority (Phase 4 slice 0) ---
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
# (Fairshare stays 0 — it needs slurmdbd, and we compute standing ourselves. See docs/DesignDoc.md §5.)
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
# by the next `arm authorize`/`admit`/`remove` (verified 2026-07-14: update -> 276, reconfigure -> 1).
# On a first setup Power is unknown here — it is benchmarked after the node is live — so this renders
# 1 and `scontrol update` carries it until the next render. Donor Arms are dynamic, so they are not in
# this file and cannot be fixed this way; enroll.reapply_weights() re-pushes them after a reconfigure.
NodeName={host} Sockets={sockets} CoresPerSocket={cores} ThreadsPerCore={threads} \
RealMemory={mem} Gres=gpu:1 Feature={vram_feature} Weight={weight} State=UNKNOWN
PartitionName={partition} Nodes=ALL Default=YES MaxTime=INFINITE State=UP
"""

# Distro slurm-wlm is built without NVML → AutoDetect=nvml is fatal. Map each node's device
# explicitly. Per-node lines are unavoidable once Arms disagree about the device path: native Linux
# has /dev/nvidia0, WSL2 has /dev/dxg. Served to Arms over configless, so the Head authors all of it.
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
# not known — this file ships cluster-wide anyway and the WSL box is a live test (docs/TODO.md). A WSL
# Arm cannot be GPU-isolated regardless: /dev/dxg is ONE device node for all its GPUs.
CGROUP_CONF_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
# Detect cgroup v1 vs v2 rather than pinning either — the fleet is mixed (native Ubuntu is v2).
CgroupPlugin=autodetect
# Confine a Spawn to the GPUs it was allocated, and no others.
ConstrainDevices=yes
"""

# --- the Power benchmark (Phase 4 slice 0) ----------------------------------
# What a device can *do*, measured — never a spec sheet. It runs **inside the Pod**, submitted by the
# Head down the ordinary Slurm path and pinned to the node, so the number reflects the machine as
# Spawns will really meet it: a WSL GPU reached through /dev/dxg, a laptop card that thermally
# throttles, a stale driver, a starved PCIe link. None of that is on a spec sheet, and all of it is
# what an hour on the card is actually worth.
#
# Power is the term that stops the meter paying an owner MORE for being SLOWER. Bill seconds alone and
# the same matmul costs 4x on a card a quarter the speed — so donating your worst GPU would pay best,
# which is precisely backwards from what Karma exists to encourage.
BENCH_PY = """\
import json, os, time, torch
assert torch.cuda.is_available(), "CUDA not visible in Pod"
dev = torch.device("cuda")
props = torch.cuda.get_device_properties(dev)
a = torch.randn({n}, {n}, device=dev)
b = torch.randn({n}, {n}, device=dev)
for _ in range({warmup}):        # the first matmul pays cuBLAS init — never time it
    c = a @ b
torch.cuda.synchronize()
t0 = time.perf_counter()
for _ in range({iters}):
    c = a @ b
torch.cuda.synchronize()
elapsed = time.perf_counter() - t0
json.dump({{
    "power": round({iters} * 2 * {n}**3 / elapsed / 1e12, 2),  # fp32 TFLOP/s
    "vram_mb": props.total_memory // 1024**2,
    "gpu": props.name,
}}, open(os.environ["CCC_OUT"] + "/power", "w"))
"""

# Submitted as the Mantle (`sudo -u ccc sbatch`), like every Spawn — it must be, or slurmd on a remote
# Arm would setuid() to an operator uid that does not exist there. --nodelist pins it to the node being
# priced. Results come home the way every remote Spawn's do: rsync to the Head's [spawns] module, which
# the Head then reads. That path is identical on the Head's own co-located Arm (it rsyncs to itself over
# the mesh), so there is no is-this-the-Head branch here.
BENCH_SBATCH_TMPL = """\
#!/bin/bash
#SBATCH --job-name=ccc-bench
#SBATCH --nodelist={node}
#SBATCH --gres=gpu:1
#SBATCH --time={minutes}
#SBATCH --chdir=/tmp
#SBATCH --output=/tmp/ccc-bench-%j.log
#SBATCH --error=/tmp/ccc-bench-%j.log
set -e
out=/tmp/ccc-bench-$SLURM_JOB_ID
trap 'rm -rf "$out"' EXIT
mkdir -p "$out"
cat > "$out/bench.py" <<'CCC_BENCH_PY'
{bench_py}
CCC_BENCH_PY
# /tmp is auto-mounted by Apptainer, so $CCC_OUT needs no explicit --bind (unlike a Spawn's, which
# lives under /var/lib/ccc).
apptainer exec --nv --env CCC_OUT="$out" {sif} python "$out/bench.py"
rsync -a "$out/power" "rsync://{head_ip}/{spawns_mod}/{bench_dir}/{node}/"
"""


# Base Pod image: official PyTorch + pinned wandb (so Spawns can log W&B out of the box).
BASE_DEF_TMPL = """\
Bootstrap: docker
From: {image}

%post
    # Runs inside the (disposable) build container only — never the host.
    python -m pip install --no-cache-dir --break-system-packages {wandb}
"""


# rsync daemon for stage-in/out. `address` pins it to the Head's mesh IP, so it never listens on the
# LAN or the personal tailnet. `hosts allow` = the mesh prefixes: being on the mesh IS the authz,
# same as /enroll. Runs as root (873 is privileged) but each module drops to `ccc` (the service
# account that owns the Head state), so files are read/written as ccc. `use chroot = no`: no root
# chroot (simpler; the mesh is the boundary). Both module roots live under HEAD_STATE_DIR (ccc-owned);
# [project] is read-only so a job can never write back into a served upload.
RSYNCD_CONF_TMPL = """\
# Rendered by `cuttle setup --head`. Do not hand-edit.
address = {mesh_ip}
port = {port}
use chroot = no
uid = {user}
gid = {group}
read only = true
[{project_mod}]
    path = {projects_dir}
    read only = true
    hosts allow = {v4} {v6}
[{spawns_mod}]
    path = {spawns_dir}
    read only = false
    hosts allow = {v4} {v6}
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
        project_mod=config.RSYNC_MODULE_PROJECT, projects_dir=config.HEAD_PROJECTS_DIR,
        spawns_mod=config.RSYNC_MODULE_SPAWNS, spawns_dir=config.HEAD_SPAWNS_DIR,
        v4=config.MESH_V4_PREFIX, v6=config.MESH_V6_PREFIX,
    )


def render_rsyncd_unit() -> str:
    return RSYNCD_UNIT_TMPL.format(ts_service=config.CCC_TS_SERVICE, conf=config.RSYNCD_CONF)


def render_base_def() -> str:
    return BASE_DEF_TMPL.format(image=config.BASE_IMAGE_DOCKER, wandb=config.WANDB_PKG)


def render_slurm_conf(mesh_ip: str, weight: int = 1) -> str:
    """`mesh_ip` = the Head's address on the CCC mesh. Baked in because MagicDNS is off: nothing in
    this system can resolve a hostname, so Slurm gets the address literally.

    `weight` = the Head Arm's Weight (∝ 1/Power). Defaults to 1 for the first setup, when the box has
    not been benchmarked yet; thereafter the caller passes it from the registry so a reconfigure
    cannot wipe it. See the NodeName comment in the template.
    """
    sockets, cores, threads = system.cpu_topology()
    return SLURM_CONF_TMPL.format(
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
        vram_feature=config.vram_feature(system.gpu_vram_mb()),
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
exit 0
"""


def render_epilog() -> str:
    return EPILOG_TMPL.format(head_scratch=config.HEAD_SCRATCH_DIR)


def render_bench_sbatch(node: str, head_mesh_ip: str) -> str:
    bench_py = BENCH_PY.format(n=config.BENCH_MATRIX_N, warmup=config.BENCH_WARMUP,
                               iters=config.BENCH_ITERS)
    return BENCH_SBATCH_TMPL.format(
        node=node,
        minutes=config.BENCH_TIME_MINUTES,
        bench_py=bench_py,
        sif=config.CCC_SIF,
        head_ip=head_mesh_ip,
        spawns_mod=config.RSYNC_MODULE_SPAWNS,
        bench_dir=config.BENCH_DIR_NAME,
    )


def render_cgroup_conf() -> str:
    """No parameters, and that is the design: configless serves ONE cgroup.conf to the whole fleet."""
    return CGROUP_CONF_TMPL


def render_gres_conf(nodes: list[tuple[str, str]]) -> str:
    """`nodes` = [(node_name, gpu_device)], the Head plus every admitted Arm."""
    header = "# Rendered by `cuttle setup --head` / `cuttle arm admit`. Do not hand-edit.\n"
    return header + "".join(GRES_CONF_LINE.format(node=n, device=d) for n, d in nodes)


def head_gres_node() -> tuple[str, str]:
    return system.hostname(), config.GPU_DEVICE
