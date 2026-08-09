"""`cuttle run --tidepool`: request-or-attach to a Tide Pool — an interactive Spawn that drops the
User into a live shell instead of running a script.

A Tide Pool is a normal Spawn end to end (same queue, Eligibility, Escrow/Karma billing — see
ccc_head_api.spawns.submit_checked); this module is the CLI-only half nothing else needed: submit (or
resolve an existing one), poll for placement, fetch the ephemeral keypair the Head minted, and exec a
REAL `ssh` — the Head-side relay (ccc_head_api.tidepool/relay.py) is what makes an Arm reachable from a
client that is not a Mesh member, but nothing on this end knows or cares that a relay exists; it looks
like any other `ssh -p <port> <host>`.

**Disconnecting never ends the Spawn.** A clean `exit`, a dropped connection, Ctrl-C — none of them
cancel it, exactly like Ctrl-C on `cuttle run --follow`'s log stream does not cancel the Spawn it is
following. It keeps billing Karma until `--time` runs out or `cuttle cancel <id>`, which is why every
exit path below prints the same reminder with the exact command to stop it.
"""

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import auth, config, settings, spawns as spawns_mod, ui, watch

_POLL_S = 1.0
_PLACEMENT_TIMEOUT_S = 3600     # an hour queued is already a long wait; fail loud rather than hang forever
_RELAY_TIMEOUT_S = 30           # the relay is brought up synchronously by the poll itself — this is slack
# How long before `--time` expires to interrupt the live session with a warning. Slurm gives an
# interactive Spawn zero grace at the limit (unlike a batch one, there is no save-and-exit moment), so
# this has to fire well before the cutoff — but not so early that a short Tide Pool never sees the
# session start. A quarter of the total, capped at 5 minutes and floored at 30s, is not exact science;
# it just has to exist (this feature's own spec is explicit that the threshold is an engineering call).
_WARN_MAX_S = 5 * 60
_WARN_MIN_S = 30


def _existing_id(token: str | None) -> int | None:
    """The positional arg, if it parses as a Spawn id — `None` (fresh request) otherwise, including when
    nothing was given at all. Ownership/type ("is this actually a Tide Pool of mine?") is NOT checked
    here — the Head's `GET /spawns/{id}/tidepool` is the one place that answers it, and answering it
    twice would just be two ways to disagree."""
    if not token:
        return None
    try:
        return int(token)
    except ValueError:
        return None


def _head_host() -> str:
    """The bare host the CLI already talks to for the Head API — the Tide Pool relay listens on the
    SAME box, at a different port, so there is no separate address to configure or discover. (When the
    Spawn landed on the Head's own co-located Arm there is no relay at all and this reaches the Pod's
    sshd directly — same address either way, which is the point. See ccc_head_api.tidepool.)"""
    url = settings.load().get("head") or ""
    host = urlsplit(url).hostname
    if not host:
        raise ui.abort("no Head configured — run `cuttle login --head <url>` first")
    return host


def _cancel_hint(spawn_id: int) -> str:
    return f"cuttle cancel {spawn_id}"


def run(script_or_id: str | None, minutes: int | None, vram: int | None, gpu: int,
        node: str | None, outputs: list[str], features: list[str], follow: bool,
        download: bool = True, manifest: str | None = None, no_gitignore: bool = False,
        root: Path | None = None) -> None:
    """Entry point from `cuttle run --tidepool`. Requests a new Tide Pool (needing --time/--vram like any
    Spawn) unless `script_or_id` resolves to an existing one, then attaches either way.

    `manifest` is optional here and only here — see `spawns.submit_tidepool`. Passing one uploads the
    working directory and builds that **Environment** into the shell, so what you debug in is what your
    Spawn would have run in."""
    if follow:
        ui.warn("--follow is ignored with --tidepool — it attaches an interactive shell instead of "
                "streaming output")
    existing = _existing_id(script_or_id)
    if existing is not None:
        spawn_id = existing
    else:
        if script_or_id:
            ui.warn(f"ignoring {script_or_id!r} — a Tide Pool has no entry script "
                    f"(pass a Spawn id instead to reattach to one)")
        spawn_id = _request(minutes, vram, gpu, node, outputs, features, manifest=manifest,
                            no_gitignore=no_gitignore, root=root)
        # Only when it declared somewhere for results to land. A Tide Pool with no --output has
        # nothing to auto-download by construction — everything it wrote lives and dies in the
        # Recovery Window, which is deliberately not something to plan around (CONTEXT.md).
        if download and outputs:
            watch.arm(spawn_id, config.OUTPUTS_DIR)   # one predictable place — see config
    _wait_and_attach(spawn_id)
    # After the shell detaches, not before: attaching blocks, and a sweeper forked ahead of it would
    # spend the whole session polling a Spawn this very process is sitting inside.
    if download and outputs:
        watch.ensure_sweeper()


