"""Spawn inspection: `status` / `logs` / `queue` / `history` / `cancel`.

A Spawn = one sbatch job wrapping `apptainer exec --nv base.sif ...`. The Slurm job id IS the spawn
id. This module is a thin API client; nothing here submits (the submit path is gone in 2.0 until Dives).

  * results   — out/err/exit live on the Head under ccc; read them back over the API
                (`status`/`logs`), never off local disk.

Auth: every call carries the bearer token from `~/.ccc/session`; the Head URL comes from
~/.ccc/config. Every route needs it. Transport is HTTPS, verified against the Head's certificate
pinned on this machine (see tls.py).
"""

import time

from . import api, auth, config, menu, ui
from .system import run


def _get_bytes(path: str, missing_ok: bool = False) -> bytes | None:
    """Authed GET of a raw body (logs text) — carries the bearer like every other call.

    `missing_ok` returns None on a 404 instead of raising.
    """
    client = auth._client()
    client.token = auth._load_token()
    try:
        return client.raw(path, missing_ok=missing_ok)
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

    **On the Head the two roads are one command.** The API is asked first and its refusal is the
    ownership test: a 403 means "not yours", which on the Head is not a dead end but the other road,
    taken automatically. So an operator who picked someone else's Spawn out of the queue is not sent
    away to retype the same thing with a flag — `--any` remains for the case the API cannot serve at
    all (see `_cancel_on_head`).

    The escalation is gated on **being on the Head and nothing else**. `is_admin` governs what a
    **Session** may do; `sudo` governs what this shell may do; and it is the shell that scancels. See
    menu.py's module docstring — "Admin" is not an axis.

    A **404 is never escalated**: an id the ledger does not know is not a Spawn this command may guess
    at, and reaping one is exactly what `--any` is for.

    Either way the Karma tick settles the Spawn for the seconds it actually held when it drops out of
    squeue, so a cancel costs its submitter what it used and no more — whoever ended it.
    """
    if not yes and not ui.ask_confirm(f"Cancel spawn {spawn_id}?"):
        raise ui.abort("not cancelled")
    if any_owner:
        _cancel_on_head(spawn_id)
    elif auth._api("DELETE", f"/spawns/{spawn_id}", refused_ok=True) is None:
        # Refused as not ours. Off the Head that is the end of it, and the Head's own words are the
        # right ones — say only where the other road is.
        if not menu.is_head():
            raise ui.abort("you can only cancel your own Spawns",
                           next_step="someone else's can only be ended by the operator on the Head")
        ui.say(f"  [muted]spawn {spawn_id} isn't yours — ending it as the Head operator[/muted]")
        _cancel_on_head(spawn_id)
    ui.ok(f"spawn {spawn_id} cancelled")


def _cancel_on_head(spawn_id: int) -> None:
    """The operator's road: scancel, not the API.

    Every Spawn runs as the Mantle, so the operator's sudo is what makes this possible at all. No
    ledger lookup — an operator reaping a runaway may well be doing it for a Spawn the Head never
    recorded, which is the one thing `DELETE /spawns/{id}` can never do (it 404s on an id it has no
    row for, deliberately).

    The marker that route writes has to be written here too, as the Head API user that owns the Spawn
    dir, or the Spawn this operator deliberately ended would still report itself as having died on its
    own. check=False: an id the Head never recorded has no dir, and reaping a runaway is exactly when
    that is true.

    The guard is not decoration. This is the only Head-local admin road that does not go through
    `auth._admin` (which execs the API venv and so cannot exist off the Head); without it, `--any` on a
    client failed in whatever way `sudo scancel` fails there, which is a message about a missing binary
    rather than about authority.
    """
    if not menu.is_head():
        raise ui.abort("only the operator on the Head can cancel someone else's Spawn",
                       next_step="run this on the Head, or ask the Spawn's submitter to cancel it")
    run(["scancel", str(spawn_id)], sudo=True)
    run(["-u", config.HEAD_API_USER, "touch",
         str(config.HEAD_SPAWNS_DIR / str(spawn_id) / "cancelled")], sudo=True, check=False)


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

    The Environment build needs no stream of its own: the Arm builds it inside the job, before the
    payload, and writes to the same `err` — so it arrives here tagged `[err]`, in order, and a User
    watching an otherwise silent Spawn sees the solve rather than nothing."""
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
            ["ID", "STATE", "WHO", "NODE", "GPU", "MIN"],
            [[r["id"], r["state"], r.get("submitted_by") or "-",
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
            [[_short(r["submitted_at"]), _short(r.get("finished_at")), r["job_id"],
              r.get("script") or "-",
              r.get("submitted_by") or "-", r.get("gpus"), r.get("minutes"),
              f"{r['cost']:.1f}" if r.get("cost") is not None else "—", _history_outcome(r)]
             for r in rows])
        if limit and len(rows) >= limit:
            ui.hint(f"showing {limit}; use --limit 0 for all")

    ui.emit(rows, as_json, render)


def status(spawn_id: int, as_json: bool = False) -> None:
    st = auth._api("GET", f"/spawns/{spawn_id}")

    def render():
        ui.data(_outcome(st).upper() if st.get("done") else st["state"])

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
