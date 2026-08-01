"""Arm Retract/Return — an owner's self-service pause (Phase 2 loose end).

App-layer, not real Slurm DRAIN (see ccc_head_api.retraction's module docstring): the mechanism is a
head.db flag steering NEW placement away from a node, plus signalling whatever is RUNNING right now.
These are the state-machine details nothing else checks: what `retraction.retract` actually catches and
signals, that Return never reprieves a caught Spawn, and what submit_checked refuses/excludes.
"""

from pathlib import Path

import pytest

from ccc_head_api import db, retraction, spawns


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    c = db.connect()
    db.init_schema(c)
    return c


class TestRetractionState:
    def test_retract_then_return_clears_it(self, conn):
        db.mark_arm_retracted(conn, "gpu-box")
        assert db.retracted_nodes(conn) == {"gpu-box"}
        db.clear_arm_retraction(conn, "gpu-box")
        assert db.retracted_nodes(conn) == set()

    def test_a_caught_job_survives_return(self, conn):
        """The whole reason this is two tables. Return only clears the ARM-level flag; a Spawn the
        Retract already caught stays caught — it is doomed regardless of an early Return."""
        db.mark_arm_retracted(conn, "gpu-box")
        db.catch_retracting_job(conn, 501, "gpu-box", "2020-01-01T00:00:00Z")
        db.clear_arm_retraction(conn, "gpu-box")
        assert db.retracted_nodes(conn) == set()
        assert {r["job_id"] for r in db.retracting_jobs(conn)} == {501}


class TestRetract:
    """`retraction.retract`/`unretract` — the module both the owner-scoped API route and the Admin's
    `--any` escape hatch call into."""

    @pytest.fixture
    def cluster(self, conn, monkeypatch):
        state = {"jobs": []}
        monkeypatch.setattr(db, "connect", lambda: conn)
        monkeypatch.setattr(retraction.slurm, "queue", lambda: state["jobs"])
        signalled = []
        monkeypatch.setattr(retraction.slurm, "signal", lambda jid, sig: signalled.append((jid, sig)))
        return state, signalled

    def test_retract_flags_the_arm(self, conn, cluster):
        retraction.retract("gpu-box")
        assert db.retracted_nodes(conn) == {"gpu-box"}

    def test_retract_catches_and_signals_only_running_jobs_on_that_node(self, conn, cluster):
        state, signalled = cluster
        state["jobs"] = [
            {"id": 301, "state": "RUNNING", "node": "gpu-box"},
            {"id": 302, "state": "PENDING", "node": "gpu-box"},    # not running yet: not caught
            {"id": 303, "state": "RUNNING", "node": "other-box"},  # a different node: not caught
        ]
        retraction.retract("gpu-box")
        assert {r["job_id"] for r in db.retracting_jobs(conn)} == {301}
        assert signalled == [(301, "TERM")]

    def test_unretract_only_clears_the_arm_flag(self, conn, cluster):
        state, signalled = cluster
        state["jobs"] = [{"id": 304, "state": "RUNNING", "node": "gpu-box"}]
        retraction.retract("gpu-box")
        retraction.unretract("gpu-box")
        assert db.retracted_nodes(conn) == set()
        assert {r["job_id"] for r in db.retracting_jobs(conn)} == {304}  # still doomed


class TestExclude:
    """The submission side: an unpinned Spawn is steered off a retracted node with a plain
    --exclude=, never a real Slurm DRAIN."""

    def test_render_adds_an_exclude_line(self):
        body = spawns._render(Path("/tmp/x"), 1, "train.py", [], [], ["out"], None, 60, "",
                              "a3f9", "s3cr3t", exclude="gpu-box,gpu-box2")
        assert "#SBATCH --exclude=gpu-box,gpu-box2" in body

    def test_no_exclude_line_when_nothing_is_retracted(self):
        body = spawns._render(Path("/tmp/x"), 1, "train.py", [], [], ["out"], None, 60, "",
                              "a3f9", "s3cr3t", exclude="")
        assert "--exclude" not in body
