"""Uploaded projects are staging, not storage (docs/TODO.md "Storage retention").

`PROJECTS_DIR/<uuid>` held every submission's extracted project — up to 4GB each — and nothing ever
deleted one. What's tested here: the `staged` marker in the rendered JOB_SCRIPT (both branches), the
sweep's three cases, the half-close of the Siphon that goes with it, and `--no-requeue`, without which
a requeued job would re-stage a project the sweep has already deleted.
"""

import os
import pathlib
import stat
from datetime import datetime, timedelta, timezone

import pytest

from ccc_head_api import db, projects, siphons
from ccc_head_api import spawns as head_spawns

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _iso(dt: datetime) -> str:
    return dt.strftime(_TS)


# --- the rendered JOB_SCRIPT --------------------------------------------------

class TestTheStagedMarker:
    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24")
        kw.setdefault("node", None)
        kw.setdefault("outputs", ["outputs"])
        return head_spawns._render(
            staging="/tmp/u", gpu=1, script="train.py", args=[], files=[],
            token="a3f9", secret="s3cr3t", **kw)

    def _head_branch(self, script: str) -> str:
        return script[script.index('if [ -d "$staging_dir" ]'):script.index("\nelse")]

    def _remote_branch(self, script: str) -> str:
        return script[script.index("else"):script.index('\ncd "$proj"')]

    def test_head_branch_marks_staged_after_the_copy(self):
        """The marker means "the Head's copy has done its job". Written before the `cp -a` returned it
        would authorise deleting a project this Spawn is still reading."""
        head = self._head_branch(self.render())
        assert head.index("cp -a --no-preserve=ownership") < head.index(': > "$spawn_dir/staged"')

    def test_remote_branch_marks_staged_after_the_stage_in_rsync(self):
        remote = self._remote_branch(self.render())
        assert remote.index('"$proj/" 2>>"$err"') < remote.index(': > "$scratch/staged"')

    def test_remote_branch_sends_the_marker_home(self):
        """A marker only the Arm can see tells the Head nothing."""
        remote = self._remote_branch(self.render())
        assert 'rsync -a "$scratch/staged" "rsync://a3f9@' in remote

    def test_a_failed_stage_in_never_marks_staged(self):
        """The failure path exits before the marker — the project is then swept when the Spawn ends,
        not while a retry could still want it."""
        remote = self._remote_branch(self.render())
        assert remote.index("ccc: this Spawn never started") < remote.index(': > "$scratch/staged"')


class TestNoRequeue:
    def test_sbatch_is_told_a_spawn_runs_once(self, fake_slurm, monkeypatch, tmp_path):
        """Slurm's default JobRequeue is 1 and the conf template never overrides it, so without this a
        node failure re-runs the script from the top — re-staging a project already deleted."""
        import subprocess

        calls = []
        real = subprocess.run

        def spy(argv, *a, **kw):
            calls.append(argv)
            return real(argv, *a, **kw)

        monkeypatch.setattr(head_spawns.subprocess, "run", spy)
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            db.add_user(conn, "alice")
            uid = db.get_user(conn, "alice")["id"]
        tar = tmp_path / "p.tar.gz"
        _write_tar(tar)
        head_spawns.submit(uid, "train.py", 1, [], tar, 60, "", ["outputs"],
                           manifest="requirements.txt")
        sbatch = next(c for c in calls if "sbatch" in c)
        assert "--no-requeue" in sbatch


def _write_tar(path):
    import io
    import tarfile

    with tarfile.open(path, "w:gz") as tf:
        # Every Spawn declares its **Environment**, so a project tar without a manifest in it is one
        # that could not be submitted at all.
        for name in ("train.py", "requirements.txt"):
            data = b"print('hi')\n"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


# --- the sweep ----------------------------------------------------------------

