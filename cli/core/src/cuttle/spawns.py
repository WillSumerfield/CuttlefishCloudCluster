"""Spawn submission + inspection: `cuttle run` / `status` / `logs`.

A Spawn = one sbatch job wrapping `apptainer exec --nv base.sif python <script>`. The Slurm job id
IS the spawn id. Slurm writes stdout/stderr to ~/.ccc/spawns/<id>.{out,err}; the job wrapper writes
the exit code to <id>.exit so status survives scontrol forgetting the job (no slurmdbd).
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path

from . import config, settings

# W&B env is injected into the job process; the wrapper forwards it into the Pod via --env.
#
# The image is resolved on the NODE, not baked in from the submitter. sbatch exports the submitter's
# environment (--export=ALL), so $HOME on a remote Arm is the *Head user's* home, which doesn't exist
# there. getent asks the node's own passwd for the job user's home. (Until ④ gives us shared storage,
# every Arm holds its own copy of base.sif, built by `cuttle setup --arm`.)
JOB_SCRIPT = """\
#!/bin/bash
#SBATCH --job-name=ccc-spawn
#SBATCH --gres=gpu:{gpu}
#SBATCH --output={spawns}/%j.out
#SBATCH --error={spawns}/%j.err
node_home=$(getent passwd "$(id -un)" | cut -d: -f6)
sif="$node_home/{sif_rel}"
apptainer exec --nv \
  --env WANDB_API_KEY="${{WANDB_API_KEY:-}}",WANDB_PROJECT="${{WANDB_PROJECT:-}}",\
WANDB_MODE="${{WANDB_MODE:-}}",WANDB_NAME="spawn-$SLURM_JOB_ID" \
  "$sif" python {script} {args}
rc=$?
echo $rc > {spawns}/$SLURM_JOB_ID.exit
exit $rc
"""


def _out(spawn_id: int) -> Path:
    return config.SPAWNS_DIR / f"{spawn_id}.out"


def _exit_file(spawn_id: int) -> Path:
    return config.SPAWNS_DIR / f"{spawn_id}.exit"


def _active_state(spawn_id: int) -> str | None:
    """Slurm state if the job is still known to squeue, else None."""
    r = subprocess.run(["squeue", "-h", "-j", str(spawn_id), "-o", "%T"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def submit(script: str, gpu: int, follow: bool, args: list[str]) -> None:
    config.SPAWNS_DIR.mkdir(parents=True, exist_ok=True)
    body = JOB_SCRIPT.format(
        gpu=gpu, spawns=config.SPAWNS_DIR,
        sif_rel=config.BASE_SIF.relative_to(Path.home()),  # resolved against the node's own $HOME
        script=script, args=" ".join(args),
    )
    with tempfile.NamedTemporaryFile("w", suffix=".sbatch", delete=False) as f:
        f.write(body)
        job_file = f.name

    # W&B vars go into sbatch's env; Slurm's default --export=ALL forwards them to the job.
    env = {**os.environ, **settings.wandb_env()}
    r = subprocess.run(["sbatch", "--parsable", job_file],
                       capture_output=True, text=True, check=True, env=env)
    spawn_id = int(r.stdout.strip())
    print(f"submitted spawn {spawn_id}")
    if follow:
        _follow(spawn_id)


def _follow(spawn_id: int) -> None:
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
