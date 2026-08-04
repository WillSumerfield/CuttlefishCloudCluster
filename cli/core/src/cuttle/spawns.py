"""Spawn submission + inspection: `cuttle run` / `status` / `logs` / `outputs`.

A Spawn = one sbatch job wrapping `apptainer exec --nv base.sif python <script>`. The Slurm job id
IS the spawn id. `cuttle run` is not Head-local: it tars the working dir and POSTs it to the Head
API (authenticated), which unpacks, renders, and submits it as the `ccc` service account, recording
who submitted it. So this module is a thin API client — the render + stage machinery lives
server-side in `ccc_head_api.spawns`.

  * upload    — a gzip-tar of the working dir. Gitignore-aware by default (skip .git, venvs, data,
                checkpoints); `--no-gitignore` uploads the whole tree.
  * results   — out/err/exit/outputs live on the Head under ccc; read them back over the API
                (`status`/`logs`/`outputs`), never off local disk. A remote submitter who never ran
                setup gets results the same way — `cuttle outputs <id>` downloads the $CCC_OUT tar.

Auth: every call carries the bearer token from `~/.ccc/credentials`; the Head URL comes from
~/.ccc/config. Every route needs it. Transport is HTTPS, verified against the Head's certificate
pinned on this machine (see tls.py).
"""

import io
import json
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import api, auth, config, mesh, settings, ui, watch
from .system import run

_TS = "%Y-%m-%dT%H:%M:%SZ"  # mirrors ccc_head_api.db's timestamp format


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
    """Authed GET of a raw body (logs text / outputs tar) — carries the bearer like every other call.

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


def tar_from_files(files: dict[str, str]) -> bytes:
    """A project built in memory from `{name: contents}`, for a Spawn CCC submits on its own behalf.

    There is no directory to upload for one of those (see cuttle.enroll.activate_arm — the whole
    project is two generated files), and writing them to a temp tree first would only add a place for
    them to be left behind.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in sorted(files.items()):
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def wait_for_exit(spawn_id: int, timeout_s: int) -> tuple[int | None, str]:
    """Block until `spawn_id` ends, then return `(exit code, stderr)`.

    For a Spawn CCC submitted itself and must judge — not for a User's, who gets `cuttle logs -f`. The
    exit code is None for a Spawn that ended without recording one (cancelled, or its Arm dropped),
    which is a failure here like any other; the caller reports it with whatever stderr survived.

    A timeout returns the same shape rather than raising, so one caller handles "it failed" and "it
    never finished" identically — from the operator's seat those are the same outcome.

    Read off the Head's own disk with sudo, not over HTTP, for the same reason `_submit_pinned` shells
    out instead of calling the API: the caller stands on the Head, and one of them (`setup --head`)
    runs before any account exists to log in as. The spawn dir is 0700 `ccc`, so `sudo cat` is what
    reaches it — the same way `_benchmark_arm` reads a Power file.
    """
    d = config.HEAD_SPAWNS_DIR / str(spawn_id)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        # `exit` is written by the job script only after the payload returns, and its presence is this
        # pipeline's "done" convention everywhere else (see ccc_head_api.spawns.JOB_SCRIPT).
        r = run(["cat", str(d / "exit")], sudo=True, capture_output=True, text=True, check=False)
        if r.returncode == 0 and r.stdout.strip():
            err = run(["cat", str(d / "err")], sudo=True, capture_output=True, text=True, check=False)
            return int(r.stdout.strip()), err.stdout or ""
        time.sleep(5)
    err = run(["cat", str(d / "err")], sudo=True, capture_output=True, text=True, check=False)
    return None, (err.stdout or "") + f"\nccc: the Spawn did not finish within {timeout_s}s"


