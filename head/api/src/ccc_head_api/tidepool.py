"""Tide Pool: connection plumbing for an interactive Spawn — a live shell in a Pod instead of a script.
Three jobs live here:

1. **Mint the keypair** (`grant`) — a fresh ed25519 pair per Spawn, never reused, never seen by the User.
   Public half goes into the job script's authorized_keys; private half is handed to the CLI over
   `GET /spawns/{id}/tidepool` — the CLI never manages a key of its own, matching CCC's built-in (no
   SSO/IdP) auth model everywhere else.

2. **Reserve the port** (`reserve_port`) — one number, reused as BOTH the sshd port inside the Pod on the
   Arm and the Head-side relay's listen port, so nothing has to reconcile the two. Reserved before the
   job_id exists — it must be baked into the job script before sbatch runs.

3. **Run the relay** (`ensure_relay`) — Users aren't Mesh members and have no route to an Arm's mesh
   address, so the Head forwards raw bytes from its own reachable address to the Arm for the life of one
   Spawn. **Except on the Head's own co-located Arm**, where the Pod is already on the address the client
   dials — see `ensure_relay` for why starting one there is actively harmful. A narrow purpose-built TCP
   proxy (relay.py), not a jump-host sshd (new attack surface, own host-key story) and not a websocket
   tunnel (new dependency; the ask is real SSH end to end).

The relay is a SEPARATE PROCESS (relay.py, its own asyncio loop), not a task inside the sync Head API or
the karma tick — so it's never blocked behind a slurmctld poll, and a wedged relay can't wedge either of
them. `ensure_relay` is idempotent and polled from `GET .../tidepool`, so attach doesn't wait on the
tick's cadence; `reap_dead` is the one place a relay ever goes back down.
"""

import logging
import os
import signal
import socket
import subprocess
import sys
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from . import db

log = logging.getLogger("ccc.api")

# One port range, reserved once at `cuttle setup --head` (cuttle.system.allow_tcp_port_range — must stay
# numerically in step with these two constants). 200 concurrent Tide Pools is far past this project's scale.
# BELOW Linux's default ephemeral range (net.ipv4.ip_local_port_range = 32768-60999) — see
# cuttle.config.TIDEPOOL_PORT_BASE for why a listen port must never sit inside it.
PORT_BASE = 31000
PORT_RANGE = 200


def _keypair() -> tuple[str, str]:
    """A fresh ed25519 keypair, OpenSSH-formatted so `ssh -i` and an authorized_keys line can use them
    with no conversion. Returns (private_pem_text, public_line_text)."""
    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode()
    return private, public + "\n"


def reserve_port(conn) -> int:
    """The lowest free port in the reserved range, actually CLAIMED (not just read) before returning —
    see db.reserve_tidepool_port for why the claim has to be its own table. Retries on the rare race
    where another submission claims the same candidate between our read and our insert."""
    for _ in range(10):
        used = db.used_tidepool_ports(conn)
        candidates = (p for p in range(PORT_BASE, PORT_BASE + PORT_RANGE) if p not in used)
        port = next(candidates, None)
        if port is None:
            raise ValueError(f"no free Tide Pool port in {PORT_BASE}-{PORT_BASE + PORT_RANGE} — too "
                             f"many Tide Pools running at once; cancel one and retry")
        if db.reserve_tidepool_port(conn, port):
            return port
    raise ValueError("could not reserve a Tide Pool port — too much contention, try again")


