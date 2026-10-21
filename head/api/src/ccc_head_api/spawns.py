"""Server-side Spawn submission + inspection.

The render + stage machinery for `cuttle run`: the CLI tars its working dir and POSTs it, and the Head
API (running as the **Mantle**, the `ccc` account) unpacks it, renders the sbatch script, submits it
**as the submitter's Guise** over the munge socket (munged authenticates whichever local uid calls it,
so becoming the Guise is enough — no munge.key is ever read), and records an Attribution row.
Everything a Spawn produces lands under HEAD_STATE_DIR/spawns/<id>/ and is read back over the API —
the submitter never touches the Head's disk.

Every Spawn runs as its submitter's **Guise** on EVERY node, under one uid pinned cluster-wide (see
guises.py). That is what makes a remote Arm work at all: slurmd setuid()s to the submitting uid, so
the account must exist, identically, wherever Slurm places the Spawn — which is what the Slurm prolog
provisions, lazily, the first time that User's work lands on a box. Nothing runs as the Mantle any
more, and a Spawn that does is a bug. A Guise is home-less by design, like the Mantle before it, so
nothing here is resolved against a home directory: the Pod image is at one absolute path on every node
(CCC_SIF) and a remote Arm scratches in /tmp.

Isolation: a job NEVER runs in the tree the rsync daemon serves — that would
mutate the copy it serves and leave a dirty tree for the next run. The `[ -d "$staging_dir" ]`
branch is "are the files already on this node?":
  * Head's co-located Arm — the <uuid> upload is on this FS. COPY it into node-local scratch, run
    there, and write out/err/exit/outputs STRAIGHT to the Spawn dir (same FS → `cuttle logs -f`
    streams live).
  * remote Arm — the upload isn't here. rsync-stage the project in over this Spawn's own **Siphon**
    (siphons.py) into ephemeral /tmp scratch, run, then stage out/err/exit/outputs BACK through it.
Either way: fresh copy in, the keep directory (`$ccc_out`) out, source untouched.

Nothing in the Pod is ever *told* where that keep directory is. A declared **Output** names a path the
payload already writes to — inside the project, or absolute in the Pod — and `$ccc_out/<path>` is mounted
onto it, so the results land in what is staged home without the submitter's code knowing CCC exists.
There is no `CCC_OUT` variable any more; see PAYLOAD_SCRIPT.

Paths are fixed (defaults below match cuttle.config; this package never imports the CLI). Only the
Head's mesh IP is dynamic — the systemd unit injects it as CCC_MESH_IP so a remote Arm can rsync home.
"""

import base64
import logging
import os
import posixpath
import re
import shlex
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from . import db
from . import environments
from . import features as features_mod
from . import guises
from . import siphons
from . import slurm
from . import substrate as substrate_mod
from . import tarstream
from . import zshrc

log = logging.getLogger("ccc.api")

# One segment of a declared **Output** path. Anchored, no leading dot, so neither a hidden directory nor
# `.` / `..` can appear — which is what stops a path walking out of the tree it is resolved against. Paths
# are split on `/` and every segment is matched, rather than matching the whole path in one go, because
# the interesting failures are all per-segment and the error should say which one.
#
# This is the ONLY thing standing between a submitted path and a shell string (JOB_SCRIPT interpolates it
# inside double quotes), so it is checked here and not left to the client — the CLI validates too, but the
# Head never trusts that a client did.
OUTPUT_SEG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class OutputPath:
    """One declared **Output**, resolved into the two paths the job script needs.

    An Output is named as a path in the submitter's own project (CONTEXT.md), so `runs/nn` is
    `$proj/runs/nn` — the directory their script already writes to, not a second place CCC invented and
    they would have had to teach their code about. A `/`-prefixed path is absolute *inside the Pod*
    instead, which is the only way to keep what a profiled Arm **Feature** writes: its working directory
    is sited at FEATURE_MOUNT-style paths of CCC's own, deliberately outside `$proj` (features.py), so
    nothing written there is reachable as a project path.

    `dest` is where the Pod sees it; `store` is where the bytes actually live, under the one `$ccc_out`
    root, and is what `cuttle outputs` tars. The two differ only in the leading `/`: the kept tree mirrors
    the path that was written to, so `-o /ccc/vlwd/logs` arrives as `outputs/ccc/vlwd/logs/`. That is also
    why `-o data/x` and `-o /data/x` cannot both be declared — see `output_plan`.
    """
    declared: str          # exactly what the submitter wrote, for error messages
    store: str             # relative to $ccc_out — the declared path with any leading '/' stripped
    absolute: bool

    @property
    def dest(self) -> str:
        """The bind destination, as a shell expression for JOB_SCRIPT (already inside double quotes)."""
        return self.declared if self.absolute else f"$proj/{self.store}"


def output_plan(outputs: list[str]) -> list[OutputPath]:
    """Validate declared **Output** paths and resolve each one. Raises ValueError naming what is wrong.

    Beyond the per-segment charset, three whole-list rules — all decidable here, at submit, and all of
    them things that would otherwise go wrong silently on the Arm:

    * **No duplicate `store`.** Two declarations that keep to the same place. This is what catches
      `-o data/x` together with `-o /data/x`: they are different directories in the Pod but one directory
      on the Head, so one would quietly overwrite the other in the tar.
    * **No nesting.** `-o runs` with `-o runs/nn` asks to bind `$ccc_out/runs/nn` onto a path that is
      *inside* the `$ccc_out/runs` bind — a mount over its own source. Refused rather than reasoned about.
    * **At least one segment.** `-o /` keeps the whole Pod.
    """
    plan: list[OutputPath] = []
    for declared in outputs:
        if not isinstance(declared, str):
            raise ValueError(f"an output must be a path, not {type(declared).__name__}")
        absolute = declared.startswith("/")
        # Trailing slashes only: an interior `//` leaves an empty segment, which is a typo worth naming
        # rather than silently collapsing.
        rel = declared.rstrip("/").lstrip("/") if absolute else declared.rstrip("/")
        segs = rel.split("/") if rel else []
        if not segs:
            raise ValueError(f"invalid output path {declared!r} — it names no folder")
        for seg in segs:
            if not OUTPUT_SEG_RE.match(seg):
                raise ValueError(
                    f"invalid output path {declared!r}: the part {seg!r} must be letters/digits/'.'/"
                    f"'_'/'-' and may not start with '.' (so no '..', and no hidden folders)")
        plan.append(OutputPath(declared=declared, store="/".join(segs), absolute=absolute))

    for i, a in enumerate(plan):
        for b in plan[i + 1:]:
            if a.store == b.store:
                raise ValueError(
                    f"outputs {a.declared!r} and {b.declared!r} would both be kept as {a.store!r} — "
                    f"declare one of them, or point one somewhere else")
            hi, lo = sorted((a, b), key=lambda p: len(p.store))
            if lo.store.startswith(hi.store + "/"):
                raise ValueError(
                    f"output {lo.declared!r} is inside output {hi.declared!r} — declare the outer folder "
                    f"on its own; everything under it is kept with it")
    return plan

# A Spawn's **Name** (CONTEXT.md) — deliberately NOT shaped like the two names above. Those are
# identifiers: they become path segments and are looked up by, so they
# are restricted to an identifier charset. A Name is a *label* — it names nothing the cluster resolves,
# so "lr sweep, 3e-4" is exactly as valid as "sweep3" and there is no reason to make a person spell
# their own words in kebab-case.
#
# What IS rejected is what would corrupt the surfaces it is drawn on: control characters. A Name is
# printed into an aligned CLI table (`cuttle.ui.table`), where a newline breaks the columns and an ANSI
# escape can repaint the terminal around it. The Dashboard needs no such guard — React escapes what it
# renders — but the Head validates for both clients rather than trusting either.
#
# 48 characters, measured after trimming. A queue row is scanned, not read.
MAX_SPAWN_NAME = 48


