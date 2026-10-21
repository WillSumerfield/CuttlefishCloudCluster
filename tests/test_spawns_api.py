"""The Head API's Spawn routes that outlive the 2.0 submit path: cancel, the log streams, and the mode
a Spawn dir lands at — plus the teardown sweep of job-keyed /tmp leftovers.
"""

import os
import pathlib
import stat
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db
from ccc_head_api import main as head_main
from ccc_head_api import slurm
from ccc_head_api import spawns as head_spawns
from cuttle import teardown


class TestTeardownSweepsWhatItMakesAmbiguous:
    """`remove_slurm_config` drops Slurm's state dir and so resets the job-id counter, which is what
    turns every leftover /tmp/ccc-<jid>* from the outgoing cluster into a landmine for the incoming
    one. The sweep belongs beside the reset that causes it."""

    def _fixture(self, tmp_path, monkeypatch):
        for name in ("ccc-9.slurm", "ccc-10-deadbeef.slurm", "ccc-bench-25.log",
                     "ccc-u60002-something", "ccc-notes.txt"):
            (tmp_path / name).write_text("x")
        (tmp_path / "ccc-9").mkdir()          # a remote Spawn's scratch dir
        monkeypatch.setattr(teardown, "_JOB_TMP_GLOBS",
                            (f"{tmp_path}/ccc-[0-9]*", f"{tmp_path}/ccc-bench-[0-9]*"))
        calls = []
        monkeypatch.setattr(teardown, "run", lambda cmd, **kw: calls.append(cmd))
        return calls

    def test_it_takes_every_job_keyed_leftover(self, tmp_path, monkeypatch):
        calls = self._fixture(tmp_path, monkeypatch)
        teardown._sweep_job_tmp()
        swept = {Path(p).name for p in calls[0][2:]}
        assert swept == {"ccc-9.slurm", "ccc-10-deadbeef.slurm", "ccc-bench-25.log", "ccc-9"}

    def test_it_leaves_everything_else_alone(self, tmp_path, monkeypatch):
        """`ccc-[0-9]*`, never `ccc-*`: a Guise's own files are `ccc-u<uid>-…`, and a teardown widening
        its blast radius over a name it merely recognises is worse than the litter it removes."""
        calls = self._fixture(tmp_path, monkeypatch)
        teardown._sweep_job_tmp()
        swept = {Path(p).name for p in calls[0][2:]}
        assert "ccc-u60002-something" not in swept
        assert "ccc-notes.txt" not in swept

    def test_nothing_to_sweep_runs_nothing(self, tmp_path, monkeypatch):
        """`rm -rf` with no paths is an error, and teardown runs check=False — so it would pass silently
        while doing nothing, which is the shape of a sweep that quietly stops working."""
        monkeypatch.setattr(teardown, "_JOB_TMP_GLOBS", (f"{tmp_path}/ccc-[0-9]*",))
        calls = []
        monkeypatch.setattr(teardown, "run", lambda cmd, **kw: calls.append(cmd))
        teardown._sweep_job_tmp()
        assert calls == []


def client(monkeypatch, is_admin: bool) -> TestClient:
    """A Head API with a logged-in caller and no Slurm, no disk — but a real (empty) database."""
    user = {"id": 1, "username": "alice", "is_admin": is_admin, "must_change_password": False}
    head_main.app.dependency_overrides[head_main.current_user] = lambda: user
    # A fresh empty db every time. Tests that need rows (the cancel tests) seed AFTER this, into this
    # same path.
    # Always a temp file, never db.DB_PATH's default — on a live Head that default is a real ccc-owned
    # store this process cannot read.
    monkeypatch.setattr(db, "DB_PATH", Path(tempfile.mkdtemp()) / "head.db")
    db.ensure_schema()
    # Same reason as DB_PATH: on a live Head the default is a real ccc-owned tree this process cannot
    # write. A cancel now leaves a marker there (spawns.mark_cancelled), so "no disk" has to mean a
    # disk of our own, not the operator's.
    monkeypatch.setattr(head_spawns, "SPAWNS_DIR", Path(tempfile.mkdtemp()) / "spawns")
    return TestClient(head_main.app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


class TestCancel:
    """DELETE /spawns/{id} — the escape that CCC never had. You may end **your own**, and only your
    own; cancel-anyone moved to the Head in Phase 6. Attribution is the ledger's (every Spawn runs as
    the Mantle), so authorization is a row lookup, not Slurm's owner field."""

    def _seed(self, owner_id: int):
        """Insert an owner and their Spawn into whatever db `client()` just stood up. Called AFTER the
        client, so it writes to the same DB_PATH the handler will read."""
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                "VALUES (?,?,?,?,0)", (owner_id, f"u{owner_id}", 0, "x"))
            db.record_spawn(conn, job_id=77, user_id=owner_id, script="train.py")
            conn.commit()

    def test_owner_may_cancel(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)  # caller is id 1
        self._seed(owner_id=1)
        killed = []
        monkeypatch.setattr(slurm, "cancel", lambda jid: killed.append(jid))
        r = c.delete("/api/spawns/77")
        assert r.status_code == 204
        assert killed == [77]

    def test_stranger_may_not_cancel(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)
        self._seed(owner_id=2)  # someone else's Spawn
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("must not scancel"))
        r = c.delete("/api/spawns/77")
        assert r.status_code == 403

    def test_an_admin_may_not_cancel_anyones_over_the_api(self, monkeypatch):
        """Cancel-anyone was an admin branch on this route until Phase 6 removed it: it is now
        `cuttle cancel <id> --any`, on the Head. The ownership test is unconditional, so admin or
        not, a Session can only stop the Spawns it owns."""
        c = client(monkeypatch, is_admin=True)
        self._seed(owner_id=2)
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("must not scancel"))
        r = c.delete("/api/spawns/77")
        assert r.status_code == 403

    def test_unknown_spawn_is_404(self, monkeypatch):
        c = client(monkeypatch, is_admin=True)
        self._seed(owner_id=1)
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("must not scancel"))
        r = c.delete("/api/spawns/999")
        assert r.status_code == 404


