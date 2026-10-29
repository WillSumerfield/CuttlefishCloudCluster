"""A finished Dive's durable ending: the tick stamps it, `dive.outcome` names it, history serves it.

The tick is the only thing that notices a Dive has ended (absence from a successful squeue poll — see
tick.py). Pins what a live-queue test never sees: an Admin's Dive (no Escrow, never settled) still gets
an ending, the first observed ending wins, and ended / timed out / lost are told apart.
"""

from datetime import datetime, timezone

import pytest

from ccc_head_api import db, dive, tick

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


def _dive(conn, job_id, user_id, hold=5.0, minutes=60):
    db.create_dive(conn, job_id, user_id, f"{job_id:032x}", "proj", [], 31000 + job_id % 100,
                   minutes=minutes, gpus=1, vram_gb=24, hold=hold)


class TestTheTickStampsTheEnd:
    def test_a_dive_that_left_the_queue_is_stamped(self, conn):
        _dive(conn, 4207, _user(conn, "alice"))
        tick._record_endings(conn, live_ids=set(), now=_NOW)
        assert db.get_dive(conn, 4207)["finished_at"] == "2026-07-26T12:00:00Z"

    def test_a_still_queued_dive_is_left_open(self, conn):
        _dive(conn, 4207, _user(conn, "alice"))
        tick._record_endings(conn, live_ids={4207}, now=_NOW)
        assert db.get_dive(conn, 4207)["finished_at"] is None

    def test_an_admin_dive_with_no_escrow_is_still_stamped(self, conn):
        """Why stamping is its own step: an Admin's Dive never settles, so settlement would leave it
        open in history forever."""
        _dive(conn, 4300, _user(conn, "root", admin=True), hold=None)
        tick._record_endings(conn, live_ids=set(), now=_NOW)
        assert db.get_dive(conn, 4300)["finished_at"] is not None

    def test_the_first_observed_ending_wins(self, conn):
        _dive(conn, 4207, _user(conn, "alice"))
        tick._record_endings(conn, set(), _NOW)
        tick._record_endings(conn, set(), _NOW.replace(hour=13))
        assert db.get_dive(conn, 4207)["finished_at"] == "2026-07-26T12:00:00Z"


class TestOutcome:
    def _ended(self, conn, started, finished, cancelled=False):
        _dive(conn, 1, _user(conn, "alice"), minutes=60)
        conn.execute("UPDATE dives SET started_at=?, finished_at=?, cancelled=? WHERE job_id=1",
                     (started, finished, int(cancelled)))
        return db.dive_detail(conn, 1)

    def test_someone_ended_it(self, conn):
        assert dive.outcome(self._ended(conn, "2026-07-26T10:00:00Z", "2026-07-26T10:05:00Z", True)) == "ended"

    def test_it_ran_its_time(self, conn):
        assert dive.outcome(self._ended(conn, "2026-07-26T10:00:00Z", "2026-07-26T11:00:20Z")) == "timed out"

    def test_it_died_early(self, conn):
        assert dive.outcome(self._ended(conn, "2026-07-26T10:00:00Z", "2026-07-26T10:20:00Z")) == "lost"

    def test_never_started(self, conn):
        assert dive.outcome(self._ended(conn, None, "2026-07-26T10:20:00Z")) == "lost"

    def test_placement_stamps_the_start_once(self, conn):
        _dive(conn, 1, _user(conn, "alice"))
        db.record_placement(conn, 1, "arm1", "2026-07-26T10:00:00Z")
        db.record_placement(conn, 1, "arm2", "2026-07-26T10:01:00Z")
        row = db.get_dive(conn, 1)
        assert (row["placed_node"], row["started_at"]) == ("arm1", "2026-07-26T10:00:00Z")


class TestTheGapBeforeTheTickStamps:
    """A Dive leaves squeue the instant it ends, but the tick only stamps it on its next pass — up to
    30s later. History excludes only what is LIVE, so for that window the row is served unstamped: a
    client must read `outcome` None as not-yet-known, never as a failure."""

    def test_an_ended_but_unstamped_dive_is_already_served_by_history(self, conn, monkeypatch):
        _dive(conn, 4400, _user(conn, "alice"))
        monkeypatch.setattr(dive.slurm, "dives", lambda: [])
        rows = dive.history(days=36500, limit=None)
        assert [r["job_id"] for r in rows] == [4400]
        assert rows[0]["finished_at"] is None and rows[0]["outcome"] is None


class TestHistoryLimit:
    """`limit` caps the archive to the newest N; None returns every row."""

    def _dated(self, conn, job_id, uid, at):
        _dive(conn, job_id, uid)
        conn.execute("UPDATE dives SET submitted_at=? WHERE job_id=?", (at, job_id))
        conn.commit()

    def test_limit_keeps_the_newest_n(self, conn):
        uid = _user(conn, "alice")
        self._dated(conn, 1, uid, "2026-07-20T00:00:00Z")   # oldest
        self._dated(conn, 2, uid, "2026-07-21T00:00:00Z")
        self._dated(conn, 3, uid, "2026-07-22T00:00:00Z")   # newest
        rows = db.dive_history(conn, "2000-01-01T00:00:00Z", [], limit=2)
        assert [r["job_id"] for r in rows] == [3, 2]

    def test_none_returns_every_row(self, conn):
        uid = _user(conn, "alice")
        self._dated(conn, 1, uid, "2026-07-20T00:00:00Z")
        self._dated(conn, 2, uid, "2026-07-21T00:00:00Z")
        assert len(db.dive_history(conn, "2000-01-01T00:00:00Z", [], limit=None)) == 2

    def test_limit_composes_with_the_live_exclusion(self, conn):
        """The exclude clause (live queue) and LIMIT must both hold: cap the newest of what's left."""
        uid = _user(conn, "alice")
        self._dated(conn, 1, uid, "2026-07-20T00:00:00Z")
        self._dated(conn, 2, uid, "2026-07-21T00:00:00Z")
        self._dated(conn, 3, uid, "2026-07-22T00:00:00Z")
        rows = db.dive_history(conn, "2000-01-01T00:00:00Z", [3], limit=1)   # 3 is live
        assert [r["job_id"] for r in rows] == [2]
