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

Auth: every call carries the bearer token from `~/.ccc/credentials`; the Head URL comes from
~/.ccc/config. Every route needs it — reads were anonymous only while the SPA had no way to log in.
Transport is HTTPS, verified against the Head's certificate pinned on this machine (see tls.py).
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

from . import auth, settings, tls


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


def _get_bytes(path: str, missing_ok: bool = False) -> bytes | None:
    """Authed GET of a raw body (logs text / outputs tar). Reads used to be anonymous; the
    browser-login slice put them behind the session, so these carry the bearer like everything else.

    `missing_ok` returns None on a 404 instead of raising: a Spawn that wrote nothing to $CCC_OUT is a
    normal outcome, not a malformed command.
    """
    url = auth._head_url()
    req = urllib.request.Request(f"{url}/api{path}")  # /api: see auth._api
    req.add_header("Authorization", f"Bearer {auth._load_token()}")
    try:
        with urllib.request.urlopen(req, timeout=30, context=tls.context(url)) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if missing_ok and e.code == 404:
            return None
        detail = e.reason
        try:
            detail = json.loads(e.read()).get("detail", e.reason)
        except Exception:
            pass
        raise typer.BadParameter(f"{path}: {detail}") from None


def submit(script: str, gpu: int, follow: bool, args: list[str], minutes: int, vram: int,
           project: str | None = None, entity: str | None = None,
           no_gitignore: bool = False, node: str | None = None) -> None:
    root = Path.cwd()
    files = None if no_gitignore else _project_files(root)
    tar_bytes = _make_tar(root, files)
    # The Head turns `vram` into a Slurm constraint: it knows every distinct card in the fleet and we
    # don't, and Slurm's constraints are set-membership with no ">=" — so an arbitrary integer only
    # works if someone OR-expands it (`--constraint=vram24|vram48`). That someone is the Head.
    #
    # `node` pins the Spawn to one Arm and is ADMIN-ONLY — enforced at the Head, not here, because a
    # client cannot be trusted to police it. It validates the name against the live node list too.
    meta = {"script": script, "gpu": gpu, "args": args, "node": node,
            "minutes": minutes, "vram": vram,
            "wandb_env": settings.wandb_env(project, entity)}
    url = auth._head_url()
    req = urllib.request.Request(
        f"{url}/api/submit", data=tar_bytes, method="POST",
        headers={"Authorization": f"Bearer {auth._load_token()}",
                 "X-CCC-Meta": json.dumps(meta), "Content-Type": "application/gzip"})
    try:
        with urllib.request.urlopen(req, timeout=120, context=tls.context(url)) as r:
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
    data = _get_bytes(f"/spawns/{spawn_id}/outputs", missing_ok=True)
    if data is None:
        print(f"spawn {spawn_id} produced no outputs "
              f"(a Spawn returns files by writing them to $CCC_OUT inside the Pod)")
        return
    dest = Path.cwd() / f"spawn-{spawn_id}-outputs.tar.gz"
    dest.write_bytes(data)
    print(f"wrote {dest}")