def clean_name(raw) -> str:
    """A submitted **Name**, trimmed and checked, or a ValueError naming what is wrong with it.

    Raises rather than falling back, because there is no acceptable fallback: a Name CCC chose would be
    a Name no person chose, which is the whole thing the rule exists to prevent.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("every Spawn needs a name — CCC does not invent one (--name)")
    name = raw.strip()
    if len(name) > MAX_SPAWN_NAME:
        raise ValueError(f"that name is {len(name)} characters; the limit is {MAX_SPAWN_NAME}")
    if any(ch < " " or ch == "\x7f" for ch in name):
        raise ValueError("a name cannot contain control characters or line breaks")
    return name


HEAD_STATE_DIR = Path(os.environ.get("CCC_HEAD_STATE", "/var/lib/ccc/head"))
PROJECTS_DIR = HEAD_STATE_DIR / "projects"
SPAWNS_DIR = HEAD_STATE_DIR / "spawns"
SCRATCH_DIR = HEAD_STATE_DIR / "scratch"
# The Pod image, at the SAME path on every node (each builds its own at setup). One constant, so the
# rendered script names the sif identically wherever Slurm places the Spawn.
CCC_SIF = Path(os.environ.get("CCC_SIF", "/opt/ccc/images/base.sif"))
# Where the payload's working directory ends up ON THE NODE, which is also where it is bound INTO the
# Pod (JOB_SCRIPT binds `$proj` by name, at the same path) — so this is the folder a VS Code remote
# session opens, and the folder `scp` puts files into. Two forms, because a Spawn's scratch differs
# between the Head's co-located Arm and a donor's box; both are written out longhand in JOB_SCRIPT, and
# `test_the_two_scratch_forms_agree` is what keeps this reading of them honest.
def pod_project_dir(job_id: int, head_arm: bool) -> str:
    scratch = f"{SCRATCH_DIR}/{job_id}" if head_arm else f"/tmp/ccc-{job_id}"
    return f"{scratch}/proj"


MESH_IP = os.environ.get("CCC_MESH_IP", "")

# How much a Spawn may write to its own Pod's SYSTEM — /usr, /etc, /var/lib/dpkg, everything outside a
# bind. Backs the sparse ext3 overlay every payload carries (see POD_OVERLAY in JOB_SCRIPT).
#
# A number at all, rather than "whatever the disk has", because this is the one writable surface a Pod
# gained that nothing else meters. A Spawn's declared **Output**s are charged to its submitter's
# Allowance and its scratch dies with the job, but `apt install` writes to a place no accounting
# reaches — so the cap is the accounting. Filesystem-enforced, which is what makes it honest: a runaway
# install hits ENOSPC inside its own Pod instead of filling the Arm's disk under every other Spawn
# running on it.
#
# 4GB because it is generous for what this is FOR — editors, a dev header, a CLI tool the Environment
# could not carry — and far short of a second Environment. A Spawn that needs more than 4GB of system
# packages is declaring an **Environment** badly, not using this feature. Sparse, so an overlay nobody
# writes to costs a few hundred KB of the Arm's disk and not 4GB.
POD_OVERLAY_MB = 4096

# Per-User concurrent-Spawn cap. Escrow already prices queue-flooding but never hard-stops it; this
# does. Admins are exempt — they are already outside the Karma economy (see submit_checked,
# db.spawn_owners).
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
# The Arm's **Licence** (temp/FEATURES_LICENCE.md): the directory its owner populated with the
# machine-locked activation state for whatever Feature software it offers. Bound at the Guise's home
# INSIDE the Pod, because that is the only place a licence manager looks: probed on hardware
# 2026-08-03, TurboActivate (vlearn) resolves its state directory through getpwuid(getuid())->pw_dir
# and ignores $HOME entirely, so no env var and no submitter-side code can redirect it.
#
# Read from passwd rather than $HOME for exactly that reason — this must be the path the Pod's own
# getpwuid returns, and apptainer injects this account's passwd entry into the container.
#
# Bound ONLY for a Spawn that requested a Feature — the Head decides that and renders the answer into
# the guard below, since only it knows what was asked for. The saved key is plaintext and world-
# readable, so a Spawn with no use for it never gets to read it. Writable, not read-only: the
# activation record is rewritten on periodic online re-validation.
guise_home=$(getent passwd "$(id -u)" | cut -d: -f6)
licbind=""
[ -n "%%WANTS_LICENCE%%" ] && [ -n "$guise_home" ] && [ -d "${CCC_LICENCE_HOME:-}" ] && licbind=",$CCC_LICENCE_HOME:$guise_home"
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
# This Spawn's card, when that card is in MIG mode. Resolved on the NODE: the Head renders one job
# script for a mixed fleet and has no business knowing which of its Arms carve.
#
# ⚠ DUPLICATED from cuttle.templates.MIG_RESOLVER_SH — head/api depends on nothing in cli/core (separate
# packages, separate venvs, and this one is served to Arms). Change one, change the other. See that
# constant for why a card is keyed by its device MINOR and not by nvidia-smi's enumeration order.
ccc_mig_uuid() {
    local minor="${1#/dev/nvidia}" gpu_uuid
    [ -d /proc/driver/nvidia/gpus ] || return 0
    gpu_uuid=$(awk -v m="$minor" '
        /^GPU UUID:/     { u = $NF }
        /^Device Minor:/ { if ($NF == m) { print u; exit } }
    ' /proc/driver/nvidia/gpus/*/information)
    [ -n "$gpu_uuid" ] || return 0
    nvidia-smi -L | awk -v g="$gpu_uuid" '
        /^GPU /      { u = $NF; gsub(/[()]/, "", u); cur = (u == g); next }
        cur && /MIG/ { m = $NF; gsub(/[()]/, "", m); print m; exit }
    '
}
# CUDA cannot name a MIG device by index — only a MIG-<uuid> reaches one — so a card in MIG mode with
# Slurm's own CUDA_VISIBLE_DEVICES left on it presents the Pod with NO GPU whatsoever. Not a slower one,
# none: `torch.cuda.device_count()` is 0 and every Spawn dies at its first `.cuda()`.
#
# The FIRST held card only, and deliberately. CUDA takes one MIG device per process however many are
# listed, so a multi-Device Spawn cannot be handed both — the second is reached by starting a second
# process, which finds it in `nvidia-smi -L` inside the Pod, where the UUIDs already are. CCC exports no
# list of its own: that is where a submitter would look anyway, and a CCC-only variable that exists on
# some Arms and not others is its own kind of trap.
#
# `--bind /dev/nvidia-caps`: `--contain` cuts /dev down and `--nv` restores the `nvidia*` nodes, but the
# capability nodes a MIG device is actually opened through live in a SUBDIRECTORY of it — without them
# the Pod holds a device it has no way to reach. The Arm's prolog is what makes them readable to this
# Guise; this only carries them across the Pod wall.
# Unquoted below on purpose: an empty expansion must add no argument at all.
migbind=""; migenv=""; ccc_uuid=""
mapfile -t ccc_devs < <(printf '%s\n' /dev/nvidia[0-9]* | sort -V)
IFS=, read -ra ccc_held <<< "${CUDA_VISIBLE_DEVICES:-}"
[ -n "${ccc_held[0]:-}" ] && ccc_uuid=$(ccc_mig_uuid "${ccc_devs[${ccc_held[0]}]}")
[ -n "$ccc_uuid" ] && migbind=",/dev/nvidia-caps" && migenv=",CUDA_VISIBLE_DEVICES=$ccc_uuid"
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
# `rm -rf "$scratch" 2>/dev/null`: on the Head branch this ALWAYS fails on the directory itself. The
# prolog hands the Guise the scratch dir but not its ccc-owned parent, so the Guise can empty it and not
# unlink it; the epilog, which runs as root, is what actually removes it. Left un-silenced, that one
# "Permission denied" is written into $sink — which makes the sink non-empty, which defeats the "keep
# only if non-empty" rule below, which leaves a file in /tmp for every Spawn ever run. That litter is
# what poisoned later job ids (see the `sink=` line above); this is where the litter was made.
cleanup() { kill "$loop_pid" 2>/dev/null; rm -rf "$scratch" 2>/dev/null; [ -s "$sink" ] || rm -f "$sink"; }

if [ -d "$staging_dir" ]; then
    # Head node: the upload is already on this FS. Never run in the served [project] tree — COPY it
    # into node-local scratch, run there, and write results STRAIGHT to the Head Spawn dir (same FS,
    # so `cuttle logs -f` streams live). The Spawn dir was pre-created by the API at submit.
    scratch="%%HEAD_SCRATCH%%/$jid"
    proj="$scratch/proj"
    trap cleanup EXIT
    mkdir -p "$proj"
    spawn_dir="%%SPAWNS%%/$jid"
    # Named BEFORE the copy, and so are the two functions below — all of it so the copy's failure branch
    # has somewhere to report and a trap that is fully armed when it fires. The remote branch names its
    # out/err ahead of its own stage-in for the same reason.
    out="$spawn_dir/out"; err="$spawn_dir/err"; exitf="$spawn_dir/exit"; ccc_out="$spawn_dir/outputs"
    stage_out() { :; }  # already on the Head
    # --no-preserve=ownership is load-bearing, not tidiness: the upload is ccc:ccc and this now runs as
    # the submitter's **Guise**, so plain `cp -a` would try to chown the copy back to the Mantle, get
    # EPERM, and FAIL the whole copy (-a implies --preserve=all, which cp treats as required). Mode and
    # timestamps still come across; ownership is the one attribute a Guise has no business restoring.
    #
    # CHECKED, and the check is the whole point of the ordering above. There is no `set -e` in this
    # script, so an unchecked cp falls straight through to the `staged` marker below — and that marker
    # is what authorises ccc_head_api.projects to delete the User's uploaded project. On ENOSPC that
    # deletes the original against a truncated copy, and the Spawn then runs incomplete code with no way
    # back. Same shape as the remote branch's stage-in check.
    if ! cp -a --no-preserve=ownership "$staging_dir/." "$proj/"; then
        echo "ccc: could not copy the project into node-local scratch at $proj —" >> "$err"
        echo "ccc: this Spawn never started. Your uploaded project is untouched. Is the Head out of disk?" >> "$err"
        echo 1 > "$exitf"
        # The upload itself survives: no `staged` marker is written below.
        rm -rf "$proj"
        exit 1
    fi
    # Stage-in is done, so the Head's copy of the upload has served its whole purpose (docs/TODO.md
    # "Storage retention"): this marker is what lets ccc_head_api.projects delete it, and it is written
    # only once the cp above has SUCCEEDED — the same "presence means done" convention `exit`
    # follows. Nothing below ever reads $staging_dir again.
    : > "$spawn_dir/staged"
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
    # Live streaming (docs/TODO.md's now-closed "Live log streaming for remote Spawns"): a backgrounded
    # loop pushes out/err home every 5s so `cuttle logs -f` tracks a remote run instead of staying blind
    # until stage_out() at the very end. rsync -a of a growing file needs no --append/--partial — it
    # just re-sends the file's current bytes, and the CLI's _drain() already tolerates a partial
    # trailing line. The keep directory rides the same loop, but gated on a settle check: a checkpoint caught
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
    #
    # -r is EXPLICIT and load-bearing. --files-from implies --dirs but NOT --recursive, and rsync(1)
    # says so outright: "The --archive (-a) option's behavior does not imply --recursive (-r), so
    # specify it explicitly, if you want it." Every ordinary entry in the list is a file, so this went
    # unnoticed — but the Environment is named as a bare DIRECTORY (`.ccc-env`, see submit), because it
    # does not exist yet when the list is written. Without -r it arrived as an empty directory, got
    # bound over %%ENV_MOUNT%%, and the Spawn died on `stat /opt/ccc/env/bin/python` after a build that
    # had succeeded. Found on hardware 2026-08-02 (Spawn 16); Head-local Spawns never saw it, that
    # branch is a `cp -a`.
    if ! rsync -a -r --files-from="$scratch/filelist" "rsync://%%TOKEN%%@%%HEAD_IP%%/proj-%%TOKEN%%/" "$proj/" 2>>"$err"; then
        echo "ccc: could not stage the project in from rsync://%%HEAD_IP%%/proj-%%TOKEN%%/ —" >> "$err"
        echo "ccc: this Spawn never started. Is the Head's rsync daemon up, and is this Arm on the mesh?" >> "$err"
        echo 1 > "$exitf"
        kill "$loop_pid" 2>/dev/null  # stop the periodic pusher first — it can't race the sync below
        stage_out
        exit 1
    fi
    # The project is on this node now, so the Head's copy has served its whole purpose (docs/TODO.md
    # "Storage retention"). Sent home LAST, after the stage-in rsync above returned — its presence on
    # the Head is ccc_head_api.projects' "safe to delete" signal, the same convention `exit`
    # follows. Best-effort: a marker that never lands only means the project waits for
    # the Spawn to END before being swept, which is the fallback that path is there for.
    : > "$scratch/staged"
    rsync -a "$scratch/staged" "rsync://%%TOKEN%%@%%HEAD_IP%%/out-%%TOKEN%%/" 2>/dev/null || true
fi

# The declared **Output**s. Each one names a path the payload ALREADY writes to — inside the project, or
# absolute in the Pod — and what happens here is that the kept directory is mounted *onto* that path, so
# an unmodified script writes its results straight into what gets staged home. Nothing is exported for it
# to find and there is no second root to write to: the folder it was already using IS the output.
#
# Three steps per output, and the order matters:
#   1. mkdir the KEEP side ($ccc_out/<store>), which is the bind source and what `cuttle outputs` tars.
#   2. SEED it from whatever the upload already had at that path, so declaring a folder an output never
#      hides from the payload what its submitter put there — `-o checkpoints` on a project that uploaded
#      a checkpoint to resume from must not silently present an empty directory and restart training.
#      Relative outputs only: nothing was ever uploaded to an absolute Pod path.
#   3. mkdir the bind DESTINATION, so apptainer has a mountpoint. Free for a relative output ($proj is
#      itself a bind, so the host directory is the mountpoint); an absolute one relies on apptainer
#      making it, which stock `enable overlay` does.
#
# `outbind` accumulates the `--bind` entries, appended to both payloads' bind lists. Empty for a Spawn
# that declared no outputs at all — every ordinary Spawn declares one, so that is a **Tide Pool**, the
# one that may decline.
outbind=""
%%OUTPUT_SETUP%%

cd "$proj"
# Scratch for whatever the payload runs, on DISK — the same fix `environments.py` makes for the BUILD
# Pod, applied to the Pod that runs the work. `--contain` gives a Pod a private /tmp carved from
# apptainer's session directory, a RAM tmpfs capped by `sessiondir max size` (64MB, apptainer's stock
# default — CCC does not set it), and that same 64MB also backs `--writable-tmpfs`. Nothing sets TMPDIR,
# so anything writing a large temp file lands there: pip unpacking a wheel, torch extracting a dataset,
# a script calling tempfile. It dies as ENOSPC, and pip in particular reports that as "This is an issue
# with network connectivity, not pip" — which sends the User debugging the mesh instead of the disk.
#
# The EXIT trap's `rm -rf "$scratch"` takes it.
mkdir -p "$scratch/tmp/pip" "$scratch/tmp/xdg" "$scratch/tmp/vscode"
# Where every tool that follows the XDG spec keeps its config, data, state and cache — pointed at the
# bound disk scratch, and NOT left to default to `$HOME/.config` and friends.
#
# Two separate problems, one answer. The Pod's home is a 64MB RAM tmpfs, so anything with a real cache
# (HuggingFace, matplotlib, a language server) fills it and dies as ENOSPC. And worse: a Spawn that
# requests an Arm **Feature** has the Arm's **Licence** directory bound OVER that home (see `licbind`),
# a `2770 ccc-licence:ccc-guises` tree that EVERY Guise on the box can read. So the moment a Pod ships
# editors, nvim's `shada` — search history, registers, the path of every file opened — and its undo
# files, which hold file CONTENTS, would land in a directory another User can read. That is a leak
# between Users, not untidiness, and redirecting the four variables closes it for every XDG-respecting
# tool at once rather than one tool at a time.
#
# The Tide Pool sets the same four again, in sshd's SetEnv (see PAYLOAD_TIDEPOOL): sshd builds a fresh
# environment per session and drops apptainer's `--env` entirely, so this line does not reach a shell.
XDG_ENV="XDG_CONFIG_HOME=/ccc-tmp/xdg/config,XDG_DATA_HOME=/ccc-tmp/xdg/data"
XDG_ENV="$XDG_ENV,XDG_STATE_HOME=/ccc-tmp/xdg/state,XDG_CACHE_HOME=/ccc-tmp/xdg/cache"
# The Pod's own writable system, and the reason `apt install` works inside a Spawn at all.
#
# A Pod's root is a read-only squashfs. `--writable-tmpfs` used to make it writable for a **Tide Pool**
# only, out of apptainer's session directory — a RAM tmpfs capped at 64MB, which is enough for the boot
# script's edits to /etc and nothing else. A sparse ext3 image instead: %%POD_OVERLAY_MB%%MB, on the
# same node-local disk as the scratch, and carried by EVERY payload rather than just the interactive
# one. The upper layer is owned by the Guise that created it, so a write to /usr or /var/lib/dpkg
# simply succeeds.
#
# It is created before the **Environment** build and handed to that Pod as well, so a declared
# **Substrate**'s packages are present while the manifest builds — a wheel that has to compile against
# a system library finds it, which is most of why declaring one is worth doing.
#
# Inside `$scratch`, so cleanup()'s `rm -rf` takes it with everything else and no Pod keeps what it
# held.
if ! apptainer overlay create --size %%POD_OVERLAY_MB%% "$scratch/overlay.img" >> "$err" 2>&1; then
    echo "ccc: could not create this Spawn's writable Pod layer at $scratch/overlay.img." >> "$err"
    echo "ccc: this Spawn never ran. Is this Arm's disk full?" >> "$err"
    echo 1 > "$exitf"; stage_out; exit 1
fi
overlay="--overlay $scratch/overlay.img"
# The Spawn's **Environment** (ccc_head_api.environments), if its submitter declared one — built HERE,
# now, out of this box's own bandwidth. The Head decided what to build and rendered the commands into
# the build block below; running them is all that happens on this side.
#
# It is built into $scratch, NOT into $proj. It dies with the
# Spawn — no box grows an env store — while the package DOWNLOADS survive in the node's shared cache,
# which is what makes the next build cheap.
#
# Bound at ONE absolute path, the same one it is built at, because neither shape survives being moved:
# a venv bakes its prefix into pyvenv.cfg, its own interpreter symlink and every console-script shebang,
# and a conda prefix bakes it into hundreds of files. Building and running on the same box in the same
# job makes that invariant trivially true.
#
# `envmount` carries the Pod-side path as a VARIABLE, empty when this Spawn declared no Environment,
# because an Arm **Feature**'s setup block below has to name it (its LD_LIBRARY_PATH includes the
# Environment's own lib dir) and those blocks arrive already substituted — see _render.
#
# `$py` starts EMPTY and there is no fallback, because there is nothing to fall back to: the Pod image
# carries no interpreter at all (cuttle.templates.BASE_DEF_TMPL). The build block is empty for exactly
# one kind of Spawn now that declaring is mandatory — a **Tide Pool**, which may decline — and it never uses
# `$py`: its payload boots a login daemon rather than an interpreter. So what it leaves behind is
# `$envbind`/`$envmount`, which correctly stay empty. An **Arm Feature**'s probe is the one other reader,
# and it fails loudly on an empty `$py` rather than silently (features.PREAMBLE).
#
# `featbind`/`featenv` are the Arm **Feature** runtime accumulators (ccc_head_api.features), appended to
# the payload's bind and env lists. Empty for almost every Spawn: only a Feature with a runtime *profile*
# renders anything into them. The build block seeds `featbind` with the Feature library prefix it built,
# read-only — nothing writes to it, and each profile's block below puts it on LD_LIBRARY_PATH.
py=""; envbind=""; envmount=""; featbind=""; featenv=""
%%ENV_BUILD%%
%%FEATURE_SETUP%%
# Under `--contain` (see the payloads below) apptainer auto-mounts NOTHING — not $HOME, not /tmp, not
# the cwd — so every directory the payload needs is bound by name: $proj (the code, and the working dir)
# and $ccc_out (where results are written and staged home from).
%%PAYLOAD%%
rc=$?
echo $rc > "$exitf"
kill "$loop_pid" 2>/dev/null  # stop the periodic pusher (remote branch only — unset is a no-op here)
                              # first, so it can't race the full sync below
# A failed stage-out is indistinguishable, from the Head, from an Arm that dropped mid-Spawn: no exit
# file ever lands, so the Spawn reads as "killed" — a Spawn that ran to completion, reported as a
# failure, with its results deleted an hour later as orphan debris.
#
# So RETRY, rather than accept that on the first refused connection. The payload is already done and the
# card is already released, so the only cost of waiting is this script's own tail; a transient drop (the
# Head restarting, a mesh hiccup, a saturated link) is the ordinary cause and it heals inside a minute.
# What this cannot fix is the Head's mesh IP having CHANGED under a running Spawn — %%HEAD_IP%% is baked
# into this script and there is nothing here to re-resolve it against. Rare, and it needs stable naming
# to fix properly.
#
# A no-op on the Head branch, where stage_out is `{ :; }` and the first call always succeeds.
stage_out_retry() {
    for backoff in 5 15 30; do
        stage_out && return 0
        sleep "$backoff"
    done
    stage_out
}
stage_out_retry || echo "ccc: staging results back to rsync://%%HEAD_IP%%/out-%%TOKEN%%/ FAILED after" \
                  "4 attempts — the Spawn ran (rc=$rc) but the Head will report it as lost." >&2
exit $rc
"""

