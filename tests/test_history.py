"""The durable outcome the tick stamps on a finished Spawn.

The tick is the only thing that notices a Spawn has ended (absence from a successful squeue
poll — see tick.py). `_record_endings` turns that into a durable row so `GET /spawns/history`
can read an outcome squeue has long since forgotten. Pins what a live-queue test never sees: an
Admin's Spawn (no Escrow, never settled) still gets an ending, and the first observed ending
wins.
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
    db.record_spawn(conn, job_id, user_id, "train.py", 60, 1, 24, node, hold=hold)


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


class TestTheGapBeforeTheTickStamps:
    """A Spawn leaves squeue the instant it ends, but the tick only stamps it on its next pass — up to
    TICK_SECONDS (30s) later. `spawn_history` excludes only what is LIVE, so for that whole window the
    row is already in the archive with a null exit code and a null finished_at.

    Pinned because both clients read these rows and one of them read that gap wrong: the Dashboard's
    outcomeOf tested exit_code first and drew the row as a red `failed` for a Spawn that had exited 0
    (hardware, 2026-08-15, Spawn 5). `finished_at` is what tells the two apart, and it is only ever
    written together with `exit_code` — that pairing is the guarantee a client can rely on.
    """

    def test_an_ended_but_unstamped_spawn_is_already_served_by_history(self, conn):
        uid = _user(conn, "alice")
        _spawn(conn, 4400, uid)
        rows = db.spawn_history(conn, "2000-01-01T00:00:00Z", exclude=[])
        assert [r["job_id"] for r in rows] == [4400]
        assert rows[0]["finished_at"] is None and rows[0]["exit_code"] is None

    def test_the_two_columns_are_only_ever_written_together(self, conn, monkeypatch):
        """So "no exit code" can never mean "failed" while finished_at is still null."""
        uid = _user(conn, "alice")
        for job_id, code in ((4401, 0), (4402, 3), (4403, None)):
            _spawn(conn, job_id, uid)
            monkeypatch.setattr(head_spawns, "exit_code", lambda jid, c=code: c)
            tick._record_endings(conn, live_ids=set(), now=_NOW)
            row = db.get_spawn(conn, job_id)
            assert row["finished_at"] is not None, "stamped rows always carry an end time"
            assert row["exit_code"] == code


class TestHistoryLimit:
    """`limit` caps the archive to the newest N — the CLI passes it so a busy cluster is not a wall of
    text; None (the Dashboard's read) returns every row."""

    def _dated(self, conn, job_id, uid, at):
        # record_spawn stamps submitted_at 'now'; rewrite it so newest-first ordering is deterministic.
        _spawn(conn, job_id, uid)
        conn.execute("UPDATE spawns SET submitted_at=? WHERE job_id=?", (at, job_id))
        conn.commit()

    def test_limit_keeps_the_newest_n(self, conn):
        uid = _user(conn, "alice")
        self._dated(conn, 1, uid, "2026-07-20T00:00:00Z")   # oldest
        self._dated(conn, 2, uid, "2026-07-21T00:00:00Z")
        self._dated(conn, 3, uid, "2026-07-22T00:00:00Z")   # newest
        rows = db.spawn_history(conn, "2000-01-01T00:00:00Z", [], limit=2)
        assert [r["job_id"] for r in rows] == [3, 2]

    def test_none_returns_every_row(self, conn):
        uid = _user(conn, "alice")
        self._dated(conn, 1, uid, "2026-07-20T00:00:00Z")
        self._dated(conn, 2, uid, "2026-07-21T00:00:00Z")
        assert len(db.spawn_history(conn, "2000-01-01T00:00:00Z", [], limit=None)) == 2

    def test_limit_composes_with_the_live_exclusion(self, conn):
        """The exclude clause (live queue) and LIMIT must both hold: cap the newest of what's left."""
        uid = _user(conn, "alice")
        self._dated(conn, 1, uid, "2026-07-20T00:00:00Z")
        self._dated(conn, 2, uid, "2026-07-21T00:00:00Z")
        self._dated(conn, 3, uid, "2026-07-22T00:00:00Z")
        rows = db.spawn_history(conn, "2000-01-01T00:00:00Z", [3], limit=1)   # 3 is live
        assert [r["job_id"] for r in rows] == [2]