def _submit_pinned(tar_bytes: bytes, script: str, args: list[str], minutes: int, vram: int,
                   gpu: int, node: str, wandb_env: dict[str, str], outputs: list[str],
                   features: list[str] | None = None, tidepool: bool = False,
                   manifest: str | None = None, activation: bool = False,
                   user: str | None = None) -> tuple[int, str | None]:
    """Submit a Spawn pinned to a named Arm, on the Head, as the Mantle.

    Pinning is an admin write, and the Head API has none — so this does not travel over HTTP at all.
    Authorization is *being the operator on this box*; attribution is whoever is logged in here. It
    shells into the API venv rather than reimplementing submission, so the Escrow and the Attribution
    row are written by the same code that serves `/submit` (see spawns.submit_checked).

    `wandb_env` rides across as JSON — the same dict the HTTP path puts in its meta, so `--project`
    and `--entity` mean the same thing on both roads.

    `tidepool` lets an Admin pin a Tide Pool exactly like any other Spawn — orthogonal to
    node-pinning, threaded through for completeness (the common Tide Pool path is unpinned).
    """
    # Named explicitly only by CCC's own Spawns (cuttle.enroll.activate_arm), which run during
    # `setup --head` — before any account has logged in, so there is no `/me` to ask. Every human path
    # leaves it None and is attributed to whoever is logged in on this box.
    me = user or auth._api("GET", "/me")["username"]
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
            "--node", node, "--wandb-env", json.dumps(wandb_env),
            # Repeatable, one `--output NAME` / `--feature NAME` per declared name — placed before the
            # trailing `*args` positional (the admin parser's `nargs="*"`) so argparse never mistakes a
            # script argument for another output/feature name.
            *[f for name in outputs for f in ("--output", name)],
            *[f for name in (features or []) for f in ("--feature", name)],
            *(["--activation"] if activation else []),
            *(["--tidepool"] if tidepool else []),
            *(["--manifest", manifest] if manifest else []),
            *args],
            # The Head's own mesh address, which the SERVICE gets from its systemd unit — this is a
            # fresh subprocess and inherits nothing from it. Without it every pinned Spawn renders
            # `rsync://<tok>@/proj-<tok>/` with no host and dies at stage-in on the far side of the
            # mesh, so `spawns.submit_checked` refuses outright rather than let one out (which is the
            # "CCC_MESH_IP is empty in the ccc-head-api unit" error, reported against the unit that
            # was in fact set correctly all along).
            env={"CCC_MESH_IP": mesh.tailscale_ip()},
        ), sudo=True, capture_output=True, text=True, check=False)
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
    # The Environment echo rides back on stderr (see ccc_head_api.admin._submit) — stdout is the id and
    # nothing else. Picked out by prefix rather than position: stderr on this path may also carry sudo's
    # own noise, and there is no line number that survives that.
    note = next((ln for ln in (r.stderr or "").splitlines() if ln.startswith("environment:")), None)
    return int(r.stdout.strip().splitlines()[-1]), note


def preview(script: str, minutes: int, vram: int, gpu: int, no_gitignore: bool,
           outputs: list[str], features: list[str] | None = None) -> None:
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
        ("needs", f"{vram} GB VRAM, {gpu} GPU{'s' if gpu != 1 else ''}"
                  + (f", features: {', '.join(features)}" if features else "")),
        ("outputs", ", ".join(outputs)),
    ], title="About to submit")


