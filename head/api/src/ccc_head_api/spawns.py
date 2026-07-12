"""Server-side Spawn submission + inspection (Phase 3a slice 2).

This is the render + stage machinery that used to live in the CLI's `cuttle/spawns.py`. It moved here
when `cuttle run` stopped being Head-local: the CLI now tars its working dir and POSTs it, and the
Head API (running as the `ccc` service account) unpacks it, renders the sbatch script, submits via the
munge socket (munged authenticates ccc's local uid — no munge.key read), and records a ledger row.
Jobs therefore run as `ccc`, and everything a Spawn produces lands under HEAD_STATE_DIR/spawns/<id>/,
read back over the API — the submitter never touches the Head's disk.

Isolation (grill 2026-07-12c). A job NEVER runs in the served `[project]` tree — that would mutate the
copy the rsync daemon serves and leave a dirty tree for the next run. The `[ -d "$staging_dir" ]`
branch is "are the files already on this node?":
  * Head's co-located Arm — the <uuid> upload is on this FS. COPY it into node-local scratch, run
    there, and write out/err/exit/outputs STRAIGHT to the Spawn dir (same FS → `cuttle logs -f`
    streams live).
  * remote Arm — the upload isn't here. rsync-stage the project in from the Head's [project] module
    into ephemeral scratch, run, then stage out/err/exit/outputs BACK to the [spawns] module.
Either way: fresh copy in, `$CCC_OUT` out, source untouched.

Paths are fixed (defaults below match cuttle.config; this package never imports the CLI). Only the
Head's mesh IP is dynamic — the systemd unit injects it as CCC_MESH_IP so a remote Arm can rsync home.
"""

import io
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from uuid import uuid4

from . import db

HEAD_STATE_DIR = Path(os.environ.get("CCC_HEAD_STATE", "/var/lib/ccc/head"))
PROJECTS_DIR = HEAD_STATE_DIR / "projects"
SPAWNS_DIR = HEAD_STATE_DIR / "spawns"
SCRATCH_DIR = HEAD_STATE_DIR / "scratch"
HEAD_SIF = Path(os.environ.get("CCC_HEAD_SIF", "/opt/ccc/images/base.sif"))
MESH_IP = os.environ.get("CCC_MESH_IP", "")
# Mirror cuttle.config for the remote-Arm branch (resolved against the ARM's home, not the Head's).
SIF_REL = ".ccc/images/base.sif"
SCRATCH_REL = ".ccc/scratch"
PROJECT_MOD = "project"
SPAWNS_MOD = "spawns"

# %%NAME%% placeholders (not str.format) so bash's own ${...} needs no escaping. Wrapper stdout/stderr
# (rsync diagnostics) go to a node-local slurm log; the PAYLOAD's stdout/stderr are redirected to files
# we then stage/keep. --chdir=/tmp: the run dir doesn't exist until we make it.
JOB_SCRIPT = """\
#!/bin/bash
#SBATCH --job-name=ccc-spawn
#SBATCH --gres=gpu:%%GPU%%
#SBATCH --chdir=/tmp
#SBATCH --output=/tmp/ccc-%j.slurm
#SBATCH --error=/tmp/ccc-%j.slurm
jid=$SLURM_JOB_ID
staging_dir="%%STAGING_DIR%%"

if [ -d "$staging_dir" ]; then
    # Head node: the upload is already on this FS. Never run in the served [project] tree — COPY it
    # into node-local scratch, run there, and write results STRAIGHT to the Head Spawn dir (same FS,
    # so `cuttle logs -f` streams live).
    scratch="%%HEAD_SCRATCH%%/$jid"
    proj="$scratch/proj"
    trap 'rm -rf "$scratch"' EXIT
    mkdir -p "$proj"
    cp -a "$staging_dir/." "$proj/"
    sif="%%HEAD_SIF%%"
    spawn_dir="%%SPAWNS%%/$jid"
    mkdir -p "$spawn_dir/outputs"
    out="$spawn_dir/out"; err="$spawn_dir/err"; exitf="$spawn_dir/exit"; ccc_out="$spawn_dir/outputs"
    stage_out() { :; }  # already on the Head
else
    # remote Arm: none of these files are here. Stage the project in from the Head's rsync daemon into
    # ephemeral scratch, run, and stage out/err/exit/outputs back at the end.
    node_home=$(getent passwd "$(id -un)" | cut -d: -f6)
    sif="$node_home/%%SIF_REL%%"
    scratch="$node_home/%%SCRATCH_REL%%/$jid"
    proj="$scratch/%%UUID%%"
    trap 'rm -rf "$scratch"' EXIT
    mkdir -p "$scratch/outputs"
    # files-from paths are module-root-relative (<uuid>/...) and pulled from the MODULE ROOT — NOT a
    # subpath source, which makes rsyncd (use chroot = no) report every file "vanished". rsync
    # recreates <uuid>/... under $scratch, so $proj resolves.
    cat > "$scratch/filelist" <<'CCC_FILELIST'
%%FILELIST%%
CCC_FILELIST
    rsync -a --files-from="$scratch/filelist" \
        "rsync://%%HEAD_IP%%/%%PROJECT_MOD%%/" "$scratch/"
    out="$scratch/out"; err="$scratch/err"; exitf="$scratch/exit"; ccc_out="$scratch/outputs"
    stage_out() {
        # out/err/outputs first, then exit LAST — its arrival is the Head's "done" marker.
        rsync -a "$out" "$err" "$ccc_out" "rsync://%%HEAD_IP%%/%%SPAWNS_MOD%%/$jid/"
        rsync -a "$exitf" "rsync://%%HEAD_IP%%/%%SPAWNS_MOD%%/$jid/"
    }
fi

cd "$proj"
# Build the Pod env list. WANDB_ENTITY is appended only when set — an empty entity makes wandb.init
# fail rather than fall back to the account default.
wenv="WANDB_API_KEY=${WANDB_API_KEY:-},WANDB_PROJECT=${WANDB_PROJECT:-},WANDB_MODE=${WANDB_MODE:-},WANDB_NAME=spawn-$jid"
[ -n "${WANDB_ENTITY:-}" ] && wenv="$wenv,WANDB_ENTITY=$WANDB_ENTITY"
# --bind the outputs dir: Apptainer auto-mounts only $HOME/tmp/cwd, and it now lives under
# /var/lib/ccc (ccc-owned Head state), so $CCC_OUT is invisible in the Pod without an explicit bind.
apptainer exec --nv --bind "$ccc_out" --env "$wenv,CCC_OUT=$ccc_out" \
  "$sif" python %%SCRIPT%% %%ARGS%% > "$out" 2> "$err"
rc=$?
echo $rc > "$exitf"
stage_out
exit $rc
"""


