"""What a Spawn must declare, and what only an Admin may.

`--time` and `--vram` are required (nothing can price a Spawn whose length nobody declared, or place one
whose needs nobody declared). `--node` is refused over the API for everyone — pinning is Head-local only.
"""

import asyncio
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest
import typer
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ccc_head_api import db
from ccc_head_api import main as head_main
from ccc_head_api import slurm
from ccc_head_api import spawns as head_spawns
from cuttle import spawns as cli_spawns
from cuttle import teardown
from cuttle.main import app as cli_app


class TestRenderedSbatch:
    """The #SBATCH header the Head writes for a Spawn."""

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24|vram80")
        kw.setdefault("node", None)
        kw.setdefault("outputs", ["outputs"])
        return head_spawns._render(
            staging="/tmp/u", gpu=1, script="train.py", args=[], files=[],
            token="a3f9", secret="s3cr3t", **kw)

    def test_time_limit_is_declared(self):
        """A bare integer is minutes to Slurm. Required of every Spawn: the partition is
        MaxTime=INFINITE, so a runaway with no limit would squat on a donor's GPU forever."""
        assert "#SBATCH --time=60" in self.render(minutes=60)

    def test_constraint_carries_the_vram_or_expansion(self):
        assert "#SBATCH --constraint=vram24|vram80" in self.render()

    def test_no_constraint_line_when_empty(self):
        """An empty `#SBATCH --constraint=` would match no node at all, rather than any node."""
        assert "--constraint" not in self.render(constraint="")

    def test_nodelist_only_when_pinned(self):
        assert "#SBATCH --nodelist=box" in self.render(node="box")
        assert "--nodelist" not in self.render(node=None)


class TestTheSbatchSinkOutlivesItsJobId:
    """`cuttle teardown` drops Slurm's state dir, so the next cluster issues job ids from 1 again while
    the previous cluster's /tmp sinks are still there — owned by accounts the new **Guise**s cannot
    write to. slurmd does not degrade gracefully when it cannot open a batch step's stdout: it kills the
    step before the script's first line ("Could not open stdout file ...: Permission denied" /
    "_fork_all_tasks: IO setup failed", rc 0xfb5, which slurmctld prints as the meaningless "WTERMSIG
    53"). The Head then has a Spawn with no out, no err, no exit code and nothing to say about it.

    A new cluster's Spawn can draw an id a previous cluster already used and die instantly on this if
    the sink name is keyed on job id alone — it must be unique per SUBMISSION instead.
    """

    def render(self, staging="/var/lib/ccc/head/projects/deadbeef-1234-5678"):
        return head_spawns._render(
            staging=staging, gpu=1, script="train.py", args=[], files=[], outputs=["outputs"],
            node=None, minutes=60, constraint="", token="a3f9", secret="s3cr3t")

    def test_the_header_and_the_shell_name_the_same_file(self):
        """Two places name this path — the #SBATCH header slurmd opens, and the `sink=` the wrapper's
        trap reaps. They have to agree, or every Spawn leaves a file behind forever."""
        body = self.render()
        assert "#SBATCH --output=/tmp/ccc-%j-deadbeef.slurm" in body
        assert "#SBATCH --error=/tmp/ccc-%j-deadbeef.slurm" in body
        assert 'sink="/tmp/ccc-$jid-deadbeef.slurm"' in body

    def test_two_submissions_never_share_a_sink(self):
        a = self.render(staging="/var/lib/ccc/head/projects/aaaaaaaa-0000")
        b = self.render(staging="/var/lib/ccc/head/projects/bbbbbbbb-0000")
        assert "/tmp/ccc-%j-aaaaaaaa.slurm" in a
        assert "/tmp/ccc-%j-bbbbbbbb.slurm" in b

    def test_no_bare_job_id_sink_survives_anywhere(self):
        """The whole point: a path keyed on `%j` or `$jid` alone is the bug."""
        body = self.render()
        assert "/tmp/ccc-%j.slurm" not in body
        assert "/tmp/ccc-$jid.slurm" not in body

    def test_the_trap_never_writes_into_its_own_sink(self):
        """What kept those stale files alive. On the Head branch the Guise can empty its scratch dir but
        not unlink it (the parent is ccc-owned; the root-run epilog does that), so an un-silenced
        `rm -rf` puts one "Permission denied" line into $sink — making it non-empty, which is exactly
        the condition under which both the trap and the epilog KEEP a sink. Every Spawn then left one."""
        body = self.render()
        assert 'rm -rf "$scratch" 2>/dev/null' in body
        assert '[ -s "$sink" ] || rm -f "$sink"' in body


class TestTeardownSweepsWhatItMakesAmbiguous:
    """The third half of the same bug (see TestTheSbatchSinkOutlivesItsJobId): `remove_slurm_config`
    drops Slurm's state dir and so resets the job-id counter, which is what turns every leftover
    /tmp/ccc-<jid>* from the outgoing cluster into a landmine for the incoming one. The sweep belongs
    beside the reset that causes it."""

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


class TestRemoteLiveStreaming:
    """docs/TODO.md's now-closed "Live log streaming for remote Spawns": a backgrounded loop in the
    REMOTE branch of JOB_SCRIPT pushes out/err/outputs home every 5s instead of only at stage_out()'s
    one-shot call at job end. The Head's own co-located branch writes straight to the final path
    already (`stage_out() { :; }`) and must stay untouched — these tests pin that boundary too."""

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24|vram80")
        kw.setdefault("node", None)
        # Deliberately not the literal name "outputs": the keep directory is itself called
        # `outputs`, so declaring that name would make the doubling check below match its own bind.
        kw.setdefault("outputs", ["results"])
        return head_spawns._render(
            staging="/tmp/u", gpu=1, script="train.py", args=[], files=[],
            token="a3f9", secret="s3cr3t", **kw)

    def _remote_branch(self, script: str) -> str:
        """The `else` (remote Arm) block of JOB_SCRIPT, isolated from the Head branch above it."""
        start = script.index("else")
        end = script.index("\ncd \"$proj\"")
        return script[start:end]

    def _head_branch(self, script: str) -> str:
        start = script.index('if [ -d "$staging_dir" ]')
        end = script.index("\nelse")
        return script[start:end]

    def test_loop_is_backgrounded_after_its_function_is_defined(self):
        remote = self._remote_branch(self.render())
        assert "push_periodic() {" in remote
        assert remote.index("push_periodic() {") < remote.index("push_periodic &")

    def test_loop_pid_is_captured_right_after_backgrounding(self):
        remote = self._remote_branch(self.render())
        assert remote.index("push_periodic &") < remote.index("loop_pid=$!")

    def test_trap_still_points_at_cleanup_and_cleanup_kills_the_loop(self):
        """`trap cleanup EXIT` is unchanged (both branches still use it) — the kill lives inside
        `cleanup()` itself, as a fallback for any exit path that isn't one of the two explicit kills."""
        script = self.render()
        assert script.count("trap cleanup EXIT") == 2   # once per branch, same as before
        assert 'cleanup() { kill "$loop_pid"' in script

    def test_loop_is_killed_before_both_stage_out_call_sites(self):
        """Design: 'kill the loop before the final stage_out() call ... so the last full sync isn't
        racing a partial periodic one.' Applies to the early stage-in-failure exit too, same race."""
        script = self.render()
        early_kill = script.index('kill "$loop_pid" 2>/dev/null  # stop the periodic pusher first')
        early_stage_out = script.index("\n        stage_out\n        exit 1")
        assert early_kill < early_stage_out

        final_kill = script.rindex('kill "$loop_pid" 2>/dev/null')
        final_stage_out = script.index('stage_out_retry || echo "ccc: staging results back')
        assert final_kill < final_stage_out
        # The kill must also precede the RETRY helper's own definition point in execution order — it is
        # defined between the two, so the loop is already dead by the time any attempt runs.
        assert final_kill < script.index("stage_out_retry() {")

    def test_out_err_push_every_5_seconds_no_settle_check(self):
        remote = self._remote_branch(self.render())
        assert "sleep 5" in remote
        # the out/err line syncs unconditionally — no -newermt / find gate, unlike outputs below.
        outerr_line = next(l for l in remote.splitlines() if 'rsync -a "$out" "$err"' in l)
        assert "newermt" not in outerr_line
        assert "find" not in outerr_line

    def test_outputs_push_is_gated_on_a_settle_check(self):
        remote = self._remote_branch(self.render())
        assert '-not -newermt "@$(( $(date +%s) - 10 ))"' in remote
        assert "-printf '%P\\n'" in remote   # relative paths, matching the outputs/ --files-from dest
        assert "$ccc_out/outputs/" not in remote  # sanity: dest is .../$jid/outputs/, not doubled

    def test_head_branch_has_no_pusher_at_all(self):
        """The Head's co-located Arm already streams live (same-FS write) — a pusher there would be
        pure overhead, and the design explicitly excludes it."""
        head = self._head_branch(self.render())
        assert "push_periodic" not in head
        assert "loop_pid" not in head
        assert head.count('stage_out() { :; }') == 1


