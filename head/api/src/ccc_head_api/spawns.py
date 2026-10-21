"""Spawn inspection: Slurm state, the exit/out/err files, and the cancelled marker.

Submission is gone in 2.0 until Dives replace it; what is left reads back a Spawn the Head already
knows. Everything a Spawn produces lands under HEAD_STATE_DIR/spawns/<id>/ and is read back over the
API — the submitter never touches the Head's disk.

Paths are fixed (defaults below match cuttle.config; this package never imports the CLI).
"""

import os
import subprocess
from pathlib import Path


HEAD_STATE_DIR = Path(os.environ.get("CCC_HEAD_STATE", "/var/lib/ccc/head"))
SPAWNS_DIR = HEAD_STATE_DIR / "spawns"
SCRATCH_DIR = HEAD_STATE_DIR / "scratch"


# Where a Spawn's working directory ends up ON THE NODE, which is also where it is bound INTO the Pod
# — so this is the folder a VS Code remote session opens, and the folder `scp` puts files into. Two
# forms, because a Spawn's scratch differs between the Head's co-located Arm (under the Head's state
# dir) and a donor's box (/tmp); the Slurm epilog (cuttle.templates) reaps the same two.
def pod_project_dir(job_id: int, head_arm: bool) -> str:
    scratch = f"{SCRATCH_DIR}/{job_id}" if head_arm else f"/tmp/ccc-{job_id}"
    return f"{scratch}/proj"


# --- reads ---------------------------------------------------------------------

def _spawn_dir(job_id: int) -> Path:
    return SPAWNS_DIR / str(job_id)


# What the Slurm prolog sets on a Spawn dir it lends to a **Guise** (templates.PROLOG_TMPL): rwx for the
# owner and for the Mantle's group, nothing for anyone else. Matched here so the API's own creation is
# not looser than the hook that may later overwrite it.
SPAWN_DIR_MODE = 0o770


def _make_spawn_dir(job_id: int) -> Path:
    """The Spawn's directory, at SPAWN_DIR_MODE rather than the service's ambient umask.

    Explicit because the prolog only covers the Head's own co-located Arm — `if [ -d "$head_spawns" ]`
    is false on a donor box — so for a REMOTE Spawn nothing else ever sets this mode, and the directory
    keeps whatever umask the API service happened to run with, for good. A Spawn dir is named by a
    sequential job id, and the 0711 parent grants exactly the traversal that makes a guessed name
    enough to read someone else's `out` and `err`.
    """
    d = _spawn_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    try:
        # chmod, not mkdir(mode=): mkdir's mode is masked by the umask, which is the whole problem.
        d.chmod(SPAWN_DIR_MODE)
    except PermissionError:
        # The prolog has lent this directory to the **Guise** and we are the Mantle, so we no
        # longer own it — which only happens once the job is RUNNING, and `mark_cancelled` runs
        # exactly then. Nothing to do: the prolog set this same mode when it took it.
        pass
    return d


def active_state(job_id: int) -> str | None:
    """Slurm state if the job is still known to squeue, else None. Slurm's answer, unedited."""
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
    d = _make_spawn_dir(job_id)
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