def grant(conn) -> tuple[int, str, str]:
    """Mint a Tide Pool's port + keypair, BEFORE the job script is rendered (both are baked into it).
    Returns (port, pubkey_line, privkey_pem). The `tidepools` row itself is written by the caller once
    the real job_id exists (spawns.submit, after sbatch returns — mirrors how the staging uuid is minted
    before sbatch and the Attribution row is written after)."""
    port = reserve_port(conn)
    private, public = _keypair()
    return port, public, private


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_relay(conn, job_id: int, node_mesh_ip: str, node: str, head_arm: bool = False) -> bool:
    """Make sure a Tide Pool's connection path is up, starting the Head-side relay if one is needed.
    Returns whether the port is actually accepting connections — what `ready` means to the CLI.

    Idempotent by design: called on every poll of `GET /spawns/{id}/tidepool` while the Spawn is
    RUNNING, so "is the process I already started still alive" has to be cheap and safe to repeat.
    `node_mesh_ip` is read fresh by the caller (off `slurm.arms()`) rather than cached here — an Arm's
    address cannot change under a running Spawn, but re-deriving it costs nothing and one fewer thing to
    keep in sync is one fewer way for this to drift from the source of truth.

    **No relay for the Head's own co-located Arm** (`head_arm`, off the registry's `head` flag). The
    relay exists because a User can't reach an Arm's mesh address directly; for this one Arm they
    demonstrably can — same box they already reach for the Dashboard/API — so the Pod's sshd is already
    reachable and `ssh <head>:<port>` lands on it directly.

    Starting one anyway is fatal, not just redundant: relay and Pod sshd share ONE port number on this
    Arm, and the relay wins the race — it fires the moment Slurm says RUNNING, before apptainer has
    booted the Pod. sshd then finds 0.0.0.0 taken and — because OpenSSH sets IPV6_V6ONLY on its v6
    listener — still binds `::` and reports itself up while unable to accept IPv4 at all. Mesh addresses
    are IPv4, so every connection reaches the relay instead, which forwards to this same Arm's mesh IP:
    **the relay forwards to itself**, and no ssh session ever reaches the Pod. `_accepting` can't see any
    of it — it connects to the relay and is satisfied.
    """
    row = db.get_tidepool(conn, job_id)
    if row is None:
        return False  # not a Tide Pool, or already reaped — nothing to do
    if head_arm:
        # Nothing to start and nothing to track, so this asks the only question left — has the Pod's
        # sshd finished booting? A short probe, not _LISTEN_TIMEOUT: that budget is for a process we
        # just forked and expect within milliseconds, whereas a Pod boot takes seconds and the CLI is
        # already polling. Once it answers true the CLI stops polling, so this costs one extra
        # "Connection closed [preauth]" line per attach — the same one the relay path already pays.
        return _accepting(row["port"], timeout=_PROBE_TIMEOUT)
    if _alive(row["relay_pid"]):
        return True
    port = row["port"]
    # Own process group (start_new_session): a relay must outlive the Head API worker that spawned it
    # (a request handler), and must not receive a signal meant for its parent. DEVNULL throughout — its
    # stdout/stderr are not worth a pipe (see ui.py's note on the same tradeoff for the clipboard tool);
    # a relay that cannot forward simply looks like a connection refused to the CLI, which is diagnosis
    # enough for a feature this narrow.
    proc = subprocess.Popen(
        [sys.executable, "-m", "ccc_head_api.relay", str(port), node_mesh_ip, str(port)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    db.set_tidepool_relay(conn, job_id, proc.pid, node)
    log.info("tidepool relay job=%s port=%s -> %s:%s pid=%s", job_id, port, node_mesh_ip, port, proc.pid)
    if not _accepting(port):
        log.warning("tidepool relay job=%s port=%s did not start listening in %ss", job_id, port,
                    _LISTEN_TIMEOUT)
        return False
    return True


# How long to give a freshly-spawned relay to bind. Generous: it is a Python interpreter start, paid
# once per attach, and the alternative to waiting is handing out a port that refuses connections.
_LISTEN_TIMEOUT = 5.0
# The head-Arm path waits on a Pod boot instead, which the CLI's own polling already covers — so this
# only has to be long enough to answer "is it up *now*", not to wait for it.
_PROBE_TIMEOUT = 0.3


def _accepting(port: int, timeout: float | None = None) -> bool:
    """Block until the relay is actually accepting on `port`, or the timeout runs out.

    `Popen` returns as soon as the fork succeeds — long before the child has imported asyncio and bound
    anything — so without this, `GET .../tidepool` reports `ready` roughly 200ms before the port
    exists. The CLI believes it and runs `ssh` immediately, which is a plain "Connection refused" on
    the FIRST attach to every Tide Pool; by the time anyone retries it works, which is exactly the
    shape of bug that survives testing. `ready` has to mean "you can connect", or it means nothing.

    A connect is the only honest test of that, and it costs one connection the relay dutifully forwards
    to the Pod's sshd — so each attach leaves one extra "Connection closed [preauth]" line in the Tide
    Pool's own stderr. Cheap, and visibly harmless next to the alternative."""
    deadline = time.monotonic() + (_LISTEN_TIMEOUT if timeout is None else timeout)
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def _kill(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass  # already gone


def reap_dead(conn, live_job_ids: set[int]) -> None:
    """Called every karma-tick pass: kill the relay and forget the connection state for any Tide Pool
    whose Spawn has left the queue. Mirrors retraction.py's shape (state + signal here, the tick is what
    calls it periodically) rather than adding a fourth systemd service just to watch one table."""
    for row in db.live_tidepools(conn):
        if row["job_id"] in live_job_ids:
            continue
        _kill(row["relay_pid"])
        db.delete_tidepool(conn, row["job_id"])