class TestTheHeadStageInIsChecked:
    """The Head branch's `cp` is what stands between a User's uploaded project and its deletion.

    `: > "$spawn_dir/staged"` is the marker `ccc_head_api.projects.sweep` reads as "the Head's copy has
    served its purpose, delete the upload". There is no `set -e` in this script, so an unchecked `cp`
    writes that marker after a copy that failed — and the Head then deletes the original while the
    working copy is truncated, running the Spawn against incomplete code with nothing to restore from.
    """

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24|vram80")
        kw.setdefault("node", None)
        kw.setdefault("outputs", ["outputs"])
        return head_spawns._render(
            staging="/tmp/u", gpu=1, script="train.py", args=[], files=[],
            token="a3f9", secret="s3cr3t", **kw)

    def _head_branch(self, script: str) -> str:
        start = script.index('if [ -d "$staging_dir" ]')
        return script[start:script.index("\nelse")]

    def test_the_copy_is_checked(self):
        head = self._head_branch(self.render())
        assert "if ! cp -a --no-preserve=ownership" in head

    def test_the_staged_marker_is_only_reachable_after_a_successful_copy(self):
        """Structural, because the marker's whole meaning is its position: it must sit after the guard
        closes, so the failure path's `exit 1` is taken before anything can write it."""
        head = self._head_branch(self.render())
        guard = head.index("if ! cp -a")
        marker = head.index(': > "$spawn_dir/staged"')
        assert guard < marker
        failure_exit = head.index("exit 1", guard)
        assert failure_exit < marker, "the failure branch must exit before the marker is reached"

    def test_the_failure_branch_can_report_itself(self):
        """out/err/exit have to be NAMED before the copy, or the failure branch has nowhere to write and
        the User meets a Spawn that failed for no stated reason."""
        head = self._head_branch(self.render())
        assert head.index('err="$spawn_dir/err"') < head.index("if ! cp -a")
        fail = head[head.index("if ! cp -a"):head.index(': > "$spawn_dir/staged"')]
        assert '>> "$err"' in fail
        assert 'echo 1 > "$exitf"' in fail

    def test_the_trap_is_fully_armed_before_the_copy_can_fire_it(self):
        """`cleanup` calls `recover_scratch`; an undefined function there is a no-op that still writes
        "command not found" to the sbatch sink, and a non-empty sink is kept in /tmp forever (see the
        `sink=` rule). So the definition must precede the first `exit` that can trigger the trap."""
        head = self._head_branch(self.render())
        assert head.index("recover_scratch() {") < head.index("if ! cp -a")

    def test_a_partial_copy_is_not_offered_as_a_recovery_window(self):
        """$proj is emptied on the way out, so recover_scratch's "nothing to recover" gate fires. A
        partial copy of the User's own upload is not something to hand back to them."""
        head = self._head_branch(self.render())
        fail = head[head.index("if ! cp -a"):head.index(': > "$spawn_dir/staged"')]
        assert 'rm -rf "$proj"' in fail


class TestStageOutIsRetried:
    """A completed Spawn whose stage-out hits a network drop is reported as *killed*: no `exit` marker
    lands, `_record_endings` stamps NULL, and an hour later retention deletes its results as debris.

    The payload is already finished and the card already released by this point, so waiting costs
    nothing but this script's own tail."""

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24|vram80")
        kw.setdefault("node", None)
        kw.setdefault("outputs", ["outputs"])
        return head_spawns._render(
            staging="/tmp/u", gpu=1, script="train.py", args=[], files=[],
            token="a3f9", secret="s3cr3t", **kw)

    def test_the_final_stage_out_retries(self):
        script = self.render()
        assert "stage_out_retry() {" in script
        assert 'stage_out_retry || echo "ccc: staging results back' in script

    def test_it_backs_off_rather_than_hammering(self):
        script = self.render()
        body = script[script.index("stage_out_retry() {"):]
        body = body[:body.index("\n}")]
        assert "for backoff in 5 15 30" in body
        assert 'sleep "$backoff"' in body

    def test_the_head_branch_still_succeeds_on_the_first_attempt(self):
        """`stage_out() { :; }` on the Head means the loop returns immediately — the retry must not put
        a delay into the co-located path, which has no network hop at all."""
        script = self.render()
        head = script[script.index('if [ -d "$staging_dir" ]'):script.index("\nelse")]
        assert "stage_out() { :; }" in head


class TestAnUnrecordedSpawnIsNeverLeftRunning:
    """`sbatch` returns a real, running job before anything records it. Between those two points a
    failure leaves a Spawn no reaper can see (tick.once walks the DB), that is never billed, that holds
    a Device, and whose outputs retention deletes an hour later as orphan debris — with the User told
    none of it. If we cannot finish recording it, we do not leave it running."""

    def _tar(self, tmp_path: Path) -> Path:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            # The manifest rides along in every project tar here: a Spawn that declares no
            # **Environment** is refused before sbatch, which is not the failure these tests are about.
            for name in ("train.py", "requirements.txt"):
                data = b"print('hi')\n"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        path = tmp_path / "project.tar.gz"
        path.write_bytes(buf.getvalue())
        return path

    @pytest.fixture
    def rig(self, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")
        monkeypatch.setattr(head_spawns, "PROJECTS_DIR", tmp_path / "projects")
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                         "VALUES (1,'alice',0,'x',0)")
            conn.commit()
        monkeypatch.setattr(head_spawns.subprocess, "run",
                            lambda *a, **kw: type("R", (), {"stdout": "9001\n", "stderr": "",
                                                            "returncode": 0})())
        cancelled = []
        monkeypatch.setattr(head_spawns.slurm, "cancel", lambda jid: cancelled.append(jid))
        return cancelled

    def test_a_failure_after_sbatch_cancels_the_job(self, rig, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns.siphons, "bind",
                            lambda *a, **kw: (_ for _ in ()).throw(OSError("disk gone")))
        with pytest.raises(OSError):
            head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[],
                               tar_path=self._tar(tmp_path), minutes=60, constraint="",
                               outputs=["out"], manifest="requirements.txt")
        assert rig == [9001], "the job sbatch already started must not be left running"

    def test_a_cancel_that_itself_fails_does_not_mask_the_real_error(self, rig, monkeypatch, tmp_path):
        """The caller has to see why the submission failed. A cancel that cannot run leaves the same
        orphan we already had — it is not worth replacing the diagnosis with."""
        monkeypatch.setattr(head_spawns.siphons, "bind",
                            lambda *a, **kw: (_ for _ in ()).throw(OSError("disk gone")))
        monkeypatch.setattr(head_spawns.slurm, "cancel",
                            lambda jid: (_ for _ in ()).throw(RuntimeError("scancel missing")))
        with pytest.raises(OSError, match="disk gone"):
            head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[],
                               tar_path=self._tar(tmp_path), minutes=60, constraint="",
                               outputs=["out"], manifest="requirements.txt")

    def test_a_clean_submit_cancels_nothing(self, rig, tmp_path):
        job_id = head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[],
                                    tar_path=self._tar(tmp_path), minutes=60, constraint="",
                                    outputs=["out"], manifest="requirements.txt")
        assert job_id == 9001
        assert rig == []


