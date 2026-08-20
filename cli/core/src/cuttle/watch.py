"""Auto-download: a **Spawn**'s results arrive on the submitter's disk without anyone asking.

`cuttle run` returns the moment the Head accepts a Spawn, hours before it ends, so `cuttle outputs
<id>` is easy to forget — against a Head that deletes results after 7 days or sooner if the 32GB
**Allowance** fills (`ccc_head_api.retention`). This module removes the step.

The file is the intent, the process only the current attempt: every armed Spawn gets
`~/.ccc/watch/<id>.json` before anything forks. The sweeper draining it is disposable — kill it,
reboot, whatever — the entries survive and `ensure_sweeper()` (in `cuttle`'s root callback) re-arms
on the next invocation of any command. No daemon, nothing installed; the sweeper exits once drained.

One sweeper, not one per Spawn: an `flock` on `~/.ccc/watch/.lock` gives `arm()` and the re-arm hook
one shared liveness check instead of N independent polling processes.

The Head learns nothing — no route, column, or migration; retention stays a pure function of time and
Allowance, and a fetched output still occupies Allowance (a Head trusting client honesty for storage
would punish the honest client).

The one place the two clients are not at operation **parity** (`CONTEXT.md`): a browser can't write
to a folder unattended, so the Dashboard has no equivalent.
"""

import fcntl
import json
import os
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import api, config

# The sweeper outlives the shell and nobody is watching it, so it polls at a fraction of `_follow`'s
# rate (0.5s) — that one is streaming logs onto a screen a human is reading, this one is asking a
# yes/no question about a job that may run for a day.
POLL_SECONDS = 15
# Completed entries are kept so `cuttle status <id>` can still say where the results went. This caps
# the bookkeeping, and is NOT a second retention rule: it drops `<id>.json` files, never a byte of
# anything downloaded. What lands on the User's disk is the User's, and CCC never removes it.
MAX_ENTRIES = 500

# Entry states. `waiting` is the only one the sweeper acts on.
WAITING = "waiting"    # Spawn not finished yet, or finished and not yet fetched
DONE = "done"          # fetched and extracted
EMPTY = "empty"        # the Head has nothing to give (wrote none, expired, or evicted)
PARKED = "parked"      # blocked on something a human must fix — logged out, disk full


# --- the watch list ---------------------------------------------------------

def _entry_path(spawn_id: int) -> Path:
    return config.WATCH_DIR / f"{spawn_id}.json"


def log_path(spawn_id: int) -> Path:
    return config.WATCH_DIR / f"{spawn_id}.log"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read(spawn_id: int) -> dict | None:
    """One entry, or None. Never raises: a half-written or hand-mangled file is not worth failing a
    `cuttle status` over, and the sweeper treats it the same way — as an entry that isn't there."""
    try:
        return json.loads(_entry_path(spawn_id).read_text())
    except Exception:
        return None


def _write(entry: dict) -> None:
    config.WATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = _entry_path(entry["spawn_id"])
    # Via a temp file: the sweeper and a `cuttle status` in another terminal read this directory
    # concurrently, and a torn read of a JSON file is indistinguishable from a corrupt one.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry))
    tmp.replace(path)


def entries() -> list[dict]:
    if not config.WATCH_DIR.is_dir():
        return []
    out = []
    for path in config.WATCH_DIR.glob("*.json"):
        try:
            out.append(json.loads(path.read_text()))
        except Exception:
            continue
    return out


def pending() -> list[dict]:
    return sorted((e for e in entries() if e.get("state") == WAITING),
                  key=lambda e: e["spawn_id"])


def arm(spawn_id: int, dest: Path) -> None:
    """Record that this box is owed `spawn_id`'s results, and where they go.

    `dest` is resolved to an absolute path here, at submit time, because the sweeper wakes up in a
    directory of its own choosing and a relative path would mean whatever happened to be current then.
    """
    _write({"spawn_id": spawn_id, "dest": str(Path(dest).resolve()), "state": WAITING,
            "note": "", "at": _now()})


def _finish(spawn_id: int, state: str, note: str, dest: str | None = None) -> None:
    entry = read(spawn_id) or {"spawn_id": spawn_id, "dest": ""}
    entry.update(state=state, note=note, at=_now())
    if dest:
        entry["dest"] = dest
    _write(entry)
    _prune()


