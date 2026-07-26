"""Phase 6 slice 4 Phase B — the durable outcome the tick stamps on a finished Spawn.

The tick is the only thing that notices a Spawn has ended (absence from a successful squeue poll — see
tick.py). `_record_endings` turns that observation into a durable row so `GET /spawns/history` can read
an outcome squeue has long since forgotten. These pin the parts a live-queue test never sees: an Admin's
Spawn (no Escrow, never settled) still gets an ending, and the first observed ending wins.
"""

from datetime import datetime, timezone

import pytest

from ccc_head_api import db, spawns as head_spawns, tick

_NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A real head.db; the tick opens its own connection, so point that at ours too."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    c = db.connect()
    db.init_schema(c)
    monkeypatch.setattr(db, "connect", lambda: c)
    return c


def _user(conn, name, admin=False):
    db.add_user(conn, name, is_admin=admin)
    return db.get_user(conn, name)["id"]


def _spawn(conn, job_id, user_id, hold=5.0, node="gpu-box"):
    db.record_spawn(conn, job_id, user_id, "train.py", f"uuid-{job_id}", 60, 1, 24, node, hold=hold)


class TestTheTickStampsOutcome:
    def test_a_finished_spawn_that_left_the_queue_is_stamped(self, conn, monkeypatch):
        uid = _user(conn, "alice")
        _spawn(conn, 4207, uid)
        monkeypatch.setattr(head_spawns, "exit_code", lambda jid: 0)
        tick._record_endings(conn, live_ids=set(), now=_NOW)
        row = db.get_spawn(conn, 4207)
        assert row["finished_at"] is not None and row["exit_code"] == 0

    def test_a_still_queued_spawn_is_left_open(self, conn, monkeypatch):
        uid = _user(conn, "alice")
        _spawn(conn, 4207, uid)
        monkeypatch.setattr(head_spawns, "exit_code", lambda jid: 0)
        tick._record_endings(conn, live_ids={4207}, now=_NOW)
        assert db.get_spawn(conn, 4207)["finished_at"] is None

    def test_an_admin_spawn_with_no_escrow_is_still_stamped(self, conn, monkeypatch):
        """The reason outcome-stamping is its own step, not folded into settlement: an Admin's Spawn
        carries no Escrow and never settles, so settlement would leave it open in history forever."""
        aid = _user(conn, "root", admin=True)
        _spawn(conn, 4300, aid, hold=None)      # Admin: outside the economy, no hold
        monkeypatch.setattr(head_spawns, "exit_code", lambda jid: 0)
        tick._record_endings(conn, live_ids=set(), now=_NOW)
        assert db.get_spawn(conn, 4300)["finished_at"] is not None

    def test_a_missing_exit_file_is_a_failure_with_no_code(self, conn, monkeypatch):
        """No exit file == the Spawn died recording no code (scancel'd, OOM, or its Arm dropped before
        stage-out). Stamped finished, exit_code NULL — history reads that as failed, not as open."""
        uid = _user(conn, "alice")
        _spawn(conn, 4207, uid)
        monkeypatch.setattr(head_spawns, "exit_code", lambda jid: None)
        tick._record_endings(conn, live_ids=set(), now=_NOW)
        row = db.get_spawn(conn, 4207)
        assert row["finished_at"] is not None and row["exit_code"] is None

    def test_the_first_observed_ending_wins(self, conn, monkeypatch):
        """Idempotent on finished_at: a later pass must not overwrite a real exit 0 with a NULL once the
        exit file has aged away underneath it."""
        uid = _user(conn, "alice")
        _spawn(conn, 4207, uid)
        monkeypatch.setattr(head_spawns, "exit_code", lambda jid: 0)
        tick._record_endings(conn, set(), _NOW)
        monkeypatch.setattr(head_spawns, "exit_code", lambda jid: None)   # file gone by the next pass
        tick._record_endings(conn, set(), _NOW)
        assert db.get_spawn(conn, 4207)["exit_code"] == 0
