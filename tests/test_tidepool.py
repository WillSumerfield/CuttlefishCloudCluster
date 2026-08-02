"""Tide Pool (docs/TODO.md): a Spawn whose payload is a live shell instead of a script.

What is tested here: the port reservation (has to be race-safe before a job_id exists), the keypair
round-trip through at-rest encryption, the two relaxed submit checks (script/outputs optional), the
`GET /spawns/{id}/tidepool` route's auth/ownership/readiness contract, tick.py's reap, and the CLI's
flag wiring + the output-optional confirm. NOT tested: a real sshd/ssh session — that needs real
hardware (same carve-out the Beak/MIG work already has in docs/TODO.md).
"""

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db, guises, slurm, tick
from ccc_head_api import main as head_main
from ccc_head_api import spawns as head_spawns
from ccc_head_api import tidepool as head_tidepool
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


class TestTidepoolCrud:
    @pytest.fixture(autouse=True)
    def _db(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CCC_HEAD_SECRET_KEY", raising=False)
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                        "VALUES (1,'alice',0,'x',0)")
            db.record_spawn(conn, job_id=99, user_id=1, script="", staging_uuid="u", tidepool=True)
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


# --- submit()/submit_checked() relax script + outputs for a Tide Pool -------

class TestSubmitSkipsScriptAndOutputsForTidepool:
    def _tar_empty(self, tmp_path: Path) -> Path:
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            pass
        path = tmp_path / "empty.tar.gz"
        path.write_bytes(buf.getvalue())
        return path

    @pytest.fixture(autouse=True)
    def _rig(self, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")
        monkeypatch.setattr(head_spawns, "PROJECTS_DIR", tmp_path / "projects")
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                        "VALUES (1,'alice',0,'x',0)")
            conn.commit()
        counter = iter(range(5000, 5100))
        # Stands in for BOTH shell-outs submit() makes: `setfacl` (the project lend, which reports
        # through its return code) and `sbatch` (which reports through stdout).
        monkeypatch.setattr(
            head_spawns.subprocess, "run",
            lambda *a, **kw: type("R", (), {"stdout": f"{next(counter)}\n", "stderr": "",
                                            "returncode": 0})())

    def test_no_script_no_outputs_does_not_raise(self, tmp_path):
        job_id = head_spawns.submit(
            user_id=1, script=None, gpu=1, args=[], wandb_env={},
            tar_path=self._tar_empty(tmp_path), minutes=30, constraint="vram24", outputs=[],
            tidepool=True)
        assert job_id >= 5000

    def test_a_tidepools_row_is_written(self, tmp_path):
        job_id = head_spawns.submit(
            user_id=1, script=None, gpu=1, args=[], wandb_env={},
            tar_path=self._tar_empty(tmp_path), minutes=30, constraint="vram24", outputs=[],
            tidepool=True)
        with db.connect() as conn:
            tp = db.get_tidepool(conn, job_id)
        assert tp is not None
        assert head_tidepool.PORT_BASE <= tp["port"] < head_tidepool.PORT_BASE + head_tidepool.PORT_RANGE

    def test_the_rendered_script_carries_the_sshd_payload(self, tmp_path):
        job_id = head_spawns.submit(
            user_id=1, script=None, gpu=1, args=[], wandb_env={},
            tar_path=self._tar_empty(tmp_path), minutes=30, constraint="vram24", outputs=[],
            tidepool=True)
        with db.connect() as conn:
            tp = db.get_tidepool(conn, job_id)
        body = head_spawns._render(
            "/tmp/u", 1, "", [], [], [], None, 30, "vram24", "a3f9", "s3cr3t",
            tidepool_port=tp["port"], tidepool_pubkey=tp["pubkey"])
        assert f"sshd -D -e -p {tp['port']}" in body
        assert tp["pubkey"].strip() in body
        assert "%%TIDEPOOL_PORT%%" not in body
        assert "%%TIDEPOOL_PUBKEY%%" not in body

    def test_the_pod_repairs_its_own_passwd_before_sshd_starts(self, tmp_path):
        """A **Guise** is `nologin` on the host, and apptainer patches the Pod's /etc/passwd with that
        host entry verbatim — shell field included (measured 2026-08-01). sshd then execs nologin and
        the User gets "This account is currently not available" instead of a shell, with the Spawn
        running and billing Karma the whole time.

        Ordering is the test: repaired before `exec sshd`, or the first login is the broken one."""
        body = head_spawns._render("/tmp/u", 1, "", [], [], [], None, 30, "vram24", "a3f9", "s",
                                   tidepool_port=41000, tidepool_pubkey="ssh-ed25519 AAAA x")
        # The backreference is spelled `\\1` in the Python source for a reason: `"\1"` in a non-raw
        # literal is chr(1), which renders a sed expression that silently replaces the whole line.
        assert r"\1:/bin/bash|" in body
        assert "\x01" not in body
        assert body.index("/etc/passwd") < body.index("exec /usr/sbin/sshd")

    def test_neither_identity_file_is_edited_in_place(self, tmp_path):
        """`sed -i` writes a temp file and rename()s it over the original, which the --writable-tmpfs
        overlay refuses with "Device or resource busy" (found on hardware with the tty-group
        workaround). Both repairs must redirect to /tmp and truncate the target instead — a `sed -i`
        creeping into either one fails at runtime, inside the Pod, where nobody is reading stderr."""
        body = head_spawns._render("/tmp/u", 1, "", [], [], [], None, 30, "vram24", "a3f9", "s",
                                   tidepool_port=41000, tidepool_pubkey="ssh-ed25519 AAAA x")
        boot = body[body.index("tp_boot.sh"):]
        # Comments are where the constraint is explained, so they mention `sed -i` on purpose.
        code = "\n".join(l for l in boot.splitlines() if not l.lstrip().startswith("#"))
        assert "sed -i" not in code
        for f in ("/etc/passwd", "/etc/group"):
            assert f"> {f}\n" in code          # truncated in place, never renamed over

    def test_an_ordinary_spawn_is_unaffected(self, tmp_path):
        """No token leakage the other direction: a normal render never mentions Tide Pool at all."""
        body = head_spawns._render("/tmp/u", 1, "train.py", [], [], ["out"], None, 30, "vram24",
                                   "a3f9", "s3cr3t")
        assert "sshd" not in body
        assert "PAYLOAD_TIDEPOOL" not in body
        # `$py`, not a literal `python`: the interpreter is chosen by the script at run time, since a
        # Spawn whose submitter declared an **Environment** runs that env's python instead
        # (ccc_head_api.environments). It still defaults to the image's own.
        assert '$py train.py' in body
        assert 'py="python"' in body

    def test_missing_outputs_still_required_without_tidepool(self, tmp_path):
        with pytest.raises(ValueError, match="output"):
            head_spawns.submit(
                user_id=1, script="train.py", gpu=1, args=[], wandb_env={},
                tar_path=self._tar_empty(tmp_path), minutes=30, constraint="vram24", outputs=[],
                tidepool=False)