# What fills `%%ENV_BUILD%%` for a Spawn that declared an **Environment**. Empty for the one that may
# not — a **Tide Pool** with no manifest — and that emptiness is the whole of the no-env case.
#
# The build runs in the SAME Pod shape as the payload (`--contain --pid --pwd "$proj"`, base.sif, this
# job's own Guise). A manifest is code — build backends, sdist `setup.py`, conda post-link scripts all
# execute — so it gets the same walls the work gets, not a weaker isolation invented for it. There is no
# `--nv`: a build needs no GPU. The card is already allocated to this job either way, which is the one
# real cost of building here rather than on the Head, and it is what buys every Spawn the ability to
# start at all (environments.py's module docstring).
#
# Output goes to `$err`, not to a separate log: it IS the answer to "why did my Spawn get that version"
# on success and to "why did it not run" on failure, and one place to look beats two. `cuttle logs`
# already streams it, live, on both branches.
ENV_BUILD = """\
mkdir -p "$scratch/env" "$scratch/tmp"%%FEATLIB_MKDIR%%
# THIS Guise's package download cache, under the 0777 parent `cuttle setup` made
# (cuttle.config.NODE_ENV_CACHE_DIR). One subtree per Guise, created here by the Guise that owns it,
# because a single shared cache fails twice over: uv builds its tree with the CREATOR's ownership and
# 0755 umask, so the first uid to build locks every later one out (hardware, 2026-08-08) — and a cache
# two Users can both write is a route for one to plant a wheel the other's build installs.
#
# `id -un` rather than a rendered name: the job already RUNS as the Guise, so this cannot disagree with
# the uid doing the writing. mkdir is unconditional and -p, so a first build and a warm one are the
# same line.
ccc_cache="%%ENV_CACHE_ROOT%%/$(id -un)"
mkdir -p "$ccc_cache"
# Emptied WHOLE at the cap rather than evicted per entry: uv and micromamba both hard-link out of it,
# so evicting one entry needs their own bookkeeping to be safe. Costs re-downloads and nothing else —
# no Spawn depends on it. Checked BEFORE the build, so this Spawn's own downloads are never the ones
# thrown away, and only ever this Guise's own subtree, so no User can cost another their cache.
csz=$(du -sb "$ccc_cache" 2>/dev/null | cut -f1)
if [ -n "$csz" ] && [ "$csz" -gt %%ENV_CACHE_MAX%% ]; then
    echo "ccc: your package cache on this node passed %%ENV_CACHE_MAX_GB%%GB — emptying it" >> "$err"
    rm -rf "$ccc_cache"/* 2>/dev/null
fi
%%STUB_FILES%%ccc_build() {
    apptainer exec --contain --pid --pwd "$proj" $overlay \\
      --bind "$proj,$scratch/env:%%ENV_MOUNT%%,$ccc_cache:%%POD_CACHE%%,\
$scratch/tmp:%%POD_TMP%%,%%TOOLS_DIR%%:%%TOOLS_DIR%%:ro%%FEATLIB_BUILD_BIND%%%%STUB_BINDS%%" \\
      --env "%%BUILD_POD_ENV%%" \\
      "$sif" "$@" >> "$err" 2>&1
}
# A failed build is a failed SPAWN. It was accepted, Escrowed and queued minutes ago and its submitter
# has already walked away, so it ends the way every other failed Spawn ends — the build's own output in
# `err`, a non-zero `exit`, and the Escrow settled but NOT refunded. That is the deliberate cost of
# building here: the Head can no longer refuse a broken manifest for free.
#
# `$ccc_building` names what is being built, reset before each group below, so the failure says which of
# the two failed rather than blaming the manifest for a Feature's conda solve.
ccc_build_or_die() {
    echo "ccc: $*" >> "$err"
    if ! ccc_build "$@"; then
        echo "ccc: $ccc_building could not be built (see above)." >> "$err"
        echo "ccc: this Spawn never ran. Fix the declaration and resubmit." >> "$err"
        echo 1 > "$exitf"
        kill "$loop_pid" 2>/dev/null
        stage_out
        exit 1
    fi
}
%%STUB_LOG%%%%BUILD_CMDS%%
%%POST%%"""

# The two shapes `%%PAYLOAD%%` can take. Everything around it — staging, the EXIT trap, stage_out, the
# periodic outputs pusher — is identical either way, which is the whole point of the slot: a Tide Pool
# is a normal Spawn that happens to run a live shell instead of a script, not a second pipeline.
#
# `--contain --pid --pwd "$proj"` is the containment set (apptainer 1.5.3), and it is what makes a Pod a
# boundary rather than a packaging format. Apptainer's own model is "the user inside is the user
# outside", so without these flags a payload could reach whatever the outside account holds through
# `/proc/<pid>/root` of any host process owned by that uid. A **Guise** is homeless and holds no
# secrets; these flags are what actually locks the door around it:
#
# * `--contain` drops the automatic $HOME/tmp/cwd binds and gives the Pod a private, empty /tmp — so a
#   Spawn on a shared Arm can no longer read every concurrent Spawn's scratch. It also blocks the host
#   passwd/group bind by itself (host 55 lines -> image 21), which is why `--no-mount` below is
#   belt-and-braces rather than load-bearing.
# * `--pid` puts the payload in its own PID namespace with its own procfs — the single flag that closes
#   `/proc/<pid>/root` and `/proc/<pid>/environ` (another Spawn's environment). Apptainer starts its
#   own `appinit` shim as PID 1, so there is no zombie-reaping problem and no tini to ship.
# * `--pwd "$proj"` is load-bearing, not cosmetic: `--contain` drops the cwd auto-bind, so $proj must be
#   bound EXPLICITLY and named as the working dir, or cwd is $HOME and `python <script>` fails outright.
#
# LC_ALL/LANG: the base image never ran locale-gen, so any inherited locale makes the payload's
# own shell/tooling write a "cannot change locale" warning into the Spawn's `err` before it has done
# anything. C.UTF-8 ships pre-generated on every Debian/Ubuntu image.
#
# `$py` is this Spawn's declared **Environment**'s interpreter, and there is no other kind: the image
# has none (cuttle.templates.BASE_DEF_TMPL). It is set together with `$envbind`, which mounts it, so the
# interpreter named here can never be one the bind did not provide.
#
# `$ccc_out` is NOT bound and NOT exported. It is the Head-side keep directory, reached only through the
# `$outbind` mounts that put each declared **Output** at the path the payload already writes to. There is
# no `CCC_OUT` in the Pod environment: a script that has to be taught a cluster-specific variable before
# it can save anything is a script that runs everywhere except here, and the submitter who forgets finds
# out after the GPU time is spent. What replaced it is that `-o runs` simply means the `runs/` the code
# was already writing.
PAYLOAD_SCRIPT = """\
apptainer exec --nv --contain --pid --pwd "$proj" $overlay --no-mount /etc/passwd,/etc/group \\
  --bind "$proj,$scratch/tmp:/ccc-tmp$envbind$licbind$featbind$outbind$migbind" \\
  --env "LC_ALL=C.UTF-8,LANG=C.UTF-8,TMPDIR=/ccc-tmp,PIP_CACHE_DIR=/ccc-tmp/pip,$XDG_ENV$migenv$featenv" \\
  "$sif" $py %%SCRIPT%% %%ARGS%% > "$out" 2> "$err\""""