def _prune() -> None:
    """Cap the bookkeeping. Oldest *settled* entries go first — a `waiting` one is an obligation and is
    never dropped, however old, because dropping it is exactly the silent loss this module exists to
    stop."""
    settled = sorted((e for e in entries() if e.get("state") != WAITING),
                     key=lambda e: e.get("at", ""))
    for entry in settled[:max(0, len(settled) - MAX_ENTRIES)]:
        _entry_path(entry["spawn_id"]).unlink(missing_ok=True)
        log_path(entry["spawn_id"]).unlink(missing_ok=True)


def note(spawn_id: int, line: str) -> None:
    """Append to a Spawn's download transcript. The sweeper has no terminal — `cuttle run` returned
    hours ago and the shell may be closed — so this file is where it says anything at all."""
    config.WATCH_DIR.mkdir(parents=True, exist_ok=True)
    with log_path(spawn_id).open("a") as fh:
        fh.write(f"{_now()}  {line}\n")


def summary(spawn_id: int) -> str | None:
    """One line for `cuttle status <id>`, or None if this box was never owed that Spawn."""
    entry = read(spawn_id)
    if entry is None:
        return None
    state, dest, why = entry.get("state"), entry.get("dest", ""), entry.get("note") or ""
    if state == DONE:
        return f"downloaded → {dest}"
    if state == EMPTY:
        return f"nothing downloaded — {why}"
    if state == PARKED:
        return f"download blocked — {why}"
    return f"download pending → {dest}"


# --- the sweeper ------------------------------------------------------------

