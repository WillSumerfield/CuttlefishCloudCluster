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
following. It keeps billing Karma until `--time` runs out or `cuttle cancel <id>`, which is why leaving
the shell says so with the exact command to stop it.

**Being thrown out at `--time` is a different ending and says a different thing.** There the Spawn is
already over — the shell died under us, the job is COMPLETING, and nothing is billing — so repeating
"still running, still billing" would be false twice over. A Tide Pool's scratch gets one copy into the
day-long Recovery Window.
"""

import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import auth, config, settings, spawns as spawns_mod, sshconfig, ui, watch

_POLL_S = 1.0
_PLACEMENT_TIMEOUT_S = 3600     # an hour queued is already a long wait; fail loud rather than hang forever
_RELAY_TIMEOUT_S = 30           # the relay is brought up synchronously by the poll itself — this is slack
# How long before `--time` expires to interrupt the live session with a warning. Nothing is LOST at the
# limit: Slurm sends TERM and waits `KillWait` (600s — cli.templates) before KILL, and bash runs the job
# script's EXIT trap on TERM, which makes the Tide Pool's Recovery Window copy (ccc_head_api.spawns). What the User loses is the *shell* — it closes mid-keystroke
# with nothing they can do about it — which is the whole of what this warns about, and why the warning
# says what happens to the files rather than "save now".
# A quarter of the total, capped at 5 minutes and floored at 30s, is not exact science;
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


def _reattach_hint(spawn_id: int) -> str:
    """How to get back into this session."""
    return f"cuttle run --tidepool {spawn_id}"


def _noun(spawn_id: int) -> str:
    return f"tide pool {spawn_id}"


def run(script_or_id: str | None, minutes: int | None, vram: int | None, gpu: int,
        node: str | None, outputs: list[str], features: list[str], follow: bool,
        download: bool = True, manifest: str | None = None, substrate: str | None = None,
        no_gitignore: bool = False, vscode: bool = False,
        root: Path | None = None, name: str = "") -> None:
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
                            substrate=substrate, no_gitignore=no_gitignore, root=root, name=name)
    if download:
        arm(spawn_id)
    _wait_and_attach(spawn_id, vscode=vscode)
    # Again on the way out, and this is the arming that matters. A Tide Pool's **Output**s are most
    # often declared by the session itself rather than at submission — a `--output` handed to a
    # reattach, or simply the folder someone decided to write to — and the moment the shell closes is
    # the first time the whole picture exists. Arming at attach as well is what survives a crash or a
    # dropped link in between, and re-arming an entry that already settled costs one re-fetch of a
    # Spawn the Head 404s in the usual case.
    if download:
        arm(spawn_id)
        # After the shell detaches, not before: attaching blocks, and a sweeper forked ahead of it
        # would spend the whole session polling a Spawn this very process is sitting inside.
        watch.ensure_sweeper()


def arm(spawn_id: int) -> None:
    """Record that this box is owed `spawn_id`'s results, in the one predictable place (see config).

    Unconditional — never gated on whether an **Output** was declared here, because the declaration
    can happen anywhere: at submission or on rejoining a Tide Pool. An Output
    whose collection depended on *how* it came to be declared would be one whose results quietly
    stayed behind, which is exactly what the gate this replaces did. A Spawn that produced nothing
    costs a 404 the sweeper settles as EMPTY.
    """
    watch.arm(spawn_id, config.OUTPUTS_DIR)


def _request(minutes: int | None, vram: int | None, gpu: int, node: str | None,
            outputs: list[str], features: list[str], manifest: str | None = None,
            substrate: str | None = None,
            no_gitignore: bool = False, root: Path | None = None, name: str = "") -> int:
    """Submit a fresh Tide Pool. `--time`/`--vram` are exactly as required as they are for any Spawn —
    nothing here weakens that gate; `--output` and `--manifest` alone are optional, the first gated
    behind a confirm instead and the second simply absent when nobody asked for one."""
    minutes = ui.need(minutes, "--time", lambda: ui.ask_int("Wall-clock limit? (minutes)"))
    vram = ui.need(vram, "--vram", lambda: ui.ask_int("GPU memory needed? (GB, total across cards)"))
    # A bare Tide Pool is one of the two Spawns with no name of its own to inherit, so it is asked for
    # one exactly like the two declarations above. Reattaching to an existing id never reaches here —
    # that Spawn was named when it was requested.
    name = ui.need(name, "--name", lambda: ui.ask_text("What should this tide pool be called?"))
    outputs = list(outputs) if outputs else []
    if not outputs:
        # Not permanently lost — the day-long recovery window rsyncs the whole scratch dir home
        # regardless of what was declared. But once that window closes nothing here is retrievable as a
        # normal output, so still worth a warning, just not "lost forever". Sharper than it used to be:
        # there is no longer a catch-all directory that is staged home whether or not anything was
        # declared, so with no --output there is nowhere in this Pod whose contents outlive it.
        warning = ("no --output declared — nothing this Tide Pool writes is kept as a result. It is "
                   "recoverable for a day after the session ends (`cuttle recover`), then gone")
        if ui.interactive():
            ui.warn(warning)
            if not ui.ask_confirm("Continue with no declared output dir?", False):
                raise ui.abort("not submitted")
        else:
            # No terminal to confirm on — this is optional, not a hard gate (unlike --time/--vram), so a
            # scripted request proceeds; it just cannot be asked first.
            ui.warn(warning)
    spawn_id = spawns_mod.submit_tidepool(minutes, vram, gpu, outputs, node=node, features=features,
                                          manifest=manifest, substrate=substrate,
                                          no_gitignore=no_gitignore, root=root, name=name)
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


def _wait_for_relay(spawn_id: int) -> tuple[int, str, str, str | None]:
    """Poll until the Head hands back the port + private key + login account + Pod working directory —
    see main.spawn_tidepool: this call is what BRINGS the relay up, not just a passive read, so it
    converges quickly rather than waiting on the karma tick's cadence.

    The account is the owner's **Guise** (`ccc-u<uid>`), and it has to come from the Head because a uid
    is a Head-side fact — there is nothing here to derive it from. `proj` is Head-side for the same
    kind of reason: it depends on which Arm took the Spawn. Older Heads send neither; falling back to
    the Mantle, and to no folder at all, keeps this CLI working against one."""
    deadline = time.monotonic() + _RELAY_TIMEOUT_S
    with ui.step("connecting"):
        while True:
            info = auth._api("GET", f"/spawns/{spawn_id}/tidepool")
            if info["ready"]:
                return (info["port"], info["private_key"],
                        info.get("user") or config.HEAD_API_USER, info.get("proj"))
            if time.monotonic() > deadline:
                raise ui.abort(f"tide pool {spawn_id} is running but never came up for SSH — "
                               f"`cuttle run --tidepool {spawn_id}` to retry")
            time.sleep(_POLL_S)


def _live_ids() -> set[int] | None:
    """Every Spawn id the Head still has in its queue, or None if it could not be asked. `None` is not
    "none of them" — see sshconfig.refresh, which prunes on this and must not act on a failed GET."""
    try:
        return {r["id"] for r in auth._api("GET", "/spawns") if r.get("id") is not None}
    except Exception:
        return None


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


def _warn_text(spawn_id: int, left: int) -> str:
    """The `--time` warning, `\\r\\n`-framed for a terminal `ssh` has in raw mode. A Tide Pool's
    scratch is copied ONCE, at the limit, into the day-long Recovery Window.
    """
    what = ("the shell closes with no warning. Declared outputs come home as normal; "
            f"everything else is recoverable for a day with `cuttle recover {spawn_id}`")
    return f"\r\n*** ccc: --time runs out in ~{left // 60}m{left % 60}s — {what} ***\r\n"


def _warn_thread(spawn_id: int, deadline: float | None,
                 stop: threading.Event) -> threading.Thread | None:
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
        os.write(2, _warn_text(spawn_id, left).encode())

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def _copy_hint(name: str, proj: str | None) -> None:
    """How to get a file INTO the session. Printed with the alias, because the two are one answer: the
    alias is the only thing carrying the Pod's relay port AND its ephemeral key, and a User who reaches
    for `<guise>@<head>` instead lands on the Head's own sshd — where the Guise is a real, passwordless
    account, so the prompt they get is one no password can answer.

    The path is spelled out rather than left as `~`: an scp with no path lands in the Pod's home, which
    is a 64MB tmpfs that dies with the session and is mirrored nowhere.
    """
    if proj:
        ui.hint(f"copy files in with `scp <file> {name}:{proj}/` (that folder is the session's working "
                f"directory — a bare `{name}:` would land in a home that keeps nothing)")


def _wait_and_attach(spawn_id: int, vscode: bool = False) -> None:
    """Attach to a placed Spawn and hold the terminal until the shell detaches — or, with `vscode`,
    hand the session to VS Code and return immediately.
    """
    _wait_for_running(spawn_id)
    port, private_key, login, proj = _wait_for_relay(spawn_id)
    deadline = _deadline(spawn_id)

    # On disk under ~/.ccc, not in a tempfile unlinked when `ssh` exits: an alias in ~/.ssh/ccc.config
    # is only as good as the IdentityFile it names, and VS Code opens its own connections long after
    # this process is gone. The key dies with the Tide Pool instead — sshconfig.refresh reaps both.
    host = _head_host()
    keypath = sshconfig.record(spawn_id, private_key=private_key, port=port, user=login,
                               host=host, proj=proj)
    name = sshconfig.alias(spawn_id)
    # THIS session is live by construction — we just waited for RUNNING and the Head just handed over
    # its key. Adding it by hand rather than trusting the queue read costs nothing and takes the one
    # outcome that would actually hurt (squeue lagging by a poll, and the sweep deleting the key this
    # very attach is about to use) off the table entirely.
    live = _live_ids()
    sshconfig.refresh(None if live is None else live | {spawn_id})

    if vscode:
        # No terminal, so no `--time` warner (nothing to write to) and no ending to report: this
        # process exits while the session goes on running, which is exactly what detaching means here.
        ui.warn(f"this GPU is yours until you cancel it — `{_cancel_hint(spawn_id)}` — or --time runs "
                f"out. Closing VS Code does NOT free it.")
        if sshconfig.open_vscode(name, proj):
            ui.ok(f"opening {_noun(spawn_id)} in VS Code ({name})")
        ui.hint(f"reconnect any time with `ssh {name}`, or `{_reattach_hint(spawn_id)}` "
                f"for a terminal here")
        _copy_hint(name, proj)
        return

    ui.warn(f"this GPU is yours until you cancel it — `{_cancel_hint(spawn_id)}` — or --time runs "
            f"out. Disconnecting (closing this terminal, a dropped connection) does NOT free it.")
    # Said up front, not only at the limit: where the files go is what a User plans the session
    # around, and learning it five minutes before the cutoff is learning it too late to act on.
    ui.hint(f"declared outputs come home as normal (`cuttle outputs {spawn_id}`); everything "
            f"else is copied when it ends and recoverable for a day "
            f"(`cuttle recover {spawn_id}`)")
    ui.hint("type `exit` to leave the shell — the Tide Pool itself keeps running either way")
    # The alias is live from here on whether or not anyone asked for VS Code — printed rather than
    # acted on, because an editor is not what most people opened a terminal to get.
    ui.hint(f"editors and a second terminal reach this session as `{name}` (ssh {name}) — "
            f"`--vscode` opens it there instead")
    _copy_hint(name, proj)
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
            f"{login}@{host}",
        ])
    finally:
        stop.set()

    _report_ending(spawn_id)


def _report_ending(spawn_id: int) -> None:
    """Say how the session ended, once the shell has let go of the terminal.

    Three endings, not two, because there are three ways to arrive here.

    `exit` was typed or the connection dropped -> the Spawn is genuinely RUNNING and genuinely
    billing, which is the warning that has to be repeated with the command to stop it.

    `--time` ran out -> Slurm TERM'd the job and the shell died under us. The job is now COMPLETING
    while its EXIT trap sends the work home, for up to `KillWait` (600s — cli.templates). It is NOT
    running and it is NOT billing — the tick only accrues onto RUNNING Spawns (ccc_head_api.tick) — so
    the running-and-billing warning is false in both halves, and at a 10-minute KillWait it would stay
    false for ten minutes. `done` cannot tell us this: it stays False through COMPLETING on purpose,
    because `watch` gates the output download on it and a download fired mid-stage-out fetches nothing.

    Already settled -> plain end.
    """
    try:
        st = auth._api("GET", f"/spawns/{spawn_id}")
        state = None if st.get("done") else (st.get("state") or "RUNNING")
    except Exception:
        state = "RUNNING"   # can't check — say the thing with a cost attached
    noun = _noun(spawn_id)
    if state == "COMPLETING":
        ui.ok(f"{noun} hit its --time limit and has stopped billing — your files are being "
              f"copied home now, `cuttle recover {spawn_id}` once it settles")
    elif state is not None:
        ui.warn(f"{noun} is still running and still billing Karma — `{_cancel_hint(spawn_id)}` to end "
                f"it, or `{_reattach_hint(spawn_id)}` to reattach")
    else:
        ui.ok(f"{noun} has ended")