def submit(script: str, gpu: int, follow: bool, args: list[str], minutes: int, vram: int,
           outputs: list[str], project: str | None = None, entity: str | None = None,
           no_gitignore: bool = False, node: str | None = None,
           features: list[str] | None = None, manifest: str | None = None,
           download: bool = True) -> None:
    root = Path.cwd()
    files = None if no_gitignore else _project_files(root)
    tar_bytes = _make_tar(root, files)
    features = features or []
    # The Head turns `vram` into a Slurm constraint: it knows every distinct card in the fleet and we
    # don't, and Slurm's constraints are set-membership with no ">=" — so an arbitrary integer only
    # works if someone OR-expands it (`--constraint=vram24|vram48`). That someone is the Head.
    #
    # Composed once, for both roads: which W&B run this Spawn is (project/entity, and nothing else —
    # the Head owns the key).
    wandb_env = settings.wandb_env(project, entity)
    # `node` pins the Spawn to one Arm. It does not travel over HTTP at all — the Head API refuses it
    # for everyone — so a pinned Spawn takes the Head-local path below instead.
    env_note = None
    if node:
        spawn_id, env_note = _submit_pinned(tar_bytes, script, args, minutes, vram, gpu, node,
                                            wandb_env, outputs, features=features, manifest=manifest)
    else:
        meta = {"script": script, "gpu": gpu, "args": args,
                "minutes": minutes, "vram": vram, "wandb_env": wandb_env, "outputs": outputs,
                # Omitted rather than sent empty, matching the rest of this dict's optional fields —
                # most Spawns declare none, and the Head reads an absent key as "no requirement".
                **({"features": features} if features else {}),
                # Absent means "look for one yourself"; "none" means "do not build one at all". Both
                # differ from a missing key only at the Head, which is where the project actually is.
                **({"manifest": manifest} if manifest else {})}
        client = auth._client()
        client.token = auth._load_token()
        try:
            resp = client.upload("/submit", tar_bytes, meta)
        except api.ApiError as e:
            if ui.DEBUG:
                raise
            raise ui.abort(e.user_message()) from None
        spawn_id, env_note = resp["id"], resp.get("environment")
    # The id goes to stdout and the confirmation to stderr, so `id=$(cuttle run ...)` captures
    # exactly the id while a human still sees what happened.
    ui.ok(f"submitted spawn {spawn_id}")
    # Which dependency file the Head picked up, or that it found none. Said HERE, while the User is
    # still watching, because the failure this catches is not an error anywhere: a manifest one
    # directory too deep builds nothing, and is otherwise only discoverable as an ImportError after the
    # Spawn has queued, been placed, and held a card.
    if env_note:
        ui.say(f"  [muted]{env_note}[/muted]")
    ui.data(str(spawn_id))
    # Armed BEFORE anything blocks, and armed even when we are about to follow: the entry is the
    # durable intent (cuttle.watch), so a Ctrl-C out of `--follow` leaves the obligation standing and
    # the next `cuttle` invocation of any kind picks it up.
    if download:
        watch.arm(spawn_id, root)
    if follow:
        _follow(spawn_id)
    if download:
        if follow:
            # Already waited for it on this terminal — fetch inline rather than forking a sweeper to
            # re-poll a Spawn we just watched end. A failure here settles nothing: the entry survives
            # and is retried later, exactly as if we had never been watching.
            _download_now(spawn_id)
        else:
            watch.ensure_sweeper()


def _download_now(spawn_id: int) -> None:
    """The foreground half of auto-download, for `--follow`. Says out loud what the sweeper would only
    have written to a log file, because here there is a terminal and a User still in front of it."""
    if not watch.drain_one(spawn_id):
        # Not settled: the Head could not be reached, or the results have not landed from a remote Arm
        # yet. Hand it to the sweeper rather than blocking this terminal any longer.
        ui.hint("results are not on the Head yet — they will be downloaded in the background")
        watch.ensure_sweeper()
        return
    ui.say(f"  [muted]{watch.summary(spawn_id)}[/muted]")
    if (watch.read(spawn_id) or {}).get("state") == watch.EMPTY:
        # The moment a User learns `--output` was wrong or incomplete — and the only moment the
        # Recovery Window is still open. Same reasoning as `outputs()`, and it only reaches a person
        # on this path: the sweeper has no terminal to say it on, and by the time they read its log
        # the 30 minutes are long gone.
        _hint_recovery(spawn_id)


def submit_tidepool(minutes: int, vram: int, gpu: int, outputs: list[str], node: str | None = None,
                    features: list[str] | None = None) -> int:
    """Request a Tide Pool — no project upload, no script: only declared needs travel.
    Returns the Spawn id (no printing — `cuttle run --tidepool`'s own flow, in tidepool.py, owns the UI
    from here, since it still has to poll for placement and attach).

    The upload body is a genuinely empty gzip-tar (`_make_tar` with an empty file list), not raw empty
    bytes — the Head unconditionally opens it as a tar archive (spawns.submit), and every client sends
    the same wire shape whether or not there is a project behind it.
    """
    features = features or []
    empty_tar = _make_tar(Path.cwd(), [])
    if node:
        # The Environment echo is dropped here alone: a Tide Pool uploads an empty tar, so there is no
        # project to have found a manifest in and the note would say "no dependency file found" about a
        # project nobody sent. tidepool.py owns this flow's UI (see the docstring above).
        return _submit_pinned(empty_tar, "", [], minutes, vram, gpu, node, {}, outputs,
                              features=features, tidepool=True)[0]
    meta = {"minutes": minutes, "vram": vram, "gpu": gpu, "outputs": outputs, "tidepool": True,
           **({"features": features} if features else {})}
    client = auth._client()
    client.token = auth._load_token()
    try:
        return client.upload("/submit", empty_tar, meta)["id"]
    except api.ApiError as e:
        if ui.DEBUG:
            raise
        raise ui.abort(e.user_message()) from None


