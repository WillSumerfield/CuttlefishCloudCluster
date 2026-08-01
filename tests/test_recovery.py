"""Recovery Window (docs/TODO.md) — a 30-min, size-capped safety net for whatever a Spawn wrote OUTSIDE
its declared $CCC_OUT, closing the gap where that data used to be destroyed the instant the JOB_SCRIPT's
EXIT trap fired.

What's tested here: the rendered JOB_SCRIPT's `recover_scratch` (both branches — Head-local same-
filesystem `mv`, remote `rsync`), the DB ledger (recovery_copies), recovery.py's registration/pool-
eviction/expiry logic, the two new API routes, and the CLI (`cuttle recover`, and `cuttle outputs`'s
miss-hint). NOT tested: an actual rsync transfer, or a live karma-tick loop — same hardware carve-out
the rest of this suite already draws for anything that needs a real Arm.
"""

import io
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db, recovery
from ccc_head_api import main as head_main
from ccc_head_api import spawns as head_spawns

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _iso(dt: datetime) -> str:
    return dt.strftime(_TS)


# --- the rendered JOB_SCRIPT --------------------------------------------------

class TestRenderedScript:
    """`recover_scratch`, defined per-branch, called from `cleanup()` before the `rm -rf` that used to
    be the only thing there."""

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24")
        kw.setdefault("node", None)
        kw.setdefault("outputs", ["outputs"])
        return head_spawns._render(
            staging="/tmp/u", gpu=1, script="train.py", args=[], files=[],
            token="a3f9", secret="s3cr3t", **kw)

    def _head_branch(self, script: str) -> str:
        start = script.index('if [ -d "$staging_dir" ]')
        end = script.index("\nelse")
        return script[start:end]

    def _remote_branch(self, script: str) -> str:
        start = script.index("else")
        end = script.index('\ncd "$proj"')
        return script[start:end]

    def test_cleanup_calls_recover_scratch_before_rm_rf(self):
        script = self.render()
        cleanup_line = next(l for l in script.splitlines() if l.startswith("cleanup()"))
        assert cleanup_line.index("recover_scratch") < cleanup_line.index("rm -rf")

    def test_recovery_max_bytes_is_baked_in(self):
        script = self.render()
        assert "%%RECOVERY_MAX_BYTES%%" not in script
        assert str(recovery.MAX_SPAWN_BYTES) in script

    def test_head_branch_relocates_proj_with_no_network_hop(self):
        head = self._head_branch(self.render())
        assert "recover_scratch() {" in head
        assert 'mv "$proj" "$spawn_dir/recovery"' in head
        assert "rsync" not in head

    def test_head_branch_writes_the_size_marker_alongside(self):
        head = self._head_branch(self.render())
        assert '"$spawn_dir/recovery.size"' in head

    def test_remote_branch_rsyncs_proj_into_a_recovery_subdir(self):
        remote = self._remote_branch(self.render())
        assert "recover_scratch() {" in remote
        assert 'rsync -a "$proj/"' in remote
        assert "/recovery/" in remote

    def test_remote_branch_writes_the_size_marker_only_after_the_transfer(self):
        """The marker's arrival on the Head is what tells the karma tick a copy is ready to register
        (recovery.register_new) — writing it before the transfer landed would be a lie."""
        remote = self._remote_branch(self.render())
        transfer = remote.index('rsync -a "$proj/"')
        marker = remote.index('echo "$sz" > "$scratch/recovery.size"')
        assert transfer < marker

    def test_both_branches_gate_on_the_same_size_cap(self):
        script = self.render()
        assert script.count(f"-le {recovery.MAX_SPAWN_BYTES}") == 2

    def test_trap_and_stage_out_ordering_is_unchanged(self):
        """recover_scratch is new; the existing EXIT-trap/stage_out contract (test_submit.py's
        TestRemoteLiveStreaming) must not have moved."""
        script = self.render()
        assert script.count("trap cleanup EXIT") == 2


