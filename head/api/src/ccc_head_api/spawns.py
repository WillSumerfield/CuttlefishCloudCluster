"""Server-side Spawn submission + inspection (Phase 3a slice 2).

This is the render + stage machinery that used to live in the CLI's `cuttle/spawns.py`. It moved here
when `cuttle run` stopped being Head-local: the CLI now tars its working dir and POSTs it, and the
Head API (running as the **Mantle**, the `ccc` account) unpacks it, renders the sbatch script, submits
it **as the submitter's Guise** over the munge socket (munged authenticates whichever local uid calls
it, so becoming the Guise is enough — no munge.key is ever read), and records an Attribution row. Everything a Spawn produces lands under HEAD_STATE_DIR/spawns/<id>/ and is read back
over the API — the submitter never touches the Head's disk.

Every Spawn runs as its submitter's **Guise** on EVERY node, under one uid pinned cluster-wide (see
guises.py). That is what makes a remote Arm work at all: slurmd setuid()s to the submitting uid, so
the account must exist, identically, wherever Slurm places the Spawn — which is what the Slurm prolog
provisions, lazily, the first time that User's work lands on a box. Nothing runs as the Mantle any
more, and a Spawn that does is a bug. A Guise is home-less by design, like the Mantle before it, so
nothing here is resolved against a home directory: the Pod image is at one absolute path on every node
(CCC_SIF) and a remote Arm scratches in /tmp.

Isolation (grill 2026-07-12c). A job NEVER runs in the tree the rsync daemon serves — that would
mutate the copy it serves and leave a dirty tree for the next run. The `[ -d "$staging_dir" ]`
branch is "are the files already on this node?":
  * Head's co-located Arm — the <uuid> upload is on this FS. COPY it into node-local scratch, run
    there, and write out/err/exit/outputs STRAIGHT to the Spawn dir (same FS → `cuttle logs -f`
    streams live).
  * remote Arm — the upload isn't here. rsync-stage the project in over this Spawn's own **Siphon**
    (siphons.py) into ephemeral /tmp scratch, run, then stage out/err/exit/outputs BACK through it.
Either way: fresh copy in, `$CCC_OUT` out, source untouched.

Paths are fixed (defaults below match cuttle.config; this package never imports the CLI). Only the
Head's mesh IP is dynamic — the systemd unit injects it as CCC_MESH_IP so a remote Arm can rsync home.
"""

import io
import logging
import os
import re
import subprocess
import tarfile
from pathlib import Path
from uuid import uuid4

from . import db
from . import environments
from . import guises
from . import recovery
from . import siphons

log = logging.getLogger("ccc.api")

# A declared output name becomes a shell-interpolated path segment (`mkdir -p "$ccc_out/<name>"` in
# JOB_SCRIPT) — anchored, no slashes, no leading dot, so neither `../../etc` nor a hidden `.` / `..`
# can walk it outside $ccc_out. This is the ONLY thing standing between a submitted name and a shell
# string, so it is checked here, not left to the client (the CLI validates too, but the Head never
# trusts that a client did).
OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

HEAD_STATE_DIR = Path(os.environ.get("CCC_HEAD_STATE", "/var/lib/ccc/head"))
PROJECTS_DIR = HEAD_STATE_DIR / "projects"
SPAWNS_DIR = HEAD_STATE_DIR / "spawns"
SCRATCH_DIR = HEAD_STATE_DIR / "scratch"
# The Pod image, at the SAME path on every node (each builds its own at setup). One constant, so the
# rendered script names the sif identically wherever Slurm places the Spawn.
CCC_SIF = Path(os.environ.get("CCC_SIF", "/opt/ccc/images/base.sif"))
MESH_IP = os.environ.get("CCC_MESH_IP", "")

# Per-User concurrent-Spawn cap (Phase 8 loose end — see docs/TODO.md's "No rate-limiting anywhere").
# Escrow already prices queue-flooding but never hard-stops it; this does. Admins are exempt — they are
# already outside the Karma economy (see submit_checked, db.spawn_owners).
MAX_CONCURRENT_SPAWNS = 10