# sshd inside the Pod, reachable at %%TIDEPOOL_PORT%% on the Arm's OWN network address (apptainer here
# runs with no --net — full host netns, same as the script path, because the Pod still needs internet egress
# and the mesh to stage its results home) — the Head-side relay (ccc_head_api.tidepool/relay.py) is what
# makes that reachable from a client that is not a Mesh member.
#
# `--contain --pid --pwd "$proj"` applies here for exactly the reasons it applies to PAYLOAD_SCRIPT
# above — and matters MORE here, since this payload hands a User a live interactive shell rather than
# running a script they wrote. Confirmed end to end against the full boot path below: sshd, pubkey
# auth, `UsePAM=no` and an interactive pty all work under containment (`--contain`'s minimal /dev still
# provides devpts), /tmp is private and empty, and the PID namespace holds — `/proc/1/root` is the
# container root, not the host's.
#
# sshd never setuid()s: the "login" is to the SAME uid already running the Pod (the submitter's
# **Guise**, homeless-by-design on the host — see the module docstring), so there is no privilege drop
# to fail. The Arm-side account it needs is provisioned by the Slurm prolog before the job step, not by
# sshd — which still needs the connecting uid to resolve via getpwuid.
#
# Two things have to be true together, or a Guise's shell isn't actually isolated:
#
# 1. The HOST's passwd/group must not land over the image's own, or every OTHER host account (not just
#    the invoking one's) leaks into the Pod. `--contain` does this by itself (host 55 lines -> image
#    21); `--no-mount /etc/passwd,/etc/group` is kept below as an explicit statement of intent, not
#    because anything depends on it.
# 2. Neither flag is enough alone: with --writable-tmpfs (needed below so sshd can write host keys +
#    authorized_keys), apptainer still patches /etc/passwd for the INVOKING uid in place — a direct
#    edit to the writable overlay, not a bind, so no mount-level flag touches it. It overwrites
#    templates.BASE_DEF_TMPL's baked `mantle:...:/home/mantle:...` entry with the invoking uid's REAL
#    host identity — confirmed under a Guise: `ccc-u60002:x:60002:60002:CCC Guise:/home/ccc-u60002:
#    /bin/bash`. So the home inside the Pod is the Guise's, and the boot script below reads it back out
#    of getpwuid rather than naming it — it's per-User, and the Head is the only side that knows the
#    uid. The CLI connects as that same account (tidepool.py, from `GET /spawns/{id}/tidepool`).
PAYLOAD_TIDEPOOL = """\
cat > "$scratch/tp_boot.sh" <<'CCC_TP_BOOT'
#!/bin/bash
set -e
mkdir -p /run/sshd
# NOT in the Guise's ~/.ssh, and this is load-bearing rather than tidiness. A Spawn that requests an Arm
# **Feature** has the Arm's **Licence** directory bound OVER that home (see `licbind` in JOB_SCRIPT), and
# that directory is `2770 ccc-licence:ccc-guises` (cuttle.setup._licence_dir) — group-writable, and owned
# by neither the Guise nor root. sshd's StrictModes (on by default) refuses an authorized_keys under a
# home like that, so pubkey auth was silently skipped and the User got a password prompt for an account
# that has no password. Every Tide Pool with a --feature was unreachable; without one the bind is absent
# and the same code worked, which is why this survived.
#
# StrictModes is emphatically NOT the thing to turn off. That directory is the shared home of EVERY
# feature-requesting Pod on the Arm, and every Guise is in `ccc-guises` — so an honoured authorized_keys
# there would let one User write their own key into another User's Tide Pool. StrictModes refusing it is
# the only reason that isn't already possible. The file belongs outside the home instead.
#
# A Pod-absolute path, under `/` (root-owned 0755 in the image) with the file 0600 and owned by the Guise
# that writes it, which is what sshd's own path check wants. `--writable-tmpfs` is what makes `/` writable
# here, the same overlay /etc/passwd and /etc/profile.d are patched through below.
cat > /ccc_authorized_keys <<'CCC_TP_PUBKEY'
%%TIDEPOOL_PUBKEY%%
CCC_TP_PUBKEY
chmod 600 /ccc_authorized_keys
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
#
# zsh, from the image (cuttle.templates.BASE_DEF_TMPL), configured by ccc_head_api.zshrc below. There is
# deliberately no "zsh if present, else bash" here. A User cannot choose their Arm (CONTEXT.md,
# **Eligibility**), so a fallback would make the shell they get depend on which node they were placed
# on — silently, and differently for the same command run twice. An Arm whose image predates this dies
# on the next line instead, loudly, which is the same bargain every other fleet-wide change makes:
# `cuttle teardown --all` + `setup`, or the node does not serve Tide Pools.
sed "s|^\\($(id -un):.*\\):[^:]*$|\\1:/usr/bin/zsh|" /etc/passwd > /tmp/ccc_passwd.new
cat /tmp/ccc_passwd.new > /etc/passwd
rm -f /tmp/ccc_passwd.new
# An interactive login shell (sshd starts one as `-zsh`, which reaches this file through the
# /etc/profile line BASE_DEF_TMPL appends to /etc/zsh/zprofile) lands in the optional project a User
# picked (CCC_PROJ, the same $proj a Script Spawn's `cd` already puts on the host side; an
# explicit `--bind "$proj"` is what makes it visible in here at all, since `--contain` drops the cwd
# auto-bind this used to lean on — see PAYLOAD_SCRIPT, which binds it the same way to find %%SCRIPT%%).
# An EMPTY dir when no project was picked, never a missing one (both branches mkdir it) — `cd` into an
# empty dir is a harmless no-op.
echo "cd \\"$CCC_PROJ\\" 2>/dev/null || true" > /etc/profile.d/ccc-tidepool.sh
# There is no $CCC_OUT any more, here or anywhere (see PAYLOAD_SCRIPT). A Tide Pool's declared **Output**s
# are bound at the paths they name, exactly as a Script Spawn's are, so saving something is `cp x results/`
# and not a variable to remember. What a shell DOES need is to be told which folders those are — the one
# thing a script never needs, because a script was written knowing where it writes. Printed at login
# rather than exported, because the answer is for the person, not the process.
#
# A Tide Pool may decline to declare any (CONTEXT.md), and then this says so plainly. That is a real
# warning and not a formality: with no Output there is no place in this Pod whose contents outlive it,
# and everything written is gone.
if [ -n "$CCC_KEPT" ]; then
  echo "echo \\"ccc: kept when this session ends — \\$CCC_KEPT\\"" >> /etc/profile.d/ccc-tidepool.sh
else
  echo "echo 'ccc: this Tide Pool declared no output — nothing you write here is kept.'" \\
    >> /etc/profile.d/ccc-tidepool.sh
fi
echo "export CCC_KEPT='$CCC_KEPT'" >> /etc/profile.d/ccc-tidepool.sh
# A Tide Pool gets the SAME **Environment** its Spawn would get, so what you debug is what runs — put it
# first on PATH rather than making the User type the interpreter's full path. Writable, too (it is this
# session's own node-local copy, so read-only would buy no safety): `pip install` at the shell works and
# dies with the session, which is most of what a Tide Pool is for.
[ -x %%ENV_BINDIR%%/python ] && \\
  echo "export PATH=\\"%%ENV_BINDIR%%:\\$PATH\\"" >> /etc/profile.d/ccc-tidepool.sh
# Everything above is in this profile rather than in apptainer's `--env` for one reason, and it is the
# same reason the two lines below have to be here too: **sshd builds a FRESH environment for every
# session and does not pass its own down.** Confirmed by experiment (2026-08-07): a marker variable and
# a TMPDIR set in sshd's own environment both come back EMPTY inside the session. So everything
# apptainer hands this Pod through --env reaches THIS script and stops here.
#
# The Arm **Feature** runtime is what sshd was silently eating. `$featenv` (ccc_head_api.features) is
# handed to this Pod through --env and stops at this script, so a **Tide Pool**
# that asked for a profiled Feature had its package bound, its system libraries built and its working
# directory arranged, and then got a shell with none of the three variables that make any of that
# reachable: `import vlearn` was a ModuleNotFoundError and `pip list` showed nothing, on the one Spawn
# whose entire point was the Feature. LD_LIBRARY_PATH especially cannot be recovered from inside — the
# loader reads it at `exec`, which is the whole reason a profile exists.
#
# The NAMES arrive as one more --env entry (CCC_FEATURE_VARS, derived from `$featenv` below) rather than
# being listed here: a profile that introduces a variable then needs no edit in this file, and the
# symptom of forgetting one would be exactly this bug again. Unquoted for the word split, which is what
# it is for. Values are read back out of this script's own environment and never re-parsed.
for v in $CCC_FEATURE_VARS; do
  if [ -n "${!v-}" ]; then echo "export $v='${!v}'" >> /etc/profile.d/ccc-tidepool.sh; fi
done
# The Pod's own /tmp is a RAM tmpfs sharing 64MB with the --writable-tmpfs overlay (see the mkdir in
# JOB_SCRIPT). Point the shell at the bound disk scratch instead, or `pip install` at a Tide Pool —
# the thing a Tide Pool is most used for — dies on any wheel bigger than what is left of that 64MB,
# with pip blaming the network for it.
echo "export TMPDIR=/ccc-tmp" >> /etc/profile.d/ccc-tidepool.sh
echo "export PIP_CACHE_DIR=/ccc-tmp/pip" >> /etc/profile.d/ccc-tidepool.sh
chmod 600 /etc/profile.d/ccc-tidepool.sh
# Where the VS Code server unpacks — ~150MB, into the same 64MB tmpfs home, which fails as ENOSPC
# partway through and is then reported by Remote-SSH as "Cannot resolve authority" with the real cause
# (StorageFull, unpacking package.json) buried four screens down its own log. Measured on real hardware
# (2026-08-18), against `cuttle run --tidepool --vscode`.
#
# VSCODE_AGENT_FOLDER in sshd's SetEnv below is NOT enough on its own, and this is why the variable did
# not save us: it names the legacy server-script location, and Remote-SSH 0.124 bootstraps through the
# Rust CLI instead, which unpacks to ~/.vscode-server/cli/servers/ and reads its own
# VSCODE_CLI_DATA_DIR. Both are set now, but the SYMLINK is the fix that does not depend on knowing
# which variable this month's client honours — whatever any of them decides to write under
# ~/.vscode-server lands on the bound disk scratch.
mkdir -p "$HOME"
ln -sfn /ccc-tmp/vscode "$HOME/.vscode-server"
# The shell's own configuration (ccc_head_api.zshrc), which the Head renders rather than the image
# bakes — that module's docstring says why. Everything ABOVE this point still lands in /etc/profile.d
# and is reached through the one line BASE_DEF_TMPL appends to /etc/zsh/zprofile: there is exactly one
# generator for CCC_KEPT, the Environment PATH and the `cd`, and switching shells did not
# fork it. zprofile is read before zshrc, so by the time the rc below runs, all of it is already set.
#
# Both heredocs are QUOTED. The rc is full of `$ZSH_CACHE_DIR` and the p10k config is 95 KB of prompt
# escapes — every `$` and backtick in either must reach the file exactly as written, and neither is
# this script's to expand.
#
# /etc/zsh exists because the image installed zsh. If it did not, this redirect fails, `set -e` ends the
# boot script, and the Tide Pool dies before sshd ever listens — see the note on the passwd patch above
# for why that is the intended outcome on a stale image rather than something to guard.
cat > /etc/zsh/zshrc <<'CCC_TP_ZSHRC'
%%ZSHRC%%
CCC_TP_ZSHRC
cat > /etc/zsh/ccc-p10k.zsh <<'CCC_TP_P10K'
%%P10K_CONFIG%%
CCC_TP_P10K
# The two files above are GLOBAL rcs, and a global rc does not satisfy zsh's new-user check: with no
# `.zshenv`/`.zprofile`/`.zshrc`/`.zlogin` in `${ZDOTDIR:-$HOME}` — which `--contain`'s empty home
# guarantees — every Tide Pool opened on the `zsh-newuser-install` wizard instead of a prompt.
#
# ZDOTDIR names this Spawn's own scratch rather than the home, which is the half that is not cosmetic:
# see POD_ZDOTDIR in ccc_head_api.zshrc for the shared-**Licence**-home hole that `touch ~/.zshrc`
# would have seeded instead of fixed. APPENDED, like the /etc/profile bridge in
# cuttle.templates.BASE_DEF_TMPL, because the distro ships its own zshenv and this only adds to it —
# and it goes in zshenv specifically because that is the one file zsh reads before it decides any of
# this, and it is global, so sshd building a fresh environment cannot drop it (the same reason
# everything else here lands in /etc/profile.d rather than in apptainer's --env).
mkdir -p %%ZDOTDIR%%
echo 'export ZDOTDIR=%%ZDOTDIR%%' >> /etc/zsh/zshenv
cat > %%USER_ZSHRC%% <<'CCC_TP_USERRC'
%%USER_ZSHRC_STUB%%
CCC_TP_USERRC
# UsePAM no: `ccc` is a `useradd --system` account with no /etc/shadow entry inside the Pod (the image
# only bakes /etc/passwd + /etc/group — see BASE_DEF_TMPL), so PAM's account phase (pam_unix, checking a
# shadow entry that doesn't exist) refuses the session even after the ephemeral keypair already
# authenticated it — "Access denied ... by PAM account configuration". Found on real hardware (2026-07-31).
# There is no real system session here worth PAM's policy checks (password aging, nologin, securetty):
# the one and only auth boundary is the ephemeral key itself.
#
# SetEnv LC_ALL/LANG=C.UTF-8: sshd's default AcceptEnv forwards the connecting client's LANG/LC_* (most
# desktop ssh_configs SendEnv them), and the base image never ran locale-gen for whatever locale
# a random User's machine happens to use — bash's very first line is then a "cannot change locale"
# warning. SetEnv overrides whatever AcceptEnv would otherwise apply (man sshd_config), forcing a locale
# every Debian/Ubuntu image ships pre-generated, without needing to know or match the User's own.
#
# AuthorizedKeysFile: the ephemeral key lives outside the Guise's home — see the note at the top of this
# script for why it has to. An absolute path with no %u/%h token, because there is exactly one account
# that ever logs into a Pod.
#
# The XDG four and VSCODE_AGENT_FOLDER ride here rather than in /etc/profile.d beside CCC_KEPT,
# and the difference matters: profile.d is read by LOGIN shells only. An editor attached over
# Remote-SSH runs a non-login, non-interactive command and would see none of it — which is exactly the
# session that most needs somewhere to write. SetEnv is sshd's own, applied to every session of every
# shape, which is why LC_ALL was already here.
#
# XDG: the Pod's home is a 64MB tmpfs and, for a Feature-requesting Spawn, a **Licence** directory
# every Guise on the Arm can read — see the XDG_ENV note in JOB_SCRIPT for the leak this closes.
# VSCODE_AGENT_FOLDER / VSCODE_CLI_DATA_DIR: the VS Code server unpacks to ~150MB, which that same
# 64MB home cannot hold. Two variables because two bootstraps — the legacy server script reads the
# first, the Rust CLI the second — and neither is load-bearing on its own: the ~/.vscode-server symlink
# in the boot script above is what actually moves the bytes.
# `/ccc-tmp` is `$scratch/tmp` on the Arm's real disk (cuttle.templates: only /tmp is a bind mount off
# BasePath's disk), so it can.
exec /usr/sbin/sshd -D -e -p %%TIDEPOOL_PORT%% -o UsePAM=no \\
  -o AuthorizedKeysFile=/ccc_authorized_keys \\
  -o "SetEnv=LC_ALL=C.UTF-8 LANG=C.UTF-8 XDG_CONFIG_HOME=/ccc-tmp/xdg/config \\
XDG_DATA_HOME=/ccc-tmp/xdg/data XDG_STATE_HOME=/ccc-tmp/xdg/state \\
XDG_CACHE_HOME=/ccc-tmp/xdg/cache VSCODE_AGENT_FOLDER=/ccc-tmp/vscode \\
VSCODE_CLI_DATA_DIR=/ccc-tmp/vscode"
CCC_TP_BOOT
chmod +x "$scratch/tp_boot.sh"
# Which variables the boot script above has to re-export past sshd — DERIVED from `$featenv` rather than
# listed anywhere, since that string is the one authority on what this Spawn's profiles actually set.
# Empty for every Spawn that asked for no profiled Feature, which makes the loop a no-op rather than a
# special case. Splitting on commas is exactly as safe as the `--env` list below, which already does.
featnames=$(printf %s "$featenv" | tr ',' '\\n' | sed -n 's/=.*//p' | tr '\\n' ' ')
featnames=${featnames% }
apptainer exec --nv --contain --pid --pwd "$proj" \\
  $overlay --no-mount /etc/passwd,/etc/group \\
  --bind "$proj,$scratch/tmp:/ccc-tmp,$scratch/tp_boot.sh:/ccc_tp_boot.sh$envbind$licbind$featbind$outbind$migbind" \\
  --env "LC_ALL=C.UTF-8,LANG=C.UTF-8,CCC_PROJ=$proj,CCC_KEPT=%%KEPT_LIST%%,TMPDIR=/ccc-tmp,PIP_CACHE_DIR=/ccc-tmp/pip$migenv,CCC_FEATURE_VARS=$featnames$featenv" \\
  "$sif" /ccc_tp_boot.sh > "$out" 2> "$err\""""