class TestSbatchsRefusalReachesTheOperator:
    """Why Slurm rejected a submission lives in sbatch's stderr and nowhere else: `capture_output`
    takes it off the terminal, and `CalledProcessError.__str__` is only the argv again. Untranslated,
    a refused Spawn is reported as the command that ran — the one part of the failure nobody needed
    to be told — and the operator is left with a bare exit status.
    """

    def _err(self, stderr="", stdout="", code=1) -> subprocess.CalledProcessError:
        return subprocess.CalledProcessError(code, ["sbatch"], output=stdout, stderr=stderr)

    def _only_sbatch_fails(self, monkeypatch, exc: Exception) -> None:
        """Fail the sbatch call and nothing else. `submit` shells out before it too — `_lend_project`
        runs setfacl — and a blanket patch would break at the earlier one, testing nothing."""
        def fake(argv, *a, **kw):
            if "sbatch" in argv:
                raise exc
            return type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()
        monkeypatch.setattr(head_spawns.subprocess, "run", fake)

    def test_what_sbatch_said_is_what_comes_back(self):
        said = head_spawns._said(self._err(stderr="sbatch: error: Invalid account\n"))
        assert said == "sbatch: error: Invalid account"

    def test_a_multi_line_refusal_arrives_whole(self):
        """Flattened, not truncated. `cuttle.spawns._submit_pinned` reports the LAST line of the admin
        tool's stderr and nothing else, so a refusal split over two lines would cross that boundary as
        its second — and sbatch habitually puts the verb on one line and the reason on the next."""
        said = head_spawns._said(self._err(
            stderr="sbatch: error: Batch job submission failed:\nRequested node configuration is not "
                   "available\n"))
        assert said == ("sbatch: error: Batch job submission failed: Requested node configuration "
                        "is not available")
        assert "\n" not in said

    def test_blank_lines_do_not_become_the_answer(self):
        assert head_spawns._said(self._err(stderr="\n\n  \nreal reason\n")) == "real reason"

    def test_stdout_is_read_when_stderr_is_empty(self):
        """A command that wrote its diagnosis to the wrong stream is still telling us why."""
        assert head_spawns._said(self._err(stdout="permission denied")) == "permission denied"

    def test_a_silent_failure_still_says_something_checkable(self):
        """The one case with no message to surface. Naming the exit status beats the empty string,
        which would render as `couldn't submit to Arm 'x': ` and read like a truncated log line."""
        assert "exited 7" in head_spawns._said(self._err(code=7))

    def test_the_submission_path_raises_it(self, monkeypatch, tmp_path):
        """End to end: the refusal has to survive `submit`'s cleanup handler, which used to re-raise
        the CalledProcessError untouched."""
        rig = _submit_rig(monkeypatch, tmp_path)
        self._only_sbatch_fails(monkeypatch, self._err(stderr="sbatch: error: Invalid user id 60002"))
        with pytest.raises(RuntimeError, match="Invalid user id 60002"):
            head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[],
                               tar_path=rig, minutes=60, constraint="", outputs=["out"],
                               manifest="requirements.txt")

    def test_the_siphon_is_still_closed_on_the_way_out(self, monkeypatch, tmp_path):
        """The translation sits inside the handler that discards the Siphon. No job id will ever
        exist for this submission, so nothing else would ever close it — a nicer error message is not
        worth leaking an rsync module for."""
        rig = _submit_rig(monkeypatch, tmp_path)
        discarded = []
        monkeypatch.setattr(head_spawns.siphons, "discard", lambda tok: discarded.append(tok))
        self._only_sbatch_fails(monkeypatch, self._err(stderr="nope"))
        with pytest.raises(RuntimeError):
            head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[],
                               tar_path=rig, minutes=60, constraint="", outputs=["out"],
                               manifest="requirements.txt")
        assert len(discarded) == 1

    def test_a_failure_that_is_not_sbatch_is_left_alone(self, monkeypatch, tmp_path):
        """Only a failed *command* has stderr to surface. Everything else keeps its own type, so the
        callers that catch specific exceptions still see them."""
        rig = _submit_rig(monkeypatch, tmp_path)
        self._only_sbatch_fails(monkeypatch, OSError("disk gone"))
        with pytest.raises(OSError, match="disk gone"):
            head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[],
                               tar_path=rig, minutes=60, constraint="", outputs=["out"],
                               manifest="requirements.txt")

    def _pinned_failure(self, monkeypatch, stderr: str) -> str:
        """What `cuttle run --node` puts in front of the operator when the admin tool dies."""
        said = []
        monkeypatch.setattr(cli_spawns.auth, "_api", lambda *a, **kw: {"username": "alice"})
        monkeypatch.setattr(cli_spawns.mesh, "tailscale_ip", lambda: "100.99.0.1")
        monkeypatch.setattr(cli_spawns.ui, "abort",
                            lambda msg, **kw: said.append(msg) or typer.Exit(1))
        monkeypatch.setattr(cli_spawns, "run", lambda a, **kw: type("R", (), {
            "returncode": 1, "stdout": "", "stderr": stderr})())
        with pytest.raises(typer.Exit):
            cli_spawns._submit_pinned(b"", "train.py", [], 60, 24, 1, "gpu-box", {}, ["out"])
        return said[0]

    def test_the_cli_drops_the_exception_class_from_the_line_it_prints(self, monkeypatch):
        """`_submit_pinned` reads the last line of a Python traceback, so the message arrives
        class-first — "RuntimeError: sbatch refused...". The class name is the only part of that line
        which tells an operator nothing."""
        msg = self._pinned_failure(monkeypatch,
                                   "Traceback (most recent call last):\n  File \"x\", line 1\n"
                                   "RuntimeError: sbatch refused this Spawn: sbatch: error: "
                                   "Invalid account\n")
        assert "sbatch refused this Spawn: sbatch: error: Invalid account" in msg
        assert "RuntimeError" not in msg

    def test_a_line_that_is_not_an_exception_is_printed_as_it_stands(self, monkeypatch):
        """The strip is anchored to a leading class name so it cannot eat a real message. sudo's own
        refusals arrive here too, and they are not tracebacks."""
        msg = self._pinned_failure(monkeypatch, "sudo: a password is required\n")
        assert "sudo: a password is required" in msg


def _submit_rig(monkeypatch, tmp_path) -> Path:
    """A Head with just enough around it for `submit` to reach sbatch, returning the project tar."""
    monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")
    monkeypatch.setattr(head_spawns, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                     "VALUES (1,'alice',0,'x',0)")
        conn.commit()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in ("train.py", "requirements.txt"):
            data = b"print('hi')\n"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    path = tmp_path / "project.tar.gz"
    path.write_bytes(buf.getvalue())
    return path


