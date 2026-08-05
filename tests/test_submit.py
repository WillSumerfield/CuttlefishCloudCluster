"""What a Spawn must declare, and what only an Admin may.

`--time` and `--vram` are required (nothing can price a Spawn whose length nobody declared, or place one
whose needs nobody declared). `--node` is refused over the API for everyone — pinning is Head-local only.
"""

import io
import json
import tarfile
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ccc_head_api import db
from ccc_head_api import main as head_main
from ccc_head_api import slurm
from ccc_head_api import spawns as head_spawns
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
        kw.setdefault("outputs", ["outputs"])
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
        final_stage_out = script.index('stage_out || echo "ccc: staging results back')
        assert final_kill < final_stage_out

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
        # flag that closes /proc/<pid>/root and another Spawn's /proc/<pid>/environ (their WANDB key).
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
    def test_wsls_gpu_node_is_bound_back_in(self, what):
        """`--contain` cost a WSL Arm its GPU twice over, and both halves are needed. The device: /dev
        drops from ~241 entries to 12 and `--nv` puts back only the nvidia* nodes, which WSL does not
        have — its GPU is the single node /dev/dxg. The driver stack: `--contain` also ignores
        apptainer.conf's `bind path` entries ("except for /etc/hosts and /etc/localtime", per the
        config's own note), which is how configure_apptainer_wsl had been supplying /usr/lib/wsl.

        Measured on hardware 2026-08-01, and the failure is quiet: --nv still injects libdxcore.so, so
        torch imports and reports its CUDA build before `torch.cuda.is_available()` comes back False.

        Decided on the NODE, not by the Head: an Arm's platform is a fact about the Arm, and the Head
        renders one script for a fleet it does not survey."""
        script = self.render() if what == "script" else self.tidepool()
        assert 'dxg=""; [ -e /dev/dxg ] && dxg="--bind /dev/dxg,/usr/lib/wsl"' in script
        assert "$dxg" in script[script.index("apptainer exec"):]

    def test_a_native_arm_binds_no_gpu_node_of_its_own(self):
        """The other half: unquoted and empty on native Linux, so it expands to NO argument. A literal
        `--bind /dev/dxg` would make every native Arm's apptainer exec fail on a path that is not
        there, which is every Arm we have but one."""
        script = self.render()
        payload = script[script.index("apptainer exec"):]
        assert "/dev/dxg" not in payload      # the path appears only in the guarded assignment above
        assert '"$dxg"' not in payload        # quoted, an empty var is an empty ARGUMENT, not nothing

    def test_the_pod_keeps_the_host_network(self):
        """--net is deliberately NOT here: the payload needs W&B egress, and the Tide Pool's sshd has to
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
            info = tarfile.TarInfo("train.py")
            info.size = 0
            tf.addfile(info, io.BytesIO(b""))
        tar = tmp_path / "up.tar.gz"
        tar.write_bytes(buf.getvalue())
        return head_spawns.submit(user_id=user_id, script="train.py", gpu=1, args=[], wandb_env={},
                                  tar_path=tar, minutes=60, constraint="vram24", outputs=["out"])

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


class TestDeclaredOutputsBecomeMkdirs:
    """docs/TODO.md's now-closed "Spawn Output Enforcement": each declared `--output` name becomes one
    `mkdir -p "$ccc_out/<name>"` in JOB_SCRIPT, made ONCE — after the if/else that sets `$ccc_out` in
    EITHER branch, before the payload runs — rather than duplicated inside each branch. That single
    placement is what makes it apply to the Head-local and remote paths alike."""

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24|vram80")
        kw.setdefault("node", None)
        return head_spawns._render(
            staging="/tmp/u", gpu=1, script="train.py", args=[], files=[],
            token="a3f9", secret="s3cr3t", **kw)

    def test_one_mkdir_per_declared_name(self):
        script = self.render(outputs=["checkpoints", "logs"])
        assert 'mkdir -p "$ccc_out/checkpoints"' in script
        assert 'mkdir -p "$ccc_out/logs"' in script

    def test_a_single_output_still_works(self):
        script = self.render(outputs=["checkpoints"])
        assert script.count('mkdir -p "$ccc_out/') == 1

    def test_mkdirs_land_after_both_branches_close_and_before_the_payload_runs(self):
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
    monkeypatch.setattr(slurm, "vram_constraint", lambda gb: "vram24")
    monkeypatch.setattr(slurm, "queue", lambda: [])  # no live Spawns — submit_checked's cap check
    monkeypatch.setattr(head_spawns, "submit", lambda *a, **kw: 4242)
    return TestClient(head_main.app)


def post(c: TestClient, **meta):
    meta.setdefault("script", "train.py")
    meta.setdefault("outputs", ["outputs"])
    return c.post("/api/submit", content=b"", headers={"X-CCC-Meta": json.dumps(meta)})


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


class TestSubmitRequiresDeclarations:
    def test_happy_path(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24)
        assert r.status_code == 200
        # `environment` is the submit-time echo (docs/TODO.md "Python Environments"). None here because
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
                            lambda *a, **kw: captured.setdefault("constraint", a[7]) or 4242)
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


class TestCliRequiresTimeAndVram:
    """No defaults, and no autofill. A `--vram` that defaulted to the fleet's biggest card would make
    every untouched field eligible for that one GPU alone — queued behind everything, at the highest
    Demand, for work that may need 2 GB."""

    def run(self, *args):
        return CliRunner().invoke(cli_app, ["run", *args])

    def test_bare_run_is_refused(self):
        assert self.run("train.py").exit_code != 0

    @pytest.mark.parametrize("args", [
        ["train.py", "--time", "60"],   # no --vram
        ["train.py", "--vram", "24"],   # no --time
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
        meta = {"script": "train.py", "minutes": 60, "vram": 24}  # no 'outputs' key at all
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
        kw.setdefault("tar_path", self._tar(tmp_path, "train.py"))
        return head_spawns.submit(user_id=1, gpu=1, args=[], wandb_env={}, minutes=60,
                                  constraint="", **kw)

    def test_empty_outputs_is_refused(self, monkeypatch, tmp_path):
        with pytest.raises(ValueError, match="output"):
            self._submit(monkeypatch, tmp_path, outputs=[])

    def test_a_traversal_attempt_is_refused(self, monkeypatch, tmp_path):
        with pytest.raises(ValueError, match="invalid output name"):
            self._submit(monkeypatch, tmp_path, outputs=["../../etc"])

    def test_a_slash_is_refused_even_without_dots(self, monkeypatch, tmp_path):
        with pytest.raises(ValueError, match="invalid output name"):
            self._submit(monkeypatch, tmp_path, outputs=["a/b"])

    def test_a_bare_dot_or_dotdot_is_refused(self, monkeypatch, tmp_path):
        """Both start with '.', which OUTPUT_NAME_RE never matches — no special-case needed."""
        for bad in (".", ".."):
            with pytest.raises(ValueError, match="invalid output name"):
                self._submit(monkeypatch, tmp_path, outputs=[bad])

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
        for good in ("checkpoints", "run-1", "epoch_09", "v2.final"):
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
            head_spawns.submit(user_id=1, script="missing.py", gpu=1, args=[], wandb_env={},
                              tar_path=self._tar(tmp_path, "other.py"), minutes=60, constraint="",
                              outputs=["out"])
        projects = tmp_path / "projects"
        assert not projects.exists() or not any(projects.iterdir())


class TestOutputsPlumbThroughToTheRenderedScript:
    """End-to-end: what `cuttle run --output` declares is what actually reaches the script sbatch is
    handed — not just what a mocked `submit` was called with. Same rig as
    TestItReachesSbatch.sbatch_env in test_wandb_key.py, but capturing the script BODY instead of the
    env. The body arrives on sbatch's STDIN now — there is no temp file to read off disk any more,
    because a 0600 file owned by the Mantle is not readable by the **Guise** sbatch runs as."""

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
        monkeypatch.setattr(slurm, "vram_constraint", lambda gb: "vram24")
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
            data = b"print('hi')\n"
            info = tarfile.TarInfo("train.py")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        tar_path = tmp_path / "upload.tar.gz"
        tar_path.write_bytes(buf.getvalue())

        head_spawns.submit_checked(1, False, "train.py", 1, [], {}, tar_path, 60, 24,
                                   ["checkpoints", "logs"])

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