class TestSweep:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        monkeypatch.setattr(head_spawns, "PROJECTS_DIR", tmp_path / "projects")
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        head_spawns.PROJECTS_DIR.mkdir()
        with db.connect() as conn:
            db.add_user(conn, "alice")

    def _project(self, uuid: str):
        d = head_spawns.PROJECTS_DIR / uuid
        d.mkdir()
        (d / "train.py").write_text("print('hi')\n")
        return d

    def _spawn(self, conn, job_id: int, uuid: str, finished_at: str | None = None):
        db.record_spawn(conn, job_id, 1, "train.py", uuid, minutes=10, gpus=1)
        if finished_at:
            db.mark_finished(conn, job_id, 0, finished_at)

    def _mark_staged(self, job_id: int):
        d = head_spawns.SPAWNS_DIR / str(job_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "staged").write_text("")

    def test_a_staged_project_is_deleted(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._spawn(conn, 1, "u1")
            d = self._project("u1")
            self._mark_staged(1)
            assert projects.sweep(conn, now) == 1
            assert not d.exists()

    def test_a_running_spawn_that_has_not_staged_keeps_its_project(self):
        """The whole point of the marker: deleting here would destroy the upload a Spawn is still
        rsyncing in."""
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._spawn(conn, 2, "u2")
            d = self._project("u2")
            assert projects.sweep(conn, now) == 0
            assert d.exists()

    def test_a_finished_spawn_that_never_staged_is_swept_anyway(self):
        """Its stage-in failed, or the marker's own rsync did. Either way it will never stage in now —
        --no-requeue is what makes that a guarantee — so nothing is waiting for this copy."""
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._spawn(conn, 3, "u3", finished_at=_iso(now))
            d = self._project("u3")
            assert projects.sweep(conn, now) == 1
            assert not d.exists()

    def test_an_unbound_project_survives_until_it_is_old(self):
        """No row names this uuid — which is ALSO what a Spawn looks like for the milliseconds between
        sbatch returning and its Attribution row being written. Deleting that one destroys a live
        Spawn's project out from under it."""
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            d = self._project("orphan")
            assert projects.sweep(conn, now) == 0
            assert d.exists()

    def test_an_old_unbound_project_is_swept(self):
        """A submission that raised between `staging.mkdir()` and `sbatch` returning. No job id will
        ever name it, so nothing else would ever delete it."""
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            d = self._project("orphan")
            later = now + timedelta(minutes=projects._ORPHAN_MINUTES + 1)
            assert projects.sweep(conn, later) == 1
            assert not d.exists()

    def test_it_is_idempotent(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._spawn(conn, 4, "u4")
            self._project("u4")
            self._mark_staged(4)
            assert projects.sweep(conn, now) == 1
            assert projects.sweep(conn, now) == 0

    def test_an_empty_projects_dir_is_not_an_error(self):
        with db.connect() as conn:
            assert projects.sweep(conn, datetime.now(timezone.utc)) == 0

    def test_a_missing_projects_dir_is_not_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "PROJECTS_DIR", tmp_path / "nope")
        with db.connect() as conn:
            assert projects.sweep(conn, datetime.now(timezone.utc)) == 0

    def test_one_spawn_staging_does_not_hold_up_another(self):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            self._spawn(conn, 5, "u5")
            self._spawn(conn, 6, "u6")
            a, b = self._project("u5"), self._project("u6")
            self._mark_staged(6)
            assert projects.sweep(conn, now) == 1
            assert a.exists() and not b.exists()


# --- the Siphon's proj half ---------------------------------------------------

class TestClosingTheProjHalf:
    """Stage-in ends the Spawn's business with the Head's copy in both senses: the bytes go, and so
    does the read grant that pointed at them."""

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        monkeypatch.setattr(head_spawns, "PROJECTS_DIR", tmp_path / "projects")
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        monkeypatch.setattr(siphons, "RSYNCD_DIR", tmp_path / "rsyncd.d")
        head_spawns.PROJECTS_DIR.mkdir()
        with db.connect() as conn:
            db.add_user(conn, "alice")

    def _bound_siphon(self, uuid: str, job_id: int) -> str:
        tok, _secret = siphons.open(uuid)
        siphons.bind(tok, job_id)
        return tok

    def test_close_proj_drops_only_the_read_module(self):
        tok = self._bound_siphon("u1", 7)
        siphons.close_proj(7)
        conf = (siphons.RSYNCD_DIR / "7.conf").read_text()
        assert f"[proj-{tok}]" not in conf
        assert f"[out-{tok}]" in conf

    def test_the_surviving_module_is_still_writable_and_still_authenticated(self):
        """It is rewritten from the same template `bind()` used, so it cannot drift into a module that
        is world-readable or rooted somewhere else."""
        tok = self._bound_siphon("u2", 8)
        siphons.close_proj(8)
        conf = (siphons.RSYNCD_DIR / "8.conf").read_text()
        assert "read only = false" in conf
        assert f"auth users = {tok}" in conf
        assert f"path = {head_spawns.SPAWNS_DIR / '8'}" in conf

    def test_close_proj_is_idempotent(self):
        self._bound_siphon("u3", 9)
        siphons.close_proj(9)
        siphons.close_proj(9)
        assert "[out-" in (siphons.RSYNCD_DIR / "9.conf").read_text()

    def test_close_proj_on_a_siphon_already_closed_outright_is_a_no_op(self):
        self._bound_siphon("u4", 10)
        siphons.close(10)
        siphons.close_proj(10)
        assert not (siphons.RSYNCD_DIR / "10.conf").exists()

    def test_the_sweep_closes_it(self):
        now = datetime.now(timezone.utc)
        tok = self._bound_siphon("u5", 11)
        (head_spawns.PROJECTS_DIR / "u5").mkdir()
        (head_spawns.SPAWNS_DIR / "11").mkdir(parents=True)
        (head_spawns.SPAWNS_DIR / "11" / "staged").write_text("")
        with db.connect() as conn:
            db.record_spawn(conn, 11, 1, "train.py", "u5", minutes=10, gpus=1)
            assert projects.sweep(conn, now) == 1
        assert f"[proj-{tok}]" not in (siphons.RSYNCD_DIR / "11.conf").read_text()


# --- wiring -------------------------------------------------------------------

class TestTheTickSweeps:
    """One wiring test, matching test_recovery's. Every condition the sweep decides on is covered
    directly above; this drives the real `tick.once` for the one thing those cannot see — that a pass
    CALLS it. Drop the line from tick.py and all of them still pass, while every project a Head ever
    receives stays on its disk until the box is rebuilt."""

    def test_a_staged_project_is_gone_after_one_pass(self, monkeypatch, tmp_path):
        from ccc_head_api import tick

        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        monkeypatch.setattr(head_spawns, "PROJECTS_DIR", tmp_path / "projects")
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        head_spawns.PROJECTS_DIR.mkdir()
        now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        with db.connect() as conn:
            monkeypatch.setattr(db, "connect", lambda: conn)  # the tick opens its own; make it ours
            monkeypatch.setattr(tick.slurm, "queue", lambda: [])
            monkeypatch.setattr(tick.slurm, "arms", lambda: [])
            db.add_user(conn, "alice")
            uid = db.get_user(conn, "alice")["id"]
            db.record_spawn(conn, 1, uid, "train.py", "uuid-1", minutes=60, gpus=1)
            proj = head_spawns.PROJECTS_DIR / "uuid-1"
            proj.mkdir()
            (proj / "train.py").write_text("print('hi')\n")
            (head_spawns.SPAWNS_DIR / "1").mkdir(parents=True)
            (head_spawns.SPAWNS_DIR / "1" / "staged").write_text("")

            tick.once(now=now)

            assert not proj.exists()


class TestWhatASubmissionLandsAs:
    """Modes, not contents. A Spawn dir holds a User's own results on a box other Users have accounts
    on, and must not inherit whatever umask the Head operator happened to provision under. The
    upload's own mode is covered in test_submit.py, on the real submit path."""

    def test_a_spawn_dir_is_not_world_readable(self):
        """The prolog sets 0770 on a Spawn dir it lends to a Guise — but only on the Head's own
        co-located Arm (`if [ -d "$head_spawns" ]` is false on a donor box). So for a REMOTE Spawn
        nothing else ever sets this, and a Spawn dir is named by a SEQUENTIAL job id while the 0711
        parent grants exactly the traversal a guessed name needs."""
        old = os.umask(0o022)
        try:
            d = head_spawns._make_spawn_dir(4321, outputs=True)
        finally:
            os.umask(old)
        assert stat.S_IMODE(d.stat().st_mode) == head_spawns.SPAWN_DIR_MODE
        assert stat.S_IMODE((d / "outputs").stat().st_mode) == head_spawns.SPAWN_DIR_MODE
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