class TestAnAbandonedUploadIsNotLeftOnDisk:
    """`_spool_upload` streams to `NamedTemporaryFile(delete=False)`, so nothing but this function ever
    reaps that file. It lands in the SYSTEM temp dir — outside every path CCC tracks, with no reaper of
    its own — so any exit that is not a `return` has to unlink. A client that Ctrl-Cs a multi-GB folder
    upload raises out of `request.stream()`, which is the ordinary way that happens."""

    class _Req:
        def __init__(self, chunks, boom=None):
            self._chunks, self._boom = chunks, boom

        async def stream(self):
            for c in self._chunks:
                yield c
            if self._boom is not None:
                raise self._boom

    def _spool(self, monkeypatch, tmp_path, req):
        monkeypatch.setattr(head_main.tempfile, "tempdir", str(tmp_path))
        return asyncio.run(head_main._spool_upload(req))

    def test_a_disconnect_mid_stream_leaves_nothing_behind(self, monkeypatch, tmp_path):
        req = self._Req([b"a" * 100, b"b" * 100], boom=ConnectionResetError("client went away"))
        with pytest.raises(ConnectionResetError):
            self._spool(monkeypatch, tmp_path, req)
        assert list(tmp_path.iterdir()) == []

    def test_a_cancelled_upload_leaves_nothing_behind(self, monkeypatch, tmp_path):
        """asyncio.CancelledError inherits BaseException, not Exception — a plain `except Exception`
        would let the most likely shutdown path strand the file."""
        req = self._Req([b"a" * 100], boom=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            self._spool(monkeypatch, tmp_path, req)
        assert list(tmp_path.iterdir()) == []

    def test_the_over_cap_refusal_still_cleans_up(self, monkeypatch, tmp_path):
        req = self._Req([b"a" * 10_000])
        monkeypatch.setattr(head_main, "MAX_UPLOAD_BYTES", 100)
        with pytest.raises(head_main.HTTPException) as e:
            self._spool(monkeypatch, tmp_path, req)
        assert e.value.status_code == 413
        assert list(tmp_path.iterdir()) == []

    def test_a_complete_upload_is_kept(self, monkeypatch, tmp_path):
        """The unlink must not be so eager it takes the successful path's file with it."""
        path = self._spool(monkeypatch, tmp_path, self._Req([b"hello", b"world"]))
        assert path.exists()
        assert path.read_bytes() == b"helloworld"


class TestPodContainment:
    """Spawn Isolation phase 1 (temp/spawn-isolation.md). Apptainer's own model is "the user inside is
    the user outside" — and outside is the Mantle, which owns head.db, head.key and every User's upload.
    Verified on hardware 2026-07-31: without these flags a payload reads all of it through
    `/proc/<pid>/root` of any ccc-owned host process, and reads every concurrent Spawn's scratch through
    the auto-mounted /tmp.

    Both payloads are pinned here — the script path and the Tide Pool path — because the Tide Pool is
    the one that hands a User a live shell rather than running code they wrote.
    """

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24")
        kw.setdefault("node", None)
        kw.setdefault("outputs", ["outputs"])
        return head_spawns._render(
            staging="/tmp/u", gpu=1, script="train.py", args=[], files=[],
            token="a3f9", secret="s3cr3t", **kw)

    def tidepool(self):
        return self.render(outputs=[], tidepool_port=41000, tidepool_pubkey="ssh-ed25519 AAAA x")

    @pytest.mark.parametrize("what", ["script", "tidepool"])
    def test_both_payloads_are_contained(self, what):
        script = self.render() if what == "script" else self.tidepool()
        payload = script[script.index("apptainer exec"):]
        # --contain: no $HOME, no host /tmp, no cwd auto-bind. --pid: its own procfs, which is the one
        # flag that closes /proc/<pid>/root and another Spawn's /proc/<pid>/environ.
        assert "--contain" in payload
        assert "--pid" in payload

    @pytest.mark.parametrize("what", ["script", "tidepool"])
    def test_the_project_is_bound_and_is_the_working_dir(self, what):
        """`--contain` drops the cwd auto-bind both payloads used to rely on, so $proj has to be named
        twice over: once as a bind source (or the code is not in the Pod at all) and once as --pwd (or
        cwd is $HOME and `python train.py` fails on a file that is right there)."""
        script = self.render() if what == "script" else self.tidepool()
        payload = script[script.index("apptainer exec"):]
        assert '--pwd "$proj"' in payload
        assert '$proj' in payload[payload.index("--bind"):payload.index("--env")]

    @pytest.mark.parametrize("what", ["script", "tidepool"])
    def test_the_locale_is_forced(self, what):
        """The base PyTorch image never ran locale-gen, so an inherited locale makes the payload's own
        shell write a "cannot change locale" warning into the Spawn's `err` before it has done
        anything. C.UTF-8 ships pre-generated on every Debian/Ubuntu image."""
        script = self.render() if what == "script" else self.tidepool()
        payload = script[script.index("apptainer exec"):]
        assert "LC_ALL=C.UTF-8" in payload and "LANG=C.UTF-8" in payload

    def test_the_script_path_also_refuses_the_host_passwd(self):
        """`--contain` blocks this by itself now (measured: host 55 lines -> image 21), but the script
        path never got the Tide Pool's explicit flag and the intent is worth stating in both."""
        assert "--no-mount /etc/passwd,/etc/group" in self.render()

    def test_the_remote_branch_creates_the_project_dir_itself(self):
        """rsync makes <uuid>/ only as a side effect of copying files INTO it, so a Spawn with an empty
        project — which every Tide Pool is, it uploads an empty tar — would leave it absent. $proj is
        now a bind source and a --pwd target, and apptainer treats a missing one as fatal."""
        script = self.tidepool()
        remote = script[script.index("\nelse"):script.index("\nfi\n")]
        assert 'mkdir -p "$scratch/outputs" "$proj"' in remote

    @pytest.mark.parametrize("what", ["script", "tidepool"])
    def test_a_mig_card_reaches_the_pod_as_a_uuid(self, what):
        """CUDA cannot name a MIG device by index — only a `MIG-<uuid>` reaches one. A card in MIG mode
        with Slurm's own CUDA_VISIBLE_DEVICES left on it presents the Pod with NO GPU at all: not a
        slower one, none. That is what dex's benchmark died of, and it is what every Spawn on that box
        would die of next.

        The capability nodes have to be bound in explicitly: `--contain` cuts /dev down,
        `--nv` restores the `nvidia*` nodes, and the nodes a MIG device is opened through live in a
        SUBDIRECTORY that neither puts back. Making them readable is the Arm prolog's job; this only
        carries them across the Pod wall.

        Decided on the NODE: whether an Arm carves its cards is a fact about the Arm, and
        the Head renders one script for a fleet it does not survey."""
        script = self.render() if what == "script" else self.tidepool()
        assert '[ -n "$ccc_uuid" ] && migbind=",/dev/nvidia-caps" && ' \
               'migenv=",CUDA_VISIBLE_DEVICES=$ccc_uuid"' in script
        payload = script[script.index("apptainer exec"):]
        assert "$migbind" in payload and "$migenv" in payload

    def test_a_whole_card_arm_adds_no_mig_argument(self):
        """The other half: both accumulators are empty on a card that is not carved,
        and they are appended INSIDE existing quoted strings, so an empty one adds nothing at all
        rather than an empty bind or a stray comma."""
        script = self.render()
        payload = script[script.index("apptainer exec"):]
        assert "/dev/nvidia-caps" not in payload   # only in the guarded assignment above
        assert '"$migbind"' not in payload and '"$migenv"' not in payload

    def test_the_feature_env_stays_last(self):
        """`$featenv` accumulates `,NAME=value` fragments and must stay at the end of the --env list,
        so $migenv goes BEFORE it rather than after — the invariant is the Feature profile's, and MIG
        does not get to weaken it just because it arrived later."""
        for script in (self.render(), self.tidepool()):
            payload = script[script.index("apptainer exec"):]
            assert payload.index("$migenv") < payload.index("$featenv")

    def test_the_pod_keeps_the_host_network(self):
        """--net is deliberately NOT here: the payload needs internet egress, and the Tide Pool's sshd has to
        be reachable at its port on the Arm's own address for the Head-side relay to reach it."""
        for script in (self.render(), self.tidepool()):
            assert "--net" not in script[script.index("apptainer exec"):]


class TestSpawnsRunAsTheirSubmitter:
    """Spawn Isolation phase 3 (temp/spawn-isolation.md): a Spawn is its submitter's **Guise**, never
    the Mantle. Phase 1's flags lock the door; this is what moves the valuables out of the room, and
    every failure here silently puts them back — a Spawn owned by `ccc` again reaches head.db,
    head.key, and every other User's upload, and nothing about it looks wrong.
    """

    @pytest.fixture
    def submitted(self, monkeypatch, tmp_path):
        """One real submit (render, unpack, Attribution row, Escrow) against a captured sbatch.
        Returns every shell-out it made, so the argv AND the account can be asserted on."""
        state = tmp_path / "head-state"
        for name in ("projects", "spawns", "scratch"):
            (state / name).mkdir(parents=True)
        monkeypatch.setattr(head_spawns, "PROJECTS_DIR", state / "projects")
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", state / "spawns")
        monkeypatch.setattr(head_spawns, "SCRATCH_DIR", state / "scratch")
        monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            db.add_user(conn, "alice")
        calls = []
        # A distinct id per submit: record_spawn's job_id is a PRIMARY KEY, so a repeated fake id
        # would collide rather than record the second Spawn.
        counter = iter(range(4242, 4300))

        def fake_run(argv, **kw):
            calls.append((list(argv), kw))
            return type("R", (), {"stdout": f"{next(counter)}\n", "stderr": "",
                                  "returncode": 0})()

        monkeypatch.setattr(head_spawns.subprocess, "run", fake_run)
        return calls

    def _submit(self, tmp_path, user_id=1):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name in ("train.py", "requirements.txt"):
                info = tarfile.TarInfo(name)
                info.size = 0
                tf.addfile(info, io.BytesIO(b""))
        tar = tmp_path / "up.tar.gz"
        tar.write_bytes(buf.getvalue())
        return head_spawns.submit(user_id=user_id, script="train.py", gpu=1, args=[],
                                  tar_path=tar, minutes=60, constraint="vram24", outputs=["out"],
                                  manifest="requirements.txt")

    def _sbatch(self, calls):
        return next(argv for argv, _ in calls if "sbatch" in argv)

    def test_the_upload_lands_owner_only(self, submitted, tmp_path, monkeypatch):
        """An uploaded project is the User's own tree and may itself carry a secret — a stray .env —
        so what it lands as must not depend on whichever umask the Head was provisioned under. The
        Guise reaches it through `_lend_project`'s ACL, never through the mode."""
        import os
        import stat
        old = os.umask(0o022)
        try:
            self._submit(tmp_path)
        finally:
            os.umask(old)
        staged = [d for d in head_spawns.PROJECTS_DIR.iterdir() if d.is_dir()]
        assert staged, "nothing was staged"
        assert stat.S_IMODE(staged[0].stat().st_mode) == 0o700

    def test_sbatch_runs_as_the_submitters_guise(self, submitted, tmp_path):
        self._submit(tmp_path)
        assert self._sbatch(submitted)[:5] == ["sudo", "-n", "-u", "ccc-u60002", "sbatch"]

    def test_no_spawn_is_ever_submitted_as_the_mantle(self, submitted, tmp_path):
        """The one testable line the whole phase reduces to."""
        self._submit(tmp_path)
        assert "ccc" not in self._sbatch(submitted)

    def test_two_users_submit_as_different_accounts(self, submitted, tmp_path):
        with db.connect() as conn:
            db.add_user(conn, "bob")
        self._submit(tmp_path, user_id=1)
        self._submit(tmp_path, user_id=2)
        accounts = [argv[3] for argv, _ in submitted if "sbatch" in argv]
        assert accounts == ["ccc-u60002", "ccc-u60003"]

    def test_the_script_goes_in_on_stdin_not_as_a_path(self, submitted, tmp_path):
        """It used to be a 0600 NamedTemporaryFile owned by the Mantle — which the Guise sbatch now
        runs as cannot read. Feeding it here is strictly better anyway: nothing on disk holds the
        project's full file list, and the delete-after-return dance goes away with it."""
        self._submit(tmp_path)
        argv, kw = next((a, k) for a, k in submitted if "sbatch" in a)
        assert argv[-1].startswith("--")         # flags only — no path argument follows
        assert kw["input"].startswith("#!/bin/bash")

    def test_the_project_is_lent_to_that_guise_before_sbatch(self, submitted, tmp_path):
        """The Head-local branch copies the project out of `projects/<uuid>` AS the Guise. The ACL is
        set before the job exists at all — a prolog-side chown could not be, because the prolog would
        have to be told the uuid, and the only channel for that needs a job id sbatch has not returned
        yet.

        `rwX`: the Environment build also runs in this tree as the Guise, and a dependency resolve
        writes into the project as a matter of course — `uv sync`'s `uv.lock`, micromamba's pip
        requirements file, a build backend's egg-info. Read-only turned each into "Permission denied"
        and failed the Spawn (hardware, 2026-08-02, Spawn 15)."""
        self._submit(tmp_path)
        facl = next((argv, i) for i, (argv, _) in enumerate(submitted) if "setfacl" in argv)
        assert "u:ccc-u60002:rwX" in facl[0]
        assert "-R" in facl[0]
        sbatch_at = next(i for i, (argv, _) in enumerate(submitted) if "sbatch" in argv)
        assert facl[1] < sbatch_at

    def test_a_head_without_setfacl_still_submits(self, submitted, monkeypatch, tmp_path):
        """Refusing here would take the whole cluster down over a Head-local optimisation — every
        REMOTE Spawn stages in over rsync and never touches this tree."""
        def flaky(argv, **kw):
            if "setfacl" in argv:
                return type("R", (), {"stdout": "", "stderr": "no such tool", "returncode": 1})()
            submitted.append((list(argv), kw))
            return type("R", (), {"stdout": "9001\n", "stderr": "", "returncode": 0})()
        monkeypatch.setattr(head_spawns.subprocess, "run", flaky)
        assert self._submit(tmp_path) == 9001

    def test_the_head_branch_does_not_try_to_restore_ownership(self):
        """`cp -a` implies --preserve=all: the upload is ccc:ccc, so a Guise copying it would try to
        chown back to the Mantle, get EPERM, and fail the WHOLE copy. Mode and timestamps still cross;
        ownership is the one attribute a Guise has no business restoring."""
        script = head_spawns.JOB_SCRIPT
        assert 'cp -a --no-preserve=ownership "$staging_dir/." "$proj/"' in script


class TestDeclaredOutputsBecomeBinds:
    """An **Output** is a path in the submitter's OWN project, and the keep directory is mounted onto it —
    so an unmodified script writes its results straight into what is staged home, with no `$CCC_OUT` to
    be taught about. Set up ONCE, after the if/else that sets `$ccc_out` in EITHER branch and before the
    payload runs, which is what makes it apply to the Head-local and remote paths alike."""

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24|vram80")
        kw.setdefault("node", None)
        return head_spawns._render(
            staging="/tmp/u", gpu=1, script="train.py", args=[], files=[],
            token="a3f9", secret="s3cr3t", **kw)

    def _setup_block(self, script: str) -> str:
        """The rendered %%OUTPUT_SETUP%% block alone — between `outbind=""` and the `cd "$proj"` that
        follows it. Scoped, because JOB_SCRIPT's prose upstream mentions `cp -a` and `$ccc_out` freely."""
        return script[script.index('outbind=""'):script.index('\ncd "$proj"\n')]

    def test_a_relative_output_is_bound_over_the_path_in_the_project(self):
        """The whole point: `-o runs` is the `runs/` the code already writes to."""
        script = self.render(outputs=["runs"])
        assert 'outbind="$outbind,$ccc_out/runs:$proj/runs"' in script
        assert 'mkdir -p "$ccc_out/runs"' in script
        assert 'mkdir -p "$proj/runs"' in script

    def test_a_nested_output_keeps_its_shape_on_both_sides(self):
        script = self.render(outputs=["out/ckpt"])
        assert 'outbind="$outbind,$ccc_out/out/ckpt:$proj/out/ckpt"' in script

    def test_an_absolute_output_is_bound_at_that_pod_path_and_kept_without_the_slash(self):
        """For what a profiled Arm Feature writes outside the project (features.VLWD_MOUNT), which no
        project path could name and the Recovery Window never sees."""
        script = self.render(outputs=["/ccc/vlwd/logs"])
        assert 'outbind="$outbind,$ccc_out/ccc/vlwd/logs:/ccc/vlwd/logs"' in script

    def test_an_absolute_output_is_never_mkdir_d_on_the_host(self):
        """Everything in this block runs on the host as the Guise, where `mkdir -p /ccc/vlwd/logs` is
        EPERM at `/` — and that error would land in $sink, making it non-empty, which leaves a file in
        /tmp for every Spawn ever run (the litter that poisoned job ids). Apptainer makes the mountpoint."""
        script = self.render(outputs=["/ccc/vlwd/logs"])
        assert 'mkdir -p "/ccc/vlwd/logs"' not in script
        assert 'mkdir -p "$ccc_out/ccc/vlwd/logs"' in script

    def test_a_relative_output_is_seeded_from_whatever_the_upload_had_there(self):
        """`-o checkpoints` on a project that uploaded a checkpoint to resume from must NOT present an
        empty directory and silently restart training."""
        script = self.render(outputs=["checkpoints"])
        assert 'cp -a "$proj/checkpoints/." "$ccc_out/checkpoints/"' in script

    def test_an_absolute_output_is_not_seeded(self):
        """Nothing was ever uploaded to a path inside the Pod."""
        assert "cp -a" not in self._setup_block(self.render(outputs=["/ccc/vlwd/logs"]))

    def test_one_bind_per_declared_output(self):
        script = self.render(outputs=["checkpoints", "logs"])
        assert script.count('outbind="$outbind,') == 2

    def test_nothing_names_ccc_out_to_the_payload(self):
        """There is no CCC_OUT in the Pod environment, and $ccc_out is not bound — the only way into the
        keep directory is through the declared paths."""
        script = self.render(outputs=["runs"])
        payload = script.split("\ncd \"$proj\"\n", 1)[1]
        assert "CCC_OUT=" not in payload
        assert '--bind "$proj,$ccc_out' not in payload

    def test_setup_lands_after_both_branches_close_and_before_the_payload_runs(self):
        script = self.render(outputs=["checkpoints"])
        mkdir_at = script.index('mkdir -p "$ccc_out/checkpoints"')
        # The LAST "\nfi\n" — the branch has its own nested if/fi (the stage-in failure check) that
        # closes earlier; what matters is being after the OUTER one.
        fi_at = script.rindex("\nfi\n", 0, mkdir_at)
        # Anchored with the leading newline: `cd "$proj"` also appears inside a comment a few lines
        # earlier ("walks on into `cd \"$proj\"`"), which a bare substring search would match instead.
        cd_at = script.index('\ncd "$proj"\n')
        assert fi_at < mkdir_at < cd_at

    def test_not_duplicated_into_either_branch(self):
        """The mkdir line is written ONCE in the template, not once per branch — so it must not appear
        inside the Head-local `if` block or the remote `else` block, only between them and the payload."""
        script = self.render(outputs=["checkpoints"])
        head_branch = script[script.index('if [ -d "$staging_dir" ]'):script.index("\nelse")]
        remote_branch = script[script.index("\nelse"):script.index("\nfi\n")]
        assert "mkdir -p \"$ccc_out/checkpoints\"" not in head_branch
        assert "mkdir -p \"$ccc_out/checkpoints\"" not in remote_branch


class FakeUser(dict):
    pass


def client(monkeypatch, is_admin: bool) -> TestClient:
    """A Head API with a logged-in caller and no Slurm, no disk — but a real (empty) database.

    Every submit quotes its Escrow through karma.quote_hold, which reads the **Tide** from head.db, so
    the store is no longer optional even for an Admin (whose hold is None — but the route still opens the
    connection, exactly as it does in production, where the Head always has head.db). Empty is the
    interesting state: with no Demand history every bucket forecasts 1.0, so the quote must come out
    exactly as it did before the Tide existed.
    """
    user = {"id": 1, "username": "alice", "is_admin": is_admin, "must_change_password": False}
    head_main.app.dependency_overrides[head_main.current_user] = lambda: user
    # A fresh empty db every time. Not optional: the route reads the Tide from head.db even for an Admin
    # (whose hold is None). Tests that need rows (the cancel tests) seed AFTER this, into this same path.
    # Always a temp file, never db.DB_PATH's default — on a live Head that default is a real ccc-owned
    # store this process cannot read.
    monkeypatch.setattr(db, "DB_PATH", Path(tempfile.mkdtemp()) / "head.db")
    db.ensure_schema()
    # Same reason as DB_PATH: on a live Head the default is a real ccc-owned tree this process cannot
    # write. A cancel now leaves a marker there (spawns.mark_cancelled), so "no disk" has to mean a
    # disk of our own, not the operator's.
    monkeypatch.setattr(head_spawns, "SPAWNS_DIR", Path(tempfile.mkdtemp()) / "spawns")
    monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box"}])
    monkeypatch.setattr(slurm, "vram_constraint", lambda gb, gpus=1: "vram24")
    monkeypatch.setattr(slurm, "queue", lambda: [])  # no live Spawns — submit_checked's cap check
    monkeypatch.setattr(head_spawns, "submit", lambda *a, **kw: 4242)
    return TestClient(head_main.app)


def post(c: TestClient, **meta):
    meta.setdefault("script", "train.py")
    meta.setdefault("outputs", ["outputs"])
    # Every Spawn declares its **Environment** — see TestManifestIsRequired below for the gate itself.
    # Defaulted here so the tests about everything ELSE aren't all about this one field.
    meta.setdefault("manifest", "requirements.txt")
    # Same reasoning for the Spawn's **Name**: required of every Spawn (TestSpawnsMustBeNamed owns that
    # gate), defaulted here so it is not the subject of every other test in the file.
    meta.setdefault("name", "a spawn")
    return c.post("/api/submit", content=b"", headers={"X-CCC-Meta": json.dumps(meta)})


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


class TestSubmitRequiresDeclarations:
    def test_happy_path(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24)
        assert r.status_code == 200
        # `environment` is the submit-time echo (docs/elements/environments.md). None here because
        # this route is stubbed at submit_checked, so no Spawn dir — and a client must treat it as
        # optional anyway, since a Spawn submitted before the feature existed has no note to give back.
        assert r.json() == {"id": 4242, "environment": None}

    @pytest.mark.parametrize("missing", ["minutes", "vram"])
    def test_missing_declaration_is_refused(self, monkeypatch, missing):
        meta = {"minutes": 60, "vram": 24}
        del meta[missing]
        r = post(client(monkeypatch, is_admin=False), **meta)
        assert r.status_code == 422
        assert missing in r.json()["detail"]


class TestSpawnsMustBeNamed:
    """A Spawn's **Name** (CONTEXT.md): what a person calls it in the queue and the archive, and the
    one thing about a Spawn CCC will not invent. It joins `--time`/`--vram`/`--output`/`--manifest` as
    something every Spawn says for itself — a cluster that named work on its submitter's behalf would
    fill its own history with rows nobody can tell apart.

    A **label**, not an identifier: free text, no charset to spell it in, and two Spawns may share one.
    What it may not contain is what would corrupt the surfaces it is drawn on."""

    def test_a_nameless_spawn_is_refused(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24, name=None)
        assert r.status_code == 422
        assert "name" in r.json()["detail"]

    def test_a_blank_name_is_refused_too(self, monkeypatch):
        """Whitespace is not a name. Sending "   " is a client working around the gate rather than a
        person answering it."""
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24, name="   ")
        assert r.status_code == 422

    def test_the_refusal_says_ccc_will_not_invent_one(self, monkeypatch):
        """The reason, not just the rule — a submitter told only "name required" reasonably wonders
        why the script's own name would not do."""
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24, name=None)
        assert "does not invent" in r.json()["detail"]

    def test_ordinary_prose_is_accepted(self, monkeypatch):
        """Spaces, punctuation and case — a Name is read, not resolved, so it is spelled how a person
        would write it rather than how a path component has to be."""
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24, name="lr sweep, 3e-4 (v2)")
        assert r.status_code == 200

    def test_it_is_capped(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24,
                 name="x" * (head_spawns.MAX_SPAWN_NAME + 1))
        assert r.status_code == 422
        assert str(head_spawns.MAX_SPAWN_NAME) in r.json()["detail"]

    def test_exactly_the_cap_is_fine(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24,
                 name="x" * head_spawns.MAX_SPAWN_NAME)
        assert r.status_code == 200

    @pytest.mark.parametrize("bad", ["two\nlines", "esc\x1b[31m", "null\x00byte", "tab\there"])
    def test_control_characters_are_refused(self, monkeypatch, bad):
        """A Name is printed into an aligned CLI table, where a newline breaks the columns and an ANSI
        escape repaints the terminal around it."""
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24, name=bad)
        assert r.status_code == 422

    def test_it_is_trimmed_rather_than_refused(self, monkeypatch):
        """Surrounding whitespace is a typo, not a violation — the stored Name is what was meant."""
        assert head_spawns.clean_name("  nightly eval  ") == "nightly eval"


