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
ProctrackType=proctrack/linuxproc
TaskPlugin=task/none
SwitchType=switch/none
MpiDefault=none
ReturnToService=2

SlurmUser=slurm
SlurmdUser=root
StateSaveLocation={state}
SlurmdSpoolDir={spool}
SlurmctldPidFile=/run/slurmctld.pid
SlurmdPidFile=/run/slurmd.pid
SlurmctldLogFile={logdir}/slurmctld.log
SlurmdLogFile={logdir}/slurmd.log

SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_Core

GresTypes=gpu
# The Head's co-located Arm is a *static* node — always in sinfo, even with slurmd down.
# Donor Arms are dynamic and appear here at runtime; Nodes=ALL is what they auto-join.
NodeName={host} Sockets={sockets} CoresPerSocket={cores} ThreadsPerCore={threads} \
RealMemory={mem} Gres=gpu:1 State=UNKNOWN
PartitionName={partition} Nodes=ALL Default=YES MaxTime=INFINITE State=UP
"""

# Distro slurm-wlm is built without NVML → AutoDetect=nvml is fatal. Map the device explicitly.
GRES_CONF_TMPL = "Name=gpu File={device}\n"

# Base Pod image: official PyTorch + pinned wandb (so Spawns can log W&B out of the box).
BASE_DEF_TMPL = """\
Bootstrap: docker
From: {image}

%post
    # Runs inside the (disposable) build container only — never the host.
    python -m pip install --no-cache-dir --break-system-packages {wandb}
"""


def render_base_def() -> str:
    return BASE_DEF_TMPL.format(image=config.BASE_IMAGE_DOCKER, wandb=config.WANDB_PKG)


def render_slurm_conf(mesh_ip: str) -> str:
    """`mesh_ip` = the Head's address on the CCC mesh. Baked in because MagicDNS is off: nothing in
    this system can resolve a hostname, so Slurm gets the address literally."""
    sockets, cores, threads = system.cpu_topology()
    return SLURM_CONF_TMPL.format(
        cluster=config.CLUSTER_NAME,
        partition=config.PARTITION,
        host=system.hostname(),
        mesh_ip=mesh_ip,
        max_nodes=config.MAX_NODE_COUNT,
        tree_width=config.TREE_WIDTH,
        sockets=sockets,
        cores=cores,
        threads=threads,
        mem=system.real_memory_mb(),
        state=config.SLURM_STATE_DIR,
        spool=config.SLURMD_SPOOL_DIR,
        logdir=config.SLURM_LOG_DIR,
    )


def render_gres_conf() -> str:
    return GRES_CONF_TMPL.format(device=config.GPU_DEVICE)
