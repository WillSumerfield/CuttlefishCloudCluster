"""**Siphon** — one Spawn's rsync grant, and the end of mesh membership being the authz.

Replaces two shared modules — `[project]` over every User's upload and `[spawns]` over every
Spawn's results — authorized by nothing but being on the Mesh. Every failure guarded below is a
variation on the same one: a Spawn reaching something that isn't its own, and none of them raise.
A module rooted one directory too high, a grant that outlives its Spawn, a conf left behind by a
failed submission — each leaves a working cluster that's simply no longer isolating anything, and
nothing surfaces until someone goes looking.

Assumes rsync 3.2.7 behavior: an `&include`d directory is re-scanned per connection, its files
inherit the parent conf's globals (`hosts allow`, `read only`) and may override them per module,
a wrong secret is refused, `../` out of a module root is refused, and unlinking a conf revokes it
on the next connection.

See temp/spawn-isolation.md.
"""

import io
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ccc_head_api import db, siphons, spawns as head_spawns, tick
from cuttle import config, setup, spawns as cli_spawns, system, templates

_TS = "%Y-%m-%dT%H:%M:%SZ"


@pytest.fixture
def state(monkeypatch, tmp_path):
    """A throwaway Head state dir with the three trees a Siphon points into."""
    for name in ("projects", "spawns", "rsyncd.d"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(head_spawns, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
    monkeypatch.setattr(siphons, "RSYNCD_DIR", tmp_path / "rsyncd.d")
    return tmp_path


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as c:
        yield c


def _conf(name) -> str:
    return (siphons.RSYNCD_DIR / f"{name}.conf").read_text()


class TestOpening:
    def test_only_the_project_module_exists_before_sbatch(self, state):
        """The out module's root is `spawns/<jid>`, and there is no jid yet. Writing a module with a
        placeholder path would serve a directory that is not this Spawn's."""
        tok, _ = siphons.open("uuid-1")
        assert "[proj-" in _conf(tok)
        assert "[out-" not in _conf(tok)

    def test_the_project_module_is_rooted_at_this_spawns_own_upload(self, state):
        """The whole phase in one assertion. Rooted at `projects/` instead — one directory up — every
        Spawn could still pull every other User's code, and every test here would still pass."""
        tok, _ = siphons.open("uuid-1")
        assert f"path = {state / 'projects' / 'uuid-1'}\n" in _conf(tok)
        assert f"path = {state / 'projects'}\n" not in _conf(tok)

    def test_the_module_is_named_for_the_token_not_the_project(self, state):
        """A module named for the uuid would be as good as public: the uuid is in the job script, the
        filelist, and every Spawn dir listing. The token is minted for this and nothing else."""
        tok, _ = siphons.open("uuid-1")
        assert "uuid-1" not in f"[proj-{tok}]"
        assert f"[proj-{tok}]" in _conf(tok)

    def test_two_spawns_of_the_same_project_get_different_siphons(self, state):
        """A re-run uploads the same code twice. Sharing a secret between them would make a
        finished Spawn's credential a live one for whatever is running now."""
        tok_a, sec_a = siphons.open("uuid-1")
        tok_b, sec_b = siphons.open("uuid-1")
        assert tok_a != tok_b and sec_a != sec_b

    def test_the_secret_is_readable_only_by_the_mantle(self, state):
        tok, secret = siphons.open("uuid-1")
        sec_file = siphons.RSYNCD_DIR / f"{tok}.secrets"
        assert sec_file.read_text() == f"{tok}:{secret}\n"
        assert sec_file.stat().st_mode & 0o077 == 0

    def test_the_module_requires_that_secret(self, state):
        """Without `auth users` the module is reachable by any Spawn that can guess the name — and
        `hosts allow` cannot help, because two Spawns on one Arm share an address."""
        tok, _ = siphons.open("uuid-1")
        assert f"auth users = {tok}" in _conf(tok)
        assert f"secrets file = {siphons.RSYNCD_DIR / (tok + '.secrets')}" in _conf(tok)


class TestBinding:
    def test_the_out_module_appears_and_is_rooted_at_this_spawns_own_results(self, state):
        tok, _ = siphons.open("uuid-1")
        siphons.bind(tok, 4812)
        assert f"[out-{tok}]" in _conf(4812)
        assert f"path = {state / 'spawns' / '4812'}\n" in _conf(4812)
        assert f"path = {state / 'spawns'}\n" not in _conf(4812)

    def test_only_the_out_module_is_writable(self, state):
        """`read only = true` is inherited from the parent conf's globals, so the proj half needs no
        line of its own — but the out half must override it, and only the out half."""
        tok, _ = siphons.open("uuid-1")
        siphons.bind(tok, 4812)
        body = _conf(4812)
        assert body.count("read only = false") == 1
        assert body.index(f"[out-{tok}]") < body.index("read only = false")

    def test_the_project_module_survives_the_rewrite(self, state):
        """bind() rewrites the whole file. Dropping the proj half would leave a Spawn that has already
        been submitted unable to fetch its own code — and only on a REMOTE Arm."""
        tok, _ = siphons.open("uuid-1")
        siphons.bind(tok, 4812)
        assert f"path = {state / 'projects' / 'uuid-1'}\n" in _conf(4812)

    def test_the_files_are_renamed_to_the_job_id(self, state):
        """The rename IS the bookkeeping — there is no token→jid mapping anywhere else, so a Siphon
        left under its token is one nothing will ever close."""
        tok, _ = siphons.open("uuid-1")
        siphons.bind(tok, 4812)
        assert not (siphons.RSYNCD_DIR / f"{tok}.conf").exists()
        assert not (siphons.RSYNCD_DIR / f"{tok}.secrets").exists()
        assert (siphons.RSYNCD_DIR / "4812.secrets").exists()

    def test_the_secret_is_unchanged_by_binding(self, state):
        """The job script was rendered with it before sbatch. A new secret here would authenticate
        nothing, and the Spawn would fail at its first stage-out with an auth error."""
        tok, secret = siphons.open("uuid-1")
        siphons.bind(tok, 4812)
        assert (siphons.RSYNCD_DIR / "4812.secrets").read_text() == f"{tok}:{secret}\n"

    def test_the_secrets_line_follows_the_rename(self, state):
        """A `secrets file` still naming <tok>.secrets after the rename points at a path that no longer
        exists — which rsyncd reports as an auth failure, not as a missing file."""
        tok, _ = siphons.open("uuid-1")
        siphons.bind(tok, 4812)
        assert tok + ".secrets" not in _conf(4812)
        assert str(siphons.RSYNCD_DIR / "4812.secrets") in _conf(4812)


class TestDiscarding:
    def test_a_submission_that_never_reached_a_job_id_leaves_nothing(self, state):
        tok, _ = siphons.open("uuid-1")
        siphons.discard(tok)
        assert list(siphons.RSYNCD_DIR.iterdir()) == []


class TestClosing:
    def test_closing_removes_both_files(self, state):
        tok, _ = siphons.open("uuid-1")
        siphons.bind(tok, 4812)
        siphons.close(4812)
        assert list(siphons.RSYNCD_DIR.iterdir()) == []

    def test_closing_a_siphon_that_is_already_closed_is_fine(self, state):
        """Two tick passes can see the same finished Spawn — the second must not raise and wedge the
        pass, which also carries Settlement."""
        siphons.close(4812)


class TestTheSweep:
    """When a Siphon closes: a grace window after its Spawn finished, not at Settlement."""

    @pytest.fixture
    def now(self):
        return datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

    def _spawn(self, conn, job_id: int, finished: datetime | None):
        db.add_user(conn, f"u{job_id}")
        uid = db.get_user(conn, f"u{job_id}")["id"]
        db.record_spawn(conn, job_id, uid, "train.py", "uuid-1", minutes=60, gpus=1)
        if finished is not None:
            db.mark_finished(conn, job_id, 0, finished.strftime(_TS))
        tok, _ = siphons.open("uuid-1")
        siphons.bind(tok, job_id)

    def test_a_running_spawn_keeps_its_siphon(self, state, conn, now):
        """It is mid-transfer by definition — the periodic pusher runs every 5 seconds."""
        self._spawn(conn, 1, finished=None)
        assert siphons.sweep(conn, now) == 0
        assert (siphons.RSYNCD_DIR / "1.conf").exists()

    def test_a_just_finished_spawn_keeps_its_siphon(self, state, conn, now):
        """Inside the grace window the grant stays."""
        self._spawn(conn, 1, finished=now - timedelta(minutes=1))
        assert siphons.sweep(conn, now) == 0
        assert (siphons.RSYNCD_DIR / "1.conf").exists()

    def test_a_finished_spawn_closes_after_the_window(self, state, conn, now):
        self._spawn(conn, 1, finished=now - timedelta(minutes=siphons._CLOSE_GRACE_MINUTES + 1))
        assert siphons.sweep(conn, now) == 1
        assert not (siphons.RSYNCD_DIR / "1.conf").exists()

    def test_one_spawn_closing_leaves_its_neighbours_open(self, state, conn, now):
        self._spawn(conn, 1, finished=now - timedelta(minutes=30))
        self._spawn(conn, 2, finished=None)
        assert siphons.sweep(conn, now) == 1
        assert (siphons.RSYNCD_DIR / "2.conf").exists()

    def test_an_orphan_is_reaped_by_age(self, state, conn, now):
        """`bind()` follows `open()` within milliseconds, so a conf still named for its token an hour
        later is debris from a submission that raised in between — and `close()` will never find it,
        because nothing knows the job id it never got."""
        tok, _ = siphons.open("uuid-1")
        old = (now - timedelta(minutes=siphons._ORPHAN_MINUTES + 1)).timestamp()
        import os
        for p in siphons.RSYNCD_DIR.iterdir():
            os.utime(p, (old, old))
        assert siphons.sweep(conn, now) == 1
        assert list(siphons.RSYNCD_DIR.iterdir()) == []

    def test_a_fresh_unbound_siphon_is_left_alone(self, state, conn, now):
        """A submission in flight RIGHT NOW is unbound and seconds old. Reaping it would delete the
        stage-in module of a Spawn that is about to start."""
        siphons.open("uuid-1")
        assert siphons.sweep(conn, now) == 0

    def test_a_siphon_whose_spawn_has_no_row_is_left_alone(self, state, conn, now):
        """The Attribution row is written after sbatch, in a later connection — so there is a window
        where the conf exists and the row does not. Closing on a missing row would race it."""
        tok, _ = siphons.open("uuid-1")
        siphons.bind(tok, 9999)
        assert siphons.sweep(conn, now) == 0

    def test_a_missing_directory_is_not_an_error(self, state, conn, now):
        """A Head that has never submitted anything runs the tick like any other."""
        import shutil
        shutil.rmtree(siphons.RSYNCD_DIR)
        assert siphons.sweep(conn, now) == 0


class TestThePinnedRoadKnowsTheHeadsAddress:
    """`cuttle run --node` does not travel over HTTP — it shells into the API venv (spawns.py's
    `_submit_pinned`). That subprocess inherits nothing from the ccc-head-api systemd unit, so the
    Head's own mesh address has to be handed to it explicitly: it is the one unit variable with no
    usable default, being dynamic.

    Without it `%%HEAD_IP%%` renders empty and every pinned Spawn's stage-in becomes
    `rsync://<tok>@/proj-<tok>/` — a URL with no host, which fails on a remote Arm minutes later and
    on another machine. `submit_checked` refuses outright rather than let one out, so the visible
    symptom is a pinned submit that cannot work at all while an unpinned one (served by the running
    service, which has the variable) works fine."""

    def _argv(self, monkeypatch) -> list[str]:
        argv: list[str] = []
        monkeypatch.setattr(cli_spawns.auth, "_api", lambda *a, **kw: {"username": "alice"})
        monkeypatch.setattr(cli_spawns.mesh, "tailscale_ip", lambda: "100.99.0.1")
        monkeypatch.setattr(cli_spawns, "run", lambda a, **kw: argv.extend(a)
                            or type("R", (), {"returncode": 0, "stdout": "4242\n", "stderr": ""})())
        cli_spawns._submit_pinned(b"", "train.py", [], 60, 24, 1, "willpersonal", {}, ["out"])
        return argv

    def test_it_carries_the_heads_mesh_ip_into_the_admin_tool(self, monkeypatch):
        assert "CCC_MESH_IP=100.99.0.1" in self._argv(monkeypatch)

    def test_the_db_path_is_still_there_too(self, monkeypatch):
        """The extra env is inserted beside CCC_HEAD_DB, ahead of the interpreter — `env` stops
        reading assignments at the first non-assignment word, so an entry that landed after the
        python path would silently become one of the admin tool's arguments instead."""
        argv = self._argv(monkeypatch)
        assert f"CCC_HEAD_DB={config.HEAD_DB}" in argv
        assert argv.index("CCC_MESH_IP=100.99.0.1") < argv.index("-m")


class TestTheTickClosesThem:
    """One wiring test, not a pattern. Every condition `sweep` decides on is covered above against the
    function directly; this drives the real `tick.once` end to end for the one thing those cannot see —
    that the pass CALLS it. Drop the call from tick.py and every test above still passes, while every
    Siphon a Head ever mints lives until the box is rebuilt."""

    def test_a_finished_spawns_grant_is_gone_after_one_pass(self, state, conn, monkeypatch):
        now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(db, "connect", lambda: conn)  # the tick opens its own; make it ours
        monkeypatch.setattr(tick.slurm, "queue", lambda: [])   # the Spawn has left the queue
        monkeypatch.setattr(tick.slurm, "arms", lambda: [])
        db.add_user(conn, "alice")
        uid = db.get_user(conn, "alice")["id"]
        db.record_spawn(conn, 1, uid, "train.py", "uuid-1", minutes=60, gpus=1)
        db.mark_finished(conn, 1, 0, (now - timedelta(
            minutes=siphons._CLOSE_GRACE_MINUTES + 1)).strftime(_TS))
        tok, _ = siphons.open("uuid-1")
        siphons.bind(tok, 1)

        tick.once(now=now)

        assert not (siphons.RSYNCD_DIR / "1.conf").exists()
        assert not (siphons.RSYNCD_DIR / "1.secrets").exists()


class TestTheRenderedScript:
    """What the Spawn itself was told. These are the assertions that would catch a Siphon that is
    minted correctly and then never used."""

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24")
        kw.setdefault("node", None)
        return head_spawns._render(staging="/tmp/u", gpu=1, script="train.py", args=[], files=[],
                                   token="a3f9", secret="s3cr3t", **kw)

    def test_it_names_no_shared_module(self):
        """The two modules this phase deletes. A script still naming either would work today (the
        Head-local branch never rsyncs) and fail the first time Slurm placed the Spawn elsewhere."""
        body = self.render()
        for line in body.splitlines():
            if "rsync://" in line:
                assert "/project/" not in line and "/spawns/" not in line, line

    def test_every_transfer_goes_through_this_spawns_own_siphon(self):
        body = self.render()
        assert "rsync://a3f9@%%HEAD_IP%%" not in body  # the placeholder really was substituted
        for line in body.splitlines():
            if "rsync://" in line and "rsync -a" in line:
                assert "/proj-a3f9/" in line or "/out-a3f9/" in line, line

    def test_the_secret_is_exported_for_rsync(self):
        assert "export RSYNC_PASSWORD=s3cr3t" in self.render()

    def test_the_pod_never_sees_the_secret(self):
        """A Tide Pool's shell is a User's. Apptainer's --env list is explicit, so the credential the
        wrapper stages out with stops at the Pod wall — and this pins that it stays that way."""
        body = self.render()
        for line in body.splitlines():
            if "--env" in line:
                assert "RSYNC_PASSWORD" not in line

    def test_the_stage_in_lands_in_the_project_dir_itself(self):
        """The module is rooted at `projects/<uuid>`, so the filelist paths no longer carry a `<uuid>/`
        prefix — which means the pull target has to be `$proj`, not the scratch dir above it. Get this
        wrong and the payload runs in an empty directory instead of failing."""
        assert '/proj-a3f9/" "$proj/"' in self.render()


class TestSubmitOpensAndBindsOne:
    """The join between the two halves of this phase: what was minted, and what the Spawn was told."""

    @pytest.fixture
    def submitted(self, state, conn, monkeypatch, tmp_path):
        """One real submit — render, unpack, Attribution row, Escrow — against a captured sbatch.
        Returns the rendered script body."""
        monkeypatch.setattr(head_spawns, "SCRATCH_DIR", tmp_path / "scratch")
        monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")
        db.add_user(conn, "alice")
        bodies = {}

        def fake_run(argv, **kw):
            if "sbatch" in argv:
                bodies["script"] = kw["input"]
            return type("R", (), {"stdout": "4812\n", "stderr": "", "returncode": 0})()

        monkeypatch.setattr(head_spawns.subprocess, "run", fake_run)

        def go():
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                # The manifest rides along because every Spawn declares its **Environment** — a Spawn
                # without one is refused before a Siphon is ever minted, which is not this test.
                for name in ("train.py", "requirements.txt"):
                    info = tarfile.TarInfo(name)
                    info.size = 0
                    tf.addfile(info, io.BytesIO(b""))
            tar = tmp_path / "up.tar.gz"
            tar.write_bytes(buf.getvalue())
            jid = head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[],
                                     tar_path=tar, minutes=60, constraint="vram24", manifest="requirements.txt")
            return jid, bodies["script"]
        return go

    def test_a_submitted_spawn_has_a_siphon_named_for_its_job_id(self, submitted):
        job_id, _ = submitted()
        assert (siphons.RSYNCD_DIR / f"{job_id}.conf").exists()
        assert (siphons.RSYNCD_DIR / f"{job_id}.secrets").exists()

    def test_the_script_carries_the_secret_that_was_minted(self, submitted):
        """A render that baked in a different secret than the module checks would produce a Spawn that
        runs and then cannot send anything home — visible only as a Spawn the Head reports as lost."""
        job_id, body = submitted()
        tok, secret = (siphons.RSYNCD_DIR / f"{job_id}.secrets").read_text().strip().split(":", 1)
        assert f"export RSYNC_PASSWORD={secret}" in body
        assert f"/proj-{tok}/" in body and f"/out-{tok}/" in body

    def test_the_siphon_is_open_before_sbatch_is_called(self, state, conn, monkeypatch, tmp_path):
        """Ordering, not tidiness: a remote Spawn stages in within milliseconds of starting, and
        sbatch returning is the same instant the job becomes runnable. A module minted afterwards
        races the job that needs it."""
        monkeypatch.setattr(head_spawns, "SCRATCH_DIR", tmp_path / "scratch")
        monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")
        db.add_user(conn, "alice")
        seen = {}

        def fake_run(argv, **kw):
            if "sbatch" in argv:
                seen["confs"] = sorted(p.name for p in siphons.RSYNCD_DIR.glob("*.conf"))
            return type("R", (), {"stdout": "4812\n", "stderr": "", "returncode": 0})()

        monkeypatch.setattr(head_spawns.subprocess, "run", fake_run)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name in ("train.py", "requirements.txt"):
                info = tarfile.TarInfo(name)
                info.size = 0
                tf.addfile(info, io.BytesIO(b""))
        tar = tmp_path / "up.tar.gz"
        tar.write_bytes(buf.getvalue())
        head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[], tar_path=tar,
                           manifest="requirements.txt",
                           minutes=60, constraint="vram24")
        assert len(seen["confs"]) == 1

    def test_an_sbatch_that_fails_leaves_no_siphon_behind(self, state, conn, monkeypatch, tmp_path):
        """It never reaches a job id, so nothing else would ever close it — `sweep` reaps orphans only
        by age, an hour later, and until then the module is live for a Spawn that does not exist."""
        monkeypatch.setattr(head_spawns, "SCRATCH_DIR", tmp_path / "scratch")
        monkeypatch.setattr(head_spawns, "MESH_IP", "100.99.0.1")
        db.add_user(conn, "alice")

        def boom(argv, **kw):
            if "sbatch" in argv:
                raise subprocess.CalledProcessError(1, argv)
            return type("R", (), {"stdout": "", "stderr": "", "returncode": 0})()

        monkeypatch.setattr(head_spawns.subprocess, "run", boom)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name in ("train.py", "requirements.txt"):
                info = tarfile.TarInfo(name)
                info.size = 0
                tf.addfile(info, io.BytesIO(b""))
        tar = tmp_path / "up.tar.gz"
        tar.write_bytes(buf.getvalue())
        # RuntimeError, not the CalledProcessError raised above: the same handler that discards the
        # Siphon re-raises sbatch's refusal carrying what it said (head_spawns._said). The type is
        # incidental here — what this test is about is the module not surviving the failure.
        with pytest.raises(RuntimeError):
            head_spawns.submit(user_id=1, script="train.py", gpu=1, args=[],
                               tar_path=tar, minutes=60, constraint="vram24",
                               manifest="requirements.txt")
        assert list(siphons.RSYNCD_DIR.iterdir()) == []

    def test_the_filelist_is_relative_to_the_project_not_its_parent(self, submitted):
        """The paths a remote Arm's --files-from asks for, against a module rooted at the upload
        itself. A surviving `<uuid>/` prefix would ask for `<uuid>/<uuid>/train.py` and fetch nothing,
        and the Spawn would die in `cd "$proj"` on the far side of the mesh."""
        _, body = submitted()
        filelist = body.split("<<'CCC_FILELIST'\n")[1].split("\nCCC_FILELIST")[0]
        # Nothing built is in here any more: the **Environment** is made on the Arm, in node-local
        # scratch, and never crosses the mesh in either direction (ccc_head_api.environments).
        assert sorted(filelist.split()) == ["requirements.txt", "train.py"]


