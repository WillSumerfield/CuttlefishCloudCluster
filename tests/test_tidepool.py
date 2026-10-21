"""Tide Pool (docs/TODO.md): a Spawn whose payload is a live shell instead of a script.

What is tested here: the port reservation (has to be race-safe before a job_id exists), the keypair
round-trip through at-rest encryption, the `GET /spawns/{id}/tidepool` route's
auth/ownership/readiness contract, tick.py's reap, the CLI's session-ending copy, and the zsh the Head
renders. Dormant in 2.0: nothing submits a Tide Pool until Dives. NOT tested: a real sshd/ssh session — that needs real
hardware (same carve-out the Beak/MIG work already has in docs/TODO.md).
"""

import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db, guises, slurm, tick, zshrc
from ccc_head_api import main as head_main
from ccc_head_api import spawns as head_spawns
from ccc_head_api import tidepool as head_tidepool
from cuttle import config, templates
from cuttle import tidepool as cli_tidepool


# --- keypair + port reservation ----------------------------------------------

class TestKeypair:
    def test_it_is_a_usable_openssh_pair(self):
        private, public = head_tidepool._keypair()
        assert private.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
        assert public.startswith("ssh-ed25519 ")

    def test_two_calls_are_different_keys(self):
        p1, _ = head_tidepool._keypair()
        p2, _ = head_tidepool._keypair()
        assert p1 != p2