def cancel(spawn_id: int, any_owner: bool = False, yes: bool = False) -> None:
    """End a Spawn early. Yours over the API from anywhere; **anyone's only on the Head**.

    Two roads on purpose. Cancelling your own Spawn is a [U] write and stays remote — it is the escape
    hatch from paying for a job you no longer want, and it has to work from the box you submitted
    from. Cancelling *someone else's* is an admin write, and the Head API has none: an operator
    standing at the Head can stop a runaway, a stolen Session cannot.

    Either way the Karma tick settles the Spawn for the seconds it actually held when it drops out of
    squeue, so a cancel costs its submitter what it used and no more.
    """
    if not yes and not ui.ask_confirm(f"Cancel spawn {spawn_id}?"):
        raise ui.abort("not cancelled")
    if any_owner:
        # scancel, not the API: every Spawn runs as the Mantle, so the operator's sudo is what makes
        # this possible at all. No ledger lookup — an operator reaping a runaway may well be doing it
        # for a Spawn the Head never recorded.
        #
        # The marker `DELETE /spawns/{id}` writes has to be written here too, as the Head API user
        # that owns the Spawn dir, or the Spawn this operator deliberately ended would still report
        # itself as having died on its own. check=False: an id the Head never recorded has no dir,
        # and reaping a runaway is exactly when that is true.
        run(["scancel", str(spawn_id)], sudo=True)
        run(["-u", config.HEAD_API_USER, "touch",
             str(config.HEAD_SPAWNS_DIR / str(spawn_id) / "cancelled")], sudo=True, check=False)
    else:
        auth._api("DELETE", f"/spawns/{spawn_id}")
    ui.ok(f"spawn {spawn_id} cancelled")


def _outcome(st: dict) -> str:
    """A finished Spawn's verdict. Neither `cancelled` nor `failed` carries an exit code — both are
    states in which none was ever recorded — so don't reach for st['exit'] in either."""
    if st["state"] == "cancelled":
        return "CANCELLED (ended early — no exit code recorded)"
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
    co-located Spawns stream live; a remote Spawn's streams arrive only at stage-out (post-hoc).

    The Environment build comes first, tagged `[env]`, and is followed on the same poll as the other
    two: it is the only thing happening for the first minutes of a Spawn that declared dependencies,
    and a User watching an otherwise silent BUILDING Spawn should see the solve rather than nothing."""
    out_pos = err_pos = env_pos = 0
    while True:
        env = _get_bytes(f"/spawns/{spawn_id}/build").decode()
        out = _get_bytes(f"/spawns/{spawn_id}/logs").decode()
        err = _get_bytes(f"/spawns/{spawn_id}/stderr").decode()
        env_pos = _drain("env", env, env_pos)
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
    recorded end is a Spawn that ended without one — deliberately (`cancelled`) or not (OOM, or its
    Arm dropped); a row still unstamped left the queue seconds ago and the tick has not settled it
    yet."""
    if r.get("finished_at") is None:
        return "—"
    code = r.get("exit_code")
    if code is None:
        return "cancelled" if r.get("cancelled") else "failed"
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
        _hint_downloads()

    ui.emit(rows, as_json, render)


def _hint_downloads() -> None:
    """What auto-download (cuttle.watch) still owes this box. A line under `history` rather than a
    column in it: the table is *everyone's* Spawns and this is only ever about yours. The actionable
    case is the blocked one — a parked entry is waiting on a login nobody has been told to perform."""
    blocked = [e for e in watch.entries() if e.get("state") == watch.PARKED]
    if blocked:
        ids = ", ".join(str(e["spawn_id"]) for e in blocked)
        ui.warn(f"{len(blocked)} download(s) blocked ({ids}) — {blocked[0].get('note') or ''}")
    waiting = watch.pending()
    if waiting:
        ui.hint(f"{len(waiting)} result(s) will download here when their Spawns finish "
                f"({', '.join(str(e['spawn_id']) for e in waiting)})")


