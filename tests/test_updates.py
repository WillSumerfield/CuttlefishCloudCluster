"""Updates from the Head: build hashes, the served wheel, laptops updating themselves, and the
rollout that pushes a new build to Arms (cuttle.buildid / dist / selfupdate / rollout)."""

import json
import os
import subprocess
import zipfile

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import enroll
from ccc_head_api import main as head_main
from ccc_head_api import slurm
from conftest import USERS
from cuttle import buildid, config, rollout, selfupdate
from test_enroll_identity import BODY, HDR, _authorize, _fake_headscale, app  # noqa: F401 (fixture)


# --- build hashes --------------------------------------------------------------

class TestBuildHash:
    def test_it_ignores_bytecode_and_the_stamp(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1")
        before = buildid.source_hash(tmp_path)
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "a.pyc").write_bytes(b"junk")
        (tmp_path / "BUILD").write_text("whatever")
        assert buildid.source_hash(tmp_path) == before

    def test_any_change_changes_it(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1")
        before = buildid.source_hash(tmp_path)
        (tmp_path / "a.py").write_text("x = 2")
        assert buildid.source_hash(tmp_path) != before

    def test_a_checkout_hashes_itself(self):
        assert not buildid.stamped()
        assert buildid.current() == buildid.source_hash(buildid.PACKAGE)

    def test_the_wheel_is_stamped_with_its_sources_hash(self, tmp_path):
        core = buildid.PACKAGE.parents[1]
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(core)],
                       check=True, capture_output=True)
        wheel = next(tmp_path.glob("cuttle-*.whl"))
        stamp = zipfile.ZipFile(wheel).read("cuttle/BUILD").decode().strip()
        assert stamp == buildid.source_hash(buildid.PACKAGE)


# --- the Head API serves its build --------------------------------------------

@pytest.fixture
def dist(head, monkeypatch, tmp_path):
    d = tmp_path / "dist"
    d.mkdir()
    (d / "build.json").write_text(json.dumps({"build": "abc", "wheel": "cuttle-0.0.1-py3-none-any.whl"}))
    (d / "cuttle-0.0.1-py3-none-any.whl").write_bytes(b"wheel bytes")
    (d / "cuttle-0.0.1-py3-none-any.whl.sig").write_bytes(b"sig bytes")
    monkeypatch.setattr(head_main, "CUTTLE_DIST", d)
    monkeypatch.setattr(enroll, "CUTTLE_DIST", d)
    return d


class TestTheHeadServesItsBuild:
    def test_anyone_can_ask_which_build(self, dist):
        c = TestClient(head_main.app, base_url="https://head")
        assert c.get("/api/build").json() == {"build": "abc"}

    def test_the_wheel_needs_a_session(self, dist):
        c = TestClient(head_main.app, base_url="https://head")
        assert c.get("/api/cuttle/wheel").status_code == 401
        c.post("/api/login", json={"username": "alice", "password": USERS["alice"]})
        r = c.get("/api/cuttle/wheel")
        assert r.content == b"wheel bytes" and "cuttle-0.0.1-py3-none-any.whl" in r.headers["content-disposition"]

    def test_no_build_yet_is_a_503(self, head, monkeypatch, tmp_path):
        monkeypatch.setattr(head_main, "CUTTLE_DIST", tmp_path / "none")
        assert TestClient(head_main.app, base_url="https://head").get("/api/build").status_code == 503

    def test_arms_get_it_from_the_enroll_api(self, app, dist):
        c = TestClient(app)
        assert c.get("/cuttle").json()["build"] == "abc"
        assert c.get("/cuttle/wheel").content == b"wheel bytes"

    def test_arms_get_the_signature_from_the_enroll_api(self, app, dist):
        """F3: the detached wheel signature, fetched + verified by the update job before install."""
        r = TestClient(app).get("/cuttle/wheel.sig")
        assert r.content == b"sig bytes"
        assert "cuttle-0.0.1-py3-none-any.whl.sig" in r.headers["content-disposition"]


# --- Arms report their build ---------------------------------------------------

