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
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from . import api, auth, settings, ui
from .system import run


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
    client = auth._client()
    client.token = auth._load_token()
    try:
        return client.raw(path, missing_ok=missing_ok)
    except api.ApiError as e:
        if ui.DEBUG:
            raise
        raise ui.abort(e.user_message()) from None


def _submit_pinned(tar_bytes: bytes, script: str, args: list[str], minutes: int, vram: int,
                   gpu: int, node: str) -> int:
    """Submit a Spawn pinned to a named Arm, on the Head, as the Mantle.

    Pinning is an admin write, and since Phase 6 the Head API has none — so this does not travel over
    HTTP at all. Authorization is *being the operator on this box*; attribution is whoever is logged
    in here. It shells into the API venv rather than reimplementing submission, so the Escrow and the
    Attribution row are written by the same code that serves `/submit` (see spawns.submit_checked).
    """
    me = auth._api("GET", "/me")["username"]
    # The tar has to be readable by the `ccc` account the admin module runs as, so it goes to a
    # world-readable temp file rather than anywhere under the operator's 0700 home.
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
        f.write(tar_bytes)
        tar_path = f.name
    Path(tar_path).chmod(0o644)
    try:
        r = run(auth._admin_argv([
            "submit", "--user", me, "--script", script, "--tar", tar_path,
            "--minutes", str(minutes), "--vram", str(vram), "--gpu", str(gpu),
            "--node", node, *args]), capture_output=True, text=True, check=False)
    finally:
        Path(tar_path).unlink(missing_ok=True)
    if r.returncode != 0:
        # A pinned submit failing is runtime state (the Arm is gone, down, or not yours), not a usage
        # error — so ui.abort, not typer.BadParameter's "Try --help" box. Surface the admin tool's
        # last line as the cause and name the check that resolves it.
        why = (r.stderr or "").strip()
        detail = why.splitlines()[-1] if why else "the pinned submit failed"
        raise ui.abort(f"couldn't submit to Arm {node!r}: {detail} "
                       f"(is it admitted? see `cuttle arm list`)")
    return int(r.stdout.strip().splitlines()[-1])


def preview(script: str, minutes: int, vram: int, gpu: int, no_gitignore: bool) -> None:
    """Show what is about to be uploaded, before it is.

    The Dashboard gates submission behind a preview; the CLI has never had one, so `cuttle run` in
    the wrong directory silently ships whatever is there. The file count and size are the point — a
    2 GB upload from a directory full of checkpoints is obvious here and invisible otherwise.

    No Karma estimate: pricing a Spawn needs a Tide quote from the Head, and a number shown here that
    disagreed with what is actually escrowed would be worse than showing nothing.
    """
    root = Path.cwd()
    files = _project_files(root) if not no_gitignore else None
    paths = files if files is not None else [p for p in root.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in paths if p.exists())
    ui.kv([
        ("script", script),
        ("upload", f"{len(paths)} files, {total / 1_048_576:.1f} MB"
                   f"{'' if no_gitignore else '  (.gitignore honored)'}"),
        ("limit", f"{minutes} min"),
        ("needs", f"{vram} GB VRAM, {gpu} GPU{'s' if gpu != 1 else ''}"),
    ], title="About to submit")


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
    # `node` pins the Spawn to one Arm. Since Phase 6 it does not travel over HTTP at all — the Head
    # API refuses it for everyone — so a pinned Spawn takes the Head-local path below instead.
    if node:
        spawn_id = _submit_pinned(tar_bytes, script, args, minutes, vram, gpu, node)
    else:
        meta = {"script": script, "gpu": gpu, "args": args,
                "minutes": minutes, "vram": vram,
                "wandb_env": settings.wandb_env(project, entity)}
        client = auth._client()
        client.token = auth._load_token()
        try:
            spawn_id = client.upload("/submit", tar_bytes, meta)["id"]
        except api.ApiError as e:
            if ui.DEBUG:
                raise
            raise ui.abort(e.user_message()) from None
    # The id goes to stdout and the confirmation to stderr, so `id=$(cuttle run ...)` captures
    # exactly the id while a human still sees what happened.
    ui.ok(f"submitted spawn {spawn_id}")
    ui.data(str(spawn_id))
    if follow:
        _follow(spawn_id)


