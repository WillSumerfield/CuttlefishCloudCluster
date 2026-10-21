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
# ascending order, so torch's device i is the i'th card of this node's gres.conf lines — the same
# index `gres_detail` reports at Settlement. The benchmark holds every card (--gres=gpu:N), so the
# allocation is the whole node and the two orders coincide exactly.
BENCH_PY = """\
import json, os, sys, time, torch

out = os.environ["CCC_BENCH_OUT"]

# `merge <gres_index>...` — fold the per-Device files into the ONE `power` file the Head polls for
# (enroll._benchmark_arm). The indices are passed in gres.conf order, so `powers` comes out in the order
# Settlement maps back: powers[n] is the Power of the card Slurm reports as IDX:n.
if sys.argv[1] == "merge":
    parts = [json.load(open("%s/power.%s" % (out, i))) for i in sys.argv[2:]]
    json.dump({{"powers": [p["power"] for p in parts],
               "vram_mb": parts[0]["vram_mb"], "gpu": parts[0]["gpu"]}},
              open(out + "/power", "w"))
    raise SystemExit

# `measure <torch_index> <gres_index>` — ONE Device, in its own process.
#
# One process per Device is not a style choice: a MIG instance is the only device its process can ever
# address, so a single process could not reach a second card on a MIG-mode Arm however it looped. The
# caller runs this once per allocated Device. `torch_index` is the index within THIS process's
# CUDA_VISIBLE_DEVICES — 0 when the caller narrowed that to one MIG UUID, the GRES index when it left
# Slurm's own value alone on a whole-card Arm.
dev, gres = int(sys.argv[2]), sys.argv[3]
assert torch.cuda.device_count() > dev, "CUDA not visible in Pod"
props = torch.cuda.get_device_properties(dev)   # under MIG this is the INSTANCE's memory, not the card's
with torch.cuda.device(dev):                    # scopes the allocator and synchronize() to this card
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
json.dump({{
    "power": round({iters} * 2 * {n}**3 / elapsed / 1e12, 2),   # fp32 TFLOP/s
    "vram_mb": props.total_memory // 1024**2,
    "gpu": props.name,
}}, open("%s/power.%s" % (out, gres), "w"))
"""