def _stub_files_sh(files: list[environments.StubFile]) -> str:
    """The shell that puts each placeholder on the Arm's own disk, before the build reads it.

    base64 in a quoted heredoc, so arbitrary zip bytes survive a job script that is otherwise text — and
    so the whole of a stub travels by the route the declaration already travels, rather than needing a
    transfer of its own. Each is ~1KB.

    Under `$scratch`, never `$proj`: see `environments.STUB_ROOT`.
    """
    root = f"$scratch/{environments.STUB_ROOT}"
    out = []
    for f in files:
        where = posixpath.dirname(f.rel)
        out.append(f'mkdir -p "{root}/{where}"' if where else f'mkdir -p "{root}"')
        out.append(f"base64 -d > \"{root}/{f.rel}\" <<'CCC_STUB'")
        out.append(base64.encodebytes(f.data).decode().rstrip("\n"))
        out.append("CCC_STUB")
    return "\n".join(out) + "\n" if out else ""


def _render_env_build(declared: environments.Manifest | None, staging: Path,
                      feature_names: list[str], stub_files: list[environments.StubFile],
                      stub_binds: list[environments.StubBind],
                      stub_log: list[str],
                      packages: list[str] | None = None) -> str:
    """Fill ENV_BUILD for this Spawn, or return "" when it has nothing to build at all.

    Everything decided here is decided ONCE, on the Head, at submit: which commands build this manifest
    (`environments.render_build`), which Feature libraries it needs, which stubs stand in for a Feature's
    packages. The Arm receives shell and runs it. That split is what keeps "what does this manifest mean"
    in one tested place while the bytes are fetched where they are cheap.

    The two halves are INDEPENDENT. A Spawn's own **Environment** exists only if it declared a manifest;
    an **Arm Feature**'s system libraries are CCC's own and are owed to anything that asked for that
    Feature — including a **Tide Pool**, which may decline to declare and still wants the Feature it
    asked for to actually load. So either half alone is enough to render this block.

    Tokens shared with the wider script (%%ENV_MOUNT%%, %%ENV_INTERPRETER%%, %%FEATLIB_MOUNT%%) are left
    for `_render`'s own pass, which runs after this text is spliced in.
    """
    specs = features_mod.lib_specs(feature_names)
    packages = packages or []
    if declared is None and not specs and not packages:
        return ""
    cmds: list[str] = []
    # The **Substrate** goes FIRST, before anything else this block builds, and that ordering is the
    # whole reason it is declared rather than installed by the User once their Spawn is already
    # running: a wheel that has to compile needs its headers present while `uv sync` runs, not after.
    #
    # One `apt-get install` for the lot, not one per name — apt resolves the set together, and a
    # package that pulls another in is then fetched once instead of racing its own dependency. The
    # shim in /usr/local/bin (cuttle.templates.APT_SHIM) is what makes this an ordinary command
    # inside a Pod; nothing here knows about namespaces, and it fetches the index on first use.
    #
    # `ccc_build_or_die` on the same terms as the Environment: a name the archive does not carry is
    # a failed Spawn with apt's own "E: Unable to locate package" in `err`, not a refusal. No archive
    # can be consulted at submit — the answer differs per Arm and per day (ccc_head_api.substrate).
    if packages:
        cmds.append(f"ccc_building={shlex.quote('the substrate: ' + ', '.join(packages))}")
        argv = ["apt-get", "install", "-y", "--no-install-recommends", *packages]
        cmds.append(f"ccc_build_or_die {' '.join(shlex.quote(a) for a in argv)}")
    if declared is not None:
        cmds.append(f"ccc_building={shlex.quote(f'the environment declared in {declared.path}')}")
        cmds += [f"ccc_build_or_die {' '.join(shlex.quote(a) for a in argv)}"
                 for argv in environments.render_build(declared, staging)]
    # AFTER the Environment: this prefix is CCC's, not the submitter's, so a Feature's libraries must not
    # be what a submitter waits through before learning their own declaration was broken.
    for feat, argv in zip(specs, environments.render_featlib_build(specs)):
        what = features_mod.lib_prefix_label(feat)
        cmds.append(f"ccc_building={shlex.quote(what)}")
        cmds.append(f"ccc_build_or_die {' '.join(shlex.quote(a) for a in argv)}")
    if specs:
        # The whole of this prefix's lib/ goes on LD_LIBRARY_PATH (ccc_head_api.features), so every
        # library conda happened to drag in SHADOWS the image's copy — for every process in the Pod, not
        # just the Feature's. ncurses is the one that shouts: nothing in any profile's `libs` asks for it
        # (it arrives under mesalib, via llvm), and the image's own `tput` is built against Ubuntu's
        # libtinfo, so an interactive shell opened on
        #     tput: symbol lookup error: undefined symbol: _nc_safe_fopen
        # five times over — once per `tput` in oh-my-zsh's you-should-use plugin.
        #
        # Safe to delete precisely BECAUSE the image ships a libtinfo.so.6 of its own: whatever in here
        # linked it resolves to Ubuntu's instead, which is the copy every other binary in the Pod was
        # already using. Deleting a library the image does NOT supply would not be safe, and this is not
        # a general prune of duplicates — libstdc++ especially must stay, since conda's is routinely
        # newer than the image's and the Feature's own objects are built against it.
        #
        # **The Arm's path, not the Pod's.** These lines run in the job script, on the Arm — only what
        # goes through `ccc_build` is inside the Pod, where %%FEATLIB_MOUNT%% is a mount that exists.
        # Written against the mount, this deleted nothing at all on every Spawn for as long as it stood:
        # `rm -f` on a glob matching nothing is silent and exits 0, so the prefix kept shadowing and the
        # error it was written to stop went on printing with the fix installed and apparently applied.
        #
        # Still WRITABLE here; the payload gets the same directory read-only (the `featbind` below).
        cmds.append('rm -f "$scratch"/featlibs/*/lib/libtinfo.so* '
                    '"$scratch"/featlibs/*/lib/libncurses*.so*')
    post = []
    if declared is not None:
        post.append('py="%%ENV_INTERPRETER%%"; envbind=",$scratch/env:%%ENV_MOUNT%%"; '
                    'envmount="%%ENV_MOUNT%%"')
    if specs:
        post.append('featbind=",$scratch/featlibs:%%FEATLIB_MOUNT%%:ro"')
    body = ENV_BUILD
    subs = {
        "%%FEATLIB_MKDIR%%": ' "$scratch/featlibs"' if specs else "",
        # Bound WRITABLE for the build that fills it, read-only for the payload that reads it — the
        # same directory, two different jobs to do.
        "%%FEATLIB_BUILD_BIND%%": ',$scratch/featlibs:%%FEATLIB_MOUNT%%' if specs else "",
        # What the payload picks up. Only a Spawn that DECLARED a manifest gets an interpreter and an
        # env mount — which is what keeps `$py` empty for a Tide Pool that asked for a Feature and
        # nothing else, exactly as it was before any of this was built here.
        "%%POST%%": "\n".join(post),
        # Written by the Arm into its own scratch, then mounted where the declaration points. The
        # DESTINATION is project-relative (environments.substitute_supplied) because only this side
        # knows where the project landed, and `$proj/../x` is `$scratch/x` on both branches — the Head's
        # co-located Arm copies into node-local scratch, a remote one rsyncs into /tmp, and `proj` is
        # `$scratch/proj` either way. Almost always empty — see that function.
        "%%STUB_FILES%%": _stub_files_sh(stub_files),
        "%%STUB_BINDS%%": "".join(
            f",$scratch/{environments.STUB_ROOT}/{b.src}:$proj/{b.dest}" for b in stub_binds),
        "%%STUB_LOG%%": "".join(f'echo {shlex.quote(line)} >> "$err"\n' for line in stub_log),
        "%%BUILD_CMDS%%": "\n".join(cmds),
        "%%ENV_CACHE_ROOT%%": str(environments.CACHE_DIR),
        "%%ENV_CACHE_MAX%%": str(environments.CACHE_MAX_GB * 1024**3),
        "%%ENV_CACHE_MAX_GB%%": str(environments.CACHE_MAX_GB),
        "%%POD_CACHE%%": environments.POD_CACHE,
        "%%POD_TMP%%": environments.POD_TMP,
        "%%TOOLS_DIR%%": str(environments.TOOLS_DIR),
        "%%BUILD_POD_ENV%%": environments.BUILD_POD_ENV,
    }
    for k, v in subs.items():
        body = body.replace(k, v)
    return body


