"""Dives, client side: `cuttle dive [MAIN] …` requests one and drops you into it;
`cuttle dive attach|end|status [id]` for the rest.

A Dive's shell is reached as `ssh <name>-<id>` — the same key and pinned host key as `ssh <name>`
(sshconfig.py). On an Arm the main folder is a copy, mirrored back to the Tidepool every minute and
read-only there meanwhile; on the Head the Dive *is* your Tidepool, with the GPU lent to it.

**Disconnecting never ends a Dive.** `exit`, a dropped connection, Ctrl-C — it keeps its GPU (and
keeps billing Karma) until `--time` runs out or `cuttle dive end`.
"""

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import auth, config, menu, sshconfig, tidepool, transfer, ui
from .system import run

VERBS = ("attach", "end", "status")
_POLL_S = 2.0
_REQUEST_TIMEOUT_S = 900       # the system layer is snapshotted + pushed before sbatch
# Warn inside the shell before --time: a quarter of the total, between 30 s and 5 min.
_WARN_MAX_S = 5 * 60
_WARN_MIN_S = 30
_TIDEPOOL_MARK = Path("/etc/ccc/tidepool/init")
_DIVE_MARK = Path("/etc/ccc/dive")


def in_tidepool() -> bool:
    return _TIDEPOOL_MARK.exists()


def in_container() -> bool:
    """Inside a Tidepool or a Dive: `cuttle` runs on the ambient session, with no key or alias here."""
    return in_tidepool() or _DIVE_MARK.exists()


def main_rel(main: str | None, cwd: Path | None = None, home: Path | None = None) -> str:
    """The main folder, relative to the Tidepool home. Inside the Tidepool it may be a path from the
    cwd (default: the cwd itself); elsewhere it is named from ~ (`proj`, `~/proj`, `/home/me/proj`)."""
    inside = in_tidepool()
    if main is None:
        if not inside:
            raise ui.abort("name the main folder, from your Tidepool home — e.g. `cuttle dive proj`")
        main = "."
    if main.startswith("~/") or main == "~":
        return main[2:].strip("/")
    if inside:
        home = home or Path.home()
        p = ((cwd or Path.cwd()) / main).resolve()
        if p == home or home not in p.parents:
            raise ui.abort(f"{p} is not a folder under your home ({home})")
        return str(p.relative_to(home))
    return re.sub(r"^/home/[^/]+/", "", main).strip("/")


def request(main: str | None, deps: list[str] | None, gpus: int | None, vram: int | None,
            time_: str | None, features: list[str] | None, env: str | None, vscode: bool = False) -> None:
    rel = main_rel(main)
    if not in_container():
        tidepool.connect()   # key uploaded, alias + pinned host key fresh
    body = {"main": rel, "deps": [d.removeprefix("~/").strip("/") for d in deps] if deps else None,
            "gpus": gpus, "vram": vram, "time": time_, "features": features or None, "env": env}
    with ui.step("snapshotting your Tidepool and queueing the Dive"):
        r = auth._api("POST", "/dives", body=body, timeout=_REQUEST_TIMEOUT_S)
    deps_s = ", ".join(f"~/{d}" for d in r["deps"]) or "none"
    ui.ok(f"Dive {r['id']} queued — main ~/{r['main']}, deps {deps_s}, {r['gpus']} GPU, "
          f"{_minutes(r['minutes'])}, {r['size_gb']:.1f} GB to stage")
    for n in r.get("notes") or []:
        ui.warn(n)
    attach(r["id"], vscode=vscode)


def _minutes(m: int) -> str:
    d, h, mi = m // 1440, m % 1440 // 60, m % 60
    return "".join(f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (mi, "m")) if v) or "0m"


def _pick(message: str) -> int:
    rows = auth._api("GET", "/dives") or []
    if not rows:
        raise ui.abort("you have no open Dives — start one with `cuttle dive <folder>`")
    if len(rows) == 1:
        return rows[0]["id"]
    return ui.ask_select(message, [ui.choice(f"{r['id']}  {r['state']}  ~/{r['main']}", r["id"])
                                   for r in rows])


def _wait_ready(dive_id: int) -> dict:
    """Poll until an sshd answers. Ctrl-C stops waiting, never the Dive."""
    try:
        with ui.step(f"waiting for Dive {dive_id}") as st:
            while True:
                info = auth._api("GET", f"/dives/{dive_id}")
                if info["ready"]:
                    return info
                if info["state"] == "ended":
                    raise ui.abort(f"Dive {dive_id} has ended")
                if hasattr(st, "update"):
                    st.update(f"Dive {dive_id}: {_state_words(info['state'])}")
                time.sleep(_POLL_S)
    except KeyboardInterrupt:
        ui.hint(f"stopped waiting — Dive {dive_id} is still queued; `cuttle dive attach {dive_id}` "
                f"later, `cuttle dive end {dive_id}` to drop it")
        raise SystemExit(0) from None


def _state_words(state: str) -> str:
    return {"pending": "queued", "running": "staging your folders"}.get(state, state)


def _live_ids() -> set[int] | None:
    try:
        return {r["id"] for r in auth._api("GET", "/dives")}
    except (Exception, SystemExit):
        return None


def ensure_alias(dive_id: int) -> tuple[str, dict]:
    """Wait for the Dive's sshd, then write its `<name>-<id>` alias. (alias, connection info)."""
    info = _wait_ready(dive_id)
    sshconfig.record(dive_id, port=info["port"], user=info["user"], host=tidepool._head_host())
    live = _live_ids()
    sshconfig.refresh(None if live is None else live | {dive_id})
    return sshconfig.alias(dive_id), info