class TestPortReservation:
    @pytest.fixture(autouse=True)
    def _db(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()

    def test_lowest_free_port_is_picked(self):
        with db.connect() as conn:
            port = head_tidepool.reserve_port(conn)
        assert port == head_tidepool.PORT_BASE

    def test_a_reserved_port_is_not_picked_again(self):
        with db.connect() as conn:
            first = head_tidepool.reserve_port(conn)
            second = head_tidepool.reserve_port(conn)
        assert first != second
        assert second == head_tidepool.PORT_BASE + 1

    def test_release_frees_it_for_reuse(self):
        with db.connect() as conn:
            first = head_tidepool.reserve_port(conn)
            db.release_tidepool_port(conn, first)
            second = head_tidepool.reserve_port(conn)
        assert second == first

    def test_exhausting_the_range_is_a_clean_error(self, monkeypatch):
        monkeypatch.setattr(head_tidepool, "PORT_RANGE", 1)
        with db.connect() as conn:
            head_tidepool.reserve_port(conn)
            with pytest.raises(ValueError, match="no free Tide Pool port"):
                head_tidepool.reserve_port(conn)


class TestThePortRangeIsNotEphemeral:
    """These ports are LISTENED on — by the relay on the Head, by the Pod's sshd on an Arm — so the
    range must sit outside the kernel's ephemeral range. Inside it, the bind races the kernel's own
    allocator on every box in the cluster: any outbound connection can be handed the port a Tide Pool
    is about to want, and the bind then fails with EADDRINUSE for no reason anyone can reproduce."""

    def test_it_is_below_the_default_ephemeral_range(self):
        assert head_tidepool.PORT_BASE + head_tidepool.PORT_RANGE <= 32768   # Linux default low bound

    def test_it_is_below_this_hosts_ephemeral_range(self):
        proc = Path("/proc/sys/net/ipv4/ip_local_port_range")
        if not proc.exists():
            pytest.skip("not Linux")
        low = int(proc.read_text().split()[0])
        assert head_tidepool.PORT_BASE + head_tidepool.PORT_RANGE <= low

    def test_the_cli_states_the_same_range(self):
        """cuttle.config duplicates these to open the ufw rule at `setup --head` (the two packages do
        not import each other), so a change on one side that misses the other silently firewalls off
        every Tide Pool."""
        from cuttle import config as cli_config
        assert cli_config.TIDEPOOL_PORT_BASE == head_tidepool.PORT_BASE
        assert cli_config.TIDEPOOL_PORT_RANGE == head_tidepool.PORT_RANGE


class TestTidepoolCrud:
    @pytest.fixture(autouse=True)
    def _db(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CCC_HEAD_SECRET_KEY", raising=False)
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                        "VALUES (1,'alice',0,'x',0)")
            db.record_spawn(conn, job_id=99, user_id=1, script="", tidepool=True)
            conn.commit()

    def test_private_key_round_trips_through_at_rest_encryption(self):
        with db.connect() as conn:
            port = head_tidepool.reserve_port(conn)
            db.create_tidepool(conn, 99, port, "ssh-ed25519 AAAA...", "-----BEGIN OPENSSH...-----")
            key = db.tidepool_private_key(conn, 99)
        assert key == "-----BEGIN OPENSSH...-----"

    def test_delete_releases_the_port_too(self):
        with db.connect() as conn:
            port = head_tidepool.reserve_port(conn)
            db.create_tidepool(conn, 99, port, "pub", "priv")
            db.delete_tidepool(conn, 99)
            assert db.get_tidepool(conn, 99) is None
            # Freed, not just forgotten — a later Tide Pool can claim the same number again.
            assert head_tidepool.reserve_port(conn) == port

    def test_spawn_row_carries_the_flag(self):
        with db.connect() as conn:
            row = db.get_spawn(conn, 99)
        assert row["tidepool"] == 1


# --- GET /spawns/{id}/tidepool over HTTP --------------------------------------

def _client(monkeypatch, tmp_path, user_id=1, is_admin=False):
    user = {"id": user_id, "username": "alice", "is_admin": is_admin, "must_change_password": False}
    head_main.app.dependency_overrides[head_main.current_user] = lambda: user
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
    monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box"}])
    return TestClient(head_main.app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


# The first uid the allocator hands out. These tests seed `users` with raw SQL, so nobody has a Guise
# until the route asks for one — and the first ask gets the base.
_GUISE = guises.name(guises.UID_BASE)


class TestTidepoolRoute:
    def _seed(self, tmp_path, job_id=77, owner_id=1, tidepool=True, port=41000):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                "VALUES (?,?,?,?,0)", (owner_id, f"u{owner_id}", 0, "x"))
            db.record_spawn(conn, job_id=job_id, user_id=owner_id, script="",
                            tidepool=tidepool)
            if tidepool:
                db.create_tidepool(conn, job_id, port, "ssh-ed25519 AAAA", "PRIVATEKEY")
            conn.commit()

    def test_unknown_spawn_is_404(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert c.get("/api/spawns/999/tidepool").status_code == 404

    def test_an_ordinary_spawn_is_404_too(self, monkeypatch, tmp_path):
        """A Spawn id that exists but was never a Tide Pool — the route refuses it outright rather than
        answering not-ready forever."""
        c = _client(monkeypatch, tmp_path)
        self._seed(tmp_path, tidepool=False)
        assert c.get("/api/spawns/77/tidepool").status_code == 404

    def test_a_strangers_tidepool_is_403(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path, user_id=1)
        self._seed(tmp_path, owner_id=2)   # owned by someone else
        assert c.get("/api/spawns/77/tidepool").status_code == 403

    def test_not_ready_while_pending(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        self._seed(tmp_path)
        monkeypatch.setattr(head_spawns, "active_state", lambda jid: "PENDING")
        r = c.get("/api/spawns/77/tidepool")
        assert r.status_code == 200
        # `user` is the owner's Guise — what the CLI ssh's in as. Present even when not ready: the CLI
        # polls this same route, and the account does not depend on the relay being up.
        assert r.json() == {"ready": False, "port": None, "private_key": None, "user": _GUISE, "proj": None}

    def test_ready_once_running_and_relayed(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        self._seed(tmp_path, port=41007)
        monkeypatch.setattr(head_spawns, "active_state", lambda jid: "RUNNING")
        monkeypatch.setattr(slurm, "spawns", lambda: [{"id": 77, "node": "gpu-box"}])
        monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box", "mesh_ip": "100.99.0.5"}])
        # ensure_relay would actually fork a subprocess — not what this test is about.
        calls = []

        def fake(conn, jid, ip, node, head_arm=False):
            calls.append((jid, ip, node, head_arm))
            return True

        monkeypatch.setattr(head_tidepool, "ensure_relay", fake)
        r = c.get("/api/spawns/77/tidepool")
        assert r.status_code == 200
        body = r.json()
        assert body == {"ready": True, "port": 41007, "private_key": "PRIVATEKEY", "user": _GUISE,
                        "proj": "/tmp/ccc-77/proj"}
        assert calls == [(77, "100.99.0.5", "gpu-box", False)]

    def test_not_ready_while_nothing_accepts_on_the_port(self, monkeypatch, tmp_path):
        """`ready` is ensure_relay's answer, not merely "we called it" — a relay that never bound, or a
        Pod whose sshd is still booting, both have to keep the CLI polling."""
        c = _client(monkeypatch, tmp_path)
        self._seed(tmp_path, port=41007)
        monkeypatch.setattr(head_spawns, "active_state", lambda jid: "RUNNING")
        monkeypatch.setattr(slurm, "spawns", lambda: [{"id": 77, "node": "gpu-box"}])
        monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box", "mesh_ip": "100.99.0.5"}])
        monkeypatch.setattr(head_tidepool, "ensure_relay", lambda *a, **kw: False)
        assert c.get("/api/spawns/77/tidepool").json()["ready"] is False

    def test_the_heads_own_arm_is_flagged_through_to_ensure_relay(self, monkeypatch, tmp_path):
        """The whole fix rides on this one boolean reaching ensure_relay — see
        TestTheHeadsOwnArmNeedsNoRelay for what happens when a relay starts there anyway."""
        c = _client(monkeypatch, tmp_path)
        self._seed(tmp_path, port=41007)
        monkeypatch.setattr(head_spawns, "active_state", lambda jid: "RUNNING")
        monkeypatch.setattr(slurm, "spawns", lambda: [{"id": 77, "node": "head-box"}])
        monkeypatch.setattr(slurm, "arms",
                            lambda: [{"name": "head-box", "mesh_ip": "100.99.0.1", "head": True}])
        seen = []
        monkeypatch.setattr(head_tidepool, "ensure_relay",
                            lambda conn, jid, ip, node, head_arm=False: seen.append(head_arm) or True)
        assert c.get("/api/spawns/77/tidepool").json()["ready"] is True
        assert seen == [True]

    def test_a_bare_tidepool_on_the_heads_own_arm_still_reports_head_scratch(
            self, monkeypatch, tmp_path):
        """On the Head's own Arm the Head-local scratch form is right."""
        c = _client(monkeypatch, tmp_path)
        self._seed(tmp_path, port=41007)
        monkeypatch.setattr(head_spawns, "active_state", lambda jid: "RUNNING")
        monkeypatch.setattr(slurm, "spawns", lambda: [{"id": 77, "node": "head-box"}])
        monkeypatch.setattr(slurm, "arms",
                            lambda: [{"name": "head-box", "mesh_ip": "100.99.0.1", "head": True}])
        monkeypatch.setattr(head_tidepool, "ensure_relay", lambda *a, **kw: True)
        assert c.get("/api/spawns/77/tidepool").json()["proj"] == \
            f"{head_spawns.SCRATCH_DIR}/77/proj"

    def test_not_ready_if_the_arm_has_no_mesh_ip_yet(self, monkeypatch, tmp_path):
        """Belt and suspenders: RUNNING in squeue but the registry hasn't caught up is not attachable."""
        c = _client(monkeypatch, tmp_path)
        self._seed(tmp_path)
        monkeypatch.setattr(head_spawns, "active_state", lambda jid: "RUNNING")
        monkeypatch.setattr(slurm, "spawns", lambda: [{"id": 77, "node": "gpu-box"}])
        monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box", "mesh_ip": None}])
        r = c.get("/api/spawns/77/tidepool")
        assert r.json() == {"ready": False, "port": None, "private_key": None, "user": _GUISE, "proj": None}


class TestReadyMeansConnectable:
    """`ready: true` is the CLI's cue to run `ssh` — there's no second check between the two. So
    the route may only say it once the relay is accepting, and `Popen` returning is not that: the
    child still has to start a Python interpreter and bind. The gap is ~200ms — long enough to
    lose to the CLI reliably, producing "Connection refused" on the FIRST attach and working on
    retry.
    """

    def _seed(self, port):
        with db.connect() as conn:
            conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                         "VALUES (1,'u1',0,'x',0)")
            db.record_spawn(conn, job_id=77, user_id=1, script="", tidepool=True)
            db.create_tidepool(conn, 77, port, "ssh-ed25519 AAAA", "PRIVATEKEY")
            conn.commit()

    def _free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_it_waits_for_the_relay_to_accept(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        port = self._free_port()
        self._seed(port)
        listening = []

        def slow_listener():
            """Stands in for the real relay: alive immediately, accepting a little later."""
            time.sleep(0.3)
            srv = socket.socket()
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", port))
            srv.listen()
            listening.append(srv)

        def fake_popen(*a, **kw):
            threading.Thread(target=slow_listener, daemon=True).start()
            return type("P", (), {"pid": os.getpid()})()   # a pid that is genuinely alive

        monkeypatch.setattr(head_tidepool.subprocess, "Popen", fake_popen)
        try:
            with db.connect() as conn:
                head_tidepool.ensure_relay(conn, 77, "100.99.0.5", "gpu-box")
            # The moment ensure_relay returns, a client must be able to connect — this IS the contract.
            with socket.socket() as s:
                s.settimeout(1.0)
                assert s.connect_ex(("127.0.0.1", port)) == 0
        finally:
            for srv in listening:
                srv.close()

    def test_a_relay_that_never_binds_does_not_hang_the_route(self, monkeypatch, tmp_path):
        """The other side of the wait: a relay that dies on startup must not wedge the request handler
        (and with it the Head API worker) — the caller gets an unusable port and a logged warning,
        which is the same outcome as before this wait existed."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        self._seed(self._free_port())
        monkeypatch.setattr(head_tidepool, "_LISTEN_TIMEOUT", 0.3)
        monkeypatch.setattr(head_tidepool.subprocess, "Popen",
                            lambda *a, **kw: type("P", (), {"pid": os.getpid()})())
        started = time.monotonic()
        with db.connect() as conn:
            assert head_tidepool.ensure_relay(conn, 77, "100.99.0.5", "gpu-box") is False
        assert time.monotonic() - started < 2.0


class TestTheHeadsOwnArmNeedsNoRelay:
    """A Tide Pool placed on the Head's co-located Arm can come up dead, with the Pod's sshd
    reporting `Bind to port 41000 on 0.0.0.0 failed: Address already in use`.

    Relay and Pod sshd share ONE port number by design — which on this Arm is one port on one box, and
    the relay gets there first (the route fires the moment Slurm says RUNNING, seconds before apptainer
    has booted the Pod). sshd then still binds `::` and looks healthy, because OpenSSH sets
    IPV6_V6ONLY, while being unable to accept IPv4 at all; mesh addresses are IPv4, so every connection
    reaches the relay — whose forward target is this same Arm. It forwards to itself, endlessly, and
    `_accepting` connects to the relay and calls it ready.
    """

    def _seed(self, port):
        with db.connect() as conn:
            conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                         "VALUES (1,'u1',0,'x',0)")
            db.record_spawn(conn, job_id=77, user_id=1, script="", tidepool=True)
            db.create_tidepool(conn, 77, port, "ssh-ed25519 AAAA", "PRIVATEKEY")
            conn.commit()

    @pytest.fixture(autouse=True)
    def _db(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        # Any Popen here is the bug itself.
        monkeypatch.setattr(head_tidepool.subprocess, "Popen",
                            lambda *a, **kw: pytest.fail("started a relay against the Head's own Arm"))

    def test_the_pods_own_sshd_is_what_ready_means(self, tmp_path):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        self._seed(port)
        try:
            with db.connect() as conn:
                assert head_tidepool.ensure_relay(conn, 77, "100.99.0.1", "head-box",
                                                  head_arm=True) is True
        finally:
            srv.close()

    def test_not_ready_while_the_pod_is_still_booting(self, tmp_path):
        """Nothing listening yet is the normal state for the first seconds of every Tide Pool — the CLI
        keeps polling, and this must answer quickly rather than burning _LISTEN_TIMEOUT per poll."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self._seed(port)
        started = time.monotonic()
        with db.connect() as conn:
            assert head_tidepool.ensure_relay(conn, 77, "100.99.0.1", "head-box", head_arm=True) is False
        assert time.monotonic() - started < 1.0

    def test_no_relay_pid_is_ever_recorded(self, tmp_path):
        """Nothing to kill, and reap_dead must still release the port when the Spawn ends."""
        self._seed(31007)
        with db.connect() as conn:
            head_tidepool.ensure_relay(conn, 77, "100.99.0.1", "head-box", head_arm=True)
            assert db.get_tidepool(conn, 77)["relay_pid"] is None
            head_tidepool.reap_dead(conn, live_job_ids=set())
            assert db.get_tidepool(conn, 77) is None
            assert 31007 not in db.used_tidepool_ports(conn)


# --- tick.py reaps a dead Tide Pool ------------------------------------------

class TestReap:
    @pytest.fixture(autouse=True)
    def _db(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CCC_HEAD_SECRET_KEY", raising=False)
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                        "VALUES (1,'alice',0,'x',0)")
            db.record_spawn(conn, job_id=1, user_id=1, script="", tidepool=True)
            db.record_spawn(conn, job_id=2, user_id=1, script="", tidepool=True)
            db.create_tidepool(conn, 1, 41000, "pub1", "priv1")
            db.create_tidepool(conn, 2, 41001, "pub2", "priv2")
            # No relay ever started for either (relay_pid NULL) — reap must still work with nothing to
            # kill, which is the common case for a Tide Pool that never got attached to.
            conn.commit()

    def test_a_spawn_no_longer_in_the_queue_is_reaped(self):
        with db.connect() as conn:
            head_tidepool.reap_dead(conn, live_job_ids={2})   # job 1 has left the queue
            assert db.get_tidepool(conn, 1) is None
            assert db.get_tidepool(conn, 2) is not None

    def test_the_port_is_freed(self):
        with db.connect() as conn:
            head_tidepool.reap_dead(conn, live_job_ids=set())
            assert 41000 not in db.used_tidepool_ports(conn)
            assert 41001 not in db.used_tidepool_ports(conn)

    def test_nothing_live_means_nothing_touched(self):
        with db.connect() as conn:
            head_tidepool.reap_dead(conn, live_job_ids={1, 2})
            assert db.get_tidepool(conn, 1) is not None
            assert db.get_tidepool(conn, 2) is not None


class TestHowASessionSaysItEnded:
    """`_report_ending` — the three ways out of an attached shell, and what each may truthfully claim.

    The bug pinned here: a session thrown out at `--time` announced that it was "still running
    and still billing Karma". Both halves were false. Slurm had already TERM'd the job, so it sat in
    COMPLETING draining its EXIT trap, and the tick only accrues onto RUNNING Spawns — but the CLI read
    the Head's `done`, which stays False through COMPLETING. At the 600s KillWait a Tide Pool needs, that lie would last ten minutes.
    """

    @pytest.fixture
    def said(self, monkeypatch):
        """Every ui line this produces, captured before Rich can wrap it to the console width."""
        lines: list[str] = []
        for name in ("ok", "warn", "hint"):
            monkeypatch.setattr(cli_tidepool.ui, name,
                                lambda m, _n=name: lines.append(f"{_n}: {m}"))
        return lines

    @pytest.fixture
    def head_says(self, monkeypatch):
        def _set(payload):
            monkeypatch.setattr(cli_tidepool.auth, "_api", lambda *a, **kw: payload)
        return _set

    def test_completing_is_neither_running_nor_billing(self, head_says, said):
        head_says({"id": 3, "done": False, "state": "COMPLETING"})
        cli_tidepool._report_ending(3)
        text = "\n".join(said)
        assert "still running" not in text
        assert "still billing" not in text
        assert "stopped billing" in text

    def test_a_genuinely_running_spawn_still_gets_the_billing_warning(self, head_says, said):
        """`exit` typed, or the connection dropped. This ending was never wrong and must survive."""
        head_says({"id": 3, "done": False, "state": "RUNNING"})
        cli_tidepool._report_ending(3)
        text = "\n".join(said)
        assert text.startswith("warn:")
        assert "still running and still billing Karma" in text
        assert "cuttle cancel 3" in text

    def test_a_settled_spawn_has_simply_ended(self, head_says, said):
        head_says({"id": 3, "done": True, "state": "done", "exit": 0})
        cli_tidepool._report_ending(3)
        text = "\n".join(said)
        assert text.startswith("ok:")
        assert "has ended" in text

    def test_an_unreachable_head_says_the_costly_thing(self, monkeypatch, said):
        """Can't ask -> assume it is still holding a card. The wrong guess here costs Karma; the wrong
        guess the other way costs nothing."""
        def _boom(*a, **kw):
            raise RuntimeError("no head")
        monkeypatch.setattr(cli_tidepool.auth, "_api", _boom)
        cli_tidepool._report_ending(3)
        assert "still running and still billing Karma" in "\n".join(said)


class TestTheTimeWarningSaysWhatBecomesOfTheFiles:
    """`_warn_text` — what the mid-session `--time` warning may claim.

    It used to claim Slurm gives an interactive Spawn NO grace period. There is one: TERM, then KILL
    after `KillWait`, and bash runs the job script's EXIT trap on TERM.
    """

    def test_no_warning_claims_there_is_no_grace_period(self):
        text = cli_tidepool._warn_text(3, 300)
        assert "NO grace period" not in text
        assert "no grace period" not in text

    def test_it_says_the_files_are_gone(self):
        assert "everything in it is gone" in cli_tidepool._warn_text(3, 300)

    def test_it_is_framed_for_a_terminal_ssh_holds_in_raw_mode(self):
        """A bare \\n leaves the next line indented by however far the cursor had got."""
        text = cli_tidepool._warn_text(3, 300)
        assert text.startswith("\r\n")
        assert text.endswith("\r\n")

    def test_the_remaining_time_is_rendered_as_minutes_and_seconds(self):
        assert "~5m0s" in cli_tidepool._warn_text(3, 300)
        assert "~0m30s" in cli_tidepool._warn_text(3, 30)


class TestTheShellTheUserGets:
    """zsh + powerlevel10k, rendered by the Head (ccc_head_api.zshrc) and written into the Pod's overlay
    at boot. The tree it needs is baked into base.sif; only these two files come from here."""

    def test_the_profile_is_bridged_into_zsh_or_it_is_silently_lost(self):
        """The single line without which this whole change is a regression, and a silent one.

        Everything a Tide Pool's shell needs past sshd — the Feature runtime, the
        Environment's PATH, the `cd` into the project — is written to /etc/profile.d/ccc-tidepool.sh,
        because sshd builds a fresh environment per session and drops apptainer's --env. **zsh does not
        read /etc/profile.d.** Debian's /etc/zsh/zprofile sources /etc/profile; Ubuntu's ships as
        comments only, and BASE_IMAGE_DOCKER is Ubuntu.

        So without the bridge the session opens, the prompt renders perfectly, and none of it is set —
        which is exactly the bug already found and fixed once on the bash path.
        """
        assert "source /etc/profile" in templates.render_base_def()
        assert "/etc/zsh/zprofile" in templates.render_base_def()

    def test_nothing_zsh_writes_lands_in_the_home_or_the_64mb_overlay(self):
        """Two different hazards, one answer. The overlay and the Pod's /tmp share 64 MB of RAM; and for
        a Spawn that requested a **Feature**, `licbind` binds the Arm's **Licence** directory over the
        Guise's home — `2770 ccc-licence:ccc-guises`, shared by every feature Pod on that Arm. A history
        file there is one User's keystrokes in a directory another User's Tide Pool can read."""
        rc = zshrc.render()
        for var in ("ZSH_CACHE_DIR", "ZSH_COMPDUMP", "HISTFILE", "_ZO_DATA_DIR",
                    "GITSTATUS_CACHE_DIR"):
            line = next(l for l in rc.splitlines() if l.startswith(f"export {var}="))
            value = line.split("=", 1)[1]
            assert value.startswith(("/ccc-tmp", "$ZSH_CACHE_DIR")), f"{var} escapes scratch: {value}"

    def test_the_cache_dir_exists_before_omz_consults_it(self):
        """oh-my-zsh tests `[[ ! -w "$ZSH_CACHE_DIR" ]]` and silently falls back to $HOME/.cache when the
        directory is not there yet — it only mkdirs it several lines later. So setting the variable
        without creating it first lands the cache in the shared home this all exists to avoid."""
        rc = zshrc.render()
        assert rc.index("mkdir -p $ZSH_CACHE_DIR") < rc.index("source $ZSH/oh-my-zsh.sh")

    def test_the_users_own_rc_is_not_in_the_home_another_user_can_write(self):
        """zsh sources $ZDOTDIR/.zshrc, and for a Spawn requesting a **Feature** the home is the Arm's
        **Licence** directory — `2770 ccc-licence:ccc-guises`, shared by every feature Pod on the node,
        with every **Guise** in the group. Left at the default, one User's `.zshrc` would be sourced by
        the next User's shell: the hole StrictModes already closes for authorized_keys, reopened by the
        shell instead of sshd. The obvious fix for the wizard above — touching ~/.zshrc — would have
        seeded it, so pointing ZDOTDIR at private per-session scratch is load-bearing, not tidiness."""
        assert zshrc.POD_ZDOTDIR.startswith("/ccc-tmp"), zshrc.POD_ZDOTDIR
        assert zshrc.POD_USER_ZSHRC.startswith("/ccc-tmp"), zshrc.POD_USER_ZSHRC

    def test_gitstatus_is_neither_pointed_at_nor_switched_off(self):
        """Both of the obvious spellings break it, and both were measured. GITSTATUS_DAEMON makes the
        installer read its version from build.info (v1.5.5) rather than install.info (v1.5.4, the release
        with an actual linux-x86_64 prebuilt), so the daemon fails its own `-G` version check.
        GITSTATUS_AUTO_INSTALL=0 becomes the installer's `-n`, which skips the block that FINDS a daemon
        at all. The binary is placed in upstream's own usrbin slot and found by name instead."""
        # Comments are where both traps are written down, so they name the variables on purpose.
        rc = "\n".join(l for l in zshrc.render().splitlines() if not l.lstrip().startswith("#"))
        assert "GITSTATUS_DAEMON" not in rc
        assert "GITSTATUS_AUTO_INSTALL" not in rc

    def test_the_daemon_is_in_the_slot_the_installer_looks_in_first(self):
        """A missing or renamed binary means every session downloads 2 MB from GitHub, or errors out on
        an Arm with no egress. The name is upstream's (`gitstatusd-${uname_s}-${uname_m}`), not ours."""
        daemon = (config.ZSH_DATA_DIR / "oh-my-zsh/custom/themes/powerlevel10k/gitstatus"
                  / "usrbin/gitstatusd-linux-x86_64")
        assert daemon.is_file() and os.access(daemon, os.X_OK)

    def test_the_context_segment_says_which_session_kind_this_is(self):
        """p10k's REMOTE class is what renders over SSH, and a Tide Pool is always one. Left alone it
        reads `ccc-u60002@<arm>` — the **Guise**, a uid nobody knows for an identity that is explicitly
        not an account."""
        assert "'tidepool@%m'" in zshrc.render()