def _render(staging: Path, gpu: int, script: str, args: list[str],
            files: list[str], outputs: list[str], node: str | None, minutes: int, constraint: str,
            token: str, secret: str, exclude: str = "", tidepool_port: int | None = None,
            tidepool_pubkey: str | None = None, wants_licence: bool = False,
            features: list[str] | None = None, activation: bool = False,
            declared: environments.Manifest | None = None,
            stub_files: list[environments.StubFile] | None = None,
            stub_binds: list[environments.StubBind] | None = None,
            stub_log: list[str] | None = None,
            packages: list[str] | None = None) -> str:
    payload = PAYLOAD_TIDEPOOL if tidepool_port is not None else PAYLOAD_SCRIPT
    # %%PAYLOAD%% has to be substituted in FIRST — it is the only token whose replacement text can
    # itself contain other tokens (%%SCRIPT%%/%%ARGS%% or %%TIDEPOOL_PORT%%/%%TIDEPOOL_PUBKEY%%,
    # depending which payload this is), and every token below is a plain single-pass `.replace()`.
    # Doing it here rather than relying on dict order keeps that dependency from being invisible.
    body = JOB_SCRIPT.replace("%%PAYLOAD%%", payload)
    # The build block is substituted here for the same reason as %%PAYLOAD%% above and not the one below:
    # its replacement text DOES contain tokens (%%ENV_MOUNT%%, %%ENV_INTERPRETER%%, %%FEATLIB_MOUNT%%),
    # deliberately, so that the mount point is written down once for the whole script rather than twice.
    #
    # NOTE the token is NEVER named in JOB_SCRIPT's own prose. `.replace()` does not know a comment from
    # code, so a mention inside the template would take a full copy of this block too — the copy's first
    # line commented out by the `# ` in front of it and the rest live. That shipped once: the `mkdir` that
    # creates $scratch/env was the commented line, and every declared Spawn died on a bind whose source
    # did not exist. `test_the_build_block_appears_exactly_once` is the guard.
    body = body.replace("%%ENV_BUILD%%",
                        _render_env_build(declared, staging, features or [],
                                          stub_files or [], stub_binds or [], stub_log or [],
                                          packages or []))
    # %%FEATURE_SETUP%% is the other one, for the opposite reason: its replacement text contains NO
    # tokens, deliberately. A Feature profile (ccc_head_api.features) is bash the Head splices in
    # verbatim, so it reads as bash rather than as a second template — everything it needs from this
    # script, including the Environment's mount point, it takes from a shell variable set above.
    body = body.replace("%%FEATURE_SETUP%%",
                        features_mod.setup_snippet(features or [], activation=activation))
    # Resolved here rather than handed in already-resolved, so this function takes what the submitter
    # declared and nothing derived. `submit` runs the same call for its own symlink check; it is pure and
    # costs a few string splits, and the alternative is a resolved type threaded through every caller.
    plan = output_plan(outputs)
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
        # The Pod's writable system layer — see POD_OVERLAY_MB.
        "%%POD_OVERLAY_MB%%": str(POD_OVERLAY_MB),
        "%%HEAD_SCRATCH%%": str(SCRATCH_DIR),
        # The Spawn's **Environment** (environments.py). The mount point is a constant — one path, on
        # every machine — but the interpreter is NOT: a conda prefix keeps its python at `ENV_MOUNT/bin`,
        # a uv env one level down in its venv, and only the manifest says which shape it is.
        #
        # Both fall back to a path UNDER the mount when nothing was declared, never to something the
        # image supplies — there is nothing there to supply one (BASE_DEF_TMPL). That leaves the unused
        # branch inert: the mount does not exist in a Pod that declared nothing, so the Tide Pool's
        # `[ -x ... ]` test is simply false, where a real directory (/usr/bin) would pass it and put the
        # image's own bin first on PATH for no reason.
        "%%ENV_MOUNT%%": environments.ENV_MOUNT,
        "%%ENV_INTERPRETER%%": (declared.interpreter if declared
                                else f"{environments.ENV_MOUNT}/bin/python"),
        "%%ENV_BINDIR%%": declared.bin_dir if declared else f"{environments.ENV_MOUNT}/bin",
        # Where the Arm **Feature** library prefix is mounted, named by %%ENV_BUILD%% (which both builds
        # and binds it) and by each profile's own setup block.
        "%%FEATLIB_MOUNT%%": features_mod.FEATLIB_MOUNT,
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
        # The declared **Output**s, resolved (output_plan) before this is ever built — every segment is
        # charset-checked, so interpolating into double quotes is enough. See the block this fills in
        # JOB_SCRIPT for what the three lines per output are doing and why they are in that order.
        #
        # Empty for a Tide Pool that declared none, the one Spawn that may decline.
        "%%OUTPUT_SETUP%%": "\n".join(
            "\n".join(filter(None, (
                f'mkdir -p "$ccc_out/{p.store}"',
                # Seed, and make the mountpoint — RELATIVE outputs only, and for the same reason both
                # times: `$proj` is a host directory, and everything on these lines runs on the host as
                # the **Guise**, outside any Pod. An absolute output names a path in the Pod's own root,
                # where the Guise can create nothing (`mkdir -p /ccc/vlwd/logs` is EPERM at `/`) and where
                # there was never an upload to seed from. Apptainer makes that mountpoint itself, in the
                # overlay, which is the ordinary "bind to a path the image does not have" case.
                #
                # Getting this wrong is not merely a no-op: the mkdir's "Permission denied" lands in
                # $sink, which makes the sink non-empty, which defeats the keep-only-if-non-empty rule and
                # leaves a file in /tmp for every Spawn ever run — the litter that poisoned job ids (see
                # the `sink=` note in JOB_SCRIPT).
                None if p.absolute else
                f'[ -n "$(ls -A "{p.dest}" 2>/dev/null)" ] && '
                f'cp -a "{p.dest}/." "$ccc_out/{p.store}/"',
                None if p.absolute else f'mkdir -p "{p.dest}"',
                f'outbind="$outbind,$ccc_out/{p.store}:{p.dest}"',
            ))) for p in plan),
        # What a **Tide Pool**'s login shell tells the person sitting in it (see PAYLOAD_TIDEPOOL). The
        # declared paths as written, since that is what they will type; empty when none were declared,
        # which the boot script turns into the warning that nothing is kept.
        "%%KEPT_LIST%%": " ".join(p.declared for p in plan),
        # Only ever non-empty when `payload` is PAYLOAD_TIDEPOOL, which is the only place either token
        # appears — an ordinary Spawn's rendered script never even contains the literal string.
        "%%TIDEPOOL_PORT%%": str(tidepool_port) if tidepool_port is not None else "",
        "%%TIDEPOOL_PUBKEY%%": tidepool_pubkey or "",
        # Whether this Spawn asked for any Arm **Feature**, and so may see the Arm's **Licence**. A
        # bare flag, not the path: the path is Arm-local (every Arm's owner chose their own) and the
        # Arm supplies it from /etc/ccc/features.env at run time, exactly as it does a Feature's path.
        "%%WANTS_LICENCE%%": "1" if wants_licence else "",
    }
    for k, v in subs.items():
        body = body.replace(k, v)
    # The shell config, spliced AFTER the loop above rather than inside it — the third substitution
    # order in this function, and the opposite case from %%PAYLOAD%% and %%ENV_BUILD%%. Those go FIRST
    # because their text contains tokens that still have to be resolved. These go LAST because their text
    # must never be SCANNED for one: the p10k config is 95 KB of prompt escapes maintained by
    # `p10k configure` rather than by anyone reading this file, and the rc beside it is edited whenever
    # somebody changes the prompt. Neither has any business being able to introduce a `%%…%%` that the
    # loop would then rewrite, and a substitution that only misfires once somebody adds a segment is not
    # a hazard worth leaving in place to save two lines.
    #
    # The stub rc is spliced here for the same reason, being a file a User is invited to edit. The two
    # paths beside it need no such care and are only kept here to sit with what they configure.
    #
    # All of them no-op for every Spawn that is not a Tide Pool: the tokens exist only inside
    # PAYLOAD_TIDEPOOL, so an ordinary Spawn's rendered script never contains any of these strings.
    if tidepool_port is not None:
        body = body.replace("%%ZSHRC%%", zshrc.render())
        body = body.replace("%%P10K_CONFIG%%", zshrc.P10K_CONFIG)
        body = body.replace("%%USER_ZSHRC_STUB%%", zshrc.USER_ZSHRC_STUB.rstrip("\n"))
        body = body.replace("%%USER_ZSHRC%%", zshrc.POD_USER_ZSHRC)
        body = body.replace("%%ZDOTDIR%%", zshrc.POD_ZDOTDIR)
    return body