class TestTheEnvironmentMustBeDeclared:
    """`--manifest` joins `--time`/`--vram`/`--output` as something every Spawn says for itself. There
    is no default and no auto-detection: an **Environment** carries its own interpreter and inherits
    nothing, so which file it is built from decides what all the work runs against — too much to infer
    from a `pyproject.toml` that might belong to something the User is only vendoring."""

    def test_a_spawn_naming_no_manifest_is_refused(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24, manifest=None)
        assert r.status_code == 422
        assert "manifest" in r.json()["detail"]

    def test_the_refusal_names_the_flag_that_fixes_it(self, monkeypatch):
        """A message that only says "required" leaves the User with nowhere to go. Refused BEFORE the
        upload is spooled, too — there is no reason to carry a gigabyte of project across the wire to
        find out a field is blank."""
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24, manifest="")
        assert "--manifest" in r.json()["detail"]
        assert "pyproject.toml" in r.json()["detail"]

    def test_a_tide_pool_may_decline(self, monkeypatch):
        """The one Spawn exempt: it is a shell, not a run, and a bare one uploads an empty tar with no
        file in it to name."""
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24, tidepool=True,
                 script=None, outputs=[], manifest=None)
        assert r.status_code == 200


class TestNodeIsNotAvailableOverTheApi:
    """Pinning a Spawn to a named Arm was admin-only over this route until Phase 6 settled that the
    Head API exposes **no admin writes at all**. It is now `cuttle run --node`, on the Head.

    A User declares what they NEED and lets Slurm choose. Naming a machine sidesteps Eligibility — and
    with it, the Demand the Spawn should have added to every card that could have run it.
    """

    def test_non_admin_may_not_pin(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24, node="gpu-box")
        assert r.status_code == 403

    def test_an_admin_may_not_pin_either(self, monkeypatch):
        """The point of the phase. An Admin has no branch here, so a stolen admin Session buys
        nothing a stolen User Session does not."""
        r = post(client(monkeypatch, is_admin=True), minutes=60, vram=24, node="gpu-box")
        assert r.status_code == 403
        assert "on the Head" in r.json()["detail"]  # and it says where the capability went

    def test_a_pin_is_refused_not_ignored(self, monkeypatch):
        """Silently dropping `node` would submit a Spawn that ran somewhere the caller did not ask
        for, and report success — worse than refusing."""
        r = post(client(monkeypatch, is_admin=True), minutes=60, vram=24, node="typo")
        assert r.status_code == 403

    def test_submitting_without_a_pin_still_works(self, monkeypatch):
        """The [U] needs-based path is untouched — only the admin branch was removed."""
        assert post(client(monkeypatch, is_admin=False), minutes=60, vram=24).status_code == 200