# --- GET /submit + /spawns/{id}/tidepool over HTTP ---------------------------

def _client(monkeypatch, tmp_path, user_id=1, is_admin=False):
    user = {"id": user_id, "username": "alice", "is_admin": is_admin, "must_change_password": False}
    head_main.app.dependency_overrides[head_main.current_user] = lambda: user
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
    monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box"}])
    monkeypatch.setattr(slurm, "vram_constraint", lambda gb: "vram24")
    monkeypatch.setattr(slurm, "queue", lambda: [])
    return TestClient(head_main.app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


class TestSubmitRouteAcceptsTidepool:
    def test_no_script_no_outputs_is_accepted(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        monkeypatch.setattr(head_spawns, "submit", lambda *a, **kw: 4242)
        meta = {"minutes": 60, "vram": 24, "tidepool": True}
        r = c.post("/api/submit", content=b"", headers={"X-CCC-Meta": json.dumps(meta)})
        assert r.status_code == 200, r.text
        assert r.json() == {"id": 4242}

    def test_still_requires_minutes_and_vram(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        meta = {"vram": 24, "tidepool": True}
        r = c.post("/api/submit", content=b"", headers={"X-CCC-Meta": json.dumps(meta)})
        assert r.status_code == 422
        assert "minutes" in r.json()["detail"]

    def test_an_ordinary_submit_still_needs_script_and_outputs(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        r = c.post("/api/submit", content=b"",
                   headers={"X-CCC-Meta": json.dumps({"minutes": 60, "vram": 24})})
        assert r.status_code == 422
        assert "script" in r.json()["detail"]


# The first uid the allocator hands out. These tests seed `users` with raw SQL, so nobody has a Guise
# until the route asks for one — and the first ask gets the base.
_GUISE = guises.name(guises.UID_BASE)


class TestTidepoolRoute:
    def _seed(self, tmp_path, job_id=77, owner_id=1, tidepool=True, port=41000):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                "VALUES (?,?,?,?,0)", (owner_id, f"u{owner_id}", 0, "x"))
            db.record_spawn(conn, job_id=job_id, user_id=owner_id, script="", staging_uuid="u",
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
        assert r.json() == {"ready": False, "port": None, "private_key": None, "user": _GUISE}

    def test_ready_once_running_and_relayed(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        self._seed(tmp_path, port=41007)
        monkeypatch.setattr(head_spawns, "active_state", lambda jid: "RUNNING")
        monkeypatch.setattr(slurm, "spawns", lambda: [{"id": 77, "node": "gpu-box"}])
        monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box", "mesh_ip": "100.99.0.5"}])
        # ensure_relay would actually fork a subprocess — not what this test is about.
        calls = []
        monkeypatch.setattr(head_tidepool, "ensure_relay",
                            lambda conn, jid, ip, node: calls.append((jid, ip, node)))
        r = c.get("/api/spawns/77/tidepool")
        assert r.status_code == 200
        body = r.json()
        assert body == {"ready": True, "port": 41007, "private_key": "PRIVATEKEY", "user": _GUISE}
        assert calls == [(77, "100.99.0.5", "gpu-box")]

    def test_not_ready_if_the_arm_has_no_mesh_ip_yet(self, monkeypatch, tmp_path):
        """Belt and suspenders: RUNNING in squeue but the registry hasn't caught up is not attachable."""
        c = _client(monkeypatch, tmp_path)
        self._seed(tmp_path)
        monkeypatch.setattr(head_spawns, "active_state", lambda jid: "RUNNING")
        monkeypatch.setattr(slurm, "spawns", lambda: [{"id": 77, "node": "gpu-box"}])
        monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box", "mesh_ip": None}])
        r = c.get("/api/spawns/77/tidepool")
        assert r.json() == {"ready": False, "port": None, "private_key": None, "user": _GUISE}


class TestReadyMeansConnectable:
    """`ready: true` is the CLI's cue to run `ssh` — there is no second check between the two. So the
    route may only say it once the relay is accepting, and `Popen` returning is not that: the child
    still has to start a Python interpreter and bind. The gap is ~200ms, which is precisely long
    enough to lose to the CLI every time and be gone by the time anyone investigates — a "Connection
    refused" on the FIRST attach to every Tide Pool, working on the retry (found on hardware
    2026-08-01, twice, on Tide Pools 14 and 16).
    """

    def _seed(self, port):
        with db.connect() as conn:
            conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                         "VALUES (1,'u1',0,'x',0)")
            db.record_spawn(conn, job_id=77, user_id=1, script="", staging_uuid="u", tidepool=True)
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
            head_tidepool.ensure_relay(conn, 77, "100.99.0.5", "gpu-box")
        assert time.monotonic() - started < 2.0


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
            db.record_spawn(conn, job_id=1, user_id=1, script="", staging_uuid="u1", tidepool=True)
            db.record_spawn(conn, job_id=2, user_id=1, script="", staging_uuid="u2", tidepool=True)
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


# --- CLI: flag wiring, id-vs-fresh-request resolution, output confirm -------

class TestExistingIdResolution:
    def test_a_digit_string_resolves_to_an_id(self):
        assert cli_tidepool._existing_id("123") == 123

    def test_none_is_a_fresh_request(self):
        assert cli_tidepool._existing_id(None) is None

    def test_a_non_numeric_string_is_a_fresh_request(self):
        """`cuttle run --tidepool train.py` — a stray script path, not a Spawn id. run() warns and
        ignores it rather than treating it as one."""
        assert cli_tidepool._existing_id("train.py") is None


class TestCliTidepoolFlagWiring:
    def test_tidepool_needs_time_and_vram_like_any_spawn(self, signed_in):
        r = signed_in("run", "--tidepool")
        assert r.exit_code != 0
        assert "--time" in r.output or "--vram" in r.output

    def test_output_optional_but_confirmed(self, signed_in, fake_slurm):
        """Declining the confirm aborts before anything is submitted."""
        r = signed_in("run", "--tidepool", "--time", "10", "--vram", "4", input="n\n")
        assert r.exit_code != 0
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) c FROM spawns").fetchone()["c"] == 0

    def test_accepting_the_confirm_submits(self, signed_in, fake_slurm):
        """Accepted -> a real submission happens (tidepool=1 in the ledger), even though this fake
        cluster can never actually place it (no real Slurm, so attach then fails loudly — that failure
        is expected and not what this test is about)."""
        signed_in("run", "--tidepool", "--time", "10", "--vram", "4", input="y\n")
        with db.connect() as conn:
            row = conn.execute("SELECT tidepool FROM spawns").fetchone()
        assert row is not None
        assert row["tidepool"] == 1
