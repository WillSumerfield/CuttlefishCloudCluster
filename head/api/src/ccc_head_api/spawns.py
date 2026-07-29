"""Server-side Spawn submission + inspection (Phase 3a slice 2).

This is the render + stage machinery that used to live in the CLI's `cuttle/spawns.py`. It moved here
when `cuttle run` stopped being Head-local: the CLI now tars its working dir and POSTs it, and the
Head API (running as the **Mantle**, the `ccc` account) unpacks it, renders the sbatch script, submits
via the munge socket (munged authenticates the Mantle's local uid — no munge.key read), and records an
Attribution row. Everything a Spawn produces lands under HEAD_STATE_DIR/spawns/<id>/ and is read back
over the API — the submitter never touches the Head's disk.

Every Spawn runs as the Mantle on EVERY node, under one uid pinned cluster-wide (cuttle.config
CCC_UID). That is what makes a remote Arm work at all: slurmd setuid()s to the submitting uid, so the
account must exist, identically, wherever Slurm places the Spawn. The Mantle is home-less by design —
it owns no per-node state — so nothing here is resolved against a home directory: the Pod image is at
one absolute path on every node (CCC_SIF) and a remote Arm scratches in /tmp.

Isolation (grill 2026-07-12c). A job NEVER runs in the served `[project]` tree — that would mutate the
copy the rsync daemon serves and leave a dirty tree for the next run. The `[ -d "$staging_dir" ]`
branch is "are the files already on this node?":
  * Head's co-located Arm — the <uuid> upload is on this FS. COPY it into node-local scratch, run
    there, and write out/err/exit/outputs STRAIGHT to the Spawn dir (same FS → `cuttle logs -f`
    streams live).
  * remote Arm — the upload isn't here. rsync-stage the project in from the Head's [project] module
    into ephemeral /tmp scratch, run, then stage out/err/exit/outputs BACK to the [spawns] module.
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
# The Pod image, at the SAME path on every node (each builds its own at setup). One constant, so the
# rendered script names the sif identically wherever Slurm places the Spawn.
CCC_SIF = Path(os.environ.get("CCC_SIF", "/opt/ccc/images/base.sif"))
MESH_IP = os.environ.get("CCC_MESH_IP", "")
PROJECT_MOD = "project"
SPAWNS_MOD = "spawns"

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
#SBATCH --output=/tmp/ccc-%j.slurm
#SBATCH --error=/tmp/ccc-%j.slurm
%%CONSTRAINT%%
%%NODELIST%%
%%EXCLUDE%%
jid=$SLURM_JOB_ID
staging_dir="%%STAGING_DIR%%"
sif="%%CCC_SIF%%"
# sbatch's own stdout sink, on whichever node runs this. The wrapper redirects everything it cares
# about into out/err, so this stays EMPTY unless the shell itself failed before those existed — which
# is the one case worth keeping. Anything else would leave a 0-byte file per Spawn in /tmp forever.
sink="/tmp/ccc-$jid.slurm"
cleanup() { rm -rf "$scratch"; [ -s "$sink" ] || rm -f "$sink"; }

if [ -d "$staging_dir" ]; then
    # Head node: the upload is already on this FS. Never run in the served [project] tree — COPY it
    # into node-local scratch, run there, and write results STRAIGHT to the Head Spawn dir (same FS,
    # so `cuttle logs -f` streams live). The Spawn dir was pre-created by the API at submit.
    scratch="%%HEAD_SCRATCH%%/$jid"
    proj="$scratch/proj"
    trap cleanup EXIT
    mkdir -p "$proj"
    cp -a "$staging_dir/." "$proj/"
    spawn_dir="%%SPAWNS%%/$jid"
    out="$spawn_dir/out"; err="$spawn_dir/err"; exitf="$spawn_dir/exit"; ccc_out="$spawn_dir/outputs"
    stage_out() { :; }  # already on the Head
else
    # remote Arm: none of these files are here. Stage the project in from the Head's rsync daemon into
    # ephemeral scratch, run, and stage out/err/exit/outputs back at the end.
    #
    # Scratch is /tmp, NOT the Mantle's home: the Mantle is home-less by design (one pinned uid,
    # cluster-wide, owning no per-node state), so a donor box is left with nothing after the trap
    # fires. Apptainer auto-mounts /tmp, so the Pod sees it with no extra bind.
    scratch="/tmp/ccc-$jid"
    proj="$scratch/%%UUID%%"
    trap cleanup EXIT
    mkdir -p "$scratch/outputs"
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
        rsync -a "$out" "$err" "$ccc_out" "rsync://%%HEAD_IP%%/%%SPAWNS_MOD%%/$jid/" || return 1
        rsync -a "$exitf" "rsync://%%HEAD_IP%%/%%SPAWNS_MOD%%/$jid/"
    }
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
            "rsync://%%HEAD_IP%%/%%PROJECT_MOD%%/" "$scratch/" 2>>"$err"; then
        echo "ccc: could not stage the project in from rsync://%%HEAD_IP%%/%%PROJECT_MOD%%/ —" >> "$err"
        echo "ccc: this Spawn never started. Is the Head's rsync daemon up, and is this Arm on the mesh?" >> "$err"
        echo 1 > "$exitf"
        stage_out
        exit 1
    fi
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
# A failed stage-out is indistinguishable, from the Head, from an Arm that dropped mid-Spawn: no exit
# file ever lands, so the Spawn reads as "killed". Say so in the node-local slurm log, which is the
# only place left that can still be written.
stage_out || echo "ccc: staging results back to rsync://%%HEAD_IP%%/%%SPAWNS_MOD%%/$jid/ FAILED —" \
                  "the Spawn ran (rc=$rc) but the Head will report it as lost." >&2
exit $rc
"""