# Submitted as the **cluster's own Guise** (`sudo -u ccc-u60001 sbatch`), like every Spawn is submitted
# as somebody's — it must be, or slurmd on a remote Arm would setuid() to an operator uid that does not
# exist there. The uid is reserved (config.GUISE_CLUSTER_UID) and the prolog creates the account on the
# Arm being priced, exactly as it would for any User. --nodelist pins it to the node being
# priced, and --gres=gpu:N takes ALL of its cards at once so one job measures every Device. Results come home the way every remote Spawn's do: rsync to the Head's [spawns] module, which
# the Head then reads. That path is identical on the Head's own co-located Arm (it rsyncs to itself over
# the mesh), so there is no is-this-the-Head branch here.
# Resolving a card's MIG device, for the job scripts that must name one. Inserted verbatim (never
# `.format`ed), so it is written in ordinary shell rather than doubled braces.
#
# **Why the device MINOR is the input.** A job knows which cards it holds as Slurm GRES indices, and a
# GRES index is a position in gres.conf — which CCC writes from `/dev/nvidiaN` paths in numeric order
# (system.gpu_devices). nvidia-smi enumerates by PCI bus instead, and the two orders are not guaranteed
# to agree. On a box whose cards are identical — dex, two RTX PRO 6000s — a mismatch is *invisible*:
# every Spawn still runs, and Settlement simply bills one card's seconds at the other card's Power,
# forever, with nothing anywhere to notice.
#
# `/proc/driver/nvidia/gpus/<bdf>/information` is what makes this exact rather than assumed: it carries
# `Device Minor` and `GPU UUID` in the SAME file, so a minor resolves to a GPU UUID with no PCI bus-id
# reformatting in between (nvidia-smi prints `00000000:07:00.0` where /proc uses `0000:07:00.0`).
# `nvidia-smi -L` then lists each MIG device underneath the GPU carrying that UUID.
#
# Prints nothing at all for a card that is not in MIG mode, which is how every caller tells the two
# fleets apart without asking the Head anything.
MIG_RESOLVER_SH = """\
ccc_mig_uuid() {
    local minor="${1#/dev/nvidia}" gpu_uuid
    [ -d /proc/driver/nvidia/gpus ] || return 0
    gpu_uuid=$(awk -v m="$minor" '
        /^GPU UUID:/     { u = $NF }
        /^Device Minor:/ { if ($NF == m) { print u; exit } }
    ' /proc/driver/nvidia/gpus/*/information)
    [ -n "$gpu_uuid" ] || return 0
    # The first MIG device under this GPU is the whole card: one full-size instance is the resting state
    # the prolog guarantees. A Spawn that re-carved its own card mid-run is on its own here, and that is
    # the point — by then it is setting CUDA_VISIBLE_DEVICES for itself.
    nvidia-smi -L | awk -v g="$gpu_uuid" '
        /^GPU /      { u = $NF; gsub(/[()]/, "", u); cur = (u == g); next }
        cur && /MIG/ { m = $NF; gsub(/[()]/, "", m); print m; exit }
    '
}
"""

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
# CCC_BENCH_OUT is the benchmark's OWN channel. This is CCC's own script writing to CCC's own path,
# so it may name it whatever it likes.
#
{mig_resolver}
# This node's Devices in gres.conf order — the list a Slurm GRES index indexes into. `sort -V` so
# nvidia10 follows nvidia2, matching system.gpu_devices()'s numeric sort exactly rather than by luck.
mapfile -t devs < <(printf '%s\\n' /dev/nvidia[0-9]* | sort -V)
# The cards Slurm gave this job. Read rather than assumed, so the merge below emits Powers in gres.conf
# order whatever the allocation was — though this job always asks for ALL of them (--gres=gpu:N), which
# is also why a node-global GRES index and an index within the allocation are the same number here.
IFS=, read -ra held <<< "$CUDA_VISIBLE_DEVICES"
#
# ONE PROCESS PER DEVICE. A MIG instance is the only device its process can ever address, so the single
# process that used to loop over `torch.cuda.device_count()` could not price a second card on a MIG-mode
# Arm however it was written. The loop moved out here, where each card gets its own interpreter.
#
# /tmp is auto-mounted by Apptainer, so it needs no explicit --bind (unlike a Spawn's, which lives
# under /var/lib/ccc). The env is bound at the ONE path it was built at and cannot be moved from
# (config.BENCH_ENV_MOUNT), and its own interpreter is what runs — the image has none.
for g in "${{held[@]}}"; do
    uuid=$(ccc_mig_uuid "${{devs[$g]}}")
    # MIG: narrow this process to the one instance it may address, which is then its torch device 0.
    # Whole card: leave Slurm's own CUDA_VISIBLE_DEVICES exactly as it set it and index into it, which
    # is what this benchmark did before MIG was ever on this fleet.
    if [ -n "$uuid" ]; then cvd="$uuid"; tdev=0; else cvd="$CUDA_VISIBLE_DEVICES"; tdev="$g"; fi
    apptainer exec --nv --env CCC_BENCH_OUT="$out" --env CUDA_VISIBLE_DEVICES="$cvd" \\
      --bind "{bench_env_dir}:{env_mount}" \\
      {sif} {env_mount}/venv/bin/python "$out/bench.py" measure "$tdev" "$g"
done
# No --nv: the merge touches no card, it only folds the per-Device files into the one `power` file the
# Head has been polling for all along (enroll._benchmark_arm reads exactly that path).
apptainer exec --env CCC_BENCH_OUT="$out" --bind "{bench_env_dir}:{env_mount}" \\
  {sif} {env_mount}/venv/bin/python "$out/bench.py" merge "${{held[@]}}"
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