class TestFeatureRequirement:
    """Arm Features (docs/TODO.md): a Spawn may optionally require named capabilities (VSim,
    IsaacSim, ...) an Arm declared having at enroll. Most Spawns need none, and declaring some must
    behave exactly like `--vram` — hard-refused before sbatch if unsatisfiable, never queued to fail
    silently later."""

    def _capture_constraint(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(head_spawns, "submit",
                            lambda *a, **kw: captured.setdefault("constraint", a[6]) or 4242)
        return captured

    def test_absent_features_change_nothing(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)  # sets its own `submit` stub first — ours must win
        captured = self._capture_constraint(monkeypatch)
        r = post(c, minutes=60, vram=24)
        assert r.status_code == 200
        assert captured["constraint"] == "vram24"

    def test_satisfiable_request_ands_onto_the_vram_constraint(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)
        monkeypatch.setattr(slurm, "arms_with_features", lambda feats: ["gpu-box"])
        monkeypatch.setattr(slurm, "feature_constraint", lambda feats: "&".join(sorted(feats)))
        captured = self._capture_constraint(monkeypatch)
        r = post(c, minutes=60, vram=24, features=["vsim"])
        assert r.status_code == 200
        assert captured["constraint"] == "(vram24)&vsim"

    def test_unsatisfiable_request_is_refused_before_sbatch(self, monkeypatch):
        """No Arm carries every requested Feature — refused NOW, naming what the cluster actually has,
        exactly like `vram_constraint`'s "no Arm has that much VRAM"."""
        c = client(monkeypatch, is_admin=False)
        monkeypatch.setattr(slurm, "arms_with_features", lambda feats: [])
        monkeypatch.setattr(slurm, "all_feature_names", lambda: ["isaacsim"])
        r = post(c, minutes=60, vram=24, features=["vsim"])
        assert r.status_code == 422
        assert "vsim" in r.json()["detail"]
        assert "isaacsim" in r.json()["detail"]


class TestAProfiledFeatureNeedsAnEnvironment:
    """A **Feature** carrying a runtime profile attaches its software to the Spawn's **Environment** —
    it probes the interpreter for its ABI, its nvidia lib dirs and its site-packages. A Spawn that
    declared none has nothing to attach to.

    Decidable without building, so refused at submit (CONTEXT.md). It used to be found on the Arm,
    after a Device was already held, and reported as `FATAL: "-c": executable file not found in $PATH`
    — apptainer's complaint about the flag that followed the empty `$py`.

    Only a **Tide Pool** can reach this gate: every other Spawn is already refused for naming no
    manifest at all."""

    def _tidepool(self, monkeypatch, **meta):
        c = client(monkeypatch, is_admin=False)
        monkeypatch.setattr(slurm, "arms_with_features", lambda feats: ["gpu-box"])
        monkeypatch.setattr(slurm, "feature_constraint", lambda feats: "&".join(sorted(feats)))
        return c.post("/api/submit", content=b"", headers={"X-CCC-Meta": json.dumps(
            {"minutes": 60, "vram": 24, "tidepool": True, "name": "a pool", **meta})})

    def test_a_bare_tide_pool_asking_for_a_profiled_feature_is_refused(self, monkeypatch):
        r = self._tidepool(monkeypatch, features=["vlearn"])
        assert r.status_code == 422
        assert "vlearn" in r.json()["detail"]

    def test_the_refusal_names_the_flag_that_fixes_it(self, monkeypatch):
        """The remedy is to declare an Environment, not to drop the Feature — the User asked for that
        Feature on purpose."""
        r = self._tidepool(monkeypatch, features=["vlearn"])
        assert "--manifest" in r.json()["detail"]

    def test_declaring_one_makes_it_fine(self, monkeypatch):
        assert self._tidepool(monkeypatch, features=["vlearn"],
                              manifest="requirements.txt").status_code == 200

    def test_an_unprofiled_feature_still_needs_nothing(self, monkeypatch):
        """`isaaclab` is placement plus `$ISAACLAB_PATH` — no interpreter is involved, so a Tide Pool
        opened to poke at a box may still ask for it with no project in hand."""
        assert self._tidepool(monkeypatch, features=["isaaclab"]).status_code == 200

    def test_a_bare_tide_pool_with_no_features_is_untouched(self, monkeypatch):
        assert self._tidepool(monkeypatch).status_code == 200


class TestCliRequiresTimeAndVram:
    """No defaults, and no autofill. A `--vram` that defaulted to the fleet's biggest card would make
    every untouched field eligible for that one GPU alone — queued behind everything, at the highest
    Demand, for work that may need 2 GB."""

    def run(self, *args):
        return CliRunner().invoke(cli_app, ["run", *args])

    def test_bare_run_is_refused(self):
        assert self.run("train.py").exit_code != 0

    @pytest.mark.parametrize("args", [
        # --name is given in both, so what is missing is the one declaration each case is about.
        ["train.py", "--name", "n", "--time", "60"],   # no --vram
        ["train.py", "--name", "n", "--vram", "24"],   # no --time
    ])
    def test_half_a_declaration_is_refused(self, args):
        """The refusal names the flag that is missing. Since Phase 6 these are promptable, so the
        message is ours rather than click's — but with no terminal to prompt on, half a declaration
        is still refused rather than defaulted."""
        r = self.run(*args)
        assert r.exit_code != 0
        missing = "--vram" if "--time" in args else "--time"
        assert missing in r.output


class TestConcurrentSpawnCap:
    """Escrow prices queue-flooding (Phase 8 loose end — docs/TODO.md's "No rate-limiting anywhere")
    but never hard-stops it; this does. 'Concurrent' is Slurm PENDING or RUNNING — queuing instead of
    running doesn't dodge the cap. Admins are exempt, matching db.spawn_owners' own exemption."""

    def _seed_owned_spawns(self, user_id: int, job_ids: list[int]):
        """Insert a user + one Spawn row per job_id, all attributed to `user_id`. Called AFTER
        `client()`, so it writes to the same DB_PATH the handler will read (see TestCancel._seed)."""
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                "VALUES (?,?,?,?,0)", (user_id, f"u{user_id}", 0, "x"))
            for jid in job_ids:
                db.record_spawn(conn, job_id=jid, user_id=user_id, script="train.py",
                                staging_uuid=f"u{jid}")
            conn.commit()

    def test_at_the_cap_is_refused(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)  # caller is id 1
        job_ids = list(range(1, head_spawns.MAX_CONCURRENT_SPAWNS + 1))
        self._seed_owned_spawns(1, job_ids)
        monkeypatch.setattr(slurm, "queue", lambda: [{"id": j} for j in job_ids])
        r = post(c, minutes=60, vram=24)
        assert r.status_code == 422
        assert "queued/running" in r.json()["detail"]

    def test_one_under_the_cap_still_works(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)
        job_ids = list(range(1, head_spawns.MAX_CONCURRENT_SPAWNS))  # one short of the cap
        self._seed_owned_spawns(1, job_ids)
        monkeypatch.setattr(slurm, "queue", lambda: [{"id": j} for j in job_ids])
        assert post(c, minutes=60, vram=24).status_code == 200

    def test_someone_elses_active_spawns_dont_count(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)  # caller is id 1
        job_ids = list(range(1, head_spawns.MAX_CONCURRENT_SPAWNS + 5))
        self._seed_owned_spawns(2, job_ids)  # all owned by a different user
        monkeypatch.setattr(slurm, "queue", lambda: [{"id": j} for j in job_ids])
        assert post(c, minutes=60, vram=24).status_code == 200

    def test_an_admin_is_exempt(self, monkeypatch):
        c = client(monkeypatch, is_admin=True)  # caller is id 1
        job_ids = list(range(1, head_spawns.MAX_CONCURRENT_SPAWNS + 5))
        self._seed_owned_spawns(1, job_ids)
        monkeypatch.setattr(slurm, "queue", lambda: [{"id": j} for j in job_ids])
        assert post(c, minutes=60, vram=24).status_code == 200