def _render(uuid: str, staging: Path, gpu: int, script: str, args: list[str],
            files: list[str], node: str | None, minutes: int, constraint: str,
            exclude: str = "") -> str:
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
        "%%HEAD_SCRATCH%%": str(SCRATCH_DIR),
        "%%CCC_SIF%%": str(CCC_SIF),
        "%%SPAWNS%%": str(SPAWNS_DIR),
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
           tar_path: Path, minutes: int, constraint: str, node: str | None = None,
           vram_gb: int = 0, hold: float | None = None, exclude: str = "") -> int:
    """Unpack an uploaded project, render + sbatch it as the Mantle, and record the Attribution row +
    the Karma Escrow. Returns the Slurm job id (== the Spawn id). `node` pins the Spawn to one Arm.

    `hold` is the Escrow quoted by the caller, or **None** for a Spawn nobody is charged for (an
    Admin's — the Admin is outside the economy). It is written in the SAME transaction as the
    Attribution row: a Spawn that existed without its hold, even briefly, would be one its submitter's
    Share had not yet been charged for, which is precisely the window a queue-flooder wants.

    `tar_path` is a spooled file, not bytes: the caller streams the upload to disk so a large project
    never sits in this process's memory (see main._spool_upload). tarfile reads it incrementally.
    """
    # Refuse rather than render `rsync:///project/`. An empty mesh IP is a Head that does not know its
    # own address — harmless for a Head-local Spawn, fatal for one Slurm happens to place on a remote
    # Arm, and only discoverable minutes later, on another machine, as a Spawn that "vanished". We
    # cannot know where it will land, so no Spawn goes out at all.
    if not MESH_IP:
        raise ValueError("the Head does not know its own mesh address (CCC_MESH_IP is empty in the "
                         "ccc-head-api unit) — a remote Arm would have nowhere to fetch this Spawn's "
                         "files from. Re-run `cuttle setup --head` on the Head to rewrite the unit")
    uuid = uuid4().hex
    staging = PROJECTS_DIR / uuid
    staging.mkdir(parents=True)
    with tarfile.open(tar_path, mode="r:gz") as tf:
        tf.extractall(staging, filter="data")  # 'data' filter blocks path-traversal entries
    # Module-root-relative list (<uuid>/...) for a remote Arm's --files-from. The upload was already
    # gitignore-filtered client-side, so every file here is meant to transfer.
    files = [p.relative_to(PROJECTS_DIR).as_posix() for p in staging.rglob("*") if p.is_file()]

    body = _render(uuid, staging, gpu, script, args, files, node, minutes, constraint, exclude)
    # delete=False so `sbatch` — a separate process — can open the path; we unlink it ourselves the
    # moment sbatch returns. sbatch has copied the script into slurmctld's state by then, so the file
    # has no reader left. Leaving them behind accumulates one per Spawn, each carrying the project's
    # full file list, under an account nobody logs in as to clean up.
    with tempfile.NamedTemporaryFile("w", suffix=".sbatch", delete=False) as f:
        f.write(body)
        job_file = f.name

    env = {**os.environ, **wandb_env}
    try:
        r = subprocess.run(["sbatch", "--parsable", job_file],
                           capture_output=True, text=True, check=True, env=env)
    finally:
        Path(job_file).unlink(missing_ok=True)
    job_id = int(r.stdout.strip())
    # Create the Spawn dir for EVERY Spawn, not just Head-local ones: a remote Arm stages its results
    # into rsync://<head>/spawns/<jid>/, and rsync will not invent that directory. Safe to do after
    # sbatch — the job cannot start before sbatch has returned its id.
    (SPAWNS_DIR / str(job_id) / "outputs").mkdir(parents=True, exist_ok=True)
    with db.connect() as conn:
        db.record_spawn(conn, job_id, user_id, script, uuid, minutes=minutes, gpus=gpu,
                        vram_gb=vram_gb, node=node, hold=hold)
    return job_id


def submit_checked(user_id: int, is_admin: bool, script: str, gpu: int, args: list[str],
                   wandb_env: dict[str, str], tar_path: Path, minutes: int, vram_gb: int,
                   node: str | None = None) -> int:
    """Validate placement, quote the Escrow, and submit — everything `/submit` does past auth.

    Extracted in Phase 6, when pinning a Spawn to an Arm (`node`) stopped being reachable over HTTP and
    became a Head-local command. Both callers come through here so a pinned Spawn is priced and
    recorded **exactly** like any other. The alternative — letting the Head-local path shell out to
    `sbatch` itself — would produce a Spawn with no Escrow and no Attribution row: one that consumes a
    donated card while nobody pays for it, and that no ledger can explain.

    Raises ValueError for an unknown Arm, a retracted one, or for a User already at the
    concurrent-Spawn cap; the callers translate (HTTP 422 / a CLI error).
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
    need = {"node": node, "vram_gb": vram_gb, "gpus": gpu}
    with db.connect() as conn:
        hold = karma.quote_hold(conn, arms, need, minutes, gpu, is_admin)
    return submit(user_id, script, gpu, args, wandb_env, tar_path, minutes, constraint, node,
                  vram_gb=vram_gb, hold=hold, exclude=exclude)


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
