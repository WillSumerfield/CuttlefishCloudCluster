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

# Distro slurm-wlm is built without NVML → AutoDetect=nvml is fatal. Map each node's device
# explicitly. Per-node lines are unavoidable once Arms disagree about the device path: native Linux
# has /dev/nvidia0, WSL2 has /dev/dxg. Served to Arms over configless, so the Head authors all of it.
GRES_CONF_LINE = "NodeName={node} Name=gpu File={device}\n"

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


def render_gres_conf(nodes: list[tuple[str, str]]) -> str:
    """`nodes` = [(node_name, gpu_device)], the Head plus every admitted Arm."""
    header = "# Rendered by `cuttle setup --head` / `cuttle arm admit`. Do not hand-edit.\n"
    return header + "".join(GRES_CONF_LINE.format(node=n, device=d) for n, d in nodes)


def head_gres_node() -> tuple[str, str]:
    return system.hostname(), config.GPU_DEVICE
