"""Spawn submission + inspection: `cuttle run` / `status` / `logs`.

A Spawn = one sbatch job wrapping `apptainer exec --nv base.sif python <script>`. The Slurm job id
IS the spawn id. Everything a Spawn produces lands on the Head under ~/.ccc/spawns/<id>/:
  out / err   — the payload's stdout / stderr
  exit        — its exit code (written last; its presence == "done", survives scontrol forgetting)
  outputs/    — whatever the job wrote to $CCC_OUT

Stage-in/out (④). `cuttle run` executes on the Head, but the job runs on whatever node Slurm picks:

  * Head's co-located Arm — the submit dir is already here. The script runs IN PLACE and writes
    results straight into ~/.ccc/spawns/<id>/ (zero-copy, no daemon needed).
  * remote Arm — the submit dir doesn't exist there. The script stages the project IN from the
    Head's rsync daemon (mesh-bound; auth == mesh membership) into an ephemeral node-local scratch
    dir, runs, then stages out/err/exit/outputs BACK to the daemon's [spawns] module. Scratch is
    wiped on exit (trap). No persistent cache — each Spawn copies fresh (don't hog donor disk).

The branch is `[ -d "$submit_dir" ]`: do I already have the files? — the actual thing that matters,
not a guess about node identity. Both branches produce the identical Head-side layout, so status,
logs, and a future SPA treat every Spawn the same regardless of where it ran.

Results are collected from $CCC_OUT only (injected into the Pod). Write checkpoints/artifacts there;
it points at the per-Spawn outputs dir in both modes.
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path

import typer

from . import config, settings

# %%NAME%% placeholders (not str.format) so bash's own ${...} needs no escaping. The image is
# resolved on the NODE via getent (sbatch --export=ALL carries the submitter's $HOME, which is wrong
# on a remote Arm). --chdir=/tmp: the submit dir may not exist on the node, so don't make slurmd try
# to cd there before we do. Wrapper stdout/stderr (rsync diagnostics) go to a node-local slurm log;
# the PAYLOAD's stdout/stderr are redirected explicitly to files we then stage home.
JOB_SCRIPT = """\
#!/bin/bash
#SBATCH --job-name=ccc-spawn
#SBATCH --gres=gpu:%%GPU%%
#SBATCH --chdir=/tmp
#SBATCH --output=/tmp/ccc-%j.slurm
#SBATCH --error=/tmp/ccc-%j.slurm
jid=$SLURM_JOB_ID
submit_dir="%%SUBMIT_DIR%%"
node_home=$(getent passwd "$(id -un)" | cut -d: -f6)
sif="$node_home/%%SIF_REL%%"

if [ -d "$submit_dir" ]; then
    # local (Head): files already here — run in place, results straight to the Spawn dir.
    proj="$submit_dir"
    spawn_dir="%%SPAWNS%%/$jid"
    mkdir -p "$spawn_dir/outputs"
    out="$spawn_dir/out"; err="$spawn_dir/err"; exitf="$spawn_dir/exit"
    ccc_out="$spawn_dir/outputs"
    stage_out() { :; }  # already on the Head
else
    # remote Arm: stage in from the Head's rsync daemon, run in ephemeral scratch, stage out.
    scratch="$node_home/%%SCRATCH_REL%%/$jid"
    proj="$scratch/%%PROJ_REL%%"
    trap 'rm -rf "$scratch"' EXIT
    mkdir -p "$proj" "$scratch/outputs"
    # files-from paths are home-relative (prefixed with the project's path under $HOME) and pulled
    # from the MODULE ROOT — NOT a subpath source, which makes rsyncd (use chroot = no) report every
    # file "vanished". rsync recreates the project's dir structure under $scratch, so proj resolves.
    cat > "$scratch/filelist" <<'CCC_FILELIST'
%%FILELIST%%
CCC_FILELIST
    rsync -a --files-from="$scratch/filelist" \
        "rsync://%%HEAD_IP%%/%%PROJECT_MOD%%/" "$scratch/"
    out="$scratch/out"; err="$scratch/err"; exitf="$scratch/exit"
    ccc_out="$scratch/outputs"
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
apptainer exec --nv --env "$wenv,CCC_OUT=$ccc_out" \
  "$sif" python %%SCRIPT%% %%ARGS%% > "$out" 2> "$err"