# %%NAME%% placeholders (not str.format) so bash's own ${...} needs no escaping. Wrapper stdout/stderr
# (rsync diagnostics) go to a node-local slurm log; the PAYLOAD's stdout/stderr are redirected to files
# we then stage/keep. --chdir=/tmp: the run dir doesn't exist until we make it.
#
# The sif is the SAME path on every node, so it's named once, outside the branch. What actually differs
# between the Head and a remote Arm is only "are the project files already on this filesystem?".
JOB_SCRIPT = """\
#!/bin/bash
#SBATCH --job-name=ccc-spawn
#SBATCH --gres=gpu:%%GPU%%
#SBATCH --time=%%MINUTES%%
#SBATCH --chdir=/tmp
#SBATCH --output=/tmp/ccc-%j-%%SINK_TAG%%.slurm
#SBATCH --error=/tmp/ccc-%j-%%SINK_TAG%%.slurm
%%CONSTRAINT%%
%%NODELIST%%
%%EXCLUDE%%
# Arm Features (docs/TODO.md): an Arm optionally declares install paths for heavy/hardware-licence-
# gated software (VSim, IsaacSim, ...) at `cuttle setup --arm`, written locally to
# /etc/ccc/features.env as `export <NAME>_PATH=...` lines. Sourced unconditionally and guarded — a
# harmless no-op on any Arm/Spawn that never touches a feature — and `export`ed there (not here) so
# Apptainer's default env passthrough (no --cleanenv below) carries it straight into the Pod.
[ -f /etc/ccc/features.env ] && . /etc/ccc/features.env
# This Spawn's **Siphon** (temp/spawn-isolation.md Phase 2, ccc_head_api.siphons): the secret for the
# two rsync modules that exist only for this job. In the script body rather than the job environment
# because sudo scrubs the environment on the way to sbatch, and widening the sudoers `env_keep` for it
# would trade a bigger unattended grant for nothing — Slurm stores the job env exactly as it stores the
# script. Deliberately NOT in the apptainer --env list below, so it stops at the Pod wall: a Tide Pool's
# shell is a User's, and it has no business holding the credential the wrapper stages out with.
export RSYNC_PASSWORD=%%RSYNC_SECRET%%
jid=$SLURM_JOB_ID
staging_dir="%%STAGING_DIR%%"
sif="%%CCC_SIF%%"
# WSL's GPU, resolved on the NODE so the Head never has to know which of its Arms is WSL. Native Linux
# exposes /dev/nvidia*, which `--nv` binds itself; WSL2 exposes ONE node, /dev/dxg, plus a driver stack
# under /usr/lib/wsl that libcuda dlopens at runtime. Phase 1 cost us BOTH, for two different reasons:
#
#   * `--contain` cuts /dev from ~241 entries to 12 (measured), and --nv puts back only the nvidia*
#     nodes — it knows nothing about /dev/dxg.
#   * `--contain` also IGNORES apptainer.conf's `bind path` entries — "except for /etc/hosts and
#     /etc/localtime", says the config's own note — which is exactly how cuttle.setup's
#     configure_apptainer_wsl had been putting /usr/lib/wsl into the Pod.
#
# --nv still injects libdxcore.so (it is in nvliblist, found via ldconfig), so the failure looks like
# working GPU support right up until it doesn't: torch imports, reports its CUDA build, and then
# torch.cuda.is_available() is False. Both halves are needed; neither alone is enough.
#
# Unquoted on purpose below: empty must expand to no argument at all, not to an empty one.
dxg=""; [ -e /dev/dxg ] && dxg="--bind /dev/dxg,/usr/lib/wsl"
# sbatch's own stdout sink, on whichever node runs this. The wrapper redirects everything it cares
# about into out/err, so this stays EMPTY unless the shell itself failed before those existed — which
# is the one case worth keeping. Anything else would leave a 0-byte file per Spawn in /tmp forever.
#
# %%SINK_TAG%% (the staging uuid's first 8 chars) is what keeps this path unique, and it is load-bearing:
# a job id ALONE is not unique over time. `cuttle teardown` drops Slurm's state dir, so the next cluster
# starts issuing ids from 1 again while every previous cluster's sink survives in /tmp — and slurmd,
# running as this Spawn's **Guise**, cannot open a file a previous cluster's account owns. It does not
# fail the redirect and carry on: it kills the batch step before its first line, reporting "Could not
# open stdout file ... Permission denied" / "IO setup failed" in the node's slurmd log and rc 0xfb5 to
# slurmctld (which prints it as the meaningless "WTERMSIG 53"). From the Head that is a Spawn with no
# out, no err, no exit code and no explanation. Diagnosed on hardware 2026-08-02 (Spawns 9, 10, 12, 14
# drew ids a cluster from two days earlier had already used).
sink="/tmp/ccc-$jid-%%SINK_TAG%%.slurm"
# kill "$loop_pid": a no-op on the Head branch (never set there) and at the two remote-branch call
# sites that already kill it explicitly first for ordering (see the stage_out call sites below) — this
# is the fallback for any OTHER way the script ends (e.g. scancel's TERM), so the periodic out/err/
# outputs pusher (below) never outlives the script as an orphan.
#
# recover_scratch, defined per-branch below, runs BEFORE the rm -rf that used to be the only thing here
# (docs/TODO.md "Recovery Window") — it copies $proj home (Head-local: a same-filesystem `mv`; remote:
# an rsync) so a User whose --output was wrong or incomplete has a 30-minute window to get the rest of
# what the Spawn wrote back, instead of losing it the instant this trap fires. Unset/undefined is a safe
# no-op in bash (prints to stderr, does not abort the function), so this is harmless even if the script
# exited before either branch got far enough to define it.
#
# `rm -rf "$scratch" 2>/dev/null`: on the Head branch this ALWAYS fails on the directory itself. The
# prolog hands the Guise the scratch dir but not its ccc-owned parent, so the Guise can empty it and not
# unlink it; the epilog, which runs as root, is what actually removes it. Left un-silenced, that one
# "Permission denied" is written into $sink — which makes the sink non-empty, which defeats the "keep
# only if non-empty" rule below, which leaves a file in /tmp for every Spawn ever run. That litter is
# what poisoned later job ids (see the `sink=` line above); this is where the litter was made.
cleanup() { kill "$loop_pid" 2>/dev/null; recover_scratch; rm -rf "$scratch" 2>/dev/null; [ -s "$sink" ] || rm -f "$sink"; }

if [ -d "$staging_dir" ]; then
    # Head node: the upload is already on this FS. Never run in the served [project] tree — COPY it
    # into node-local scratch, run there, and write results STRAIGHT to the Head Spawn dir (same FS,
    # so `cuttle logs -f` streams live). The Spawn dir was pre-created by the API at submit.
    scratch="%%HEAD_SCRATCH%%/$jid"
    proj="$scratch/proj"
    trap cleanup EXIT
    mkdir -p "$proj"
    # --no-preserve=ownership is load-bearing, not tidiness: the upload is ccc:ccc and this now runs as
    # the submitter's **Guise**, so plain `cp -a` would try to chown the copy back to the Mantle, get
    # EPERM, and FAIL the whole copy (-a implies --preserve=all, which cp treats as required). Mode and
    # timestamps still come across; ownership is the one attribute a Guise has no business restoring.
    cp -a --no-preserve=ownership "$staging_dir/." "$proj/"
    spawn_dir="%%SPAWNS%%/$jid"
    # Stage-in is done, so the Head's copy of the upload has served its whole purpose (docs/TODO.md
    # "Storage retention"): this marker is what lets ccc_head_api.projects delete it, and it is written
    # only AFTER the cp above has returned — the same "presence means done" convention `exit` and
    # `recovery.size` follow. Nothing below ever reads $staging_dir again.
    : > "$spawn_dir/staged"
    out="$spawn_dir/out"; err="$spawn_dir/err"; exitf="$spawn_dir/exit"; ccc_out="$spawn_dir/outputs"
    stage_out() { :; }  # already on the Head
    recover_scratch() {
        # $proj already lives on the Head's own disk — no network hop, just relocate it under the Spawn
        # dir before cleanup()'s rm -rf reaches it. Same two gates as the remote branch: nothing to
        # recover, or over RECOVERY_MAX_BYTES (see ccc_head_api.recovery) — either way skip entirely,
        # no copy attempted, same as today.
        [ -n "$(ls -A "$proj" 2>/dev/null)" ] || return 0
        sz=$(du -sb "$proj" 2>/dev/null | cut -f1)
        [ -n "$sz" ] && [ "$sz" -le %%RECOVERY_MAX_BYTES%% ] || return 0
        mv "$proj" "$spawn_dir/recovery" && echo "$sz" > "$spawn_dir/recovery.size"
    }
else
    # remote Arm: none of these files are here. Stage the project in from the Head's rsync daemon into
    # ephemeral scratch, run, and stage out/err/exit/outputs back at the end.
    #
    # Scratch is /tmp, NOT the Guise's home: a Guise is home-less by design (one pinned uid per User,
    # cluster-wide, owning no per-node state — the account itself is `useradd -M`), so a donor box is
    # left with nothing after the trap fires. The Pod does NOT see this path as /tmp — `--contain` gives it a private, empty /tmp of its
    # own (that is the point: the host /tmp on a shared Arm holds every concurrent Spawn's scratch) — so
    # the two directories the payload actually needs, $proj and $ccc_out, are bound in by name.
    scratch="/tmp/ccc-$jid"
    proj="$scratch/proj"
    trap cleanup EXIT
    # $proj is created HERE, not left to the stage-in. rsync makes <uuid>/ as a side effect of copying
    # files into it, so a Spawn with an EMPTY project (every Tide Pool: it uploads an empty tar) would
    # otherwise leave the directory absent — and $proj is now an explicit `--bind` source and `--pwd`
    # target for both payloads, which apptainer treats as fatal when it does not exist. The Head branch
    # has always mkdir'd it; this makes the two branches agree.
    mkdir -p "$scratch/outputs" "$proj"
    # Named BEFORE the stage-in, so a stage-in that fails still has somewhere to say so and a way to
    # send it home. Nothing has run yet, so out/err start empty rather than absent — rsync would skip
    # a missing file and the Head would show no reason at all.
    out="$scratch/out"; err="$scratch/err"; exitf="$scratch/exit"; ccc_out="$scratch/outputs"
    : > "$out"; : > "$err"
    stage_out() {
        # out/err/outputs first, then exit LAST — its arrival is the Head's "done" marker. Bail before
        # sending exit if the logs did not land: a "done" marker with no logs beside it is worse than
        # no marker, because it claims the Spawn was accounted for. The destination spawns/<jid>/
        # already exists: the API pre-created it at submit, for every Spawn.
        rsync -a "$out" "$err" "$ccc_out" "rsync://%%TOKEN%%@%%HEAD_IP%%/out-%%TOKEN%%/" || return 1
        rsync -a "$exitf" "rsync://%%TOKEN%%@%%HEAD_IP%%/out-%%TOKEN%%/"
    }
    recover_scratch() {
        # $proj is the whole working dir the payload ran in — everything it wrote OUTSIDE the declared
        # $CCC_OUT subdirs (relative paths land here; see `cd "$proj"` below) is only ever here, and
        # cleanup()'s rm -rf destroys it the instant this script exits. rsync it home first, UNLESS it's
        # over RECOVERY_MAX_BYTES (docs/TODO.md "Recovery Window") — then skip entirely, no copy
        # attempted, same as today. The size marker (recovery.size) is written and sent home LAST, after
        # the transfer lands — its presence on the Head is the karma tick's "a copy is ready to
        # register" signal (ccc_head_api.recovery), since this trap can still be mid-rsync well after
        # stage_out() has already told the Head the Spawn is done.
        #
        # An EMPTY $proj returns before any of that. Every Tide Pool starts as one (it uploads an empty
        # tar) and a User who picked no project keeps it that way, so without this gate the common case
        # would register a ~4KB copy of nothing: `cuttle outputs` and the Dashboard would both offer a
        # Recovery Window whose download is empty. Nothing written means nothing to recover, and every
        # surface should agree there is no window rather than one holding an empty dir.
        [ -n "$(ls -A "$proj" 2>/dev/null)" ] || return 0
        sz=$(du -sb "$proj" 2>/dev/null | cut -f1)
        [ -n "$sz" ] && [ "$sz" -le %%RECOVERY_MAX_BYTES%% ] || return 0
        rsync -a "$proj/" "rsync://%%TOKEN%%@%%HEAD_IP%%/out-%%TOKEN%%/recovery/" 2>/dev/null || return 0
        echo "$sz" > "$scratch/recovery.size"
        rsync -a "$scratch/recovery.size" "rsync://%%TOKEN%%@%%HEAD_IP%%/out-%%TOKEN%%/" 2>/dev/null
    }
    # Live streaming (docs/TODO.md's now-closed "Live log streaming for remote Spawns"): a backgrounded
    # loop pushes out/err home every 5s so `cuttle logs -f` tracks a remote run instead of staying blind
    # until stage_out() at the very end. rsync -a of a growing file needs no --append/--partial — it
    # just re-sends the file's current bytes, and the CLI's _drain() already tolerates a partial
    # trailing line. $CCC_OUT rides the same loop, but gated on a settle check: a checkpoint caught
    # mid-write would land corrupted, unlike append-only log text, so only files whose mtime is older
    # than the quiet window (10s = 2x the push interval, long enough a normal write has finished) go
    # out. `-printf '%P\\n'` gives paths already relative to $ccc_out, which is what the outputs/
    # --files-from below wants. Best-effort throughout: a failed tick just retries next one, and the
    # unconditional stage_out() at EXIT remains the real source of truth regardless.
    push_periodic() {
        while true; do
            sleep 5
            rsync -a "$out" "$err" "rsync://%%TOKEN%%@%%HEAD_IP%%/out-%%TOKEN%%/" 2>/dev/null || true
            find "$ccc_out" -type f -not -newermt "@$(( $(date +%s) - 10 ))" -printf '%P\\n' \
                > "$scratch/outputs_filelist" 2>/dev/null
            if [ -s "$scratch/outputs_filelist" ]; then
                rsync -a --files-from="$scratch/outputs_filelist" "$ccc_out/" \
                    "rsync://%%TOKEN%%@%%HEAD_IP%%/out-%%TOKEN%%/outputs/" 2>/dev/null || true
            fi
        done
    }
    push_periodic &
    loop_pid=$!
    # files-from paths are module-root-relative (<uuid>/...) and pulled from the MODULE ROOT — NOT a
    # subpath source, which makes rsyncd (use chroot = no) report every file "vanished". rsync
    # recreates <uuid>/... under $scratch, so $proj resolves.
    cat > "$scratch/filelist" <<'CCC_FILELIST'
%%FILELIST%%
CCC_FILELIST
    # Stop here if the project never arrived. Without this the script walks on into `cd "$proj"` — a
    # directory rsync never created — and runs the payload against whatever /tmp happens to hold,
    # which surfaces as a confusing python error instead of "the files could not be fetched".
    if ! rsync -a --files-from="$scratch/filelist" \
            "rsync://%%TOKEN%%@%%HEAD_IP%%/proj-%%TOKEN%%/" "$proj/" 2>>"$err"; then
        echo "ccc: could not stage the project in from rsync://%%HEAD_IP%%/proj-%%TOKEN%%/ —" >> "$err"
        echo "ccc: this Spawn never started. Is the Head's rsync daemon up, and is this Arm on the mesh?" >> "$err"
        echo 1 > "$exitf"
        kill "$loop_pid" 2>/dev/null  # stop the periodic pusher first — it can't race the sync below
        stage_out
        exit 1
    fi
    # The project is on this node now, so the Head's copy has served its whole purpose (docs/TODO.md
    # "Storage retention"). Sent home LAST, after the stage-in rsync above returned — its presence on
    # the Head is ccc_head_api.projects' "safe to delete" signal, the same convention `exit` and
    # `recovery.size` follow. Best-effort: a marker that never lands only means the project waits for
    # the Spawn to END before being swept, which is the fallback that path is there for.
    : > "$scratch/staged"
    rsync -a "$scratch/staged" "rsync://%%TOKEN%%@%%HEAD_IP%%/out-%%TOKEN%%/" 2>/dev/null || true
fi

# Declared output subdirs, under the ONE $ccc_out root — made before the payload runs so the script can
# rely on them existing rather than having to mkdir its own. Names are validated server-side (no slashes,
# no leading dot) before they ever reach this template, so quoting the name is enough; there is no second
# root or env var per output — see docs/TODO.md's now-closed "Spawn Output Enforcement".
%%MKDIR_OUTPUTS%%

cd "$proj"
# The Spawn's **Environment** (ccc_head_api.environments), if its submitter declared one. The Head built
# it before this Spawn was ever placed, and it travelled INSIDE the project — so it needed no transfer of
# its own on either branch.
#
# Moved OUT of $proj before anything runs, and that is not tidiness: $proj is what recover_scratch()
# copies for the Recovery Window and measures against RECOVERY_MAX_BYTES, so an 8GB conda env left in
# place would silently disable recovery for exactly the Spawns most likely to need it.
#
# Bound at ONE absolute path, the same one it was built at, because neither shape survives being moved:
# a venv bakes its prefix into pyvenv.cfg and every console-script shebang, and a conda prefix bakes it
# into hundreds of files. The directory underneath differs on the Head and on an Arm; the path the env
# sees never does.
py="%%BASE_INTERPRETER%%"; envbind=""
if [ -d "$proj/%%ENV_DIRNAME%%" ]; then
    mv "$proj/%%ENV_DIRNAME%%" "$scratch/env"
    py="%%ENV_INTERPRETER%%"; envbind=",$scratch/env:%%ENV_MOUNT%%"
fi
# Build the Pod env list. WANDB_ENTITY is appended only when set — an empty entity makes wandb.init
# fail rather than fall back to the account default.
wenv="WANDB_API_KEY=${WANDB_API_KEY:-},WANDB_PROJECT=${WANDB_PROJECT:-},WANDB_MODE=${WANDB_MODE:-},WANDB_NAME=spawn-$jid"
[ -n "${WANDB_ENTITY:-}" ] && wenv="$wenv,WANDB_ENTITY=$WANDB_ENTITY"
# Under `--contain` (see the payloads below) apptainer auto-mounts NOTHING — not $HOME, not /tmp, not
# the cwd — so every directory the payload needs is bound by name: $proj (the code, and the working dir)
# and $ccc_out (where results are written and staged home from).
%%PAYLOAD%%
rc=$?
echo $rc > "$exitf"
kill "$loop_pid" 2>/dev/null  # stop the periodic pusher (remote branch only — unset is a no-op here)
                              # first, so it can't race the full sync below
# A failed stage-out is indistinguishable, from the Head, from an Arm that dropped mid-Spawn: no exit
# file ever lands, so the Spawn reads as "killed". Say so in the node-local slurm log, which is the
# only place left that can still be written.
stage_out || echo "ccc: staging results back to rsync://%%HEAD_IP%%/out-%%TOKEN%%/ FAILED —" \
                  "the Spawn ran (rc=$rc) but the Head will report it as lost." >&2
exit $rc
"""