class TestTheDaemonConfig:
    def test_it_serves_no_shared_module(self):
        """`[project]` over every upload and `[spawns]` over every Spawn's results. Their absence is
        the phase."""
        conf = templates.render_rsyncd_conf("100.99.0.1")
        assert "[project]" not in conf
        assert "[spawns]" not in conf

    def test_it_includes_the_siphon_directory(self):
        assert f"&include {config.RSYNCD_DIR}" in templates.render_rsyncd_conf("100.99.0.1")

    def test_the_globals_a_siphon_inherits_are_all_stated(self):
        """An &include'd file starts parsing with the parent's defaults — which is the only reason a
        Siphon needs to state neither the mesh restriction nor the drop to `ccc`. Verified against
        rsync 3.2.7. Any of these going missing silently widens every Siphon on the Head."""
        conf = templates.render_rsyncd_conf("100.99.0.1")
        assert "read only = true" in conf          # a Siphon's proj half relies on this default
        assert f"uid = {config.HEAD_API_USER}" in conf
        assert f"hosts allow = {config.MESH_V4_PREFIX} {config.MESH_V6_PREFIX}" in conf
        assert "strict modes = false" in conf      # a Siphon's secrets file is ccc-owned, not root's

    def test_the_live_siphon_names_are_not_listable(self):
        assert "list = false" in templates.render_rsyncd_conf("100.99.0.1")

    def test_the_benchmark_module_reaches_only_the_bench_tree(self):
        """The one module that is not per-Spawn. Rooted at the Spawns dir instead, a benchmark — which
        runs on a donor's box like any other job — could overwrite any Spawn's results."""
        conf = templates.render_rsyncd_conf("100.99.0.1")
        assert f"path = {config.HEAD_SPAWNS_DIR / config.BENCH_DIR_NAME}" in conf
        assert f"path = {config.HEAD_SPAWNS_DIR}\n" not in conf

    def test_the_benchmarks_secret_is_out_of_reach_of_the_head_api(self):
        """RSYNCD_DIR is writable by the Head API by necessity — it is what mints Siphons — so
        anything in it is rewritable by it. The [bench] credential is the one part of the daemon's
        config that stays in the root-owned conf, and `strict modes` stays on for it because of that."""
        conf = templates.render_rsyncd_conf("100.99.0.1")
        assert str(config.RSYNCD_BENCH_SECRETS) in conf
        assert not str(config.RSYNCD_BENCH_SECRETS).startswith(str(config.RSYNCD_DIR))
        assert "strict modes = true" in conf