def cancel(spawn_id: int, any_owner: bool = False, yes: bool = False) -> None:
    """End a Spawn early. Yours over the API from anywhere; **anyone's only on the Head**.

    Two roads on purpose. Cancelling your own Spawn is a [U] write and stays remote — it is the escape
    hatch from paying for a job you no longer want, and it has to work from the box you submitted
    from. Cancelling *someone else's* is an admin write, and since Phase 6 the Head API has none: an
    operator standing at the Head can stop a runaway, a stolen Session cannot.

    Either way the Karma tick settles the Spawn for the seconds it actually held when it drops out of
    squeue, so a cancel costs its submitter what it used and no more.
    """
    if not yes and not ui.ask_confirm(f"Cancel spawn {spawn_id}?"):
        raise ui.abort("not cancelled")
    if any_owner:
        # scancel, not the API: every Spawn runs as the Mantle, so the operator's sudo is what makes
        # this possible at all. No ledger lookup — an operator reaping a runaway may well be doing it
        # for a Spawn the Head never recorded.
        run(["scancel", str(spawn_id)], sudo=True)
    else:
        auth._api("DELETE", f"/spawns/{spawn_id}")
    ui.ok(f"spawn {spawn_id} cancelled")


def _outcome(st: dict) -> str:
    """A finished Spawn's verdict. `failed` carries no exit code by definition — it's the state of a
    Spawn that never recorded one (scancel'd, OOM-killed, or its Arm vanished before stage-out), so
    don't reach for st['exit'] there."""
    if st["state"] == "failed":
        return "FAILED (killed or its Arm dropped — no exit code recorded)"
    return f"done (exit {st['exit']})"


def _drain(label: str, text: str, pos: int) -> int:
    """Print whole lines of `text[pos:]`, each tagged `[label]`, and return the new consumed offset.
    Only complete lines are emitted; a trailing partial line is held until its newline arrives, so a
    prefix never splits a line. Returns pos advanced to the last newline consumed."""
    end = text.rfind("\n", pos) + 1          # 0 if no newline yet → nothing complete to emit
    if end <= pos:
        return pos
    for line in text[pos:end].splitlines():
        print(f"[{label}] {line}", flush=True)
    return end


def _follow(spawn_id: int) -> None:
    """Interleave stdout and stderr live until the Spawn ends, each line tagged `[out]`/`[err]` (the two
    are separate files, indistinguishable once merged). Ordering is per-poll, so approximate. Local/
    co-located Spawns stream live; a remote Spawn's streams arrive only at stage-out (post-hoc)."""
    out_pos = err_pos = 0
    while True:
        out = _get_bytes(f"/spawns/{spawn_id}/logs").decode()
        err = _get_bytes(f"/spawns/{spawn_id}/stderr").decode()
        out_pos = _drain("out", out, out_pos)
        err_pos = _drain("err", err, err_pos)
        st = auth._api("GET", f"/spawns/{spawn_id}")
        if st.get("done"):
            # Re-read once more (the streams may have grown since the poll above), then flush every
            # remaining line — including a final partial one with no trailing newline that _drain holds.
            for label, path, pos in (("out", "logs", out_pos), ("err", "stderr", err_pos)):
                text = _get_bytes(f"/spawns/{spawn_id}/{path}").decode()
                pos = _drain(label, text, pos)
                if len(text) > pos:
                    print(f"[{label}] {text[pos:]}", flush=True)
            ui.ok(f"spawn {spawn_id} {_outcome(st)}")
            return
        time.sleep(0.5)