class TestLogStreams:
    """`/logs` serves stdout, `/stderr` serves stderr — separate files on disk (spawns.py writes both).
    A Spawn that dies on a traceback leaves `out` empty and the cause in `err`; before /stderr existed
    that cause was unreachable from every client. A stream with no file yet reads as empty, not 404."""

    def _spawn_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path)
        d = tmp_path / "77"
        d.mkdir()
        return d

    def test_logs_serves_stdout_stderr_serves_stderr(self, monkeypatch, tmp_path):
        c = client(monkeypatch, is_admin=False)
        d = self._spawn_dir(monkeypatch, tmp_path)
        (d / "out").write_text("epoch 1 loss=2.3\n")
        (d / "err").write_text("Traceback (most recent call last)\n")
        assert c.get("/api/spawns/77/logs").text == "epoch 1 loss=2.3\n"
        assert c.get("/api/spawns/77/stderr").text == "Traceback (most recent call last)\n"

    def test_a_crash_leaves_the_cause_only_in_stderr(self, monkeypatch, tmp_path):
        """The reported bug: out empty + FAILED, cause on disk. /stderr is the reach that was missing."""
        c = client(monkeypatch, is_admin=False)
        d = self._spawn_dir(monkeypatch, tmp_path)
        (d / "out").write_text("")
        (d / "err").write_text("RuntimeError: CUDA out of memory\n")
        assert c.get("/api/spawns/77/logs").text == ""
        assert "CUDA out of memory" in c.get("/api/spawns/77/stderr").text

    def test_a_missing_stream_is_empty_not_404(self, monkeypatch, tmp_path):
        c = client(monkeypatch, is_admin=False)
        self._spawn_dir(monkeypatch, tmp_path)   # dir exists, neither file written yet
        assert c.get("/api/spawns/77/logs").status_code == 200
        assert c.get("/api/spawns/77/stderr").status_code == 200
        assert c.get("/api/spawns/77/stderr").text == ""


class TestWhatASpawnDirLandsAs:
    """Modes, not contents. A Spawn dir holds a User's own results on a box other Users have accounts
    on, and must not inherit whatever umask the Head operator happened to provision under."""

    @pytest.fixture(autouse=True)
    def _spawns_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")

    def test_a_spawn_dir_is_not_world_readable(self):
        """The prolog sets 0770 on a Spawn dir it lends to a Guise — but only on the Head's own
        co-located Arm (`if [ -d "$head_spawns" ]` is false on a donor box). So for a REMOTE Spawn
        nothing else ever sets this, and a Spawn dir is named by a SEQUENTIAL job id while the 0711
        parent grants exactly the traversal a guessed name needs."""
        old = os.umask(0o022)
        try:
            d = head_spawns._make_spawn_dir(4321)
        finally:
            os.umask(old)
        assert stat.S_IMODE(d.stat().st_mode) == head_spawns.SPAWN_DIR_MODE
        assert not stat.S_IMODE(d.stat().st_mode) & 0o007, "other has nothing"

    def test_a_lent_directory_is_left_alone(self, monkeypatch):
        """Once the prolog hands a Spawn dir to the Guise, the Mantle no longer owns it and cannot
        chmod it — and `mark_cancelled` runs at exactly that moment. Cancelling a RUNNING Spawn must
        not fail over a mode the prolog already set."""
        d = head_spawns._make_spawn_dir(9911)
        real = pathlib.Path.chmod

        def lent(self, *a, **kw):
            if self == d:
                raise PermissionError(1, "Operation not permitted")
            return real(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "chmod", lent)
        assert head_spawns._make_spawn_dir(9911) == d
