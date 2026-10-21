"""Output retention (docs/TODO.md "Storage retention") — declared Output results live 7
days, and a User holds at most 32GB of them at once.

What's tested here: registration off the tick's own signal (no marker of its own), the TTL running from
the Spawn's END rather than from registration, per-User oldest-first eviction, the two API surfaces (the
`/retention` status route and `/outputs`' now-informative 404), and `cuttle outputs` telling a User
their results expired rather than that the Spawn produced nothing. NOT tested: a live tick loop over
real days — the same carve-out the rest of the suite draws.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db, retention
from ccc_head_api import main as head_main
from ccc_head_api import spawns as head_spawns
from cuttle import config

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _iso(dt: datetime) -> str:
    return dt.strftime(_TS)


class _Base:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        head_spawns.SPAWNS_DIR.mkdir()
        with db.connect() as conn:
            db.add_user(conn, "alice")
            db.add_user(conn, "bob")

    def _outputs(self, job_id: int, size: int = 16):
        d = head_spawns.SPAWNS_DIR / str(job_id) / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        (d / "ckpt.pt").write_bytes(b"x" * size)
        return d

    def _spawn(self, conn, job_id: int, user_id: int = 1, finished_at: str | None = None):
        db.record_spawn(conn, job_id, user_id, "train.py", f"u{job_id}", minutes=10, gpus=1)
        if finished_at:
            db.mark_finished(conn, job_id, 0, finished_at)


class TestRegistration(_Base):
    """No marker of its own: `stage_out` sends `exit` LAST, so a Spawn the tick has stamped finished is
    one whose outputs have already landed."""

    def test_a_finished_spawns_outputs_are_registered_and_sized(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._spawn(conn, 1, finished_at=_iso(now))
            self._outputs(1, size=4096)
            assert retention.register_new(conn, now) == 1
            assert db.get_output(conn, 1)["bytes"] == 4096

    def test_a_running_spawn_is_not_registered(self):
        """Its outputs are still being written to — the periodic pusher is mid-flight."""
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._spawn(conn, 2)
            self._outputs(2)
            assert retention.register_new(conn, now) == 0
            assert db.get_output(conn, 2) is None

    def test_a_spawn_that_produced_nothing_is_still_registered(self):
        """At 0 bytes. It costs no Allowance, and it means the empty dir is reaped on the same schedule
        as everything else rather than lingering forever."""
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._spawn(conn, 3, finished_at=_iso(now))
            (head_spawns.SPAWNS_DIR / "3" / "outputs").mkdir(parents=True)
            assert retention.register_new(conn, now) == 1
            assert db.get_output(conn, 3)["bytes"] == 0

    def test_never_registers_the_same_spawn_twice(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._spawn(conn, 4, finished_at=_iso(now))
            self._outputs(4)
            retention.register_new(conn, now)
            assert retention.register_new(conn, now) == 0

    def test_the_ttl_runs_from_the_spawns_end_not_from_registration(self):
        """A tick that was down for a day must not silently grant a Spawn an extra day of retention."""
        now = datetime.now(timezone.utc)
        ended = now - timedelta(days=2)
        with db.connect() as conn:
            self._spawn(conn, 5, finished_at=_iso(ended))
            self._outputs(5)
            retention.register_new(conn, now)
            assert db.get_output(conn, 5)["created_at"] == _iso(ended)
            left = retention.status(conn, 5)["expires_at"]
            assert left == _iso(ended + timedelta(days=retention.TTL_DAYS))

    def test_a_late_registration_can_be_expired_immediately(self):
        """Follows from the rule above: a Head that was down past the whole TTL registers, then reaps."""
        now = datetime.now(timezone.utc)
        ended = now - timedelta(days=retention.TTL_DAYS + 1)
        with db.connect() as conn:
            self._spawn(conn, 6, finished_at=_iso(ended))
            d = self._outputs(6)
            retention.register_new(conn, now)
            assert retention.reap_expired(conn, now) == 1
            assert not d.exists()

    def test_a_spawn_dir_with_no_attribution_row_survives_until_it_is_old(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            d = self._outputs(99)
            assert retention.register_new(conn, now) == 0
            assert d.exists()

    def test_an_old_spawn_dir_with_no_attribution_row_is_dropped(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            d = self._outputs(99)
            later = now + timedelta(minutes=retention._ORPHAN_MINUTES + 1)
            retention.register_new(conn, later)
            assert not d.exists()

    def test_a_non_numeric_dir_is_ignored(self):
        now = datetime.now(timezone.utc)
        (head_spawns.SPAWNS_DIR / "staging" / "outputs").mkdir(parents=True)
        with db.connect() as conn:
            assert retention.register_new(conn, now) == 0

    def test_a_missing_spawns_dir_is_not_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "nope")
        with db.connect() as conn:
            assert retention.register_new(conn, datetime.now(timezone.utc)) == 0


class TestExpiry(_Base):
    def test_outputs_past_the_ttl_are_deleted(self):
        now = datetime.now(timezone.utc)
        stale = now - timedelta(days=retention.TTL_DAYS, minutes=1)
        with db.connect() as conn:
            self._spawn(conn, 1, finished_at=_iso(stale))
            d = self._outputs(1)
            db.add_output(conn, 1, 16, at=_iso(stale))
            assert retention.reap_expired(conn, now) == 1
            assert db.get_output(conn, 1) is None
            assert not d.exists()

    def test_fresh_outputs_are_left_alone(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._spawn(conn, 2, finished_at=_iso(now))
            d = self._outputs(2)
            db.add_output(conn, 2, 16, at=_iso(now))
            assert retention.reap_expired(conn, now) == 0
            assert d.exists()

    def test_the_whole_spawn_directory_goes_not_just_the_outputs(self):
        """`out`, `err`, `exit` and the markers had no deleter anywhere, so one directory per Spawn
        ever submitted accumulated forever — and `register_new` lists that directory every pass, so the
        tick's cost grew with the cluster's entire history. The Spawn's ROW survives: history and
        Attribution are the accounting record, and outlive the files."""
        now = datetime.now(timezone.utc)
        stale = now - timedelta(days=retention.TTL_DAYS + 1)
        with db.connect() as conn:
            self._spawn(conn, 3, finished_at=_iso(stale))
            self._outputs(3)
            spawn_dir = head_spawns.SPAWNS_DIR / "3"
            (spawn_dir / "out").write_text("epoch 1\n")
            (spawn_dir / "exit").write_text("0\n")
            db.add_output(conn, 3, 16, at=_iso(stale))
            retention.reap_expired(conn, now)
            assert not spawn_dir.exists()
            assert head_spawns.swept(3), "the logs read as expired, not as a Spawn that said nothing"
            assert db.get_spawn(conn, 3) is not None

    def test_the_dir_is_not_recreated_empty(self):
        """An empty dir would serve a tar that reads as "this Spawn produced nothing" — the one wrong
        answer. Its ABSENCE is what lets the 404 say the results expired."""
        now = datetime.now(timezone.utc)
        stale = now - timedelta(days=retention.TTL_DAYS + 1)
        with db.connect() as conn:
            self._spawn(conn, 4, finished_at=_iso(stale))
            self._outputs(4)
            db.add_output(conn, 4, 16, at=_iso(stale))
            retention.reap_expired(conn, now)
            assert retention.evicted(4)


class TestAllowance(_Base):
    """32GB per User, and an output is ALWAYS accepted — the Karma is already spent and the Arm already
    did the work, so the eviction lands on that User's own older results instead."""

    @pytest.fixture(autouse=True)
    def _small(self, monkeypatch):
        monkeypatch.setattr(retention, "MAX_USER_BYTES", 1000)

    def _held(self, conn, job_id: int, user_id: int, size: int, at: str):
        self._spawn(conn, job_id, user_id, finished_at=at)
        self._outputs(job_id)
        db.add_output(conn, job_id, size, at=at)

    def test_a_new_result_over_the_allowance_evicts_the_oldest_first(self):
        with db.connect() as conn:
            self._held(conn, 1, 1, 400, "2026-01-01T00:00:00Z")
            self._held(conn, 2, 1, 400, "2026-01-02T00:00:00Z")
            retention._make_room(conn, 1, 400)
            assert db.get_output(conn, 1) is None
            assert db.get_output(conn, 2) is not None

    def test_eviction_repeats_until_it_fits(self):
        with db.connect() as conn:
            self._held(conn, 1, 1, 400, "2026-01-01T00:00:00Z")
            self._held(conn, 2, 1, 400, "2026-01-02T00:00:00Z")
            self._held(conn, 3, 1, 400, "2026-01-03T00:00:00Z")
            retention._make_room(conn, 1, 400)
            assert db.get_output(conn, 1) is None
            assert db.get_output(conn, 2) is None
            assert db.get_output(conn, 3) is not None

    def test_one_users_results_never_evict_anothers(self):
        """The whole reason the Allowance is per User and not a shared pool (CONTEXT.md, Allowance)."""
        with db.connect() as conn:
            self._held(conn, 1, 2, 900, "2026-01-01T00:00:00Z")  # bob's, and older
            self._held(conn, 2, 1, 900, "2026-01-02T00:00:00Z")  # alice's
            retention._make_room(conn, 1, 400)                   # alice needs room
            assert db.get_output(conn, 1) is not None            # bob's is untouched, older or not
            assert db.get_output(conn, 2) is None                # hers is the one that goes

    def test_a_result_larger_than_the_whole_allowance_still_lands(self):
        """Always accepted: refusing at the last moment loses the one thing the User paid for. It
        empties their held results and registers anyway — the TTL is what bounds it after that."""
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._held(conn, 1, 1, 400, "2026-01-01T00:00:00Z")
            self._spawn(conn, 2, 1, finished_at=_iso(now))
            self._outputs(2, size=5000)
            assert retention.register_new(conn, now) == 1
            assert db.get_output(conn, 1) is None
            assert db.get_output(conn, 2)["bytes"] == 5000

    def test_registration_makes_room_before_it_registers(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._held(conn, 1, 1, 900, "2026-01-01T00:00:00Z")
            self._spawn(conn, 2, 1, finished_at=_iso(now))
            self._outputs(2, size=200)
            retention.register_new(conn, now)
            assert db.user_output_bytes(conn, 1) <= retention.MAX_USER_BYTES

    def test_a_deleted_users_results_count_against_nothing(self):
        """`remove_user` NULLs the Attribution row's user_id. There is no Allowance left to charge, and
        the TTL still reaps it on schedule."""
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._spawn(conn, 1, 1, finished_at=_iso(now))
            conn.execute("UPDATE spawns SET user_id = NULL WHERE job_id = 1")
            conn.commit()
            self._outputs(1, size=5000)
            assert retention.register_new(conn, now) == 1


class TestStatus(_Base):
    def test_none_when_nothing_is_held(self):
        with db.connect() as conn:
            assert retention.status(conn, 999) is None

    def test_expires_at_is_the_spawns_end_plus_the_ttl(self):
        with db.connect() as conn:
            self._spawn(conn, 1, finished_at="2026-01-01T00:00:00Z")
            db.add_output(conn, 1, 2048, at="2026-01-01T00:00:00Z")
            st = retention.status(conn, 1)
            assert st["bytes"] == 2048
            assert st["expires_at"] == "2026-01-08T00:00:00Z"

    def test_evicted_is_false_while_the_dir_is_there_even_if_empty(self):
        """A Spawn that wrote nothing has an EMPTY outputs dir, not a missing one — `submit` makes it
        for every Spawn. Confusing the two is what would tell a User their results expired when the
        Spawn simply never wrote any."""
        (head_spawns.SPAWNS_DIR / "7" / "outputs").mkdir(parents=True)
        assert not retention.evicted(7)


# --- the API ------------------------------------------------------------------
# In-process rig for the retention routes.

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


class TestApi:
    def test_retention_route_reports_what_is_held(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        with db.connect() as conn:
            db.record_spawn(conn, 1, 1, "train.py", "u1", minutes=10, gpus=1)
            db.add_output(conn, 1, 4096, at="2026-01-01T00:00:00Z")
        r = c.get("/api/spawns/1/retention")
        assert r.status_code == 200
        assert r.json() == {"bytes": 4096, "expires_at": "2026-01-08T00:00:00Z"}

    def test_retention_route_404s_when_nothing_is_held(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert c.get("/api/spawns/404/retention").status_code == 404

    def test_retention_route_401s_without_a_session(self, tmp_path):
        assert TestClient(head_main.app).get("/api/spawns/1/retention").status_code == 401

    def test_outputs_404_says_expired_when_the_dir_is_gone(self, monkeypatch, tmp_path):
        """The distinction the whole 404 exists to draw: results that were held and are now gone read
        differently from a Spawn that never wrote any."""
        c = _client(monkeypatch, tmp_path)
        with db.connect() as conn:
            db.record_spawn(conn, 8, 1, "train.py", "u8", minutes=10, gpus=1)
        r = c.get("/api/spawns/8/outputs")
        assert r.status_code == 404
        assert "no longer held" in r.json()["detail"]

    def test_outputs_404_says_wrote_nothing_when_the_dir_is_empty(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        (tmp_path / "spawns" / "9" / "outputs").mkdir(parents=True)
        with db.connect() as conn:
            db.record_spawn(conn, 9, 1, "train.py", "u9", minutes=10, gpus=1)
        r = c.get("/api/spawns/9/outputs")
        assert r.status_code == 404
        assert "wrote no outputs" in r.json()["detail"]

    def test_history_carries_the_countdown(self, monkeypatch, tmp_path):
        from ccc_head_api import slurm
        c = _client(monkeypatch, tmp_path)
        monkeypatch.setattr(slurm, "spawns", lambda: [])
        with db.connect() as conn:
            db.record_spawn(conn, 10, 1, "train.py", "u10", minutes=10, gpus=1)
            db.mark_finished(conn, 10, 0, "2026-01-01T00:00:00Z")
            db.add_output(conn, 10, 2048, at="2026-01-01T00:00:00Z")
        rows = c.get("/api/spawns/history?days=3650").json()
        row = next(r for r in rows if r["job_id"] == 10)
        assert row["output_bytes"] == 2048
        assert row["output_expires_at"] == "2026-01-08T00:00:00Z"
        assert "output_created_at" not in row  # internal column, not for the wire

    def test_a_row_with_nothing_held_carries_null(self, monkeypatch, tmp_path):
        from ccc_head_api import slurm
        c = _client(monkeypatch, tmp_path)
        monkeypatch.setattr(slurm, "spawns", lambda: [])
        with db.connect() as conn:
            db.record_spawn(conn, 11, 1, "train.py", "u11", minutes=10, gpus=1)
            db.mark_finished(conn, 11, 0, "2026-01-01T00:00:00Z")
        row = next(r for r in c.get("/api/spawns/history?days=3650").json() if r["job_id"] == 11)
        assert row["output_expires_at"] is None


# --- the CLI ------------------------------------------------------------------

class TestCliOutputs:
    def test_results_land_unpacked_where_the_auto_download_puts_them(self, signed_in, monkeypatch,
                                                                     tmp_path):
        """One predictable location, whichever way results were fetched, and the tree rather than the
        tar it travelled in. Into the cwd it would land wherever the shell happened to be standing —
        and running the command twice would leave two copies of a 3GB result in two directories."""
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        out = tmp_path / "spawns" / "20" / "outputs" / "runs"
        out.mkdir(parents=True)
        (out / "ckpt.pt").write_bytes(b"weights")
        r = signed_in("outputs", "20")
        assert r.exit_code == 0, r.output
        landed = config.OUTPUTS_DIR / "spawn-20-outputs"
        assert (landed / "runs" / "ckpt.pt").read_bytes() == b"weights"
        # Said out loud. A download whose destination is only in the docs is one the User goes looking
        # for in the directory they ran the command from.
        assert str(landed) in r.output.replace("\n", "")   # the console wraps long paths

    def test_a_miss_says_the_results_expired(self, signed_in, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        with db.connect() as conn:
            db.record_spawn(conn, 11, 1, "train.py", "u11", minutes=10, gpus=1)
        r = signed_in("outputs", "11")
        assert r.exit_code == 0, r.output
        assert "no longer held" in r.output

    def test_a_spawn_that_wrote_nothing_says_so_instead(self, signed_in, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        (tmp_path / "spawns" / "12" / "outputs").mkdir(parents=True)
        with db.connect() as conn:
            db.record_spawn(conn, 12, 1, "train.py", "u12", minutes=10, gpus=1)
        r = signed_in("outputs", "12")
        assert r.exit_code == 0, r.output
        assert "wrote no outputs" in r.output


# --- wiring -------------------------------------------------------------------

class TestTheTickRegistersAndReaps:
    """One wiring test, matching test_projects'."""

    def test_one_pass_registers_a_finished_spawns_outputs(self, monkeypatch, tmp_path):
        from ccc_head_api import tick

        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        monkeypatch.setattr(head_spawns, "PROJECTS_DIR", tmp_path / "projects")
        now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        with db.connect() as conn:
            monkeypatch.setattr(db, "connect", lambda: conn)
            monkeypatch.setattr(tick.slurm, "queue", lambda: [])
            monkeypatch.setattr(tick.slurm, "arms", lambda: [])
            db.add_user(conn, "alice")
            uid = db.get_user(conn, "alice")["id"]
            db.record_spawn(conn, 1, uid, "train.py", "uuid-1", minutes=60, gpus=1)
            db.mark_finished(conn, 1, 0, _iso(now - timedelta(minutes=1)))
            d = head_spawns.SPAWNS_DIR / "1" / "outputs"
            d.mkdir(parents=True)
            (d / "ckpt.pt").write_bytes(b"x" * 512)

            tick.once(now=now)

            assert db.get_output(conn, 1)["bytes"] == 512