class TestArmsReport:
    def test_a_finished_setup_records_its_build(self, app, monkeypatch):
        _authorize("arm1")
        _fake_headscale({"arm1": ["100.99.0.5"]}, monkeypatch)
        c = TestClient(app, client=("100.99.0.5", 9))
        c.post("/enroll", json=BODY, headers=HDR)
        assert "build" not in enroll._load()["arm1"]
        c.post("/enroll", json={**BODY, "build": "abc"}, headers=HDR)
        assert enroll._load()["arm1"]["build"] == "abc"

    def test_only_the_arm_itself_can_report_a_failure(self, app, monkeypatch):
        _authorize("arm1")
        _fake_headscale({"arm1": ["100.99.0.5"], "arm2": ["100.99.0.6"]}, monkeypatch)
        body = {"build": "abc", "error": "boom"}
        r = TestClient(app, client=("100.99.0.6", 9)).post("/arms/arm1/update-failed", json=body, headers=HDR)
        assert r.status_code == 403
        r = TestClient(app, client=("100.99.0.5", 9)).post("/arms/arm1/update-failed", json=body, headers=HDR)
        assert r.status_code == 204
        assert enroll._load()["arm1"]["update_error"] == body

    def test_a_good_report_clears_the_old_failure(self, app, monkeypatch):
        _authorize("arm1")
        _fake_headscale({"arm1": ["100.99.0.5"]}, monkeypatch)
        c = TestClient(app, client=("100.99.0.5", 9))
        c.post("/arms/arm1/update-failed", json={"build": "abc", "error": "boom"}, headers=HDR)
        c.post("/enroll", json={**BODY, "build": "abc"}, headers=HDR)
        assert "update_error" not in enroll._load()["arm1"]


# --- laptops update themselves -------------------------------------------------

class _Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self.content, self.headers = status, body, headers or {}

    def json(self):
        return json.loads(self.content)


class _Client:
    def __init__(self, build):
        self.build, self.calls = build, []

    def get(self, path, **kw):
        self.calls.append(path)
        if path == "/build":
            return _Resp(200, json.dumps({"build": self.build}).encode())
        return _Resp(200, b"whl", {"content-disposition": 'attachment; filename="cuttle-1-py3-none-any.whl"'})