class TestOutputsRequiredOverTheApi:
    """docs/TODO.md's now-closed "Spawn Output Enforcement": a Spawn that names no output folder is one
    whose results have nowhere retrievable to land, so the Head 422s it — same tier of hard requirement
    as `minutes`/`vram`, checked at the same route-level gate (`submit` in `spawns.py` is mocked here,
    same as TestSubmitRequiresDeclarations above)."""

    def test_an_empty_list_is_refused(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24, outputs=[])
        assert r.status_code == 422
        assert "outputs" in r.json()["detail"]

    def test_a_missing_field_is_refused(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)
        # Named, so the refusal is about the missing 'outputs' and not about the missing Name — the
        # name gate sits ahead of this one.
        meta = {"script": "train.py", "minutes": 60, "vram": 24, "name": "a spawn"}  # no 'outputs' key
        r = c.post("/api/submit", content=b"", headers={"X-CCC-Meta": json.dumps(meta)})
        assert r.status_code == 422
        assert "outputs" in r.json()["detail"]

    def test_declaring_one_still_works(self, monkeypatch):
        assert post(client(monkeypatch, is_admin=False), minutes=60, vram=24,
                    outputs=["checkpoints"]).status_code == 200


class TestSubmitValidatesOutputsAndScript:
    """The real `spawns.submit()` (not the mocked route above) — since non-empty outputs, a safe output
    name, and the script actually being present in the tar are all checked here, before anything is
    written to disk or handed to sbatch. This is what the Head-local pinned path (`cuttle run --node`,
    which never travels over HTTP) gets for free by sharing this function."""

    def _tar(self, tmp_path: Path, *names: str) -> Path:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for n in names:
                data = b"print('hi')\n"
                info = tarfile.TarInfo(n)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        path = tmp_path / "project.tar.gz"
        path.write_bytes(buf.getvalue())
        return path

    def _submit(self, monkeypatch, tmp_path, **kw):
        monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")
        monkeypatch.setattr(head_spawns, "PROJECTS_DIR", tmp_path / "projects")
        kw.setdefault("script", "train.py")
        kw.setdefault("outputs", ["out"])
        kw.setdefault("manifest", "requirements.txt")
        kw.setdefault("tar_path", self._tar(tmp_path, "train.py", "requirements.txt"))
        return head_spawns.submit(user_id=1, gpu=1, args=[], minutes=60,
                                  constraint="", **kw)

    def test_empty_outputs_is_refused(self, monkeypatch, tmp_path):
        with pytest.raises(ValueError, match="output"):
            self._submit(monkeypatch, tmp_path, outputs=[])

    def test_a_traversal_attempt_is_refused(self, monkeypatch, tmp_path):
        with pytest.raises(ValueError, match="invalid output path"):
            self._submit(monkeypatch, tmp_path, outputs=["../../etc"])

    def test_a_dot_segment_anywhere_in_the_path_is_refused(self, monkeypatch, tmp_path):
        """Every segment is matched, not just the first, so `.` and `..` are unreachable at any depth
        and no separate traversal check is needed."""
        for bad in (".", "..", "a/../b", "a/./b", "runs/.hidden"):
            with pytest.raises(ValueError, match="invalid output path"):
                self._submit(monkeypatch, tmp_path, outputs=[bad])

    def test_two_outputs_kept_in_the_same_place_are_refused(self, monkeypatch, tmp_path):
        """Different directories in the Pod, one directory on the Head — one would overwrite the other
        in the tar. Decidable here, so it is decided here."""
        with pytest.raises(ValueError, match="would both be kept"):
            self._submit(monkeypatch, tmp_path, outputs=["out/x", "/out/x"])

    def test_an_output_nested_inside_another_is_refused(self, monkeypatch, tmp_path):
        """Binding $ccc_out/runs/nn onto a path inside the $ccc_out/runs bind is a mount over its own
        source."""
        with pytest.raises(ValueError, match="is inside output"):
            self._submit(monkeypatch, tmp_path, outputs=["runs", "runs/nn"])

    def test_an_output_reached_through_a_symlink_in_the_project_is_refused(self, monkeypatch, tmp_path):
        """CCC mounts the keep directory at that path; a link would resolve the mountpoint somewhere
        else, and the Spawn would keep whatever lives there instead of what it wrote."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for n in ("train.py", "requirements.txt"):
                data = b"x\n"
                info = tarfile.TarInfo(n); info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            # In-TREE on purpose: extractall(filter="data") already drops an absolute or escaping
            # symlink, so the one that can actually reach this check is a link to a sibling directory —
            # which would still make the Spawn keep the wrong folder.
            link = tarfile.TarInfo("runs")
            link.type, link.linkname = tarfile.SYMTYPE, "data"
            tf.addfile(link)
        tar = tmp_path / "linked.tar.gz"; tar.write_bytes(buf.getvalue())
        with pytest.raises(ValueError, match="passes through a symlink"):
            self._submit(monkeypatch, tmp_path, outputs=["runs"], tar_path=tar)

    def test_a_normal_name_is_accepted(self, monkeypatch, tmp_path):
        """Sanity check the regex isn't so strict it refuses ordinary names — carried all the way
        through to a (faked) sbatch, which needs a real user row for record_spawn's FK. A distinct job
        id per name: record_spawn's job_id is a PRIMARY KEY, and the same fake id twice would collide."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                        "VALUES (1,'alice',0,'x',0)")
            conn.commit()
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        counter = iter(range(4200, 4300))
        monkeypatch.setattr(head_spawns.subprocess, "run",
                            lambda *a, **kw: type("R", (), {"stdout": f"{next(counter)}\n",
                                                            "stderr": "", "returncode": 0})())
        for good in ("checkpoints", "run-1", "epoch_09", "v2.final", "out/ckpt", "/ccc/vlwd/logs"):
            job_id = self._submit(monkeypatch, tmp_path, outputs=[good])
            assert job_id >= 4200

    def test_a_script_missing_from_the_tar_is_refused(self, monkeypatch, tmp_path):
        with pytest.raises(ValueError, match="was not found in the uploaded project"):
            self._submit(monkeypatch, tmp_path, script="missing.py",
                         tar_path=self._tar(tmp_path, "other.py"))

    def test_nothing_is_staged_when_the_script_is_missing(self, monkeypatch, tmp_path):
        """The refusal is checked against the tar's own member list, BEFORE `staging.mkdir()` — a bad
        submission leaves no half-written project dir behind."""
        monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")
        monkeypatch.setattr(head_spawns, "PROJECTS_DIR", tmp_path / "projects")
        with pytest.raises(ValueError):
            head_spawns.submit(user_id=1, script="missing.py", gpu=1, args=[],
                              tar_path=self._tar(tmp_path, "other.py"), minutes=60, constraint="",
                              outputs=["out"])
        projects = tmp_path / "projects"
        assert not projects.exists() or not any(projects.iterdir())