# Base Pod image: plain Ubuntu and the handful of things a Pod cannot bring with it. NO Python, no torch, no
# CUDA — every Spawn declares an **Environment** and that Environment carries its own interpreter and its
# own torch (ccc_head_api.environments), so anything supplied here would be shadowed by what the Spawn
# brought and paid for in durable disk on every donated box regardless. ~150 MB instead of 2.9 GB.
#
# `--nv` still injects the host's driver at exec, and PyPI's torch wheels bundle their own CUDA runtime,
# so removing the CUDA image costs a GPU Spawn nothing.
#
# * `git` — the binary every other git tool shells out to: `git clone`, `pip install git+https://`,
#   and uv's git dependencies all end up here. ~15MB.
# * `openssh-server` — sshd runs INSIDE the Pod, unprivileged, as the Mantle (see
#   the Tide Pool payload). It never setuid()s: the "login" is to the SAME uid already
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
#   declared, and `--contain` (the Pod's payload) hides the host's — a box with gcc in /usr/bin and
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
# * `zsh` plus the vendored oh-my-zsh tree and gitstatusd (config.ZSH_DATA_DIR) — the shell a **Tide
#   Pool** hands its User. Only the *tree* is here; the two files that configure it are rendered by the
#   Head at every session (ccc_head_api.zshrc), because a prompt is the part anybody actually iterates on
#   and this image can only be changed by a fleet-wide teardown.
# * `/etc/zsh/zprofile` gets one appended line, and it is the load-bearing half of the whole change.
#   The Tide Pool payload puts the `cd` into the project and the scratch TMPDIR in
#   `/etc/profile.d/ccc-tidepool.sh`, because sshd builds a fresh environment per session and
#   drops apptainer's `--env`. **zsh does not read /etc/profile.d.** Debian's zprofile sources
#   /etc/profile and Ubuntu's ships as comments only, so without this line every one of those is silently
#   lost — the session opens, the prompt looks right, and the shell is in the wrong place. That exact
#   bug was already found and fixed once on the bash path (spawns.py, 2026-08-07); this is the line that
#   keeps switching shells from re-introducing it.
# * `ncurses-term` — sshd forwards the connecting client's TERM, and `ncurses-base` carries 61 entries
#   which cover xterm/screen and not tmux-256color, kitty, alacritty or wezterm. A missing terminfo entry
#   renders as a mangled prompt, which reads to a User as "p10k is broken" rather than as a TERM problem.
# * `man-db`, the removal of dpkg's excludes file, and a reinstall of the core tools — the
#   `colored-man-pages` plugin is inert without a `man`, and an ubuntu image discards /usr/share/man/* on
#   unpack. Removing the excludes only helps packages installed *after* it, so the already-unpacked core
#   is reinstalled to get its pages back; otherwise `man zsh` works and `man ls` does not, which is a
#   worse state than having no man at all.
# * `nano` and `neovim` — the editors. A **Tide Pool** is a shell a person sits in, and until now the
#   only way to change a line of code in one was to edit it somewhere else and re-upload. Two, not one,
#   and not a choice offered at submit: which editor a person reaches for is a fifty-year-old argument
#   and neither costs enough to be worth having it. Between them they are ~20 MB against build-essential's
#   250, so the trade is not close.
# * `curl` — the image has neither curl nor wget, and VS Code's Remote-SSH downloads its server into the
#   Pod over one of them. Nothing else in CCC needs it: every transfer CCC itself makes is rsync over the
#   Mesh.
# * `/usr/local/bin/{apt,apt-get,sudo}` — the shims that make `apt install` work for a Guise. See
#   APT_SHIM below, which is where the reasoning lives.
# * a baked-in `mantle` passwd/group/home entry, at exactly CCC_UID/CCC_GID — the ONE thing unprivileged
#   sshd still needs that a stock image does not have: the connecting uid has to resolve via
#   getpwuid, or sshd cannot find a home dir for authorized_keys or set up a session at all. Written
#   directly rather than via useradd/groupadd (not guaranteed present in every base image) — three text
#   appends and a directory, nothing that can fail for a missing tool.
# What `apt` means inside a Pod. Baked into /usr/local/bin by the %post block below, ahead of
# /usr/bin on PATH, so a User types the command they already know and it works.
#
# **Why it cannot just be `apt`.** A Pod runs as an unprivileged **Guise** and `dpkg` refuses outright:
# "requested operation requires superuser privilege". Two separate things are missing, and only one of
# them is solved by the writable overlay the payload now carries (ccc_head_api.spawns): the overlay
# gives `/usr` and `/var/lib/dpkg` somewhere to write, but dpkg also `chown`s every unpacked file to
# root:root, and that is a question about identity rather than about permissions.
#
# **A nested user namespace is most of the answer.** `unshare -r` maps the Guise to a real uid 0 one
# level down, so `chown(0,0)` is not faked but genuinely permitted — and it wraps only this command, so
# nothing touches the Pod's own process tree. That is what makes apptainer's `--fakeroot` unnecessary
# as well: a Tide Pool's sshd is PID 1 in its Pod, and wrapping THAT would fake the privilege
# separation and per-session setuid() it actually performs.
#
# **`unshare -r` maps ONE id pair, and that is the rest of the story.** A package shipping a file owned
# by any group other than root — `utmp` (tmux, screen), `crontab`, `ssl-cert`, `messagebus` — makes dpkg
# call `chown(0, 43)` for a gid that does not exist in that namespace, and the kernel answers EINVAL
# rather than EPERM. dpkg treats it as fatal: `tmux` unpacked, `libutempter0` did not, and the install
# died half-done ("error setting ownership of './usr/lib/.../utempter': Invalid argument").
#
# `fakeroot` closes it, but ONLY with `FAKEROOTDONTTRYCHOWN=1`, and the distinction is the whole reason
# this line reads the way it does. libfakeroot intercepts `chown` and then still issues the real syscall
# underneath, swallowing the failure only when it is EPERM — so plain `fakeroot` passes EINVAL straight
# through and changes nothing. That variable stops it attempting the real call at all. Measured here
# (2026-08-18): `unshare -r fakeroot chown 0:43` fails EINVAL, the same command with the variable set
# succeeds. Ownership is then faked rather than applied, which costs nothing real: ns-uid 0 IS the
# Guise, so every file dpkg unpacks was already landing Guise-owned on the disk underneath.
#
# `APT::Sandbox::User=root` is not optional. apt drops to `_apt` to fetch, and a namespace holding one
# mapped uid has no such account — without it every download dies on "setgroups: Operation not
# permitted" long before dpkg is reached.
#
# The `update` is once per Pod, behind a marker on the bound disk scratch. BASE_DEF_TMPL ends with
# `rm -rf /var/lib/apt/lists/*` (an image ships no stale index), so a fresh Pod knows of no packages
# at all and the first install would otherwise fail with "unable to locate package" — which reads as
# a broken name rather than a missing index.
#
# `DEBIAN_FRONTEND=noninteractive` because there is no one to answer. A package that stops to ask
# (tzdata is the usual one) would hang the Spawn holding it until its wall clock ran out.
#
# Three lines are dropped from stderr, and only three — each measured on hardware, each printed on
# every single invocation, none of them meaning anything failed. `setpriv: setresuid failed` is
# man-db's trigger trying to drop to its own service account, and `chown to root:adm` is apt failing
# to give its own log away, both direct consequences of a namespace that maps ONE uid. `debconf:
# delaying package configuration` is the slim image not shipping apt-utils, which is deliberate.
# Leaving them in would teach Users to read past apt's stderr, and that is where the real failures
# appear too. Anything else apt says comes through untouched.
APT_SHIM = """\
#!/bin/sh
# Rendered into every Pod image by `cuttle setup`. Do not hand-edit.
export DEBIAN_FRONTEND=noninteractive
ccc_noise='setpriv: setresuid failed|chown to root:adm of file|^debconf: delaying package'
# stderr is filtered via a file rather than inline, so the exit status stays apt's own rather than
# grep's; stdout, where apt's progress and package list go, is never touched and stays live.
ccc_apt() {
    ccc_err=/ccc-tmp/.apt-stderr.$$
    ccc_ns="unshare -r env FAKEROOTDONTTRYCHOWN=1 fakeroot"
    $ccc_ns /usr/bin/apt-get -o APT::Sandbox::User=root "$@" 2>"$ccc_err"
    ccc_rc=$?
    grep -Ev "$ccc_noise" "$ccc_err" >&2
    rm -f "$ccc_err"
    return $ccc_rc
}
[ -f /ccc-tmp/.apt-updated ] || { ccc_apt update && : > /ccc-tmp/.apt-updated; }
ccc_apt "$@"
"""