# The two shapes `%%PAYLOAD%%` can take. Everything around it — staging, the EXIT trap, stage_out, the
# periodic $CCC_OUT pusher — is identical either way, which is the whole point of the slot: a Tide Pool
# (docs/TODO.md) is a normal Spawn that happens to run a live shell instead of a script, not a second
# pipeline.
#
# `--contain --pid --pwd "$proj"` is the containment set (verified on hardware 2026-07-31, apptainer
# 1.5.3), and it is what makes a Pod a boundary rather than a packaging format. Apptainer's own model is
# "the user inside is the user outside" — and outside used to be the Mantle, which owns head.db,
# head.key, and every User's upload. Without these flags a payload reached all of it through
# `/proc/<pid>/root` of any ccc-owned host process. A **Guise** now moves those valuables out of the
# room entirely; these flags are still what locks the door, and neither alone is the whole answer:
#
# * `--contain` drops the automatic $HOME/tmp/cwd binds and gives the Pod a private, empty /tmp — so a
#   Spawn on a shared Arm can no longer read every concurrent Spawn's scratch. It also blocks the host
#   passwd/group bind on its own (measured: host 55 lines -> image 21), which is why `--no-mount` below
#   is now belt-and-braces rather than the load-bearing thing it used to be.
# * `--pid` puts the payload in its own PID namespace with its own procfs — the single flag that closes
#   `/proc/<pid>/root` and `/proc/<pid>/environ` (another Spawn's WANDB_API_KEY). Apptainer starts its
#   own `appinit` shim as PID 1, so there is no zombie-reaping problem and no tini to ship.
# * `--pwd "$proj"` is load-bearing, not cosmetic: `--contain` drops the cwd auto-bind this payload
#   used to rely on to find %%SCRIPT%%, so $proj is now bound EXPLICITLY and named as the working dir.
#   Without it cwd is $HOME and `python <script>` fails outright.
#
# LC_ALL/LANG: the base PyTorch image never ran locale-gen, so any inherited locale makes the payload's
# own shell/tooling write a "cannot change locale" warning into the Spawn's `err` before it has done
# anything. C.UTF-8 ships pre-generated on every Debian/Ubuntu image.
#
# `$py` is the image's own `python` unless this Spawn's submitter declared an **Environment**, in which
# case it is that env's interpreter and `$envbind` mounts it — both set above, together, so the
# interpreter named here can never be one the bind did not provide.
PAYLOAD_SCRIPT = """\
apptainer exec --nv --contain --pid --pwd "$proj" --no-mount /etc/passwd,/etc/group $dxg \\
  --bind "$proj,$ccc_out$envbind" --env "$wenv,LC_ALL=C.UTF-8,LANG=C.UTF-8,CCC_OUT=$ccc_out" \\
  "$sif" $py %%SCRIPT%% %%ARGS%% > "$out" 2> "$err\""""