def attach(dive_id: int | None = None, vscode: bool = False) -> None:
    dive_id = dive_id or _pick("Attach to which Dive?")
    if in_container():
        # No key or alias in here: the shell is reached from your own machine.
        info = _wait_ready(dive_id)
        if info["head"]:
            ui.ok(f"Dive {dive_id} is on the Head: this Tidepool now has the GPU")
        else:
            ui.ok(f"Dive {dive_id} is up on {info.get('node')} — from your own machine: "
                  f"`cuttle dive attach {dive_id}`")
        return
    name, info = ensure_alias(dive_id)
    folder = f"/home/{info['user']}/{info['main']}"
    if info["head"]:
        ui.ok(f"Dive {dive_id} is on the Head: your Tidepool itself now has the GPU")
    else:
        ui.ok(f"Dive {dive_id} on {info.get('node')}: ~/{info['main']} is mirrored back to your Tidepool "
              f"every minute (read-only there until the Dive ends)")
    ui.warn(f"the GPU is yours until `cuttle dive end {dive_id}` or --time runs out — disconnecting "
            f"does not free it")
    if vscode:
        if sshconfig.open_vscode(name, folder):
            ui.ok(f"opening Dive {dive_id} in VS Code ({name})")
        return
    stop = threading.Event()
    _warn_thread(dive_id, _deadline(dive_id), stop)
    try:
        with transfer.Forward(name) as fwd:
            ui.hint(f"`cuttle download <path>` sends to {fwd.root}")
            ui.hint(f"`cuttle upload <path>` fetches from {fwd.root}")
            ui.hint(f"type `exit` to leave — the Dive keeps its GPU until `cuttle dive end {dive_id}`")
            with ui.handover():
                subprocess.run(["ssh", *fwd.ssh_args(), name])
    finally:
        stop.set()
    _report_ending(dive_id)


def _deadline(dive_id: int) -> float | None:
    try:
        row = next((r for r in auth._api("GET", "/dives") if r["id"] == dive_id), None)
    except (Exception, SystemExit):
        return None
    if not row or not row.get("time_limit") or not row.get("start_time"):
        return None
    return time.monotonic() + (row["start_time"] + row["time_limit"] * 60 - time.time())


def _warn_text(left: int) -> str:
    """`\\r\\n`-framed: ssh holds the terminal in raw mode."""
    return (f"\r\n*** ccc: --time runs out in ~{left // 60}m{left % 60}s — the Dive ends; the main "
            f"folder gets a final mirror, everything else on the Arm is gone ***\r\n")


def _warn_thread(dive_id: int, deadline: float | None, stop: threading.Event) -> None:
    if deadline is None:
        return
    remaining = deadline - time.monotonic()
    warn_at = deadline - max(_WARN_MIN_S, min(_WARN_MAX_S, remaining / 4))
    if warn_at <= time.monotonic():
        return

    def _loop():
        if not stop.wait(warn_at - time.monotonic()):
            os.write(2, _warn_text(max(0, round(deadline - time.monotonic()))).encode())

    threading.Thread(target=_loop, daemon=True).start()


def _report_ending(dive_id: int) -> None:
    """Left the shell: still running (and billing), ending (hit --time / ended), or gone."""
    try:
        state = next((r["state"] for r in auth._api("GET", "/dives") if r["id"] == dive_id), None)
    except (Exception, SystemExit):
        state = "running"
    if state is None or state in ("ending",) or state.startswith("completing"):
        ui.ok(f"Dive {dive_id} has ended")
    else:
        ui.warn(f"Dive {dive_id} is still running and still billing Karma — `cuttle dive end {dive_id}` "
                f"to end it, `cuttle dive attach {dive_id}` to go back")


def end(dive_id: int | None = None, yes: bool = False) -> None:
    """Your own over the API, from anywhere. Someone else's only on the Head: the API's 403 is the
    ownership test, and the operator's road is `sudo scancel` (the tick closes it either way). A 404
    is never escalated."""
    dive_id = dive_id or _pick("End which Dive?")
    if not yes and not ui.ask_confirm(f"End Dive {dive_id}? (the main folder gets a final mirror)"):
        raise ui.abort("not ended")
    if auth._api("DELETE", f"/dives/{dive_id}", refused_ok=True) is None:
        if not menu.is_head():
            raise ui.abort("you can only end your own Dives",
                           next_step="someone else's can only be ended by the operator on the Head")
        ui.say(f"  [muted]Dive {dive_id} isn't yours — ending it as the Head operator[/muted]")
        run(["scancel", str(dive_id)], sudo=True)
        run(["-u", config.HEAD_API_USER, "touch",
             str(config.HEAD_SPAWNS_DIR / str(dive_id) / "cancelled")], sudo=True, check=False)
    sshconfig.forget(dive_id)
    ui.ok(f"Dive {dive_id} ending")


def status(as_json: bool = False) -> None:
    rows = auth._api("GET", "/dives") or []

    def render():
        if not rows:
            ui.say("no open Dives")
            return
        ui.table(["ID", "STATE", "NODE", "MAIN", "DEPS", "GPU", "TIME"],
                 [[r["id"], r["state"], r.get("node") or "-", f"~/{r['main']}",
                   ", ".join(r["deps"]) or "-", r["gpus"], _minutes(r["minutes"] or 0)] for r in rows])

    ui.emit(rows, as_json, render)