def _render(uuid: str, staging: Path, gpu: int, script: str, args: list[str],
            files: list[str]) -> str:
    subs = {
        "%%GPU%%": str(gpu),
        "%%STAGING_DIR%%": str(staging),
        "%%HEAD_SCRATCH%%": str(SCRATCH_DIR),
        "%%HEAD_SIF%%": str(HEAD_SIF),
        "%%SPAWNS%%": str(SPAWNS_DIR),
        "%%SIF_REL%%": SIF_REL,
        "%%SCRATCH_REL%%": SCRATCH_REL,
        "%%UUID%%": uuid,
        "%%HEAD_IP%%": MESH_IP,
        "%%PROJECT_MOD%%": PROJECT_MOD,
        "%%SPAWNS_MOD%%": SPAWNS_MOD,
        "%%FILELIST%%": "\n".join(files),
        "%%SCRIPT%%": script,
        "%%ARGS%%": " ".join(args),
    }
    body = JOB_SCRIPT
    for k, v in subs.items():
        body = body.replace(k, v)
    return body


def submit(user_id: int, script: str, gpu: int, args: list[str], wandb_env: dict[str, str],
           tar_bytes: bytes) -> int:
    """Unpack an uploaded project, render + sbatch it as ccc, and record the attribution row. Returns
    the Slurm job id (== the Spawn id)."""
    uuid = uuid4().hex
    staging = PROJECTS_DIR / uuid
    staging.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        tf.extractall(staging, filter="data")  # 'data' filter blocks path-traversal entries
    # Module-root-relative list (<uuid>/...) for a remote Arm's --files-from. The upload was already
    # gitignore-filtered client-side, so every file here is meant to transfer.
    files = [p.relative_to(PROJECTS_DIR).as_posix() for p in staging.rglob("*") if p.is_file()]

    body = _render(uuid, staging, gpu, script, args, files)
    with tempfile.NamedTemporaryFile("w", suffix=".sbatch", delete=False) as f:
        f.write(body)
        job_file = f.name

    env = {**os.environ, **wandb_env}
    r = subprocess.run(["sbatch", "--parsable", job_file],
                       capture_output=True, text=True, check=True, env=env)
    job_id = int(r.stdout.strip())
    with db.connect() as conn:
        db.record_spawn(conn, job_id, user_id, script, uuid)
    return job_id


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


def out_text(job_id: int) -> str | None:
    f = _spawn_dir(job_id) / "out"
    return f.read_text() if f.exists() else None


def outputs_tar(job_id: int) -> bytes | None:
    """gzip-tar of the Spawn's $CCC_OUT dir, for `cuttle outputs <id>`. None if it produced none."""
    outputs = _spawn_dir(job_id) / "outputs"
    if not outputs.is_dir():
        return None
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(outputs, arcname="outputs")
    return buf.getvalue()