BASE_DEF_TMPL = """\
Bootstrap: docker
From: {image}

%files
    {zsh_src}/oh-my-zsh {omz}

%post
    # Runs inside the (disposable) build container only — never the host.
    #
    # The excludes file goes FIRST: it is what tells dpkg to throw away /usr/share/man/* as each package
    # unpacks, so it has to be gone before anything below is installed or their pages go with it.
    rm -f /etc/dpkg/dpkg.cfg.d/excludes
    apt-get update && apt-get install -y --no-install-recommends openssh-server ca-certificates \
      build-essential zsh zoxide ncurses-term man-db nano neovim curl fakeroot \
      && apt-get install -y --reinstall --no-install-recommends \
      coreutils bash grep sed findutils procps tar \
      && rm -rf /var/lib/apt/lists/*
    echo "mantle:x:{uid}:{gid}:CCC Mantle:/home/mantle:/bin/bash" >> /etc/passwd
    echo "mantle:x:{gid}:" >> /etc/group
    mkdir -p /home/mantle && chown {uid}:{gid} /home/mantle
    # The bridge. See the note above this template — without it a zsh login silently loses everything
    # the Tide Pool payload writes to /etc/profile.d.
    echo "emulate sh -c 'source /etc/profile'" >> /etc/zsh/zprofile
    # %files preserves the building operator's ownership and mode. Asserted here instead, so the tree is
    # identical whoever built the node: oh-my-zsh runs `compaudit` over its own directories at every
    # login and warns loudly about anything group-writable or owned by a third party, and a Pod's shell
    # is not the place to be explaining that.
    chown -R root:root {omz}
    chmod -R go-w {omz}
    chmod 0755 {omz}/custom/themes/powerlevel10k/gitstatus/usrbin/gitstatusd-linux-x86_64
    # The package-manager shims. /usr/local/bin precedes /usr/bin on every PATH a Pod ever has, so
    # these are what `apt` means in here without anybody being told.
    cat > /usr/local/bin/apt-get <<'CCC_APT'
{apt_shim}
CCC_APT
    cp /usr/local/bin/apt-get /usr/local/bin/apt
    # `sudo` grants nothing and pretends to grant nothing — a Pod has no privilege left to escalate
    # to. It exists because `sudo apt install x` is muscle memory, and without it that types out as
    # "sudo: command not found", which reads as a broken image rather than as an unnecessary word.
    cat > /usr/local/bin/sudo <<'CCC_SUDO'
#!/bin/sh
exec "$@"
CCC_SUDO
    chmod 0755 /usr/local/bin/apt-get /usr/local/bin/apt /usr/local/bin/sudo
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
                                uid=config.CCC_UID, gid=config.CCC_GID,
                                zsh_src=config.ZSH_DATA_DIR, omz=config.POD_OMZ_DIR,
                                apt_shim=APT_SHIM.rstrip("\n"))


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

# The GPU UUID of a device minor, out of the one file that carries both (see MIG_RESOLVER_SH for why
# the minor is what everything here is keyed by). Used as nvidia-smi's `-i`, so no index can drift.
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
    return EPILOG_TMPL.format(head_scratch=config.HEAD_SCRATCH_DIR,
                              head_spawns=config.HEAD_SPAWNS_DIR,
                              mantle=config.HEAD_API_USER,
                              guise_group=config.GUISE_GROUP,
                              mig_admin=MIG_ADMIN_SH)


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
        mig_resolver=MIG_RESOLVER_SH,
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