def _lend_project(staging: Path, guise: str) -> None:
    """Let one **Guise** use one uploaded project, and nothing else.

    The Head's co-located Arm copies the project out of `projects/<uuid>` as the Guise (JOB_SCRIPT's
    Head branch), and the **Environment** build runs in it as the Guise too. That directory is
    `ccc:ccc 0700` and stays that way — this adds a POSIX ACL naming exactly one account, which the
    Mantle can do with **no privilege at all**, because it already owns the directory. The 0711 on
    `projects/` above it is what lets the Guise reach this dir by name without being able to list its
    siblings.

    `rwX`, not `rX`, and the w is the build's: a dependency resolve WRITES INTO THE PROJECT as a matter
    of course, and read-only made that a failure with no useful message. `uv sync` writes `uv.lock`
    beside the manifest (always — there is no flag that resolves without recording it, and --frozen
    needs a lock that a `pyproject.toml`-only project has not got); micromamba writes the pip
    requirements it hands to `pip` beside `environment.yml`; a src-layout build backend writes its
    egg-info. Each of those came back as "Permission denied (os error 13)" and took the Spawn with it.
    Nothing is given away by allowing it: the writer is the submitter
    of this upload, acting on their own files, one uuid that no other User can name — and what a build
    leaves behind (a lock naming exactly what got installed) is worth carrying to the Arm, which it is,
    since stage-in copies the project after the build.

    Done here rather than in the prolog, and that is not a style choice: the prolog would have to be
    TOLD the uuid, and the only channel for that (a file under `spawns/<jid>/`) cannot exist until
    sbatch has returned the job id — by which time the prolog may already have run. This happens before
    the job exists at all, so there is no race to lose.

    Best-effort: a Head whose filesystem has no ACL support, or no `setfacl`, still runs every REMOTE
    Spawn correctly (they stage in over rsync and never touch this tree). Refusing the submission
    outright would take the whole cluster down over a Head-local optimisation.
    """
    r = subprocess.run(["setfacl", "-R", "-m", f"u:{guise}:rwX", str(staging)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("could not lend %s to %s (%s) — a Spawn placed on the Head's own Arm will not be "
                    "able to read its project", staging, guise, (r.stderr or "").strip())


def _said(e: subprocess.CalledProcessError) -> str:
    """What a failed command actually printed, flattened to ONE line.

    One line because of where this ends up. A pinned submit runs the admin tool under sudo in a
    subprocess (cuttle.spawns._submit_pinned) and reports back the LAST line of its stderr and nothing
    else — so a three-line sbatch error crossing that boundary arrives as its third line, which is
    rarely the one naming the fault. Joined rather than truncated: sbatch says "error: Batch job
    submission failed: ..." across two lines about as often as one.

    stdout is the fallback because a command that failed while writing its diagnosis to the wrong
    stream is still telling us why, and an empty message here would send the operator back to a bare
    exit code — the state this helper exists to end.
    """
    for stream in (e.stderr, e.stdout):
        lines = [ln.strip() for ln in (stream or "").splitlines() if ln.strip()]
        if lines:
            return " ".join(lines)
    return f"it printed nothing at all, and exited {e.returncode}"


def submit(user_id: int, script: str | None, gpu: int, args: list[str],
           tar_path: Path | None, minutes: int, constraint: str, outputs: list[str],
           node: str | None = None, vram_gb: int = 0, hold: float | None = None, exclude: str = "",
           tidepool: bool = False, manifest: str | None = None, substrate: str | None = None,
           features: list[str] | None = None,
           activation: bool = False,
           name: str = "") -> int:
    """Unpack an uploaded project, render + sbatch it **as the submitter's Guise**, and record the
    Attribution row + the Karma Escrow. Returns the Slurm job id (== the Spawn id). `node` pins the
    Spawn to one Arm.

    `hold` is the Escrow quoted by the caller, or **None** for a Spawn nobody is charged for (an
    Admin's — the Admin is outside the economy). It is written in the SAME transaction as the
    Attribution row: a Spawn that existed without its hold, even briefly, would be one its submitter's
    Share had not yet been charged for, which is precisely the window a queue-flooder wants.

    `tar_path` is a spooled
    file, not bytes: the caller streams the upload to disk so a large project never sits in this
    process's memory (see main._spool_upload). tarfile reads it incrementally. A Tide Pool still
    uploads one (an empty gzip-tar — every client sends the same wire shape) since there is no project
    to run; `script` rides along as None and the tar's member list is never checked against it.

    `outputs` names paths in the submitter's own project (or, `/`-prefixed, absolute inside the Pod) —
    resolved and checked by `output_plan` here, not just at the HTTP boundary, so the Head-local pinned
    path (`cuttle run --node`) gets the identical guarantee. Both checks below (outputs
    non-empty, script present in the project) are hard validation for an ORDINARY Spawn: a Spawn that
    failed either would either write nowhere retrievable or never start at all, so neither is worth a
    Spawn's Escrow and a wasted place in the queue. `tidepool=True` skips both — a live shell has no
    script to find, and where it keeps anything is where a User happens to point it, not a name the Head
    can enforce ahead of time (the CLI still warns, see cuttle/tidepool.py).

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
    plan = output_plan(outputs)

    uuid = uuid4().hex
    staging = PROJECTS_DIR / uuid
    # Checked against the tar's own member list, BEFORE anything is written to disk: a script that
    # isn't here would never start (or, worse, silently run some OTHER script if bash's glob ever
    # changed underneath us), and refusing before staging.mkdir() means a bad submission leaves
    # nothing behind. Skipped for a Tide Pool — `script` is None, and its (empty) tar names nothing
    # to find.
    with tarfile.open(tar_path, mode="r:gz") as tf:
        if not tidepool and script not in tf.getnames():
            raise ValueError(f"script {script!r} was not found in the uploaded project — check the "
                             f"path matches exactly (and isn't excluded by .gitignore)")
        # 0700, not the service's ambient umask: an uploaded project is the User's own tree and may
        # itself carry a secret (a stray .env), so what it lands as must not depend on whoever
        # provisioned this Head. The Guise reaches it through `_lend_project`'s ACL, not through the
        # mode — a named-user ACL entry is what grants access here, so tightening this changes
        # nothing for the Spawn that owns it.
        staging.mkdir(parents=True, mode=0o700)
        tf.extractall(staging, filter="data")  # 'data' filter blocks path-traversal entries
    # A declared **Output** is a path in the project, and the job script mounts the keep directory onto
    # it — so a SYMLINK anywhere along that path would resolve the mountpoint somewhere else entirely, and
    # the Spawn would keep whatever lives there instead of what it wrote. Refused here rather than
    # reasoned about on the Arm: the upload is already on disk, the answer is a stat, and a Spawn cannot
    # be given a mountpoint whose meaning depends on a link its submitter may not have known was there.
    #
    # `is_symlink()` per ancestor, not `resolve()`: what matters is whether a link is traversed, not
    # whether the destination happens to land back inside the project. Nothing to check for an absolute
    # output — no upload was ever written to a Pod path.
    for p in plan:
        if p.absolute:
            continue
        walked = staging
        for seg in p.store.split("/"):
            walked = walked / seg
            if walked.is_symlink():
                raise ValueError(
                    f"output {p.declared!r} passes through a symlink in your project "
                    f"({walked.relative_to(staging).as_posix()!r}) — CCC mounts the folder it keeps "
                    f"at that path, and a link would send it somewhere else. Declare a real folder")
    # The Guise is allocated HERE rather than just before sbatch, because the **Environment** build
    # below runs as it — a manifest is code, and CCC runs no User's code as the Mantle (CONTEXT.md,
    # **Guise**). `_lend_project` comes with it: the build enters a Pod with the project bound and this
    # ACL is what lets the Guise read it.
    with db.connect() as conn:
        guise = guises.name(db.allocate_guise(conn, user_id))
    _lend_project(staging, guise)

    # What the declaration MEANS is decided here, synchronously; the build itself happens on the Arm.
    # Detection is a couple of stat calls, and everything it and `require_tool` can raise is a REJECTION
    # — nothing declared, a manifest that does not exist, one nothing can classify, a node without the
    # tools — so it stays here, where a BuildError is still a plain 4xx and no Spawn has been brought
    # into existence to Escrow or queue. Past this point a bad declaration costs a failed Spawn.
    #
    # A **Tide Pool** is the one Spawn that may decline: it is a shell, not a run, and a bare one uploads
    # an empty tar with no file in it to name. Declaring is still allowed there and still builds — the
    # exemption is from the requirement, not from the feature.
    declared = None
    stub_files: list[environments.StubFile] = []
    stub_binds: list[environments.StubBind] = []
    stub_log: list[str] = []
    project = staging
    # The **Substrate**, read on the same terms and in the same breath. Optional for EVERY Spawn, not
    # just a Tide Pool, so there is no `or not tidepool` here: most work needs no system package, and
    # unlike an Environment there is nothing load-bearing to refuse the absence of.
    packages = substrate_mod.read(project, substrate)
    if manifest or not tidepool:
        declared = environments.detect(project, manifest)
        environments.require_tool(declared, features or [])
        # Anything the project declares that an **Arm Feature** supplies instead. A rejection for the
        # same reason as the two above — asking for a Feature's package without asking for the Feature
        # is decided here, in stat calls, and would otherwise be an ImportError after a GPU was held.
        environments.check_supplied(project, declared, features or [])
        # Each Feature-supplied wheel gets an empty placeholder of the same name and version, mounted
        # over what the declaration points at. Nothing is written into the project
        # (`environments.substitute_supplied`).
        stub_files, stub_binds, stub_log = environments.substitute_supplied(project, declared)

    # Module-root-relative list for a remote Arm's --files-from. This Spawn's Siphon is rooted at its
    # OWN projects/<uuid>, not at the shared parent, so the paths are relative to the upload itself —
    # that re-rooting IS the isolation, and it is why the list no longer carries a <uuid>/ prefix. The
    # upload was already gitignore-filtered client-side, so every file here is meant to transfer.
    #
    # Symlinks are listed too: a project can legitimately contain one, and rsync -a copies a symlink AS a
    # symlink. Nothing built appears here any more — the Environment, the Feature library prefix and now
    # the stub wheels are all made on the Arm, in node-local scratch, and never cross the mesh in either
    # direction.
    #
    # What a stub bind COVERS is left out, which is the one thing this walk decides rather than reports.
    # A Feature's package is licensed to the machine that runs it, so a submitter's real wheel has no
    # business on a donor's disk — it used to be overwritten before the walk ever saw it, and leaving it
    # in the transfer now that nothing is overwritten would ship the licensed build the placeholder
    # exists to avoid. The bind hides it on every node either way, so nothing needs it there.
    covered = {b.dest for b in stub_binds if b.over_project}
    files = [
        rel.as_posix() for rel in (p.relative_to(staging) for p in staging.rglob("*"))
        if (staging / rel).is_symlink() or (staging / rel).is_file()
        if rel.as_posix() not in covered]

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
                   token, secret, exclude, tidepool_port=tp_port, tidepool_pubkey=tp_pubkey,
                   wants_licence=bool(features), features=features, activation=activation,
                   declared=declared, stub_files=stub_files, stub_binds=stub_binds,
                   stub_log=stub_log, packages=packages)
    # The script goes in on STDIN, not as a path: a temp file would need to be readable by the Guise
    # sbatch runs as, and feeding it here is strictly better anyway — there is no temp file to leak the
    # project's full file list, and no delete-after-return dance needed.
    try:
        # --no-requeue: a Spawn runs ONCE (CONTEXT.md). `JobRequeue` is unset in the conf template so
        # Slurm's default (1) would otherwise apply, and a requeued job re-runs this script from the
        # top — re-staging a project stage-in has already deleted (see ccc_head_api.projects). It also
        # makes explicit what retraction already assumed: a Spawn whose
        # node failed is a failed Spawn, not one that quietly restarts somewhere else hours later,
        # having been re-billed for and re-queued behind everyone who was waiting.
        #
        # No `--hold` any more, and nothing to release. The hold existed to keep Slurm from placing a
        # Spawn before the Head had finished building its Environment; the Arm builds it now, inside its
        # own allocation, so there is nothing for the Head to wait on and every Spawn is runnable the
        # moment sbatch returns.
        argv = ["sbatch", "--parsable", "--no-requeue"]
        # cwd="/" for the same reason the build sets it: Slurm takes the job's WorkDir from the
        # SUBMITTING process's cwd, and a Head-local submit inherits whatever directory the operator
        # ran `cuttle` from — which the Guise that will run the job cannot necessarily enter. "/" is
        # traversable by everyone and the job script uses absolute paths throughout.
        r = subprocess.run(guises.as_owner(argv, guise), input=body, cwd="/",
                           capture_output=True, text=True, check=True)
    except Exception as e:
        siphons.discard(token)  # no job id will ever exist for it, so nothing else would ever close it
        # `capture_output` means sbatch's refusal goes into the exception and nowhere else, and
        # CalledProcessError's own str() is the argv again — so a submission that Slurm rejected
        # reported the command that failed and never the reason, which is the only part nobody could
        # have guessed. Re-raise carrying what sbatch said.
        if isinstance(e, subprocess.CalledProcessError):
            raise RuntimeError(f"sbatch refused this Spawn: {_said(e)}") from e
        raise
    job_id = int(r.stdout.strip())
    # Everything from here to the `spawns` row is the window where a real, running job exists that
    # nothing has recorded — invisible to every reaper the tick runs (tick.once), never billed, holding
    # a Device, and an hour after it ends retention.register_new deletes its outputs as orphan debris.
    # The User is told none of it. So the same bargain as the sbatch above: if we cannot finish
    # recording the Spawn, we do not leave it running.
    try:
        # Create the Spawn dir for EVERY Spawn, not just Head-local ones: a remote Arm stages its results
        # into its Siphon's [out-…] module, which is rooted HERE, and rsync will not invent the directory
        # a module points at. Safe after sbatch — the job cannot start before sbatch returned its id.
        _make_spawn_dir(job_id, outputs=True)
        # The Environment echo, written where both clients can read it back rather than returned, so no
        # caller's signature changes and the Spawn page can still say months later what this Spawn ran
        # against. Said at submit because the worst case — a manifest one directory too deep — is not a
        # build failure at all: nothing else would ever mention it, and the User would meet it as an
        # ImportError after paying for a GPU.
        #
        # The **Substrate** rides on the same line rather than a second one, so every reader of this
        # file — the CLI's echo, the Dashboard's composer, `cuttle admin` — shows it without knowing
        # it exists. Only when one was declared: most Spawns have none, and saying so on every submit
        # would be noise around the fact that actually matters.
        note = environments.describe(declared)
        if packages:
            note += " · " + substrate_mod.describe(packages)
        (SPAWNS_DIR / str(job_id) / "environment").write_text(note + "\n")
        # Only now can the out module name a path. The proj half has been live since before sbatch; this
        # adds the other and renames the pair to the job id, which is how the tick finds it to close.
        siphons.bind(token, job_id)
        with db.connect() as conn:
            db.record_spawn(conn, job_id, user_id, script or "", uuid, minutes=minutes, gpus=gpu,
                            vram_gb=vram_gb, node=node, hold=hold, tidepool=tidepool, name=name)
            if tidepool:
                db.create_tidepool(conn, job_id, tp_port, tp_pubkey, tp_privkey)
    except Exception:
        # Best-effort both: the raise is what the caller must see, and a cancel that fails leaves the
        # same orphan we already had. A SIGKILL of this process still slips through — closing THAT
        # needs a reconciler in the tick, which cannot yet tell an orphan from the enrollment
        # benchmark (legitimately row-less, sbatched outside this API).
        try:
            slurm.cancel(job_id)
        except Exception:
            log.exception("could not cancel unrecorded spawn %s — it is running with no row", job_id)
        siphons.discard(token)
        raise
    return job_id


def submit_checked(user_id: int, is_admin: bool, script: str | None, gpu: int, args: list[str],
                   tar_path: Path | None, minutes: int, vram_gb: int,
                   outputs: list[str], node: str | None = None,
                   features: list[str] | None = None, tidepool: bool = False,
                   manifest: str | None = None, substrate: str | None = None,
                   activation: bool = False,
                   name: str = "") -> int:
    """Validate placement, quote the Escrow, and submit — everything `/submit` does past auth.

    Shared by both callers so a pinned Spawn (`node`, Head-local only — never reachable over HTTP) is
    priced and recorded **exactly** like any other. The alternative — letting the Head-local path shell
    out to `sbatch` itself — would produce a Spawn with no Escrow and no Attribution row: one that
    consumes a donated card while nobody pays for it, and that no ledger can explain.

    `features` is optional and empty by default — most Spawns need none.
    When given, every name must be carried by at least one currently-admitted Arm or the Spawn is
    refused outright before ever reaching sbatch (mirrors `slurm.vram_constraint`'s "no Arm has that
    much VRAM" — hard validation, no partial/best-effort placement).

    `gpu` is how many **Device**s to hold at once, always on a single Arm, and is at least 1 — there is
    no Device-less work here. `vram_gb` is the **total** across them, which `slurm.vram_constraint`
    turns into the per-card need; the two are checked as one question, so a Spawn nothing can host is
    refused here rather than left to pend forever.

    `tidepool` rides straight through to `submit()`, which is the one place either the
    outputs-required gate or the script-in-tar check are actually skipped — placement, pricing and
    Attribution are otherwise completely unaffected, because a Tide Pool holds a Device exactly like any
    other Spawn.

    `activation` is CCC activating a Feature's licence on a newly-admitted Arm (cuttle.enroll), and it
    is the one thing here that no HTTP client can ask for: `/submit` does not accept it and never
    should, because it is what binds an Arm owner's licence key into a Pod. It arrives only from the
    Head-local admin path, where authorization is being the operator on the Head.

    Raises ValueError for an unknown Arm, a retracted one, an unsatisfiable feature request, or a User
    already at the concurrent-Spawn cap; the callers translate (HTTP 422 / a CLI error).

    `name` is the Spawn's **Name**, and it is **validated here** rather than at the route: it is
    required of every Spawn on this cluster, and a gate only `/submit` enforced would be one the
    Head-local admin path walked straight past.
    """
    # Imported here rather than at module scope: `karma` and `slurm` both reach for this module's
    # siblings, and a top-level import turns that into a cycle.
    from . import karma, slurm

    # First, before the cap and before placement: a nameless Spawn is refused whatever else is true of
    # it, and being told the name is missing beats being told the queue is full and then told again.
    name = clean_name(name)

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
    if gpu < 1:
        raise ValueError("a Spawn holds at least one GPU — --gpus must be 1 or more")
    constraint = slurm.vram_constraint(vram_gb, gpu)
    features = features or []
    if features:
        wanted = sorted(set(features))
        if not slurm.arms_with_features(wanted):
            known = ", ".join(slurm.all_feature_names()) or "(none)"
            raise ValueError(f"no Arm has every requested feature ({', '.join(wanted)}) — Arm "
                             f"Features currently in the cluster: {known}")
        # A **Feature** carrying a runtime profile attaches its software to this Spawn's
        # **Environment** — it probes the interpreter for its ABI, its nvidia lib dirs and its
        # site-packages (features.PREAMBLE). With no Environment declared there is nothing to attach
        # to, and the Spawn could only fail on the Arm after holding a Device to find out.
        #
        # Decidable here, so refused here: CONTEXT.md's rule is that everything knowable without
        # building is settled before the Spawn exists. Only a **Tide Pool** can reach this — every
        # other Spawn is already refused for naming no manifest — which is why the remedy names
        # --manifest rather than suggesting the Feature be dropped.
        if not (manifest or "").strip():
            needs_env = features_mod.profiled(wanted)
            if needs_env:
                raise ValueError(
                    f"{', '.join(needs_env)} needs an environment to attach to, and this Spawn "
                    f"declared none — pass --manifest naming the dependency file to build. (A Tide "
                    f"Pool may skip --manifest only when it asks for no such Feature.)")
        # AND onto the vram OR-expansion — parenthesized so `(vram24|vram48)&vsim` reads unambiguously
        # rather than relying on Slurm's own operator precedence.
        constraint = f"({constraint})&{slurm.feature_constraint(wanted)}" if constraint \
            else slurm.feature_constraint(wanted)
    need = {"node": node, "vram_gb": vram_gb, "gpus": gpu}
    with db.connect() as conn:
        hold = karma.quote_hold(conn, arms, need, minutes, gpu, is_admin)
    return submit(user_id, script, gpu, args, tar_path,
                  minutes, constraint, outputs, node, vram_gb=vram_gb, hold=hold, exclude=exclude,
                  tidepool=tidepool, manifest=manifest, substrate=substrate, features=features,
                  activation=activation, name=name)


# --- reads ---------------------------------------------------------------------

def _spawn_dir(job_id: int) -> Path:
    return SPAWNS_DIR / str(job_id)


# What the Slurm prolog sets on a Spawn dir it lends to a **Guise** (templates.PROLOG_TMPL): rwx for the
# owner and for the Mantle's group, nothing for anyone else. Matched here so the API's own creation is
# not looser than the hook that may later overwrite it.
SPAWN_DIR_MODE = 0o770


def _make_spawn_dir(job_id: int, outputs: bool = False) -> Path:
    """The Spawn's directory, at SPAWN_DIR_MODE rather than the service's ambient umask.

    Explicit because the prolog only covers the Head's own co-located Arm — `if [ -d "$head_spawns" ]`
    is false on a donor box — so for a REMOTE Spawn nothing else ever sets this mode, and the directory
    keeps whatever umask the API service happened to run with, for good. That matters more here than
    for an upload: `projects/<uuid>` is named by an unguessable uuid, but a Spawn dir is named by a
    sequential job id, and the 0711 parent grants exactly the traversal that makes a guessed name
    enough to read someone else's `out`, `err` and results.
    """
    d = _spawn_dir(job_id)
    (d / "outputs" if outputs else d).mkdir(parents=True, exist_ok=True)
    for p in (d, d / "outputs") if outputs else (d,):
        try:
            # chmod, not mkdir(mode=): mkdir's mode is masked by the umask, which is the whole problem.
            p.chmod(SPAWN_DIR_MODE)
        except PermissionError:
            # The prolog has lent this directory to the **Guise** and we are the Mantle, so we no
            # longer own it — which only happens once the job is RUNNING, and `mark_cancelled` runs
            # exactly then. Nothing to do: the prolog set this same mode when it took it.
            pass
    return d


def active_state(job_id: int) -> str | None:
    """Slurm state if the job is still known to squeue, else None. Slurm's answer, unedited.

    It used to need an override: a Spawn `--hold`ed while the Head built its Environment reported as
    PENDING, and only the Head knew better. The Arm builds inside its own allocation now, so a Spawn
    installing its dependencies is RUNNING and squeue says so.
    """
    r = subprocess.run(["squeue", "-h", "-j", str(job_id), "-o", "%T"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def exit_code(job_id: int) -> int | None:
    """The recorded exit code once the Spawn is done, else None. Its presence == 'done'."""
    f = _spawn_dir(job_id) / "exit"
    return int(f.read_text().strip()) if f.exists() else None


def environment_note(job_id: int) -> str | None:
    """What this Spawn's **Environment** was decided to be, written at submit. None for Spawns that
    predate the feature."""
    f = _spawn_dir(job_id) / "environment"
    return f.read_text().strip() if f.exists() else None


def mark_cancelled(job_id: int) -> None:
    """Record that WE ended this Spawn, before scancel does it.

    A cancelled Spawn records no exit code — scancel's TERM kills the wrapper before it can write one
    — so it is otherwise indistinguishable from a Spawn whose Arm dropped off the mesh. Slurm cannot
    settle the question afterwards either: accounting storage is off (`AccountingStorageType = null`),
    so there is no `sacct` to ask once the job leaves squeue. The one moment the answer is known for
    certain is right here, so it is written down rather than inferred later.
    """
    d = _make_spawn_dir(job_id)
    (d / "cancelled").touch()


def was_cancelled(job_id: int) -> bool:
    return (_spawn_dir(job_id) / "cancelled").exists()


def swept(job_id: int) -> bool:
    """True when this Spawn's whole directory is gone — `retention._evict` took it.

    `submit` creates the directory for every Spawn before sbatch, so its absence is never "nothing was
    written yet". It is what lets `/logs` say the logs expired instead of returning the empty string a
    Spawn that printed nothing also returns."""
    return not _spawn_dir(job_id).exists()


def out_text(job_id: int) -> str | None:
    f = _spawn_dir(job_id) / "out"
    return f.read_text() if f.exists() else None


def err_text(job_id: int) -> str | None:
    """The Spawn's stderr — the traceback of a Spawn that died is here, not in `out`."""
    f = _spawn_dir(job_id) / "err"
    return f.read_text() if f.exists() else None


def outputs_tar(job_id: int):
    """gzip-tar of the Spawn's keep directory, for `cuttle outputs <id>` — as `(chunks, bytes)`, or
    None if it produced none.

    The tree inside mirrors the paths that were declared: `-o runs` arrives as `outputs/runs/…`, and an
    absolute `-o /ccc/vlwd/logs` as `outputs/ccc/vlwd/logs/…` (the leading `/` is what is dropped, and the
    only thing that is — see OutputPath).

    Emptiness — not existence — is the test: submit() pre-creates the dir for every Spawn, so its mere
    presence says nothing. An empty dir means "nothing written yet / nothing produced", not "ready".
    Asked here, before the generator is handed back, because the caller's 404 has to be decidable while
    it is still possible to send one (`tarstream.weigh`).

    That same walk returns what the tree weighs, which the route sends as `X-CCC-Bytes` so the client
    has a denominator. Read off the disk rather than the retention ledger: the ledger row is written by
    a 30s tick, so a User collecting results the moment their Spawn ended got no bar at all.
    """
    outputs = _spawn_dir(job_id) / "outputs"
    total = tarstream.weigh(outputs)
    if total is None:
        return None
    return tarstream.chunks(outputs, "outputs"), total