def status(spawn_id: int, as_json: bool = False) -> None:
    st = auth._api("GET", f"/spawns/{spawn_id}")

    def render():
        ui.data(_outcome(st).upper() if st.get("done") else st["state"])
        # Where this box's copy of the results went, if it is owed one. `cuttle status` is the one
        # command a User runs *about a specific Spawn*, so it is where "downloaded → ./spawn-41-outputs/"
        # belongs — the sweeper had no terminal to say it on.
        line = watch.summary(spawn_id)
        if line:
            ui.say(f"  [muted]{line}[/muted]")

    ui.emit(st, as_json, render)


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


def _recovery_status(spawn_id: int) -> dict | None:
    """The Recovery Window's live state for `spawn_id`, or None if it has none —
    never made (skipped for size, or the Arm dropped before it landed), evicted, or already expired.
    A courtesy lookup, not a load-bearing one: any failure here (not just a 404) is swallowed rather
    than aborting whatever command called it, since the caller has already succeeded or failed on its
    own terms by the time it asks."""
    client = auth._client()
    client.token = auth._load_token()
    try:
        return client.json("GET", f"/spawns/{spawn_id}/recovery", missing_ok=True)
    except api.ApiError:
        return None


def _minutes_left(expires_at: str) -> int:
    expires = datetime.strptime(expires_at, _TS).replace(tzinfo=timezone.utc)
    return int((expires - datetime.now(timezone.utc)).total_seconds() // 60)


def _hint_recovery(spawn_id: int) -> None:
    """Reactive Recovery Window messaging: the moment a User discovers `--output` was
    wrong or incomplete — right here, at the "no outputs" miss — is exactly when they need to know the
    rest of the Spawn's working dir might still be recoverable. Silent when it isn't (closed, skipped
    for size, or none was ever made): there's nothing more honest to say than what `outputs()` already
    said above."""
    st = _recovery_status(spawn_id)
    if st is None:
        return
    left = _minutes_left(st["expires_at"])
    if left <= 0:
        return
    ui.hint(f"the Recovery Window is still open ({left}m left) — `cuttle recover {spawn_id}` grabs "
            f"everything else the Spawn wrote, not just $CCC_OUT")


def outputs(spawn_id: int) -> None:
    """Download a Spawn's $CCC_OUT dir as a tar into the cwd.

    Two different misses, and the Head is the one that can tell them apart (see main.spawn_outputs), so
    its 404 detail is printed verbatim rather than replaced by a guess here: a Spawn that WROTE nothing,
    versus one whose results were held and are now gone — 7 days elapsed, or evicted for the submitter's
    8GB Allowance. Telling a User their Spawn produced nothing when it produced 3GB they simply came
    back too late for is the one answer worse than saying neither."""
    client = auth._client()
    client.token = auth._load_token()
    try:
        data = client.raw(f"/spawns/{spawn_id}/outputs")
    except api.ApiError as e:
        if e.status != 404:
            if ui.DEBUG:
                raise
            raise ui.abort(e.user_message()) from None
        ui.warn(e.user_message())
        _hint_recovery(spawn_id)
        return
    dest = Path.cwd() / f"spawn-{spawn_id}-outputs.tar.gz"
    dest.write_bytes(data)
    ui.ok(f"wrote {dest}")


def recover(spawn_id: int) -> None:
    """Download a Spawn's Recovery Window copy — its whole working dir as it stood when
    the Spawn ended, not just what it wrote to $CCC_OUT — for as long as the 30-minute window is open.

    A separate artifact from `outputs()` on purpose: the declared $CCC_OUT tar is permanent and never
    changes shape; this one expires, so the two are never merged into one download whose contents would
    otherwise silently change once the window closes.
    """
    data = _get_bytes(f"/spawns/{spawn_id}/recovery/download", missing_ok=True)
    if data is None:
        ui.warn(f"spawn {spawn_id} has no open Recovery Window "
                f"(already closed, over the 16GB cap, or none was made)")
        return
    dest = Path.cwd() / f"spawn-{spawn_id}-recovery.tar.gz"
    dest.write_bytes(data)
    ui.ok(f"wrote {dest}")