def _request(minutes: int | None, vram: int | None, gpu: int, node: str | None,
            outputs: list[str], features: list[str], manifest: str | None = None,
            no_gitignore: bool = False, root: Path | None = None) -> int:
    """Submit a fresh Tide Pool. `--time`/`--vram` are exactly as required as they are for any Spawn —
    nothing here weakens that gate; `--output` and `--manifest` alone are optional, the first gated
    behind a confirm instead and the second simply absent when nobody asked for one."""
    minutes = ui.need(minutes, "--time", lambda: ui.ask_int("Wall-clock limit? (minutes)"))
    vram = ui.need(vram, "--vram", lambda: ui.ask_int("GPU memory needed? (GB, total across cards)"))
    outputs = list(outputs) if outputs else []
    if not outputs:
        # Not permanently lost — the day-long recovery window rsyncs the whole scratch dir home
        # regardless of what was declared. But once that window closes nothing here is retrievable
        # through the normal $CCC_OUT/outputs path, so still worth a warning, just not "lost forever".
        warning = ("no --output declared — anything this Tide Pool writes OUTSIDE a declared output "
                   "dir is only recoverable for a day after it ends, then gone (only $CCC_OUT is "
                   "staged back to the Head as a normal output)")
        if ui.interactive():
            ui.warn(warning)
            if not ui.ask_confirm("Continue with no declared output dir?", False):
                raise ui.abort("not submitted")
        else:
            # No terminal to confirm on — this is optional, not a hard gate (unlike --time/--vram), so a
            # scripted request proceeds; it just cannot be asked first.
            ui.warn(warning)
    spawn_id = spawns_mod.submit_tidepool(minutes, vram, gpu, outputs, node=node, features=features,
                                          manifest=manifest, no_gitignore=no_gitignore, root=root)
    ui.ok(f"requested tide pool {spawn_id}")
    return spawn_id


def _wait_for_running(spawn_id: int) -> None:
    deadline = time.monotonic() + _PLACEMENT_TIMEOUT_S
    with ui.step("waiting for placement"):
        while True:
            st = auth._api("GET", f"/spawns/{spawn_id}")
            if st["state"] == "RUNNING":
                return
            if st.get("done"):
                raise ui.abort(f"tide pool {spawn_id} ended before it started "
                               f"({spawns_mod._outcome(st)})")
            if time.monotonic() > deadline:
                raise ui.abort(f"tide pool {spawn_id} is still queued after an hour — it is not gone, "
                               f"just not placed yet; `cuttle run --tidepool {spawn_id}` to keep waiting")
            time.sleep(_POLL_S)


def _wait_for_relay(spawn_id: int) -> tuple[int, str, str]:
    """Poll until the Head hands back the port + private key + login account — see main.spawn_tidepool:
    this call is what BRINGS the relay up, not just a passive read, so it converges quickly rather than
    waiting on the karma tick's cadence.

    The account is the owner's **Guise** (`ccc-u<uid>`), and it has to come from the Head because a uid
    is a Head-side fact — there is nothing here to derive it from. Older Heads send no `user`; falling
    back to the Mantle keeps this CLI working against one."""
    deadline = time.monotonic() + _RELAY_TIMEOUT_S
    with ui.step("connecting"):
        while True:
            info = auth._api("GET", f"/spawns/{spawn_id}/tidepool")
            if info["ready"]:
                return info["port"], info["private_key"], info.get("user") or config.HEAD_API_USER
            if time.monotonic() > deadline:
                raise ui.abort(f"tide pool {spawn_id} is running but never came up for SSH — "
                               f"`cuttle run --tidepool {spawn_id}` to retry")
            time.sleep(_POLL_S)


