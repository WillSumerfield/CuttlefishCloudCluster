"""Spawn submission + inspection: `cuttle run` / `status` / `logs` / `outputs`.

A Spawn = one sbatch job wrapping `apptainer exec --nv base.sif python <script>`. The Slurm job id
IS the spawn id. As of Phase 3a slice 2 `cuttle run` is no longer Head-local: it tars the working dir
and POSTs it to the Head API (authenticated), which unpacks, renders, and submits it as the `ccc`
service account, recording who submitted it. So this module is now a thin API client — the render +
stage machinery lives server-side in `ccc_head_api.spawns`.

  * upload    — a gzip-tar of the working dir. Gitignore-aware by default (skip .git, venvs, data,
                checkpoints); `--no-gitignore` uploads the whole tree.
  * results   — out/err/exit/outputs live on the Head under ccc; read them back over the API
                (`status`/`logs`/`outputs`), never off local disk. A remote submitter who never ran
                setup gets results the same way — `cuttle outputs <id>` downloads the $CCC_OUT tar.

Auth: every call carries the bearer token from `~/.ccc/login`; the Head URL comes from ~/.ccc/config.
Reads (`status`/`logs`/`outputs`) are anonymous in slice 2, but submission requires a login.
"""

import io
import json
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import typer

from . import auth, settings


def _project_files(root: Path) -> list[str]:
    """The gitignore-aware transfer set, computed where the repo lives. git itself is the only thing
    that gets gitignore semantics exactly right — tracked plus untracked-but-not-ignored. A non-git
    dir falls back to every file under it."""
    git = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True)
    if git.returncode == 0:
        return [ln for ln in git.stdout.splitlines() if ln]
    return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]


def _make_tar(root: Path, files: list[str] | None) -> bytes:
    """gzip-tar the project. `files` = the gitignore-aware list; None = the whole tree (--no-gitignore)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        if files is None:
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    tf.add(p, arcname=p.relative_to(root).as_posix())
        else:
            for f in files:
                p = root / f
                if p.is_file():
                    tf.add(p, arcname=f)
    return buf.getvalue()


def _get_bytes(path: str) -> bytes:
    """Anonymous GET of a raw body (logs text / outputs tar)."""
    req = urllib.request.Request(f"{auth._head_url()}{path}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = e.reason
        try:
            detail = json.loads(e.read()).get("detail", e.reason)
        except Exception:
            pass
        raise typer.BadParameter(f"{path}: {detail}") from None


def submit(script: str, gpu: int, follow: bool, args: list[str],
           project: str | None = None, entity: str | None = None,
           no_gitignore: bool = False, node: str | None = None) -> None:
    root = Path.cwd()
    files = None if no_gitignore else _project_files(root)
    tar_bytes = _make_tar(root, files)
    # `node` pins the Spawn to one Arm; omitted, Slurm schedules it. The Head validates the name
    # against the live node list (it knows the Arms; we don't) and 422s with the known names.
    meta = {"script": script, "gpu": gpu, "args": args, "node": node,
            "wandb_env": settings.wandb_env(project, entity)}
    req = urllib.request.Request(
        f"{auth._head_url()}/submit", data=tar_bytes, method="POST",
        headers={"Authorization": f"Bearer {auth._load_token()}",
                 "X-CCC-Meta": json.dumps(meta), "Content-Type": "application/gzip"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", e.reason)
        except Exception:
            detail = e.reason
        raise typer.BadParameter(f"/submit: {detail}") from None
    spawn_id = resp["id"]
    print(f"submitted spawn {spawn_id}")
    if follow:
        _follow(spawn_id)


def _outcome(st: dict) -> str:
    """A finished Spawn's verdict. `failed` carries no exit code by definition — it's the state of a
    Spawn that never recorded one (scancel'd, OOM-killed, or its Arm vanished before stage-out), so
    don't reach for st['exit'] there."""
    if st["state"] == "failed":
        return "FAILED (killed or its Arm dropped — no exit code recorded)"
    return f"done (exit {st['exit']})"


def _follow(spawn_id: int) -> None:
    """Stream stdout until the Spawn ends. Local/co-located Spawns stream live (out is written on the
    Head as the job runs); a remote Spawn's out only arrives at stage-out (post-hoc)."""
    pos = 0
    while True:
        text = _get_bytes(f"/spawns/{spawn_id}/logs").decode()
        if len(text) > pos:
            print(text[pos:], end="", flush=True)
            pos = len(text)
        st = auth._api("GET", f"/spawns/{spawn_id}")
        if st.get("done"):
            text = _get_bytes(f"/spawns/{spawn_id}/logs").decode()
            if len(text) > pos:
                print(text[pos:], end="", flush=True)
            print(f"\nspawn {spawn_id} {_outcome(st)}")
            return
        time.sleep(0.5)


def status(spawn_id: int) -> None:
    st = auth._api("GET", f"/spawns/{spawn_id}")
    print(_outcome(st).upper() if st.get("done") else st["state"])


def logs(spawn_id: int, follow: bool) -> None:
    if follow:
        _follow(spawn_id)
    else:
        print(_get_bytes(f"/spawns/{spawn_id}/logs").decode(), end="")


def outputs(spawn_id: int) -> None:
    """Download a Spawn's $CCC_OUT dir as a tar into the cwd."""
    data = _get_bytes(f"/spawns/{spawn_id}/outputs")
    dest = Path.cwd() / f"spawn-{spawn_id}-outputs.tar.gz"
    dest.write_bytes(data)
    print(f"wrote {dest}")