# sshd inside the Pod, reachable at %%TIDEPOOL_PORT%% on the Arm's OWN network address (apptainer here
# runs with no --net — full host netns, same as the script path, because the Pod still needs W&B egress
# and the mesh to stage its results home) — the Head-side relay (ccc_head_api.tidepool/relay.py) is what
# makes that reachable from a client that is not a Mesh member.
#
# `--contain --pid --pwd "$proj"` applies here for exactly the reasons it applies to PAYLOAD_SCRIPT
# (see above) — and it matters MORE here, because this payload hands a User a live interactive shell
# rather than running a script they wrote. Verified end to end on hardware (2026-07-31) against the full
# boot path below: sshd, pubkey auth, `UsePAM=no` and an interactive pty all work under containment
# (`--contain`'s minimal /dev still provides devpts), /tmp is private and empty, and the PID namespace
# holds — `/proc/1/root` is the container root, not the host's.
#
# sshd never setuid()s: the "login" is to the SAME uid already running the Pod (the submitter's
# **Guise**, homeless-by-design on the host — see the module docstring), so there is no privilege drop
# to fail. The Arm-side account it needs is provisioned by the Slurm prolog before the job step, not by
# sshd. That still needs the connecting uid to resolve via getpwuid — and it does, but NOT under the
# name this once assumed.
#
# Found on real hardware (2026-07-30/31), in two parts:
#
# 1. The HOST's passwd/group must not land over the image's own, or every OTHER host account (not just
#    the invoking one's) leaks into the Pod. `--contain` now does this by itself (measured: host 55 lines ->
#    image 21); `--no-mount /etc/passwd,/etc/group` is kept below as an explicit statement of intent,
#    not because anything depends on it any more.
# 2. Neither flag is enough on its own: with --writable-tmpfs (needed below so sshd can write host keys
#    + authorized_keys), apptainer still patches /etc/passwd for the INVOKING uid in place — a direct
#    edit to the writable overlay, not a bind, so no mount-level flag touches it. It does not add a fake
#    line; it overwrites templates.BASE_DEF_TMPL's baked `mantle:...:/home/mantle:...` entry, in place,
#    with the invoking uid's REAL host identity — measured again under a Guise (Probe 1b, 2026-07-31):
#    `ccc-u60002:x:60002:60002:CCC Guise:/home/ccc-u60002:/bin/bash`. So the home inside the Pod is the
#    Guise's, not `/home/mantle` and no longer `/home/ccc`, and the boot script below reads it back out
#    of getpwuid rather than naming it — it is per-User now, and the Head is the only side that knows
#    the uid. The CLI connects as that same account (tidepool.py, from `GET /spawns/{id}/tidepool`).
PAYLOAD_TIDEPOOL = """\
cat > "$scratch/tp_boot.sh" <<'CCC_TP_BOOT'
#!/bin/bash
set -e
home=$(getent passwd "$(id -u)" | cut -d: -f6)
mkdir -p /run/sshd "$home/.ssh"
cat > "$home/.ssh/authorized_keys" <<'CCC_TP_PUBKEY'
%%TIDEPOOL_PUBKEY%%
CCC_TP_PUBKEY
chmod 700 "$home/.ssh"
chmod 600 "$home/.ssh/authorized_keys"
ssh-keygen -A >/dev/null 2>&1
# apptainer runs unprivileged (a user namespace, not setuid-root), and its gid_map does not cover the
# container's `tty` group (gid 5) — so when sshd allocates a pty, glibc's grantpt() tries to chown it to
# that unmapped gid and fails EINVAL ("chown(/dev/pts/N, uid, 5) failed: Invalid argument"), and sshd
# aborts the session ("Attempt to write login records by non-root user"). Documented upstream
# (apptainer/apptainer#297) with no real fix, only this workaround: with no `tty` group to look up,
# grantpt() falls back to the caller's own (correctly-mapped) gid instead. Found on real hardware
# (2026-07-31). `sed -i` itself doesn't survive this overlay — it writes a temp file and rename()s over
# the original, which fails "Device or resource busy" here — so this truncates and rewrites in place
# instead, no rename involved.
grep -v '^tty:' /etc/group > /tmp/ccc_group.new
cat /tmp/ccc_group.new > /etc/group
rm -f /tmp/ccc_group.new
# A **Guise** is `--shell /usr/sbin/nologin` on the host, which is right there and wrong in here:
# apptainer patches the Pod's /etc/passwd with the invoking uid's REAL host entry — shell field and all
# (measured 2026-08-01, and already implied by Probe 1b) — so sshd execs nologin and the session dies
# with "This account is currently not available." before the User sees anything. The host account keeps
# nologin, which is correct (nothing should ever log into a Guise on a host, and nothing can — no
# password, no authorized_keys); a Pod is the one place an interactive shell is the entire point.
# Same truncate-in-place dance as the group file above, and for the same reason: no `sed -i` here.
sed "s|^\\($(id -un):.*\\):[^:]*$|\\1:/bin/bash|" /etc/passwd > /tmp/ccc_passwd.new
cat /tmp/ccc_passwd.new > /etc/passwd
rm -f /tmp/ccc_passwd.new
# So an interactive login shell (sshd starts one as `-bash`, sourcing /etc/profile.d) can find where to
# write results — the same $CCC_OUT the stage_out/EXIT-trap machinery rsyncs home either way, just not
# under a name the Head enforced (the "at least one --output" gate is skipped for a Tide Pool) — and
# lands in the optional project a User picked (CCC_PROJ, the same $proj a Script Spawn's `cd` already
# puts on the host side; an explicit `--bind "$proj"` is what makes it visible in here at all, since
# `--contain` drops the cwd auto-bind this used to lean on — see PAYLOAD_SCRIPT, which binds it the same
# way to find %%SCRIPT%%). An EMPTY dir when no project was picked, never a missing one (both branches
# mkdir it) — `cd` into an empty dir is a harmless no-op.
echo "export CCC_OUT=\\"$CCC_OUT\\"" > /etc/profile.d/ccc-tidepool.sh
echo "cd \\"$CCC_PROJ\\" 2>/dev/null || true" >> /etc/profile.d/ccc-tidepool.sh
# A Tide Pool gets the SAME **Environment** its Spawn would get, so what you debug is what runs — put it
# first on PATH rather than making the User type the interpreter's full path. Writable, too (it is this
# session's own node-local copy, so read-only would buy no safety): `pip install` at the shell works and
# dies with the session, which is most of what a Tide Pool is for.
[ -x %%ENV_MOUNT%%/bin/python ] && \\
  echo "export PATH=\\"%%ENV_MOUNT%%/bin:\\$PATH\\"" >> /etc/profile.d/ccc-tidepool.sh
# UsePAM no: `ccc` is a `useradd --system` account with no /etc/shadow entry inside the Pod (the image
# only bakes /etc/passwd + /etc/group — see BASE_DEF_TMPL), so PAM's account phase (pam_unix, checking a
# shadow entry that doesn't exist) refuses the session even after the ephemeral keypair already
# authenticated it — "Access denied ... by PAM account configuration". Found on real hardware (2026-07-31).
# There is no real system session here worth PAM's policy checks (password aging, nologin, securetty):
# the one and only auth boundary is the ephemeral key itself.
#
# SetEnv LC_ALL/LANG=C.UTF-8: sshd's default AcceptEnv forwards the connecting client's LANG/LC_* (most
# desktop ssh_configs SendEnv them), and the base PyTorch image never ran locale-gen for whatever locale
# a random User's machine happens to use — bash's very first line is then a "cannot change locale"
# warning. SetEnv overrides whatever AcceptEnv would otherwise apply (man sshd_config), forcing a locale
# every Debian/Ubuntu image ships pre-generated, without needing to know or match the User's own.
exec /usr/sbin/sshd -D -e -p %%TIDEPOOL_PORT%% -o UsePAM=no -o "SetEnv=LC_ALL=C.UTF-8 LANG=C.UTF-8"
CCC_TP_BOOT
chmod +x "$scratch/tp_boot.sh"
apptainer exec --nv --contain --pid --pwd "$proj" $dxg \\
  --writable-tmpfs --no-mount /etc/passwd,/etc/group \\
  --bind "$proj,$ccc_out,$scratch/tp_boot.sh:/ccc_tp_boot.sh$envbind" \\
  --env "$wenv,LC_ALL=C.UTF-8,LANG=C.UTF-8,CCC_OUT=$ccc_out,CCC_PROJ=$proj" \\
  "$sif" /ccc_tp_boot.sh > "$out" 2> "$err\""""


