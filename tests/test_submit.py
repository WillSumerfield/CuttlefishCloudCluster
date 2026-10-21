"""What a Spawn must declare, and what only an Admin may.

`--time` and `--vram` are required (nothing can price a Spawn whose length nobody declared, or place one
whose needs nobody declared). `--node` is refused over the API for everyone — pinning is Head-local only.
"""

import os
import pathlib
import stat
import subprocess
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
        return head_spawns._render(
            sink_tag="deadbeef", gpu=1, script="train.py", args=[],
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

    def render(self, sink_tag="deadbeef12345678"):
        return head_spawns._render(
            sink_tag=sink_tag, gpu=1, script="train.py", args=[],
            node=None, minutes=60, constraint="", token="a3f9", secret="s3cr3t")

    def test_the_header_and_the_shell_name_the_same_file(self):
        """Two places name this path — the #SBATCH header slurmd opens, and the `sink=` the wrapper's
        trap reaps. They have to agree, or every Spawn leaves a file behind forever."""
        body = self.render()
        assert "#SBATCH --output=/tmp/ccc-%j-deadbeef.slurm" in body
        assert "#SBATCH --error=/tmp/ccc-%j-deadbeef.slurm" in body
        assert 'sink="/tmp/ccc-$jid-deadbeef.slurm"' in body

    def test_two_submissions_never_share_a_sink(self):
        a = self.render(sink_tag="aaaaaaaa0000")
        b = self.render(sink_tag="bbbbbbbb0000")
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
    REMOTE branch of JOB_SCRIPT pushes out/err home every 5s instead of only at stage_out()'s
    one-shot call at job end. The Head's own co-located branch writes straight to the final path
    already (`stage_out() { :; }`) and must stay untouched — these tests pin that boundary too."""

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24|vram80")
        kw.setdefault("node", None)
        return head_spawns._render(
            sink_tag="deadbeef", gpu=1, script="train.py", args=[],
            token="a3f9", secret="s3cr3t", **kw)

    def _remote_branch(self, script: str) -> str:
        """The `else` (remote Arm) block of JOB_SCRIPT, isolated from the Head branch above it."""
        start = script.index("else")
        end = script.index("\ncd \"$proj\"")
        return script[start:end]

    def _head_branch(self, script: str) -> str:
        start = script.index('\nif [ -d "')
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

    def test_loop_is_killed_before_the_final_stage_out(self):
        """Design: 'kill the loop before the final stage_out() call ... so the last full sync isn't
        racing a partial periodic one.'"""
        script = self.render()
        final_kill = script.rindex('kill "$loop_pid" 2>/dev/null')
        final_stage_out = script.index('stage_out_retry || echo "ccc: staging results back')
        assert final_kill < final_stage_out
        # The kill must also precede the RETRY helper's own definition point in execution order — it is
        # defined between the two, so the loop is already dead by the time any attempt runs.
        assert final_kill < script.index("stage_out_retry() {")

    def test_out_err_push_every_5_seconds_no_settle_check(self):
        remote = self._remote_branch(self.render())
        assert "sleep 5" in remote
        # the out/err line syncs unconditionally — no -newermt / find gate.
        outerr_line = next(l for l in remote.splitlines() if 'rsync -a "$out" "$err"' in l)
        assert "newermt" not in outerr_line
        assert "find" not in outerr_line

    def test_head_branch_has_no_pusher_at_all(self):
        """The Head's co-located Arm already streams live (same-FS write) — a pusher there would be
        pure overhead, and the design explicitly excludes it."""
        head = self._head_branch(self.render())
        assert "push_periodic" not in head
        assert "loop_pid" not in head
        assert head.count('stage_out() { :; }') == 1


class TestStageOutIsRetried:
    """A completed Spawn whose stage-out hits a network drop is reported as *killed*: no `exit` marker
    lands, `_record_endings` stamps NULL, and an hour later retention deletes its results as debris.

    The payload is already finished and the card already released by this point, so waiting costs
    nothing but this script's own tail."""

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24|vram80")
        kw.setdefault("node", None)
        return head_spawns._render(
            sink_tag="deadbeef", gpu=1, script="train.py", args=[],
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
        head = script[script.index('\nif [ -d "'):script.index("\nelse")]
        assert "stage_out() { :; }" in head


class TestAnUnrecordedSpawnIsNeverLeftRunning:
    """`sbatch` returns a real, running job before anything records it. Between those two points a
    failure leaves a Spawn no reaper can see (tick.once walks the DB), that is never billed, that holds
    a Device — with the User told
    none of it. If we cannot finish recording it, we do not leave it running."""

    @pytest.fixture
    def rig(self, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")
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
                               minutes=60, constraint="")
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
                               minutes=60, constraint="")

    def test_a_clean_submit_cancels_nothing(self, rig, tmp_path):
        job_id = head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[],
                                    minutes=60, constraint="")
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
        """Fail the sbatch call and nothing else."""
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
        _submit_rig(monkeypatch, tmp_path)
        self._only_sbatch_fails(monkeypatch, self._err(stderr="sbatch: error: Invalid user id 60002"))
        with pytest.raises(RuntimeError, match="Invalid user id 60002"):
            head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[],
                               minutes=60, constraint="")

    def test_the_siphon_is_still_closed_on_the_way_out(self, monkeypatch, tmp_path):
        """The translation sits inside the handler that discards the Siphon. No job id will ever
        exist for this submission, so nothing else would ever close it — a nicer error message is not
        worth leaking an rsync module for."""
        _submit_rig(monkeypatch, tmp_path)
        discarded = []
        monkeypatch.setattr(head_spawns.siphons, "discard", lambda tok: discarded.append(tok))
        self._only_sbatch_fails(monkeypatch, self._err(stderr="nope"))
        with pytest.raises(RuntimeError):
            head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[],
                               minutes=60, constraint="")
        assert len(discarded) == 1

    def test_a_failure_that_is_not_sbatch_is_left_alone(self, monkeypatch, tmp_path):
        """Only a failed *command* has stderr to surface. Everything else keeps its own type, so the
        callers that catch specific exceptions still see them."""
        _submit_rig(monkeypatch, tmp_path)
        self._only_sbatch_fails(monkeypatch, OSError("disk gone"))
        with pytest.raises(OSError, match="disk gone"):
            head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[],
                               minutes=60, constraint="")

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
            cli_spawns._submit_pinned("train.py", [], 60, 24, 1, "gpu-box")
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