rc=$?
echo $rc > "$exitf"
stage_out
exit $rc
"""


def _spawn_dir(spawn_id: int) -> Path:
    return config.SPAWNS_DIR / str(spawn_id)


def _out(spawn_id: int) -> Path:
    return _spawn_dir(spawn_id) / "out"


def _exit_file(spawn_id: int) -> Path:
    return _spawn_dir(spawn_id) / "exit"


def _active_state(spawn_id: int) -> str | None:
    """Slurm state if the job is still known to squeue, else None."""
    r = subprocess.run(["squeue", "-h", "-j", str(spawn_id), "-o", "%T"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def _project_files(root: Path) -> list[str]:
    """The gitignore-aware transfer set, computed on the Head where the repo lives. git itself is the
    only thing that gets gitignore semantics exactly right — tracked plus untracked-but-not-ignored.
    A non-git dir falls back to every file under it."""
    git = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True)
    if git.returncode == 0:
        return [ln for ln in git.stdout.splitlines() if ln]
    return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]


def _render_script(gpu: int, submit_dir: Path, proj_rel: Path, head_ip: str,
                   files: list[str], script: str, args: list[str]) -> str:
    subs = {
        "%%GPU%%": str(gpu),
        "%%SUBMIT_DIR%%": str(submit_dir),
        "%%SIF_REL%%": str(config.BASE_SIF.relative_to(Path.home())),
        "%%SPAWNS%%": str(config.SPAWNS_DIR),
        "%%SCRATCH_REL%%": config.SCRATCH_REL,
        "%%HEAD_IP%%": head_ip,
        "%%PROJECT_MOD%%": config.RSYNC_MODULE_PROJECT,
        "%%SPAWNS_MOD%%": config.RSYNC_MODULE_SPAWNS,
        "%%PROJ_REL%%": proj_rel.as_posix(),
        # Home-relative so they resolve against the [project] module root (rooted at $HOME).
        "%%FILELIST%%": "\n".join(f"{proj_rel.as_posix()}/{f}" for f in files),
        "%%SCRIPT%%": script,
        "%%ARGS%%": " ".join(args),
    }
    body = JOB_SCRIPT
    for k, v in subs.items():
        body = body.replace(k, v)
    return body


def submit(script: str, gpu: int, follow: bool, args: list[str],
           project: str | None = None, entity: str | None = None) -> None:
    project_root = Path.cwd()
    try:
        proj_rel = project_root.relative_to(Path.home())
    except ValueError:
        raise typer.BadParameter(
            f"a project must live under your home dir ({Path.home()}) so the Head can stage it to a "
            f"remote Arm; cwd is {project_root}")
    mesh = settings.load().get("mesh", {})
    head_ip = mesh.get("mesh_ip")
    if not head_ip:
        raise typer.BadParameter("no Head mesh IP in config — run `cuttle setup --head` first")

    config.SPAWNS_DIR.mkdir(parents=True, exist_ok=True)
    body = _render_script(gpu, project_root, proj_rel, head_ip,
                          _project_files(project_root), script, args)
    with tempfile.NamedTemporaryFile("w", suffix=".sbatch", delete=False) as f:
        f.write(body)
        job_file = f.name

    # W&B vars go into sbatch's env; Slurm's default --export=ALL forwards them to the job.
    env = {**os.environ, **settings.wandb_env(project, entity)}
    r = subprocess.run(["sbatch", "--parsable", job_file],
                       capture_output=True, text=True, check=True, env=env)
    spawn_id = int(r.stdout.strip())
    print(f"submitted spawn {spawn_id}")
    if follow:
        _follow(spawn_id)


def _follow(spawn_id: int) -> None:
    """Stream stdout until the Spawn ends. Local Spawns stream live (the file is written in place);
    a remote Spawn's `out` only arrives when it stages out at the end (post-hoc — see docs/TODO.md)."""
    out = _out(spawn_id)
    pos = 0
    while True:
        if out.exists():
            data = out.read_text()
            if len(data) > pos:
                print(data[pos:], end="", flush=True)
                pos = len(data)
        if _exit_file(spawn_id).exists() and _active_state(spawn_id) is None:
            break
        time.sleep(0.5)
    if out.exists():
        data = out.read_text()
        if len(data) > pos:
            print(data[pos:], end="", flush=True)
    code = _exit_file(spawn_id).read_text().strip()
    print(f"\nspawn {spawn_id} done (exit {code})")


def status(spawn_id: int) -> None:
    state = _active_state(spawn_id)
    if state:
        print(state)
    elif _exit_file(spawn_id).exists():
        print(f"DONE exit {_exit_file(spawn_id).read_text().strip()}")
    else:
        print("UNKNOWN (not queued; no exit record)")


def logs(spawn_id: int, follow: bool) -> None:
    out = _out(spawn_id)
    if follow:
        subprocess.run(["tail", "-n", "+1", "-f", str(out)])  # Ctrl-C to stop
    elif out.exists():
        print(out.read_text(), end="")
    else:
        print(f"no logs yet for spawn {spawn_id}")