# --- the DB ledger -------------------------------------------------------------

class TestDbLedger:
    @pytest.fixture(autouse=True)
    def _db(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            db.add_user(conn, "alice")

    def _spawn(self, conn, job_id: int, finished_at: str | None = None) -> None:
        db.record_spawn(conn, job_id, 1, "train.py", "uuid", minutes=10, gpus=1)
        if finished_at is not None:
            db.mark_finished(conn, job_id, 0, finished_at)

    def test_unfinished_spawns_are_not_candidates(self):
        with db.connect() as conn:
            self._spawn(conn, 1)
            since = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
            assert db.recently_finished_without_recovery(conn, since) == []

    def test_already_registered_spawns_are_not_candidates(self):
        with db.connect() as conn:
            now = _iso(datetime.now(timezone.utc))
            self._spawn(conn, 2, finished_at=now)
            db.add_recovery(conn, 2, 1000, at=now)
            since = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
            assert db.recently_finished_without_recovery(conn, since) == []

    def test_a_recently_finished_unregistered_spawn_is_a_candidate(self):
        with db.connect() as conn:
            now = _iso(datetime.now(timezone.utc))
            self._spawn(conn, 3, finished_at=now)
            since = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
            assert db.recently_finished_without_recovery(conn, since) == [3]

    def test_pool_bytes_sums_every_row(self):
        with db.connect() as conn:
            self._spawn(conn, 1)
            self._spawn(conn, 2)
            db.add_recovery(conn, 1, 100)
            db.add_recovery(conn, 2, 250)
            assert db.recovery_pool_bytes(conn) == 350

    def test_pool_bytes_is_zero_when_empty(self):
        with db.connect() as conn:
            assert db.recovery_pool_bytes(conn) == 0

    def test_oldest_is_the_earliest_created_at(self):
        with db.connect() as conn:
            self._spawn(conn, 1)
            self._spawn(conn, 2)
            db.add_recovery(conn, 1, 100, at="2026-01-01T00:00:00Z")
            db.add_recovery(conn, 2, 200, at="2026-01-02T00:00:00Z")
            assert db.oldest_recovery(conn)["job_id"] == 1

    def test_expired_recoveries_stops_at_the_cutoff(self):
        with db.connect() as conn:
            self._spawn(conn, 1)
            self._spawn(conn, 2)
            db.add_recovery(conn, 1, 100, at="2026-01-01T00:00:00Z")
            db.add_recovery(conn, 2, 200, at="2026-06-01T00:00:00Z")
            assert db.expired_recoveries(conn, "2026-03-01T00:00:00Z") == [1]

    def test_delete_recovery_removes_the_row(self):
        with db.connect() as conn:
            self._spawn(conn, 1)
            db.add_recovery(conn, 1, 100)
            db.delete_recovery(conn, 1)
            assert db.get_recovery(conn, 1) is None


# --- recovery.py's policy: registration, pool eviction, expiry ----------------

class TestRegisterNew:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        with db.connect() as conn:
            db.add_user(conn, "alice")

    def _finished(self, conn, job_id: int, at: str) -> None:
        db.record_spawn(conn, job_id, 1, "train.py", "uuid", minutes=10, gpus=1)
        db.mark_finished(conn, job_id, 0, at)

    def test_registers_a_landed_marker(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._finished(conn, 10, _iso(now))
            d = head_spawns.SPAWNS_DIR / "10"
            d.mkdir(parents=True)
            (d / "recovery.size").write_text("12345")
            assert recovery.register_new(conn, now) == 1
            assert db.get_recovery(conn, 10)["bytes"] == 12345

    def test_skips_a_spawn_whose_marker_has_not_landed_yet(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._finished(conn, 11, _iso(now))
            assert recovery.register_new(conn, now) == 0
            assert db.get_recovery(conn, 11) is None

    def test_never_registers_the_same_spawn_twice(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._finished(conn, 12, _iso(now))
            d = head_spawns.SPAWNS_DIR / "12"
            d.mkdir(parents=True)
            (d / "recovery.size").write_text("999")
            recovery.register_new(conn, now)
            assert recovery.register_new(conn, now) == 0

    def test_tolerates_a_garbage_marker_rather_than_crashing(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._finished(conn, 13, _iso(now))
            d = head_spawns.SPAWNS_DIR / "13"
            d.mkdir(parents=True)
            (d / "recovery.size").write_text("not a number")
            assert recovery.register_new(conn, now) == 0
            assert db.get_recovery(conn, 13) is None

    def test_a_spawn_finished_outside_the_registration_window_is_never_tried(self):
        """Bounded on purpose: a copy that never lands (skipped for size, or the Arm dropped) must not
        be re-checked forever, or this candidate list only ever grows."""
        now = datetime.now(timezone.utc)
        stale = now - timedelta(minutes=recovery._REGISTRATION_WINDOW_MINUTES + 1)
        with db.connect() as conn:
            self._finished(conn, 14, _iso(stale))
            d = head_spawns.SPAWNS_DIR / "14"
            d.mkdir(parents=True)
            (d / "recovery.size").write_text("42")  # even if it DID eventually land
            assert recovery.register_new(conn, now) == 0


class TestPoolEviction:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        monkeypatch.setattr(recovery, "MAX_POOL_BYTES", 1000)
        with db.connect() as conn:
            db.add_user(conn, "alice")

    def _seed_copy(self, conn, job_id: int, size: int, at: str) -> None:
        db.record_spawn(conn, job_id, 1, "train.py", "uuid", minutes=10, gpus=1)
        db.mark_finished(conn, job_id, 0, at)
        db.add_recovery(conn, job_id, size, at=at)
        d = head_spawns.SPAWNS_DIR / str(job_id) / "recovery"
        d.mkdir(parents=True)
        (d / "f.bin").write_bytes(b"x" * min(size, 16))  # content irrelevant; the DB row carries `bytes`

    def test_a_new_copy_over_the_cap_evicts_the_oldest_first(self):
        with db.connect() as conn:
            self._seed_copy(conn, 1, 400, "2026-01-01T00:00:00Z")
            self._seed_copy(conn, 2, 400, "2026-01-02T00:00:00Z")
            recovery._make_room(conn, 400)  # 400+400+400 > 1000 -> the oldest (1) goes
            assert db.get_recovery(conn, 1) is None
            assert db.get_recovery(conn, 2) is not None
            assert not (head_spawns.SPAWNS_DIR / "1" / "recovery").exists()

    def test_eviction_repeats_until_it_fits(self):
        with db.connect() as conn:
            self._seed_copy(conn, 1, 400, "2026-01-01T00:00:00Z")
            self._seed_copy(conn, 2, 400, "2026-01-02T00:00:00Z")
            self._seed_copy(conn, 3, 400, "2026-01-03T00:00:00Z")
            recovery._make_room(conn, 400)  # 1200+400 > 1000 -> evict 1, still 800+400>1000 -> evict 2
            assert db.get_recovery(conn, 1) is None
            assert db.get_recovery(conn, 2) is None
            assert db.get_recovery(conn, 3) is not None

    def test_no_eviction_when_it_already_fits(self):
        with db.connect() as conn:
            self._seed_copy(conn, 1, 100, "2026-01-01T00:00:00Z")
            self._seed_copy(conn, 2, 100, "2026-01-02T00:00:00Z")
            recovery._make_room(conn, 100)  # 200+100 <= 1000
            assert db.get_recovery(conn, 1) is not None
            assert db.get_recovery(conn, 2) is not None


class TestExpiry:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        with db.connect() as conn:
            db.add_user(conn, "alice")

    def test_reap_expired_deletes_the_row_and_the_dir(self):
        now = datetime.now(timezone.utc)
        stale = now - timedelta(minutes=recovery.WINDOW_MINUTES + 1)
        with db.connect() as conn:
            db.record_spawn(conn, 1, 1, "train.py", "uuid", minutes=10, gpus=1)
            db.mark_finished(conn, 1, 0, _iso(stale))
            db.add_recovery(conn, 1, 100, at=_iso(stale))
            d = head_spawns.SPAWNS_DIR / "1" / "recovery"
            d.mkdir(parents=True)
            assert recovery.reap_expired(conn, now) == 1
            assert db.get_recovery(conn, 1) is None
            assert not d.exists()

    def test_reap_expired_leaves_a_fresh_copy_alone(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            db.record_spawn(conn, 2, 1, "train.py", "uuid", minutes=10, gpus=1)
            db.mark_finished(conn, 2, 0, _iso(now))
            db.add_recovery(conn, 2, 100, at=_iso(now))
            assert recovery.reap_expired(conn, now) == 0
            assert db.get_recovery(conn, 2) is not None


class TestStatus:
    @pytest.fixture(autouse=True)
    def _db(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            db.add_user(conn, "alice")

    def test_none_when_there_is_no_copy(self):
        with db.connect() as conn:
            assert recovery.status(conn, 999) is None

    def test_expires_at_is_created_at_plus_the_window(self):
        with db.connect() as conn:
            db.record_spawn(conn, 1, 1, "train.py", "uuid", minutes=10, gpus=1)
            db.add_recovery(conn, 1, 555, at="2026-01-01T00:00:00Z")
            st = recovery.status(conn, 1)
            assert st == {"bytes": 555, "expires_at": f"2026-01-01T00:{recovery.WINDOW_MINUTES:02d}:00Z"}


class TestTarBytes:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")

    def test_none_when_no_recovery_dir_exists(self):
        assert recovery.tar_bytes(42) is None

    def test_none_for_an_empty_recovery_dir(self):
        """Emptiness, not existence, is the test — mirrors spawns.outputs_tar's own rule: submit()
        pre-creates dirs, so presence alone says nothing."""
        (head_spawns.SPAWNS_DIR / "43" / "recovery").mkdir(parents=True)
        assert recovery.tar_bytes(43) is None

    def test_tars_the_recovery_dir(self):
        d = head_spawns.SPAWNS_DIR / "44" / "recovery"
        d.mkdir(parents=True)
        (d / "result.txt").write_text("hi")
        data = recovery.tar_bytes(44)
        assert data is not None
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            names = tf.getnames()
        assert any(n.endswith("result.txt") for n in names)


# --- the two new API routes ---------------------------------------------------

def _client(monkeypatch, tmp_path) -> TestClient:
    user = {"id": 1, "username": "alice", "is_admin": False, "must_change_password": False}
    head_main.app.dependency_overrides[head_main.current_user] = lambda: user
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        db.add_user(conn, "alice")
    monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
    return TestClient(head_main.app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


class TestRecoveryStatusRoute:
    def test_404_when_no_copy(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        r = c.get("/api/spawns/999/recovery")
        assert r.status_code == 404

    def test_200_with_bytes_and_expiry_when_present(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        with db.connect() as conn:
            db.record_spawn(conn, 1, 1, "train.py", "uuid", minutes=10, gpus=1)
            db.add_recovery(conn, 1, 4096, at="2026-01-01T00:00:00Z")
        r = c.get("/api/spawns/1/recovery")
        assert r.status_code == 200
        assert r.json() == {"bytes": 4096, "expires_at": f"2026-01-01T00:{recovery.WINDOW_MINUTES:02d}:00Z"}

    def test_401_without_a_session(self, tmp_path):
        # No dependency_overrides, no monkeypatched user — the real auth dependency runs and refuses.
        assert TestClient(head_main.app).get("/api/spawns/1/recovery").status_code == 401


class TestRecoveryDownloadRoute:
    def test_404_when_no_copy(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert c.get("/api/spawns/999/recovery/download").status_code == 404

    def test_200_with_a_gzip_tar_when_present(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        d = tmp_path / "spawns" / "7" / "recovery"
        d.mkdir(parents=True)
        (d / "checkpoint.pt").write_bytes(b"weights")
        r = c.get("/api/spawns/7/recovery/download")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/gzip"
        assert "spawn-7-recovery.tar.gz" in r.headers["content-disposition"]


# --- history: the per-row Recovery Window countdown ---------------------------

class TestHistoryCarriesRecoveryExpiry:
    def test_a_row_with_an_open_window_carries_its_expiry(self, monkeypatch, tmp_path):
        from ccc_head_api import slurm
        c = _client(monkeypatch, tmp_path)
        monkeypatch.setattr(slurm, "spawns", lambda: [])  # nothing live — everything is history
        with db.connect() as conn:
            db.record_spawn(conn, 1, 1, "train.py", "uuid", minutes=10, gpus=1)
            db.mark_finished(conn, 1, 0, "2026-01-01T00:00:00Z")
            db.add_recovery(conn, 1, 4096, at="2026-01-01T00:00:00Z")
        r = c.get("/api/spawns/history?days=3650")
        assert r.status_code == 200
        row = next(row for row in r.json() if row["job_id"] == 1)
        assert row["recovery_bytes"] == 4096
        assert row["recovery_expires_at"] == f"2026-01-01T00:{recovery.WINDOW_MINUTES:02d}:00Z"
        assert "recovery_created_at" not in row  # internal column, not for the wire

    def test_a_row_with_no_copy_carries_null(self, monkeypatch, tmp_path):
        from ccc_head_api import slurm
        c = _client(monkeypatch, tmp_path)
        monkeypatch.setattr(slurm, "spawns", lambda: [])
        with db.connect() as conn:
            db.record_spawn(conn, 2, 1, "train.py", "uuid", minutes=10, gpus=1)
            db.mark_finished(conn, 2, 0, "2026-01-01T00:00:00Z")
        r = c.get("/api/spawns/history?days=3650")
        row = next(row for row in r.json() if row["job_id"] == 2)
        assert row["recovery_expires_at"] is None


# --- the CLI: `cuttle recover`, and `cuttle outputs`'s miss-hint ---------------
# Uses conftest.py's real end-to-end rig (`signed_in` -> `cli` -> `head`/`ccc_home`), which already
# patches db.DB_PATH and seeds alice/root — the same real routes tested above, invoked as a User would.

class TestCliRecover:
    def test_recover_downloads_the_tar(self, signed_in, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        monkeypatch.chdir(tmp_path)
        d = tmp_path / "spawns" / "5" / "recovery"
        d.mkdir(parents=True)
        (d / "ckpt.pt").write_bytes(b"weights")
        r = signed_in("recover", "5")
        assert r.exit_code == 0, r.output
        assert (tmp_path / "spawn-5-recovery.tar.gz").exists()

    def test_a_miss_warns_rather_than_erroring(self, signed_in, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        r = signed_in("recover", "999")
        assert r.exit_code == 0, r.output
        assert "Recovery Window" in r.output


class TestCliOutputsRecoveryHint:
    def test_a_miss_hints_an_open_window(self, signed_in, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        (tmp_path / "spawns" / "6" / "outputs").mkdir(parents=True)  # empty -> no outputs, a real miss
        with db.connect() as conn:
            db.record_spawn(conn, 6, 1, "train.py", "uuid", minutes=10, gpus=1)
            db.add_recovery(conn, 6, 2048, at=_iso(datetime.now(timezone.utc)))
        r = signed_in("outputs", "6")
        assert r.exit_code == 0, r.output
        assert "Recovery Window is still open" in r.output
        assert "cuttle recover 6" in r.output

    def test_a_miss_with_no_recovery_stays_quiet(self, signed_in, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        (tmp_path / "spawns" / "7" / "outputs").mkdir(parents=True)
        r = signed_in("outputs", "7")
        assert r.exit_code == 0, r.output
        assert "Recovery Window" not in r.output