class TestOutputsPlumbThroughToTheRenderedScript:
    """End-to-end: what `cuttle run --output` declares is what actually reaches the script sbatch is
    handed — not just what a mocked `submit` was called with, capturing the script BODY. The body
    arrives on sbatch's STDIN now — there is no temp file to read off disk any more, because a 0600 file
    owned by the Mantle is not readable by the **Guise** sbatch runs as."""

    @pytest.fixture
    def rendered(self, monkeypatch, tmp_path):
        state = tmp_path / "head-state"
        for name in ("projects", "spawns", "scratch"):
            (state / name).mkdir(parents=True)
        monkeypatch.setattr(head_spawns, "PROJECTS_DIR", state / "projects")
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", state / "spawns")
        monkeypatch.setattr(head_spawns, "SCRATCH_DIR", state / "scratch")
        monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")
        monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box"}])
        monkeypatch.setattr(slurm, "vram_constraint", lambda gb, gpus=1: "vram24")
        monkeypatch.setattr(slurm, "queue", lambda: [])
        # submit_checked reads the DB even for a non-admin's cap check, retracted-node check, and Tide
        # quote — and submit() writes the Attribution row at the end (FK'd to a real user).
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                        "VALUES (1,'alice',0,'x',0)")
            conn.commit()
        captured: dict[str, str] = {}

        def fake_run(argv, **kw):
            # Two shell-outs reach here: the project lend (`setfacl ...`) and the submission itself.
            # Only the latter carries the script, and it carries it on stdin.
            if "sbatch" in argv:
                captured["argv"] = list(argv)
                captured["body"] = kw["input"]
            return type("R", (), {"stdout": "4242\n", "stderr": "", "returncode": 0})()

        monkeypatch.setattr(head_spawns.subprocess, "run", fake_run)
        return captured

    def test_declared_outputs_reach_the_actual_sbatch_script(self, rendered, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name in ("train.py", "requirements.txt"):
                data = b"print('hi')\n"
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        tar_path = tmp_path / "upload.tar.gz"
        tar_path.write_bytes(buf.getvalue())

        head_spawns.submit_checked(1, False, "train.py", 1, [], tar_path, 60, 24,
                                   ["checkpoints", "logs"], manifest="requirements.txt",
                                   name="a spawn")

        assert 'mkdir -p "$ccc_out/checkpoints"' in rendered["body"]
        assert 'mkdir -p "$ccc_out/logs"' in rendered["body"]


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
            db.record_spawn(conn, job_id=77, user_id=owner_id, script="train.py", staging_uuid="u")
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


class TestLicenceBind:
    """The Arm's **Licence**, bound at the Spawn's Guise home inside the Pod (temp/FEATURES_LICENCE.md).

    Probed on hardware 2026-08-03: TurboActivate (vlearn) resolves its activation state through
    getpwuid(getuid())->pw_dir and ignores $HOME entirely — a bogus HOME still validated, a redirected
    one did not. So the mount point is not a convention we picked, it is the only path that works, and
    no submitter-side code can substitute for it.
    """

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24")
        kw.setdefault("node", None)
        kw.setdefault("outputs", ["outputs"])
        return head_spawns._render(
            staging="/tmp/u", gpu=1, script="train.py", args=[], files=[],
            token="a3f9", secret="s3cr3t", **kw)

    def test_a_spawn_with_no_feature_can_never_reach_the_licence(self):
        """The saved licence key is plaintext and world-readable, so the bind is gated on having asked
        for a Feature at all — not merely on the Arm happening to have one."""
        body = self.render(wants_licence=False)
        assert 'licbind=""' in body                 # declared, so the bind expression stays valid
        assert '[ -n "" ]' in body                  # the gate, rendered false: never assigned
        assert "$CCC_LICENCE_HOME:$guise_home" in body   # only ever inside that dead branch

    def test_a_feature_spawn_binds_it_at_the_guise_home(self):
        body = self.render(wants_licence=True)
        assert '[ -n "1" ]' in body
        assert 'licbind=",$CCC_LICENCE_HOME:$guise_home"' in body

    def test_the_home_comes_from_passwd_not_from_the_environment(self):
        """`$HOME` is what the probe proved is NOT consulted — the Pod's own getpwuid is, and apptainer
        injects this account's passwd entry. Reading passwd here is what makes the two agree."""
        body = self.render(wants_licence=True)
        assert 'guise_home=$(getent passwd "$(id -u)" | cut -d: -f6)' in body
        assert "$HOME" not in body.split("licbind=")[0].split("guise_home=")[-1]

    def test_an_arm_with_no_licence_declared_is_not_an_error(self):
        """Most Arms offer a Feature that needs no licence, or no Feature at all. The Arm supplies
        CCC_LICENCE_HOME from /etc/ccc/features.env; absent, the guard simply never fires."""
        body = self.render(wants_licence=True)
        assert '[ -d "${CCC_LICENCE_HOME:-}" ]' in body

    @pytest.mark.parametrize("tp", [None, 41000])
    def test_both_payloads_carry_the_bind(self, tp):
        """A Tide Pool is a Spawn that happens to run a shell — a User debugging why their licensed
        software will not start is exactly who needs the licence present."""
        kw = {"tidepool_port": tp, "tidepool_pubkey": "ssh-ed25519 AAAA x"} if tp else {}
        body = self.render(wants_licence=True, outputs=[] if tp else ["outputs"], **kw)
        payload = body[body.index("apptainer exec"):]
        assert "$envbind$licbind" in payload