class TestTheBenchmarksSecretIsMintedOnce:
    """The one credential in this phase with a cluster lifetime rather than a Spawn's, which is what
    makes re-minting it a real event: `enroll` reads it at benchmark time and bakes it into that
    sbatch, so a rotation orphans any benchmark already in flight. It stages its Power file home,
    fails auth, and the Arm sits admitted-but-unbenchmarked — a state with no error anywhere near it."""

    def _mint(self, monkeypatch, exists: bool) -> tuple[list[list[str]], list[str]]:
        """Run `ensure_bench_secret` against a Head that does (or does not) already have one, and
        return every command it shelled out plus whatever any `install` was handed.

        The payload is captured DURING the call rather than read back after: `system.install_file`
        unlinks its temp file on the way out, deliberately, so the [bench] credential never sits
        in /tmp in plaintext. Both `run` bindings are patched because setup.py does `from
        .system import run`, so the two names are separate objects: the `test -s` probe goes
        through setup's, the install through system's.
        """
        calls: list[list[str]] = []
        payloads: list[str] = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if argv[0] == "install":
                payloads.append(Path(argv[-2]).read_text())
            # `test -s <path>` is how it asks; every other call is the write itself.
            code = (0 if exists else 1) if argv[0] == "test" else 0
            return type("R", (), {"returncode": code, "stdout": "", "stderr": ""})()

        monkeypatch.setattr(setup, "run", fake_run)
        monkeypatch.setattr(system, "run", fake_run)
        setup.ensure_bench_secret()
        return calls, payloads

    def test_a_head_without_one_gets_one(self, monkeypatch):
        calls, payloads = self._mint(monkeypatch, exists=False)
        install = [c for c in calls if c[0] == "install"]
        assert len(install) == 1
        assert install[0][-1] == str(config.RSYNCD_BENCH_SECRETS)
        assert "600" in install[0]      # rsyncd refuses a world-readable secrets file outright
        user, secret = payloads[0].strip().split(":", 1)
        assert user == config.RSYNC_BENCH_USER
        assert len(secret) >= 32        # guessable here means writable [bench] from any mesh host

    def test_the_credential_does_not_stay_in_tmp(self, monkeypatch):
        """0600 root at its destination, and for 11 days a plaintext copy in /tmp beside it."""
        calls, _ = self._mint(monkeypatch, exists=False)
        tmp = [c[-2] for c in calls if c[0] == "install"][0]
        assert not Path(tmp).exists()

    def test_a_setup_rerun_keeps_the_one_it_has(self, monkeypatch):
        calls, _ = self._mint(monkeypatch, exists=True)
        assert [c for c in calls if c[0] == "install"] == []