def _render(staging: Path, gpu: int, script: str, args: list[str],
            files: list[str], outputs: list[str], node: str | None, minutes: int, constraint: str,
            token: str, secret: str, exclude: str = "", tidepool_port: int | None = None,
            tidepool_pubkey: str | None = None) -> str:
    payload = PAYLOAD_TIDEPOOL if tidepool_port is not None else PAYLOAD_SCRIPT
    # %%PAYLOAD%% has to be substituted in FIRST — it is the only token whose replacement text can
    # itself contain other tokens (%%SCRIPT%%/%%ARGS%% or %%TIDEPOOL_PORT%%/%%TIDEPOOL_PUBKEY%%,
    # depending which payload this is), and every token below is a plain single-pass `.replace()`.
    # Doing it here rather than relying on dict order keeps that dependency from being invisible.
    body = JOB_SCRIPT.replace("%%PAYLOAD%%", payload)
    subs = {
        "%%GPU%%": str(gpu),
        # A bare integer is minutes to Slurm. Required of every Spawn: nothing can price a job whose
        # length nobody declared, and MaxTime=INFINITE means a runaway would otherwise squat forever.
        "%%MINUTES%%": str(minutes),
        # The OR-expansion of --vram over the fleet's real cards (slurm.vram_constraint).
        "%%CONSTRAINT%%": f"#SBATCH --constraint={constraint}" if constraint else "",
        # Pin the Spawn to one Arm, or leave scheduling to Slurm. ADMIN-ONLY (checked in main.submit):
        # Users declare what they NEED, and Slurm picks the fastest free card that qualifies. A blank
        # line when unset, so the #SBATCH block just ends early.
        "%%NODELIST%%": f"#SBATCH --nodelist={node}" if node else "",
        # Steer an unpinned Spawn away from every currently-retracted Arm (see retraction.py). Never set
        # alongside %%NODELIST%% — submit_checked refuses a pinned Spawn aimed at a retracted Arm
        # outright rather than silently excluding the one node the operator explicitly named.
        "%%EXCLUDE%%": f"#SBATCH --exclude={exclude}" if exclude else "",
        "%%STAGING_DIR%%": str(staging),
        # What makes the sbatch sink's path unique per SUBMISSION rather than per job id — see the
        # `sink=` line in JOB_SCRIPT for why that distinction is the difference between a Spawn running
        # and a Spawn dying before its first line. The staging uuid is already unique per submission and
        # is not a credential (`projects/<uuid>` is 0700 and its ACL names one Guise), so it needs no
        # separate nonce; 8 chars is far past enough to never collide inside one /tmp.
        "%%SINK_TAG%%": Path(staging).name[:8],
        # Recovery Window (docs/TODO.md): the per-Spawn cap `recover_scratch` gates on, both branches.
        "%%RECOVERY_MAX_BYTES%%": str(recovery.MAX_SPAWN_BYTES),
        "%%HEAD_SCRATCH%%": str(SCRATCH_DIR),
        # The Spawn's **Environment** (environments.py). Every one of these is a constant, not a
        # per-Spawn value: whether this Spawn HAS an env is decided by the rendered script at run time
        # (`[ -d "$proj/.ccc-env" ]`), not here — so one rendering serves both cases and a build that
        # produced nothing degrades to the base interpreter on its own.
        "%%ENV_DIRNAME%%": environments.ENV_DIRNAME,
        "%%ENV_MOUNT%%": environments.ENV_MOUNT,
        "%%ENV_INTERPRETER%%": environments.ENV_INTERPRETER,
        "%%BASE_INTERPRETER%%": environments.BASE_INTERPRETER,
        "%%CCC_SIF%%": str(CCC_SIF),
        "%%SPAWNS%%": str(SPAWNS_DIR),
        "%%HEAD_IP%%": MESH_IP,
        # This Spawn's Siphon (siphons.py). The token is both rsync module suffix and rsync username;
        # the secret is what `auth users` checks it against. Neither is the job id: the modules have to
        # exist before sbatch can return one.
        "%%TOKEN%%": token,
        "%%RSYNC_SECRET%%": secret,
        "%%FILELIST%%": "\n".join(files),
        "%%SCRIPT%%": script,
        "%%ARGS%%": " ".join(args),
        # One `mkdir -p` per declared name, all under the same $ccc_out root — validated (OUTPUT_NAME_RE)
        # before this is ever built, so quoting is enough. Empty only for a Tide Pool (docs/TODO.md): the
        # "at least one --output" gate is skipped for it alone, so this line is simply blank there.
        "%%MKDIR_OUTPUTS%%": "\n".join(f'mkdir -p "$ccc_out/{name}"' for name in outputs),
        # Only ever non-empty when `payload` is PAYLOAD_TIDEPOOL, which is the only place either token
        # appears — an ordinary Spawn's rendered script never even contains the literal string.
        "%%TIDEPOOL_PORT%%": str(tidepool_port) if tidepool_port is not None else "",
        "%%TIDEPOOL_PUBKEY%%": tidepool_pubkey or "",
    }
    for k, v in subs.items():
        body = body.replace(k, v)
    return body