def _deadline(spawn_id: int) -> float | None:
    """This Tide Pool's `start_time + time_limit`, as a monotonic-clock deadline — or None if either is
    unknown (a freshly-started Spawn squeue hasn't fully reported yet). Best-effort: missing it just
    means no advance warning, never a reason to refuse the session."""
    try:
        rows = auth._api("GET", "/spawns")
    except Exception:
        return None
    row = next((r for r in rows if r["id"] == spawn_id), None)
    if not row or not row.get("time_limit") or not row.get("start_time"):
        return None
    wall_deadline = row["start_time"] + row["time_limit"] * 60
    return time.monotonic() + (wall_deadline - time.time())


def _warn_thread(spawn_id: int, deadline: float | None, stop: threading.Event) -> threading.Thread | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    warn_at = deadline - max(_WARN_MIN_S, min(_WARN_MAX_S, remaining / 4))
    if warn_at <= time.monotonic():
        return None   # already inside the warning window — nothing useful left to schedule

    def _loop():
        if stop.wait(warn_at - time.monotonic()):
            return   # the session ended before the warning was due
        # Direct terminal write, not ui.warn: `ssh` owns the screen (raw mode, its own pty) by the time
        # this fires, and this is a second thread writing beside it — interleaved, not framed. That is
        # an accepted cosmetic cost; the alternative is no warning at all inside a live session.
        left = max(0, round(deadline - time.monotonic()))
        os.write(2, f"\r\n*** ccc: this Tide Pool's --time runs out in ~{left // 60}m{left % 60}s — "
                    f"Slurm gives an interactive Spawn NO grace period, save now ***\r\n".encode())

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def _wait_and_attach(spawn_id: int) -> None:
    _wait_for_running(spawn_id)
    port, private_key, login = _wait_for_relay(spawn_id)
    deadline = _deadline(spawn_id)

    fd, keypath = tempfile.mkstemp(prefix="ccc-tidepool-")
    try:
        os.write(fd, private_key.encode())
        os.close(fd)
        os.chmod(keypath, 0o600)

        ui.warn(f"this GPU is yours until you cancel it — `{_cancel_hint(spawn_id)}` — or --time runs "
                f"out. Disconnecting (closing this terminal, a dropped connection) does NOT free it.")
        ui.hint("type `exit` to leave the shell — the Tide Pool itself keeps running either way")
        stop = threading.Event()
        _warn_thread(spawn_id, deadline, stop)
        try:
            # Real ssh, real protocol — the relay is a pure byte pipe underneath (relay.py); nothing
            # about the session is aware it exists. Host-key checking is off deliberately: the Pod mints
            # a fresh host key every Tide Pool, so there is nothing to pin, and the actual security
            # boundary is the ephemeral client keypair handed over the CLI's own authenticated session.
            #
            # Login account is the submitter's own **Guise**, never `mantle@` or `ccc@`: apptainer's
            # --writable-tmpfs patches the invoking uid's /etc/passwd entry to its real host identity,
            # so the invoking uid is the Guise owning this Spawn. User never types it (nobody knows
            # their own uid) — see spawns.PAYLOAD_TIDEPOOL.
            subprocess.run([
                "ssh", "-i", keypath, "-p", str(port),
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                # LogLevel=ERROR: the discarded (/dev/null) known_hosts still gets an "added" notice
                # every single connection otherwise — pure noise for a host key nothing ever pins.
                "-o", "LogLevel=ERROR",
                "-o", "IdentitiesOnly=yes", "-o", "ServerAliveInterval=30",
                f"{login}@{_head_host()}",
            ])
        finally:
            stop.set()
    finally:
        Path(keypath).unlink(missing_ok=True)

    # Whether `exit` was typed or the connection just dropped, the Spawn itself is likely still running
    # — say so again, with the exact command, rather than leaving the User to wonder why it is not in
    # `cuttle queue` anymore (it still would be) or why Karma is still moving.
    try:
        st = auth._api("GET", f"/spawns/{spawn_id}")
        still_running = not st.get("done")
    except Exception:
        still_running = True   # can't check — say the safe thing
    if still_running:
        ui.warn(f"tide pool {spawn_id} is still running and still billing Karma — "
                f"`{_cancel_hint(spawn_id)}` to end it, or `cuttle run --tidepool {spawn_id}` to "
                f"reattach")
    else:
        ui.ok(f"tide pool {spawn_id} has ended")