@pytest.fixture
def wheel_install(monkeypatch, tmp_path):
    """A stamped cuttle with a session; records the install and the re-run instead of doing them."""
    monkeypatch.setattr(buildid, "stamped", lambda: True)
    monkeypatch.setattr(buildid, "current", lambda: "mine")
    monkeypatch.delenv("CCC_JUST_UPDATED", raising=False)
    monkeypatch.setattr(config, "SESSION_FILE", tmp_path / "session")
    config.SESSION_FILE.write_text("tok")
    did = []
    monkeypatch.setattr(selfupdate.subprocess, "run",
                        lambda cmd, **kw: did.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(selfupdate.os, "execvp", lambda *a: did.append("rerun"))
    return did


class TestLaptopsUpdateThemselves:
    def test_a_different_build_installs_the_heads_and_reruns(self, wheel_install):
        selfupdate.check(_Client("heads"), None)
        assert wheel_install[0][1:4] == ["tool", "install", "--force"]
        assert wheel_install[0][4].endswith("cuttle-1-py3-none-any.whl")
        assert wheel_install[1] == "rerun"

    def test_the_same_build_does_nothing(self, wheel_install):
        c = _Client("mine")
        selfupdate.check(c, None)
        assert c.calls == ["/build"] and wheel_install == []

    def test_a_checkout_never_updates_itself(self, wheel_install, monkeypatch):
        monkeypatch.setattr(buildid, "stamped", lambda: False)
        c = _Client("heads")
        selfupdate.check(c, None)
        assert c.calls == []

    def test_no_session_waits_for_login(self, wheel_install):
        config.SESSION_FILE.unlink()
        c = _Client("heads")
        selfupdate.check(c, None)
        assert c.calls == ["/build"] and wheel_install == []

    def test_the_rerun_does_not_loop(self, wheel_install, monkeypatch):
        monkeypatch.setenv("CCC_JUST_UPDATED", "1")
        c = _Client("heads")
        selfupdate.check(c, None)
        assert c.calls == []


# --- the rollout to Arms -------------------------------------------------------

@pytest.fixture
def roll(tmp_path):
    """The standalone rollout script, loaded with Slurm faked. `jobs` is what squeue shows."""
    ns: dict = {"__name__": "rollout"}
    exec(rollout._SCRIPT, ns)
    calls, jobs, mail = [], {}, []

    def sh(*cmd):
        calls.append(cmd)
        if cmd[0] == "squeue":
            name = cmd[cmd.index("-n") + 1].removeprefix(ns["PREFIX"])
            return subprocess.CompletedProcess(cmd, 0, jobs.get(name, ""), "")
        return subprocess.CompletedProcess(cmd, 0, "42\n", "")
    ns["sh"] = sh
    ns["email"] = lambda conf, subject, body: mail.append(subject)
    paths = {k: tmp_path / f"{k}.json" for k in ("reg", "meta", "state")}
    paths["meta"].write_text(json.dumps({"build": "new", "wheel": "w.whl"}))

    def run(reg, *restart):
        paths["reg"].write_text(json.dumps(reg))
        calls.clear()
        ns["main"](str(paths["reg"]), str(paths["meta"]), str(paths["state"]), "conf", "http://h", *restart)
        return json.loads(paths["state"].read_text())
    run.calls, run.jobs, run.mail = calls, jobs, mail
    return run


def _cmds(calls, word):
    return [c for c in calls if word in c]


OLD = {"arm1": {"admitted": True, "build": "old"}}


class TestTheRollout:
    def test_an_out_of_date_arm_is_reserved_and_gets_a_job(self, roll):
        state = roll(OLD)
        assert state["arm1"] == {"target": "new", "attempts": 1, "status": "waiting", "error": None}
        assert _cmds(roll.calls, "create")[0][3] == "ReservationName=ccc-update-arm1"
        sbatch = next(c for c in roll.calls if c[0] == "sbatch")
        assert "--reservation=ccc-update-arm1" in sbatch and "--exclusive" in sbatch

    def test_a_live_job_is_left_to_run(self, roll):
        roll(OLD)
        roll.jobs["arm1"] = "RUNNING"
        state = roll(OLD)
        assert state["arm1"]["status"] == "updating" and state["arm1"]["attempts"] == 1
        assert not any(c[0] == "sbatch" for c in roll.calls)

    def test_done_reopens_the_arm(self, roll):
        roll(OLD)
        state = roll({"arm1": {"admitted": True, "build": "new"}})
        assert state == {}
        assert ("scontrol", "delete", "ReservationName=ccc-update-arm1") in roll.calls

    def test_two_retries_then_closed_with_an_alert(self, roll):
        for _ in range(3):
            roll(OLD)
        reg = {"arm1": {"admitted": True, "build": "old",
                        "update_error": {"build": "new", "error": "uv exploded"}}}
        state = roll(reg)
        assert state["arm1"]["attempts"] == 3
        assert state["arm1"]["status"] == "failed" and state["arm1"]["error"] == "uv exploded"
        assert roll.mail == ["CCC: updating Arm arm1 failed"]
        roll(reg)
        assert len(roll.mail) == 1   # once, not every pass
        assert not any(c[0] == "sbatch" for c in roll.calls)

    def test_arm_update_starts_the_tries_over(self, roll):
        for _ in range(4):
            roll(OLD)
        state = roll(OLD, "arm1")
        assert state["arm1"]["attempts"] == 1 and state["arm1"]["status"] == "waiting"

    def test_the_heads_own_arm_and_unadmitted_arms_are_left_alone(self, roll):
        state = roll({"head": {"admitted": True, "head": True, "build": "old"},
                      "new-arm": {"admitted": False}})
        assert state == {} and roll.calls == []

    def test_the_job_script_is_valid_python(self, roll):
        ns: dict = {}
        exec(rollout._SCRIPT, ns)
        compile(ns["JOB"] % ("http://h", "arm1", "new", str(config.ARM_UPDATER_RECORD)), "job", "exec")


# --- the Arm update job's tool swap (the part that broke an install mid-update) -----------------

def _job():
    """The helpers from the Arm-side update JOB, loaded without running its network `main()`."""
    outer: dict = {"__name__": "rollout"}
    exec(rollout._SCRIPT, outer)
    job = outer["JOB"] % ("http://h", "arm1", "new", str(config.ARM_UPDATER_RECORD))
    ns: dict = {"__name__": "job-test"}
    exec(job, ns)
    return ns


def _tool(tmp_path):
    """A pretend installed cuttle: a tool-env dir with a file, and a shim."""
    toolenv = tmp_path / "tools" / "cuttle"
    toolenv.mkdir(parents=True)
    (toolenv / "old").write_text("old-env")
    exe = tmp_path / "bin" / "cuttle"
    exe.parent.mkdir()
    exe.write_text("old-shim")
    return toolenv, exe


class TestTheToolSwap:
    def test_a_proven_install_replaces_the_old_build_and_drops_it(self, tmp_path):
        toolenv, exe = _tool(tmp_path)

        def install():   # uv builds a fresh env + shim (old was renamed aside first)
            toolenv.mkdir(parents=True, exist_ok=True)
            (toolenv / "new").write_text("new-env")
            exe.write_text("new-shim")
            return 0, "installed"

        err = _job()["swap_tool"](str(toolenv), str(exe), os.getuid(), install, lambda: 0)
        assert err is None
        assert (toolenv / "new").exists() and not (toolenv / "old").exists()
        assert exe.read_text() == "new-shim"
        assert not (tmp_path / "tools" / "cuttle.ccc-old").exists()   # old copy dropped
        assert not (tmp_path / "bin" / "cuttle.ccc-old").exists()

    def test_a_failed_install_restores_the_working_build(self, tmp_path):
        toolenv, exe = _tool(tmp_path)

        def install():   # uv leaves a half-built env then fails
            toolenv.mkdir(parents=True, exist_ok=True)
            (toolenv / "half").write_text("broken")
            exe.write_text("broken-shim")
            return 1, "uv exploded"

        err = _job()["swap_tool"](str(toolenv), str(exe), os.getuid(), install, lambda: 0)
        assert "kept the previous build" in err and "uv exploded" in err
        assert (toolenv / "old").read_text() == "old-env" and not (toolenv / "half").exists()
        assert exe.read_text() == "old-shim"

    def test_an_install_that_does_not_verify_is_rolled_back(self, tmp_path):
        toolenv, exe = _tool(tmp_path)

        def install():
            toolenv.mkdir(parents=True, exist_ok=True)
            exe.write_text("new-but-broken")
            return 0, "installed"

        err = _job()["swap_tool"](str(toolenv), str(exe), os.getuid(), install, lambda: 1)
        assert err and exe.read_text() == "old-shim" and (toolenv / "old").exists()

    def test_report_foreign_names_the_owner_of_a_stray_file(self, tmp_path, capsys):
        root = tmp_path / "env"
        root.mkdir()
        (root / "stray").write_text("x")
        _job()["report_foreign"](str(root), os.getuid() + 1)   # pretend the owner is someone else
        err = capsys.readouterr().err
        assert "stray" in err and "not the Arm's owner" in err

    def test_report_foreign_is_silent_when_everything_is_the_owners(self, tmp_path, capsys):
        root = tmp_path / "env"
        root.mkdir()
        (root / "mine").write_text("x")
        _job()["report_foreign"](str(root), os.getuid())
        assert capsys.readouterr().err == ""


# --- the Head's update jobs aren't Dives ---------------------------------------

def test_update_jobs_are_not_listed_as_dives(monkeypatch):
    jobs = [{"job_id": 1, "name": "ccc-update-arm1", "user_name": "root", "partition": slurm.PARTITION,
             "job_state": ["PENDING"]},
            {"job_id": 2, "name": "dive", "user_name": "ccc-g2001", "partition": slurm.PARTITION,
             "job_state": ["PENDING"]}]
    monkeypatch.setattr(slurm, "_slurm_json", lambda *a: {"jobs": jobs})
    assert [d["id"] for d in slurm.dives()] == [2]
    assert [j["id"] for j in slurm.queue()] == [2]