def _lend_project(staging: Path, guise: str) -> None:
    """Let one **Guise** read one uploaded project, and nothing else.

    The Head's co-located Arm copies the project out of `projects/<uuid>` as the Guise (JOB_SCRIPT's
    Head branch). That directory is `ccc:ccc 0700` and stays that way — this adds a POSIX ACL naming
    exactly one account, which the Mantle can do with **no privilege at all**, because it already owns
    the directory. `rX` is read plus traverse-if-directory; the 0711 on `projects/` above it is what
    lets the Guise reach this dir by name without being able to list its siblings.

    Done here rather than in the prolog, and that is not a style choice: the prolog would have to be
    TOLD the uuid, and the only channel for that (a file under `spawns/<jid>/`) cannot exist until
    sbatch has returned the job id — by which time the prolog may already have run. This happens before
    the job exists at all, so there is no race to lose.

    Best-effort: a Head whose filesystem has no ACL support, or no `setfacl`, still runs every REMOTE
    Spawn correctly (they stage in over rsync and never touch this tree). Refusing the submission
    outright would take the whole cluster down over a Head-local optimisation.
    """
    r = subprocess.run(["setfacl", "-R", "-m", f"u:{guise}:rX", str(staging)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("could not lend %s to %s (%s) — a Spawn placed on the Head's own Arm will not be "
                    "able to read its project", staging, guise, (r.stderr or "").strip())


def submit(user_id: int, script: str | None, gpu: int, args: list[str], wandb_env: dict[str, str],
           tar_path: Path, minutes: int, constraint: str, outputs: list[str], node: str | None = None,
           vram_gb: int = 0, hold: float | None = None, exclude: str = "",
           tidepool: bool = False, manifest: str | None = None) -> int:
    """Unpack an uploaded project, render + sbatch it **as the submitter's Guise**, and record the
    Attribution row + the Karma Escrow. Returns the Slurm job id (== the Spawn id). `node` pins the
    Spawn to one Arm.

    `hold` is the Escrow quoted by the caller, or **None** for a Spawn nobody is charged for (an
    Admin's — the Admin is outside the economy). It is written in the SAME transaction as the
    Attribution row: a Spawn that existed without its hold, even briefly, would be one its submitter's
    Share had not yet been charged for, which is precisely the window a queue-flooder wants.

    `tar_path` is a spooled file, not bytes: the caller streams the upload to disk so a large project
    never sits in this process's memory (see main._spool_upload). tarfile reads it incrementally. A
    Tide Pool (docs/TODO.md) still uploads one (an empty gzip-tar — every client sends the same wire
    shape) since there is no project to run; `script` rides along as None and the tar's member list is
    never checked against it.

    `outputs` names the subdirs made under the one `$CCC_OUT` root before the payload runs (docs/TODO.md's
    now-closed "Spawn Output Enforcement") — checked here, not just at the HTTP boundary, so the
    Head-local pinned path (`cuttle run --node`) gets the identical guarantee. Both checks below (outputs
    non-empty, script present in the tar) are hard validation for an ORDINARY Spawn: a Spawn that failed
    either would either write nowhere retrievable or never start at all, so neither is worth a Spawn's
    Escrow and a wasted place in the queue. `tidepool=True` skips both — a live shell has no script to
    find, and its `$CCC_OUT` is where a User happens to point it, not a name the Head can enforce ahead
    of time (the CLI still warns, see cuttle/tidepool.py).
    """
    # Refuse rather than render `rsync://tok@/proj-tok/`. An empty mesh IP is a Head that does not know its
    # own address — harmless for a Head-local Spawn, fatal for one Slurm happens to place on a remote
    # Arm, and only discoverable minutes later, on another machine, as a Spawn that "vanished". We
    # cannot know where it will land, so no Spawn goes out at all.
    if not MESH_IP:
        raise ValueError("the Head does not know its own mesh address (CCC_MESH_IP is empty in the "
                         "ccc-head-api unit) — a remote Arm would have nowhere to fetch this Spawn's "
                         "files from. Re-run `cuttle setup --head` on the Head to rewrite the unit")
    if not outputs and not tidepool:
        raise ValueError("at least one output folder (--output/-o) is required — declare where your "
                         "script writes its results before submitting")
    for name in outputs:
        if not OUTPUT_NAME_RE.match(name):
            raise ValueError(f"invalid output name {name!r} — letters/digits/'.'/'_'/'-' only, may not "
                             f"start with '.' (no slashes, no '..')")

    uuid = uuid4().hex
    staging = PROJECTS_DIR / uuid
    # Checked against the tar's own member list, BEFORE anything is written to disk: a script that isn't
    # here would never start (or, worse, silently run some OTHER script if bash's glob ever changed
    # underneath us), and refusing before staging.mkdir() means a bad submission leaves nothing behind.
    # Skipped for a Tide Pool — `script` is None, and its (empty) tar names nothing to find.
    with tarfile.open(tar_path, mode="r:gz") as tf:
        if not tidepool and script not in tf.getnames():
            raise ValueError(f"script {script!r} was not found in the uploaded project — check the "
                             f"path matches exactly (and isn't excluded by .gitignore)")
        staging.mkdir(parents=True)
        tf.extractall(staging, filter="data")  # 'data' filter blocks path-traversal entries
    # The Guise is allocated HERE rather than just before sbatch, because the **Environment** build
    # below runs as it — a manifest is code, and CCC runs no User's code as the Mantle (CONTEXT.md,
    # **Guise**). `_lend_project` comes with it: the build enters a Pod with the project bound and this
    # ACL is what lets the Guise read it.
    with db.connect() as conn:
        guise = guises.name(db.allocate_guise(conn, user_id))
    _lend_project(staging, guise)

    # Built BEFORE `files` is computed, so the env is in the list a remote Arm stages in — it rides the
    # transfer that already exists rather than getting one of its own. Built before sbatch at all, so no
    # Device is ever held while packages download (see environments.py). A BuildError here is a
    # ValueError to the caller like every other refusal in this function: the Spawn does not exist yet,
    # nothing has been Escrowed, and nothing is left behind but the staging dir the caller sweeps.
    # A Tide Pool is included deliberately: you debug against the Environment your Spawn would get, or
    # you are not debugging the thing that failed. (Its tar is empty, so it declares nothing and this is
    # a no-op — unless a User picked a project for it, which is exactly when it matters.)
    declared = environments.detect(staging, manifest)
    if declared is not None:
        environments.build(staging, declared, guise)
    environments.prune_cache()

    # Module-root-relative list for a remote Arm's --files-from. This Spawn's Siphon is rooted at its
    # OWN projects/<uuid>, not at the shared parent, so the paths are relative to the upload itself —
    # that re-rooting IS the isolation, and it is why the list no longer carries a <uuid>/ prefix. The
    # upload was already gitignore-filtered client-side, so every file here is meant to transfer.
    #
    # Symlinks are listed too, and that is load-bearing for the Environment: a --system-site-packages
    # venv's `bin/python` points into the Pod image, which does not exist on the Head's own filesystem,
    # so `is_file()` alone reports it missing and the env would arrive on the Arm with no interpreter.
    # rsync -a copies a symlink AS a symlink, and inside the Pod on the Arm it resolves.
    files = [p.relative_to(staging).as_posix() for p in staging.rglob("*")
             if p.is_symlink() or p.is_file()]

    # Minted BEFORE the script is rendered — both are baked into it (see PAYLOAD_TIDEPOOL) — and
    # reserved in the SAME connection the caller's Escrow quote already opened, so a port claim and the
    # Spawn it belongs to are decided in one place. See ccc_head_api.tidepool's module docstring for why
    # the port has to be reserved ahead of a job_id that does not exist yet.
    tp_port = tp_pubkey = tp_privkey = None
    if tidepool:
        from . import tidepool as tidepool_mod
        with db.connect() as conn:
            tp_port, tp_pubkey, tp_privkey = tidepool_mod.grant(conn)

    # Opened BEFORE the render that bakes it in, and before sbatch — the Spawn stages in within
    # milliseconds of starting, and sbatch returning the id is the same instant it becomes runnable, so
    # a module named for the id could not exist in time. See ccc_head_api.siphons.
    token, secret = siphons.open(uuid)
    body = _render(staging, gpu, script or "", args, files, outputs, node, minutes, constraint,
                   token, secret, exclude, tidepool_port=tp_port, tidepool_pubkey=tp_pubkey)
    # sbatch propagates its own environment to the job, which is how WANDB_* reaches the wrapper's
    # `wenv` line. Already composed by submit_checked (compose_wandb_env) — the submitting User's
    # stored key, never the client's. sudo scrubs the environment, so the four WANDB_* names are in
    # `env_keep` on the sudoers rule (cuttle.headapi) — without that every Spawn would silently log
    # nothing, which a User only discovers afterwards in an empty W&B project.
    env = {**os.environ, **wandb_env}
    # The script goes in on STDIN, not as a path. It used to be a 0600 NamedTemporaryFile owned by the
    # Mantle, which the Guise sbatch now runs as cannot read — and feeding it here is strictly better
    # anyway: there is no temp file to leak the project's full file list, and the delete-after-return
    # dance goes with it.
    try:
        # --no-requeue: a Spawn runs ONCE (CONTEXT.md). `JobRequeue` is unset in the conf template so
        # Slurm's default (1) would otherwise apply, and a requeued job re-runs this script from the
        # top — re-staging a project stage-in has already deleted (docs/TODO.md "Storage retention",
        # ccc_head_api.projects). It also makes explicit what retraction already assumed: a Spawn whose
        # node failed is a failed Spawn, not one that quietly restarts somewhere else hours later,
        # having been re-billed for and re-queued behind everyone who was waiting.
        r = subprocess.run(guises.as_owner(["sbatch", "--parsable", "--no-requeue"], guise), input=body,
                           capture_output=True, text=True, check=True, env=env)
    except Exception:
        siphons.discard(token)  # no job id will ever exist for it, so nothing else would ever close it
        raise
    job_id = int(r.stdout.strip())
    # Create the Spawn dir for EVERY Spawn, not just Head-local ones: a remote Arm stages its results
    # into its Siphon's [out-…] module, which is rooted HERE, and rsync will not invent the directory a
    # module points at. Safe to do after sbatch — the job cannot start before sbatch has returned its id.
    (SPAWNS_DIR / str(job_id) / "outputs").mkdir(parents=True, exist_ok=True)
    # Only now can the out module name a path. The proj half has been live since before sbatch; this
    # adds the other and renames the pair to the job id, which is how the tick finds it to close.
    siphons.bind(token, job_id)
    with db.connect() as conn:
        db.record_spawn(conn, job_id, user_id, script or "", uuid, minutes=minutes, gpus=gpu,
                        vram_gb=vram_gb, node=node, hold=hold, tidepool=tidepool)
        if tidepool:
            db.create_tidepool(conn, job_id, tp_port, tp_pubkey, tp_privkey)
    return job_id


def compose_wandb_env(user_id: int, client_env: dict[str, str], wandb: bool = True) -> dict[str, str]:
    """The Spawn's W&B environment: **the Head owns the key, the client owns the run.**

    A client used to send the whole thing, which is why the Dashboard had no metrics at all — the
    browser has no key to send, so it sent `WANDB_MODE=disabled` on every Spawn. The key is now the
    submitting User's, held here (db.get_wandb_key), and only the run's *identity* still comes from the
    client: WANDB_PROJECT and WANDB_ENTITY, which are `cuttle run --project/--entity`.

    Anything else the client sent is dropped, `WANDB_API_KEY` and `WANDB_MODE` above all. Honouring a
    client-sent key would let a submitter log another User's Spawns into an account of their choosing,
    and honouring a client-sent mode would let one silently disable the metrics they had just asked the
    Head to enable. Neither is the client's to decide any more, so neither is read.

    No stored key means no W&B, and that has to be said out loud: an EMPTY env leaves WANDB_MODE unset,
    which makes `wandb.init()` fail rather than skip. WANDB_ENTITY stays absent when there is none — an
    empty entity breaks `wandb.init` too, instead of falling back to the account default (see the
    rendered job script's `wenv` line).

    `wandb` is the client's *intent* — "not this one", per Spawn — and it is a separate flag rather
    than a client-sent WANDB_MODE precisely so the rule above still holds: the client says whether to
    log, never how. False means disabled even with a key stored, and the key is not injected at all —
    there is no reason to hand a Pod a secret it was just told not to use. Absent is True, so every
    client that never heard of this keeps the behaviour it had.
    """
    if not wandb:
        return {"WANDB_MODE": "disabled"}
    env = {k: v for k, v in client_env.items() if k in ("WANDB_PROJECT", "WANDB_ENTITY") and v}
    with db.connect() as conn:
        key = db.get_wandb_key(conn, user_id)
    if key is None:
        return {"WANDB_MODE": "disabled"}
    env.setdefault("WANDB_PROJECT", "ccc")
    return {**env, "WANDB_API_KEY": key, "WANDB_MODE": "online"}


def submit_checked(user_id: int, is_admin: bool, script: str | None, gpu: int, args: list[str],
                   wandb_env: dict[str, str], tar_path: Path, minutes: int, vram_gb: int,
                   outputs: list[str], node: str | None = None, wandb: bool = True,
                   features: list[str] | None = None, tidepool: bool = False) -> int:
    """Validate placement, quote the Escrow, and submit — everything `/submit` does past auth.

    Extracted in Phase 6, when pinning a Spawn to an Arm (`node`) stopped being reachable over HTTP and
    became a Head-local command. Both callers come through here so a pinned Spawn is priced and
    recorded **exactly** like any other. The alternative — letting the Head-local path shell out to
    `sbatch` itself — would produce a Spawn with no Escrow and no Attribution row: one that consumes a
    donated card while nobody pays for it, and that no ledger can explain.

    `wandb_env` arrives as whatever the client asked for and is **recomposed here**, not taken as
    given — see compose_wandb_env. Here rather than in the route so the Head-local pinned path
    (`cuttle run --node`) gets the identical treatment, for the same reason the Escrow does. `wandb`
    is the per-Spawn off-switch that rides alongside it, and defaults on: a caller that never sets it
    submits exactly the Spawn it always did.

    `features` (docs/TODO.md "Arm Features") is optional and empty by default — most Spawns need none.
    When given, every name must be carried by at least one currently-admitted Arm or the Spawn is
    refused outright before ever reaching sbatch (mirrors `slurm.vram_constraint`'s "no Arm has that
    much VRAM" — hard validation, no partial/best-effort placement).

    `tidepool` (docs/TODO.md) rides straight through to `submit()`, which is the one place either the
    outputs-required gate or the script-in-tar check are actually skipped — placement, pricing and
    Attribution are otherwise completely unaffected, because a Tide Pool holds a Device exactly like any
    other Spawn.

    Raises ValueError for an unknown Arm, a retracted one, an unsatisfiable feature request, or a User
    already at the concurrent-Spawn cap; the callers translate (HTTP 422 / a CLI error).
    """
    # Imported here rather than at module scope: `karma` and `slurm` both reach for this module's
    # siblings, and a top-level import turns that into a cycle.
    from . import karma, slurm

    if not is_admin:
        # "Concurrent" = queued OR running (slurm.queue()'s live set) — a User can't dodge the cap by
        # parking Spawns PENDING instead of RUNNING. spawn_owners already excludes Admins, matching the
        # exemption here (see its docstring in db.py).
        live_ids = [j["id"] for j in slurm.queue() if j["id"] is not None]
        with db.connect() as conn:
            owners = db.spawn_owners(conn, live_ids)
        active = sum(1 for uid in owners.values() if uid == user_id)
        if active >= MAX_CONCURRENT_SPAWNS:
            raise ValueError(f"you already have {active} queued/running Spawns (max "
                             f"{MAX_CONCURRENT_SPAWNS}) — wait for one to finish or cancel one")

    arms = slurm.arms()
    if node and node not in [a["name"] for a in arms]:
        raise ValueError(f"unknown Arm '{node}' — known Arms: "
                         f"{', '.join(a['name'] for a in arms) or '(none)'}")
    with db.connect() as conn:
        retracted = db.retracted_nodes(conn)
    if node and node in retracted:
        raise ValueError(f"Arm '{node}' is retracted — its owner has paused it, so it takes no new "
                         f"Spawns until they return it")
    # Unpinned only: a pinned Spawn already named the one node it may run on above, and excluding a
    # retracted node it wasn't headed for anyway would be a no-op.
    exclude = ",".join(sorted(retracted)) if not node else ""
    constraint = slurm.vram_constraint(vram_gb)
    features = features or []
    if features:
        wanted = sorted(set(features))
        if not slurm.arms_with_features(wanted):
            known = ", ".join(slurm.all_feature_names()) or "(none)"
            raise ValueError(f"no Arm has every requested feature ({', '.join(wanted)}) — Arm "
                             f"Features currently in the cluster: {known}")
        # AND onto the vram OR-expansion — parenthesized so `(vram24|vram48)&vsim` reads unambiguously
        # rather than relying on Slurm's own operator precedence.
        constraint = f"({constraint})&{slurm.feature_constraint(wanted)}" if constraint \
            else slurm.feature_constraint(wanted)
    need = {"node": node, "vram_gb": vram_gb, "gpus": gpu}
    with db.connect() as conn:
        hold = karma.quote_hold(conn, arms, need, minutes, gpu, is_admin)
    return submit(user_id, script, gpu, args, compose_wandb_env(user_id, wandb_env, wandb), tar_path,
                  minutes, constraint, outputs, node, vram_gb=vram_gb, hold=hold, exclude=exclude,
                  tidepool=tidepool)


# --- reads (anonymous in slice 2) -------------------------------------------

def _spawn_dir(job_id: int) -> Path:
    return SPAWNS_DIR / str(job_id)


def active_state(job_id: int) -> str | None:
    """Slurm state if the job is still known to squeue, else None."""
    r = subprocess.run(["squeue", "-h", "-j", str(job_id), "-o", "%T"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def exit_code(job_id: int) -> int | None:
    """The recorded exit code once the Spawn is done, else None. Its presence == 'done'."""
    f = _spawn_dir(job_id) / "exit"
    return int(f.read_text().strip()) if f.exists() else None


def mark_cancelled(job_id: int) -> None:
    """Record that WE ended this Spawn, before scancel does it.

    A cancelled Spawn records no exit code — scancel's TERM kills the wrapper before it can write one
    — so it is otherwise indistinguishable from a Spawn whose Arm dropped off the mesh. Slurm cannot
    settle the question afterwards either: accounting storage is off (`AccountingStorageType = null`),
    so there is no `sacct` to ask once the job leaves squeue. The one moment the answer is known for
    certain is right here, so it is written down rather than inferred later.
    """
    d = _spawn_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cancelled").touch()


def was_cancelled(job_id: int) -> bool:
    return (_spawn_dir(job_id) / "cancelled").exists()


def out_text(job_id: int) -> str | None:
    f = _spawn_dir(job_id) / "out"
    return f.read_text() if f.exists() else None


def err_text(job_id: int) -> str | None:
    """The Spawn's stderr — the traceback of a Spawn that died is here, not in `out`."""
    f = _spawn_dir(job_id) / "err"
    return f.read_text() if f.exists() else None


def outputs_tar(job_id: int) -> bytes | None:
    """gzip-tar of the Spawn's $CCC_OUT dir, for `cuttle outputs <id>`. None if it produced none.

    Emptiness — not existence — is the test: submit() pre-creates the dir for every Spawn, so its mere
    presence says nothing. An empty dir means "nothing written yet / nothing produced", not "ready".
    """
    outputs = _spawn_dir(job_id) / "outputs"
    if not any(p.is_file() for p in outputs.rglob("*")):
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(outputs, arcname="outputs")
    return buf.getvalue()
