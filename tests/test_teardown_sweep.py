"""Teardown sweeps the job-keyed /tmp leftovers a Slurm state reset makes ambiguous."""

from pathlib import Path

from cuttle import teardown



class TestTeardownSweepsWhatItMakesAmbiguous:
    """`remove_slurm_config` drops Slurm's state dir and so resets the job-id counter, which is what
    turns every leftover /tmp/ccc-<jid>* from the outgoing cluster into a landmine for the incoming
    one. The sweep belongs beside the reset that causes it."""

    def _fixture(self, tmp_path, monkeypatch):
        for name in ("ccc-9.slurm", "ccc-10-deadbeef.slurm", "ccc-bench-25.log",
                     "ccc-u60002-something", "ccc-notes.txt"):
            (tmp_path / name).write_text("x")
        (tmp_path / "ccc-9").mkdir()          # a job's scratch dir
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