def queue(as_json: bool = False, limit: int = 0) -> None:
    """The live Spawn queue — every pending/running Spawn on the cluster, and who submitted it.

    Everyone's, like the Dashboard's landing table: the queue is shared, so what is running ahead of
    yours is exactly what you want to see. `WHO` is the *ledger's* attribution, not Slurm's — every
    Spawn runs as the Mantle, so Slurm's own user field says `ccc` for all of them (see the Head's
    /spawns). A `-` there is a Spawn the Head has no attribution row for. `limit` (0 = all) caps to the
    front of the queue — running + next-to-run — so a busy cluster is not a wall of text."""
    rows = auth._api("GET", f"/spawns?limit={limit}" if limit else "/spawns")

    def render():
        if not rows:
            ui.say("the queue is empty")
            return
        ui.table(
            ["ID", "SPAWN", "STATE", "WHO", "NODE", "GPU", "MIN"],
            [[r["id"], r["name"], r["state"], r.get("submitted_by") or "-",
              r.get("node") or "-", r.get("gpus"), r.get("time_limit")] for r in rows])
        if limit and len(rows) >= limit:
            ui.hint(f"showing {limit}; use --limit 0 for all")

    ui.emit(rows, as_json, render)


def _short(ts: str | None) -> str:
    """An ISO timestamp trimmed to the minute, for a table cell — or a dash if there is none yet."""
    return ts[:16].replace("T", " ") if ts else "-"


def _history_outcome(r: dict) -> str:
    """A finished Spawn's verdict, as a word. Mirrors GET /spawns/{id}: a NULL exit code with a
    recorded end is a Spawn that died without one (scancel'd, OOM, or its Arm dropped); a row still
    unstamped left the queue seconds ago and the tick has not settled it yet."""
    if r.get("finished_at") is None:
        return "—"
    code = r.get("exit_code")
    if code is None:
        return "failed"
    return "done" if code == 0 else f"failed ({code})"


def history(days: int = 30, as_json: bool = False, limit: int = 0) -> None:
    """Past Spawns on the cluster — everything the live queue has forgotten — newest first. Everyone's,
    like `queue`: the cluster is shared. Each row shows who submitted it, what it declared, what it
    settled at, and how it ended. `limit` (0 = all) caps to the newest N so a busy cluster is not a
    wall of text."""
    q = f"/spawns/history?days={days}"
    if limit:
        q += f"&limit={limit}"
    rows = auth._api("GET", q)

    def render():
        if not rows:
            ui.say(f"no Spawns in the last {days} days")
            return
        ui.table(
            ["SUBMITTED", "FINISHED", "ID", "SCRIPT", "WHO", "GPU", "MIN", "COST", "OUTCOME"],
            [[_short(r["submitted_at"]), _short(r.get("finished_at")), r["job_id"], r["script"],
              r.get("submitted_by") or "-", r.get("gpus"), r.get("minutes"),
              f"{r['cost']:.1f}" if r.get("cost") is not None else "—", _history_outcome(r)]
             for r in rows])
        if limit and len(rows) >= limit:
            ui.hint(f"showing {limit}; use --limit 0 for all")

    ui.emit(rows, as_json, render)


def status(spawn_id: int, as_json: bool = False) -> None:
    st = auth._api("GET", f"/spawns/{spawn_id}")
    ui.emit(st, as_json,
            lambda: ui.data(_outcome(st).upper() if st.get("done") else st["state"]))


def logs(spawn_id: int, follow: bool) -> None:
    if follow:
        _follow(spawn_id)
        return
    out = _get_bytes(f"/spawns/{spawn_id}/logs").decode().rstrip()
    err = _get_bytes(f"/spawns/{spawn_id}/stderr").decode().rstrip()
    if not out and not err:
        ui.say("(no output yet)")
        return
    # Both sections always, so a FAILED Spawn's traceback (in stderr, out empty) is never hidden.
    ui.data(f"=== stdout ===\n{out}\n\n=== stderr ===\n{err}")


def outputs(spawn_id: int) -> None:
    """Download a Spawn's $CCC_OUT dir as a tar into the cwd."""
    data = _get_bytes(f"/spawns/{spawn_id}/outputs", missing_ok=True)
    if data is None:
        ui.warn(f"spawn {spawn_id} produced no outputs")
        ui.hint("a Spawn returns files by writing them to $CCC_OUT inside the Pod")
        return
    dest = Path.cwd() / f"spawn-{spawn_id}-outputs.tar.gz"
    dest.write_bytes(data)
    ui.ok(f"wrote {dest}")