def _submit_rig(monkeypatch, tmp_path) -> None:
    """A Head with just enough around it for `submit` to reach sbatch."""
    monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")
    monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                     "VALUES (1,'alice',0,'x',0)")
        conn.commit()


class TestPodContainment:
    """Spawn Isolation phase 1 (temp/spawn-isolation.md). Apptainer's own model is "the user inside is
    the user outside" — and outside is the Mantle, which owns head.db, head.key and every User's results.
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
        return head_spawns._render(
            sink_tag="deadbeef", gpu=1, script="train.py", args=[],
            token="a3f9", secret="s3cr3t", **kw)

    def tidepool(self):
        return self.render(tidepool_port=41000, tidepool_pubkey="ssh-ed25519 AAAA x")

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
        """$proj is a bind source and a --pwd target, and apptainer treats a missing one as fatal."""
        script = self.tidepool()
        remote = script[script.index("\nelse"):script.index("\nfi\n")]
        assert 'mkdir -p "$proj"' in remote

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
    head.key, and every other User's results, and nothing about it looks wrong.
    """

    @pytest.fixture
    def submitted(self, monkeypatch, tmp_path):
        """One real submit (render, Attribution row, Escrow) against a captured sbatch.
        Returns every shell-out it made, so the argv AND the account can be asserted on."""
        state = tmp_path / "head-state"
        for name in ("spawns", "scratch"):
            (state / name).mkdir(parents=True)
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
        return head_spawns.submit(user_id=user_id, script="train.py", gpu=1, args=[],
                                  minutes=60, constraint="vram24")

    def _sbatch(self, calls):
        return next(argv for argv, _ in calls if "sbatch" in argv)

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
        runs as cannot read. Feeding it here is strictly better anyway: the delete-after-return dance
        goes away with it."""
        self._submit(tmp_path)
        argv, kw = next((a, k) for a, k in submitted if "sbatch" in a)
        assert argv[-1].startswith("--")         # flags only — no path argument follows
        assert kw["input"].startswith("#!/bin/bash")

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
    # The Spawn's **Name** is required of every Spawn (TestSpawnsMustBeNamed owns that gate), defaulted
    # here so it is not the subject of every other test in the file.
    meta.setdefault("name", "a spawn")
    return c.post("/api/submit", json=meta)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


class TestSubmitRequiresDeclarations:
    def test_happy_path(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24)
        assert r.status_code == 200
        assert r.json() == {"id": 4242}

    @pytest.mark.parametrize("missing", ["minutes", "vram"])
    def test_missing_declaration_is_refused(self, monkeypatch, missing):
        meta = {"minutes": 60, "vram": 24}
        del meta[missing]
        r = post(client(monkeypatch, is_admin=False), **meta)
        assert r.status_code == 422
        assert missing in r.json()["detail"]


class TestSpawnsMustBeNamed:
    """A Spawn's **Name** (CONTEXT.md): what a person calls it in the queue and the archive, and the
    one thing about a Spawn CCC will not invent. It joins `--time`/`--vram` as
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
                            lambda *a, **kw: captured.setdefault("constraint", a[5]) or 4242)
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
    """A **Feature** carrying a runtime profile attaches its software to an **Environment** — it probes
    the interpreter for its ABI, its nvidia lib dirs and its site-packages. A Spawn builds none, so it
    has nothing to attach to.

    Decidable without building, so refused at submit (CONTEXT.md), not found on the Arm after a Device
    was already held."""

    def _tidepool(self, monkeypatch, **meta):
        c = client(monkeypatch, is_admin=False)
        monkeypatch.setattr(slurm, "arms_with_features", lambda feats: ["gpu-box"])
        monkeypatch.setattr(slurm, "feature_constraint", lambda feats: "&".join(sorted(feats)))
        return c.post("/api/submit",
                      json={"minutes": 60, "vram": 24, "tidepool": True, "name": "a pool", **meta})

    def test_a_bare_tide_pool_asking_for_a_profiled_feature_is_refused(self, monkeypatch):
        r = self._tidepool(monkeypatch, features=["vlearn"])
        assert r.status_code == 422
        assert "vlearn" in r.json()["detail"]

    def test_an_unprofiled_feature_still_needs_nothing(self, monkeypatch):
        """`isaaclab` is placement plus `$ISAACLAB_PATH` — no interpreter is involved, so a Tide Pool
        may still ask for it."""
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
                db.record_spawn(conn, job_id=jid, user_id=user_id, script="train.py")
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
        return head_spawns._render(
            sink_tag="deadbeef", gpu=1, script="train.py", args=[],
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
        body = self.render(wants_licence=True, **kw)
        payload = body[body.index("apptainer exec"):]
        assert "$envbind$licbind" in payload


class TestWhatASubmissionLandsAs:
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


class TestNoRequeue:
    def test_sbatch_is_told_a_spawn_runs_once(self, fake_slurm, monkeypatch, tmp_path):
        """Slurm's default JobRequeue is 1 and the conf template never overrides it, so without this a
        node failure re-runs the script from the top."""
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
        head_spawns.submit(uid, "train.py", 1, [], 60, "")
        sbatch = next(c for c in calls if "sbatch" in c)
        assert "--no-requeue" in sbatch