def _lock():
    """The one liveness question, asked identically by `ensure_sweeper` and the sweeper itself.

    Returns an open file handle holding an exclusive lock, or None if another process holds it. The
    handle must be kept alive for as long as the claim is meant to last — a closed file drops the
    lock, which is what makes this self-healing across a `kill -9`.
    """
    config.WATCH_DIR.mkdir(parents=True, exist_ok=True)
    fh = (config.WATCH_DIR / ".lock").open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _launch() -> None:
    """Fork the sweeper, detached. `start_new_session` puts it in its own session so closing the
    terminal does not SIGHUP it; `cwd="/"` keeps it from pinning the directory it was submitted from
    (every destination it needs is already absolute in the entry)."""
    subprocess.Popen(
        [sys.executable, "-m", "cuttle.watch"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True, cwd="/")


def ensure_sweeper() -> None:
    """Make sure something is draining the watch list. Called after `cuttle run` arms an entry, and
    from the root callback of every other command — which is what re-arms after a reboot.

    Cheap when there is nothing to do: an absent or drained directory costs one `glob` and returns.
    Never raises — a client whose watch dir is unreadable should still be able to run `cuttle status`.
    """
    try:
        if not pending():
            return
        held = _lock()
        if held is None:
            return          # a sweeper is already alive; it will pick up whatever we just wrote
        # Released before forking rather than passed down: the child takes its own, so there is
        # exactly one acquire path and no inherited-fd subtlety. A second `cuttle` racing us here at
        # worst forks a sweeper that loses the lock and exits immediately.
        held.close()
        _launch()
    except Exception:
        pass


def _client(token: str) -> api.Api:
    client = api.for_head()
    client.token = token
    return client


def _token() -> str | None:
    """The session bearer, re-read from disk on every pass rather than held in memory.

    Deliberately not `auth._load_token` — that raises a Typer error, which is the right answer for a
    command and the wrong one for a background process. Re-reading is what makes `cuttle logout`
    honest: no copy of the token is ever taken, so logging out really does stop the downloads, and
    logging back in resumes them.
    """
    try:
        return config.SESSION_FILE.read_text().strip() or None
    except OSError:
        return None


def _member(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
    """The extraction filter: the stdlib's `data` rules, then drop the archive's own `outputs/` prefix.

    Order matters. `data_filter` runs FIRST and against the names as the archive wrote them, so a
    member naming an absolute path, a `..`, or a link out of the tree is refused before anything here
    touches it — stripping a component from an already-checked name can only move it further *in*.

    The prefix is `outputs_tar`'s (`ccc_head_api.spawns`, `arcname="outputs"`), and it is redundant
    twice over by the time it lands: the directory is already called `spawn-<id>-outputs`, and inside
    it are the User's own declared `--output` names. Without this the result is
    `spawn-41-outputs/outputs/checkpoints/`.
    """
    member = tarfile.data_filter(member, path)
    if member is None or member.name == "outputs":
        return None
    if member.name.startswith("outputs/"):
        member = member.replace(name=member.name[len("outputs/"):], deep=False)
    return member


def target(dest: Path, spawn_id: int) -> Path:
    """Where this Spawn's results land: `dest/spawn-<id>-outputs/`.

    `dest` is config.OUTPUTS_DIR unless the Spawn was submitted with `--dest`, and it is created rather
    than required: the default is CCC's own directory and may not exist on a box that has never
    downloaded anything. A `--dest` whose parent is gone (deleted, an unmounted share) falls back to
    the default — results still have to land somewhere rather than be dropped for a reason the User
    will never see.

    Resolved before the download rather than after, because `api.download_extract` needs somewhere to
    put the `.partial` directory it fills, and the fallback must not be discovered with 3GB in hand."""
    if not dest.is_dir() and dest != config.OUTPUTS_DIR:
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError:
            dest = config.OUTPUTS_DIR
            note(spawn_id, f"--dest is unreachable — extracting to {dest} instead")
    return dest / f"spawn-{spawn_id}-outputs"


def drain_one(spawn_id: int, token: str | None = None) -> bool:
    """Advance one entry. Returns True if it is settled (nothing more to do), False to try again later.

    The Spawn's *ending* is irrelevant here — exit 0, non-zero, FAILED, CANCELLED all download the
    same way. A run killed at its `--time` limit with six hours of checkpoints in a declared output is
    precisely the case where the results matter most, and gating on a clean exit would throw it away.
    """
    entry = read(spawn_id)
    if entry is None or entry.get("state") != WAITING:
        return True
    token = token or _token()
    if token is None:
        _finish(spawn_id, PARKED, "not logged in — run `cuttle login` and it resumes")
        return True
    try:
        client = _client(token)
        state = client.json("GET", f"/spawns/{spawn_id}")
        if not state.get("done"):
            return False
        # Straight from the wire onto disk, never through this process's memory: the sweeper is a
        # background process on a User's own machine and a 3GB result is a perfectly ordinary one.
        out = target(Path(entry["dest"]), spawn_id)
        client.download_extract(f"/spawns/{spawn_id}/outputs", out, member=_member)
    except api.ApiError as e:
        if e.status == 401:
            _finish(spawn_id, PARKED, "session expired — run `cuttle login` and it resumes")
            return True
        if e.status == 404:
            # The Head can tell "wrote nothing" from "held it and it is now gone" (7 days elapsed, or
            # evicted for the Allowance) and no guess made here would be as good, so its own words are
            # what gets recorded — same reasoning as `spawns.outputs`.
            note(spawn_id, e.detail)
            _finish(spawn_id, EMPTY, e.detail)
            return True
        # Unreachable Head, a 5xx, a transport hiccup, a transfer cut mid-stream: the entry is an
        # obligation, not an attempt, so it stays exactly as it is and the next pass tries again.
        note(spawn_id, f"retrying — {e.detail}")
        return False
    except OSError as e:
        _finish(spawn_id, PARKED, f"could not write the results — {e}")
        return True
    note(spawn_id, f"extracted to {out}")
    _finish(spawn_id, DONE, "", dest=str(out))
    return True


def one_pass() -> int:
    """Try every outstanding entry once. Returns how many are still owed afterwards.

    Every entry is attempted, unconditionally — an early return the moment one Spawn turns out to
    still be running would leave the *finished* Spawn behind it in the queue waiting another poll, or,
    behind a long enough job, until the sweeper died with the machine.
    """
    todo = pending()
    if not todo:
        return 0
    token = _token()
    for entry in todo:
        drain_one(entry["spawn_id"], token)
    return len(pending())


def sweep() -> None:
    """The detached process: drain the watch list, then exit. Holds the lock for its whole life, so
    `ensure_sweeper` can answer "is one alive?" without a pid file that could name a recycled pid."""
    held = _lock()
    if held is None:
        return
    try:
        while one_pass():
            # Re-checked at the top rather than returned on: a `cuttle run` in another terminal may
            # have armed an entry while this pass was working, and it would find the lock still held.
            time.sleep(POLL_SECONDS)
    finally:
        held.close()


if __name__ == "__main__":       # `python -m cuttle.watch`
    # Not a Typer command on purpose: `ui.run_app` claims the alternate screen for every command it
    # frames, and the root callback would re-enter `ensure_sweeper` from inside the sweeper itself.
    os.umask(0o077)
    sweep()
