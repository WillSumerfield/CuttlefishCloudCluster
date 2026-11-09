"""Dives, Head API side: settings (cuttle.toml + args), caps, the request's order of steps and its
cleanup, single-writer, attach readiness, end, and the tick's close.

NOT tested: real sudo/helper/sbatch/relay to an Arm — hardware list (temp/2.0-plan.md).
"""

import json
import subprocess

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from ccc_head_api import db, karma, slurm
from ccc_head_api import dive as D
from ccc_head_api import main as head_main
from ccc_head_api import tidepool as head_tp
from cuttle import config
from cuttle import ccc_launch as L


@pytest.fixture
def conn_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        for uid, name in ((1, "alice"), (2, "bob")):
            conn.execute("INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                         "VALUES (?,?,0,'x',0)", (uid, name))
        conn.commit()
    return tmp_path


ALICE = {"id": 1, "username": "alice", "is_admin": False, "must_change_password": False}


class TestConstantsAgree:
    def test_with_cuttle(self):
        assert (D.PORT_BASE, D.PORT_RANGE) == (config.TIDEPOOL_PORT_BASE, config.TIDEPOOL_PORT_RANGE)
        assert D.MAX_MINUTES == config.DIVE_MAX_MINUTES
        assert D.LAUNCH == str(config.CCC_LAUNCH_BIN)

    def test_port_range_is_below_the_ephemeral_range(self):
        assert D.PORT_BASE + D.PORT_RANGE <= 32768

    def test_the_spec_line_is_what_ccc_launch_reads(self, monkeypatch):
        monkeypatch.setattr(D, "MESH_IP", "100.99.0.1")
        spec = D.make_spec("alice", "ab" * 16, "s" * 43, "ccc.lab:5000/ccc/u-alice@sha256:" + "a" * 64,
                           "proj", ["data"], 31000, "pyproject.toml", ["vlearn"])
        script = D.render_job(spec, 1, 60, "", "")
        got = L.validate_spec(L.read_spec(script))
        assert got == spec and got["dive"]["head"] == D.MESH_IP
        assert got["features"] == ["vlearn"]

    def test_an_ordinary_dives_spec_carries_no_features_key(self):
        spec = D.make_spec("alice", "ab" * 16, "s" * 43, "ccc.lab:5000/ccc/u-alice@sha256:" + "a" * 64,
                           "proj", [], 31000, None, [])
        assert "features" not in spec   # byte-for-byte what it was before Features existed
        assert "CCC_DIVE_FEATURES" not in spec["env"] and "feature_stubs" not in spec["dive"]

    def test_a_feature_dives_spec_carries_the_features_env_and_the_stub_payload(self, monkeypatch):
        monkeypatch.setattr(D, "MESH_IP", "100.99.0.1")
        stubs = {"files": [{"rel": "../wheels/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl", "b64": "AA=="}],
                 "digests": {"vlearn": "a" * 64}, "lock": "uv.lock"}
        spec = D.make_spec("alice", "ab" * 16, "s" * 43, "ccc.lab:5000/ccc/u-alice@sha256:" + "a" * 64,
                           "proj", [], 31000, "pyproject.toml", ["vlearn"], stubs)
        assert spec["env"]["CCC_DIVE_FEATURES"] == "vlearn"   # what the in-container hook arranges for
        assert spec["dive"]["feature_stubs"] == stubs
        # Survives the base64 spec line and ccc-launch's own validation intact.
        assert L.validate_spec(L.read_spec(D.render_job(spec, 1, 60, "", ""))) == spec


class TestSettings:
    @pytest.mark.parametrize("v,m", [(90, 90), ("90", 90), ("90m", 90), ("4h", 240), ("2d", 2880),
                                     ("1d12h", 2160), ("1h30m", 90)])
    def test_parse_time(self, v, m):
        assert D.parse_time(v) == m

    @pytest.mark.parametrize("v", ["", "4x", "h", "-3"])
    def test_bad_time(self, v):
        with pytest.raises(ValueError):
            D.parse_time(v)

    def test_args_override_toml(self):
        s = D.settings({"time": "4h", "gpus": 2, "deps": ["data"], "vram": 16}, {"time": "1d", "gpus": None})
        assert (s["minutes"], s["gpus"], s["deps"]) == (1440, 2, ["data"])

    def test_time_is_required_and_capped(self):
        with pytest.raises(HTTPException, match="needs --time"):
            D.settings({}, {"vram": 8})
        with pytest.raises(HTTPException, match="between 1m and 14d"):
            D.settings({}, {"time": "15d", "vram": 8})
        assert D.settings({}, {"time": "14d", "vram": 8})["minutes"] == config.DIVE_MAX_MINUTES

    def test_vram_is_required(self):
        with pytest.raises(HTTPException, match="needs --vram"):
            D.settings({}, {"time": "1h"})
        with pytest.raises(HTTPException, match="at least 1"):
            D.settings({}, {"time": "1h", "vram": 0})
        assert D.settings({"vram": 24}, {"time": "1h"})["vram"] == 24

    def test_env_stays_in_the_main_folder(self):
        with pytest.raises(HTTPException):
            D.settings({}, {"time": "1h", "vram": 8, "env": "../x/pyproject.toml"})

    def test_a_profiled_feature_needs_an_env(self):
        with pytest.raises(HTTPException, match="needs an environment"):
            D.settings({}, {"time": "1h", "vram": 8, "features": ["vlearn"]})

    def test_toml_round_trip(self):
        d = {"deps": ["data", "My Lib"], "gpus": 2, "vram": 40, "time": "4h", "features": ["vsim"],
             "env": "pyproject.toml"}
        assert D.load_toml(D.dump_toml(d)) == d
        assert "gpus" not in D.dump_toml({"time": "1h", "gpus": None})

    def test_a_broken_toml_is_the_users_to_fix(self):
        with pytest.raises(ValueError, match="cuttle.toml"):
            D.load_toml("time = ")


PREP = {"main": "proj", "deps": [], "folders": {"proj": 2**30}, "dotfiles": 2**20,
        "dotfile_cap": 2**30, "dotfiles_biggest": [{"path": ".zshrc", "bytes": 2**20}], "layer": 2**30,
        "toml": None}


class TestCaps:
    def test_under_the_cap(self):
        D.check_sizes(PREP)

    def test_over_25_gb_names_the_parts(self):
        with pytest.raises(HTTPException, match=r"30\.0 GB — over the 25\.0 GB cap.*~/proj 29\.0 GB"):
            D.check_sizes({**PREP, "folders": {"proj": 29 * 2**30}, "dotfiles": 0})

    def test_dotfiles_over_1_gb_lists_the_biggest(self):
        with pytest.raises(HTTPException, match=r"dotfiles are 2\.0 GB.*\.oh-my-zsh"):
            D.check_sizes({**PREP, "dotfiles": 2 * 2**30,
                           "dotfiles_biggest": [{"path": ".oh-my-zsh/big", "bytes": 2**31}]})


class FakeHead:
    """The helper, sbatch and Slurm, recorded."""

    def __init__(self, monkeypatch, prep=None, sbatch_rc=0):
        self.calls, self.prep, self.sbatch_rc = [], prep or dict(PREP), sbatch_rc
        monkeypatch.setattr(D, "MESH_IP", "100.99.0.1")
        monkeypatch.setattr(D.subprocess, "run", self.run)
        monkeypatch.setattr(slurm, "vram_constraint", lambda v, g: "")
        monkeypatch.setattr(slurm, "arms", lambda: [])
        monkeypatch.setattr(slurm, "cancel", lambda j: self.calls.append(("scancel", j)))
        monkeypatch.setattr(karma, "quote_hold", lambda *a, **k: 5.0)

    def run(self, argv, input=None, **kw):
        if "sbatch" in argv:
            self.calls.append(("sbatch", input))
            if self.sbatch_rc:
                raise subprocess.CalledProcessError(1, argv, "", "error: Batch job submission failed")
            return subprocess.CompletedProcess(argv, 0, "77\n", "")
        verb = argv[argv.index(D.HELPER) + 1]
        self.calls.append((verb, json.loads(input) if input else None))
        out = {"dive-prep": self.prep, "dive-snap": {"image": "ccc.lab:5000/ccc/u-alice@sha256:" + "f" * 64}}
        return subprocess.CompletedProcess(argv, 0, json.dumps(out[verb]) if verb in out else "", "")

    def verbs(self):
        return [c[0] for c in self.calls]


class TestRequest:
    def test_order_prep_snap_open_sbatch_then_recorded(self, conn_db, monkeypatch):
        h = FakeHead(monkeypatch)
        out = D.request(ALICE, "proj", None, {"time": "2h", "vram": 8})
        assert h.verbs() == ["dive-prep", "dive-snap", "dive-open", "sbatch"]
        assert out["id"] == 77 and out["minutes"] == 120
        opened = h.calls[2][1]
        assert D.load_toml(opened["toml"]) == {"time": "2h", "vram": 8}   # args become the folder's defaults
        with db.connect() as conn:   # the Dive's ambient session, handed to the helper for sys/
            user = db.lookup_session(conn, opened["session"])
        assert user["username"] == "alice" and user["session_scope"] == "dive"
        script = h.calls[3][1]
        assert "#SBATCH --time=120" in script and "--gres=gpu:1" in script
        assert f'exec sudo -n {D.LAUNCH} "$SLURM_JOB_ID"' in script
        with db.connect() as conn:
            d = db.get_dive(conn, 77)
            assert d["main"] == "proj" and d["port"] == D.PORT_BASE and not d["closed"]
            assert d["user_id"] == 1

    def test_toml_supplies_deps_and_time_and_is_not_rewritten(self, conn_db, monkeypatch):
        h = FakeHead(monkeypatch, prep={**PREP, "toml": 'time = "3h"\nvram = 8\ndeps = ["data"]\n'})
        D.request(ALICE, "proj", None, {})
        assert h.verbs() == ["dive-prep", "dive-prep", "dive-snap", "dive-open", "sbatch"]
        assert h.calls[1][1]["deps"] == ["data"]
        assert h.calls[3][1]["toml"] is None

    _VLEARN_MANIFEST = ('[project]\nname = "t"\nversion = "0"\nrequires-python = "==3.11.14"\n'
                        'dependencies = ["vlearn"]\n\n[tool.uv.sources]\n'
                        'vlearn = { path = "../wheels/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl" }\n')

    def test_a_supplied_package_without_its_feature_is_refused_before_any_karma(self, conn_db, monkeypatch):
        """The whole reason check_supplied runs at submit: the Dive would build cleanly, place anywhere,
        and die at `import` — after the User paid to hold a GPU. Caught before snap/open/sbatch."""
        h = FakeHead(monkeypatch, prep={**PREP, "manifest": self._VLEARN_MANIFEST})
        with pytest.raises(HTTPException) as e:
            D.request(ALICE, "proj", None, {"time": "1h", "vram": 8, "env": "pyproject.toml"})
        assert e.value.status_code == 422 and "--feature vlearn" in e.value.detail
        # The manifest re-prep ran, then the refusal cleaned up — nothing was pushed or submitted.
        assert h.verbs() == ["dive-prep", "dive-prep", "dive-close"]

    def test_a_feature_dive_ships_its_stub_payload_in_the_job_spec(self, conn_db, monkeypatch):
        h = FakeHead(monkeypatch, prep={**PREP, "manifest": self._VLEARN_MANIFEST})
        monkeypatch.setattr(slurm, "arms_with_features", lambda f: ["node1"])
        monkeypatch.setattr(slurm, "feature_constraint", lambda f: "vlearn")
        out = D.request(ALICE, "proj", None,
                        {"time": "1h", "vram": 8, "env": "pyproject.toml", "features": ["vlearn"]})
        spec = L.validate_spec(L.read_spec(h.calls[-1][1]))
        assert spec["env"]["CCC_DIVE_FEATURES"] == "vlearn"
        st = spec["dive"]["feature_stubs"]
        assert st["files"][0]["rel"].endswith(".whl") and len(st["digests"]["vlearn"]) == 64
        assert st["lock"] == "uv.lock" and out["features"] == ["vlearn"]

    def test_a_held_main_folder_is_refused(self, conn_db, monkeypatch):
        FakeHead(monkeypatch)
        D.request(ALICE, "proj", None, {"time": "1h", "vram": 8})
        h = FakeHead(monkeypatch)
        with pytest.raises(HTTPException) as e:
            D.request(ALICE, "proj", None, {"time": "1h", "vram": 8})
        assert e.value.status_code == 409 and "held by Dive 77" in e.value.detail
        assert h.verbs()[-1] == "dive-close"     # the second request's prep is cleaned up

    def test_over_the_cap_cleans_up_before_anything_is_pushed(self, conn_db, monkeypatch):
        h = FakeHead(monkeypatch, prep={**PREP, "folders": {"proj": 30 * 2**30}})
        with pytest.raises(HTTPException):
            D.request(ALICE, "proj", None, {"time": "1h", "vram": 8})
        assert h.verbs() == ["dive-prep", "dive-close"]

    def test_sbatch_refusal_releases_the_port_and_closes(self, conn_db, monkeypatch):
        h = FakeHead(monkeypatch, sbatch_rc=1)
        with pytest.raises(HTTPException, match="sbatch refused"):
            D.request(ALICE, "proj", None, {"time": "1h", "vram": 8})
        assert h.verbs()[-1] == "dive-close"
        with db.connect() as conn:
            assert db.used_dive_ports(conn) == set()

    def test_a_helper_refusal_is_the_users_422(self, conn_db, monkeypatch):
        monkeypatch.setattr(D, "MESH_IP", "100.99.0.1")
        monkeypatch.setattr(D.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
            a, 2, "", "ccc-tidepool: ~/nope is not a folder in your Tidepool\n"))
        with pytest.raises(HTTPException) as e:
            D.request(ALICE, "nope", None, {"time": "1h"})
        assert e.value.status_code == 422 and e.value.detail == "~/nope is not a folder in your Tidepool"


def _client(monkeypatch, user=ALICE):
    head_main.app.dependency_overrides[head_main.current_user] = lambda: user
    head_main.app.dependency_overrides[head_main.ambient_user] = lambda: user
    return TestClient(head_main.app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


def _seed(job_id=77, user_id=1, main="proj", port=31000):
    with db.connect() as conn:
        db.reserve_dive_port(conn, port)
        db.create_dive(conn, job_id, user_id, f"{job_id:032x}", main, [], port, minutes=60, gpus=1)
        db.allocate_guise(conn, user_id)


class TestAttach:
    def test_unknown_is_404_and_foreign_has_no_connection(self, conn_db, monkeypatch):
        _seed(user_id=2)
        monkeypatch.setattr(slurm, "state", lambda j: "RUNNING")
        monkeypatch.setattr(D, "ensure_relay", lambda *a: pytest.fail("not the caller's Dive"))
        c = _client(monkeypatch)
        assert c.get("/api/dives/5").status_code == 404
        r = c.get("/api/dives/77").json()
        assert r["state"] == "running" and r["main"] == "proj"
        assert "port" not in r and "host_key" not in r

    def test_not_ready_while_queued(self, conn_db, monkeypatch):
        _seed()
        monkeypatch.setattr(slurm, "state", lambda j: "PENDING")
        r = _client(monkeypatch).get("/api/dives/77").json()
        assert (r["ready"], r["state"]) == (False, "pending")

    def test_a_head_dive_is_its_own_port_with_no_relay(self, conn_db, monkeypatch):
        _seed()
        monkeypatch.setattr(slurm, "state", lambda j: "RUNNING")
        monkeypatch.setattr(slurm, "dives", lambda: [{"id": 77, "node": "dex"}])
        monkeypatch.setattr(slurm, "arms", lambda: [{"name": "dex", "mesh_ip": "100.99.0.1", "head": True}])
        monkeypatch.setattr(D, "ensure_relay", lambda *a: pytest.fail("no relay on the Head"))
        monkeypatch.setattr(D, "_banner", lambda p: True)
        monkeypatch.setattr(D.tp_mod, "cluster_name", lambda: "head1")
        r = _client(monkeypatch).get("/api/dives/77").json()
        assert r["ready"] and r["port"] == 31000 and r["node"] == "head1"

    def test_an_arm_dive_is_ready_only_when_sshd_answers(self, conn_db, monkeypatch):
        _seed()
        monkeypatch.setattr(slurm, "state", lambda j: "RUNNING")
        monkeypatch.setattr(slurm, "dives", lambda: [{"id": 77, "node": "arm1"}])
        monkeypatch.setattr(slurm, "arms", lambda: [{"name": "arm1", "mesh_ip": "100.99.0.7"}])
        monkeypatch.setattr(D, "ensure_relay", lambda *a: True)
        monkeypatch.setattr(D, "_banner", lambda p: False)
        c = _client(monkeypatch)
        assert c.get("/api/dives/77").json()["ready"] is False   # relay up, still staging
        monkeypatch.setattr(D, "_banner", lambda p: True)
        r = c.get("/api/dives/77").json()
        assert r["ready"] and r["port"] == 31000 and r["user"] == "alice" and r["node"] == "arm1"

    def test_end_is_yours_only(self, conn_db, monkeypatch):
        _seed()
        _seed(78, user_id=2, main="x", port=31001)
        ended = []
        monkeypatch.setattr(slurm, "cancel", ended.append)
        c = _client(monkeypatch)
        assert c.delete("/api/dives/78").status_code == 403
        assert c.delete("/api/dives/77").status_code == 204
        assert c.delete("/api/dives/5").status_code == 404
        with db.connect() as conn:
            assert ended == [77] and db.get_dive(conn, 77)["cancelled"]
            assert not db.get_dive(conn, 78)["cancelled"]


class TestClose:
    def test_a_job_gone_from_the_queue_is_closed_through_the_helper(self, conn_db, monkeypatch):
        _seed()
        _seed(78, main="x", port=31001)
        ran = []
        monkeypatch.setattr(D.subprocess, "run",
                            lambda a, **k: ran.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
        with db.connect() as conn:
            gone = db.create_session(conn, 1, scope="dive", dive=f"{77:032x}")
            kept = db.create_session(conn, 1, scope="dive", dive=f"{78:032x}")
            D.reap_dead(conn, {78})
            assert db.get_dive(conn, 77)["closed"] and not db.get_dive(conn, 78)["closed"]
            assert db.lookup_session(conn, gone) is None and db.lookup_session(conn, kept)
            assert db.used_dive_ports(conn) == {31001}
        assert ran == [["sudo", "-n", D.HELPER, "dive-close", f"{77:032x}"]]

    def test_a_failed_close_is_retried_next_tick(self, conn_db, monkeypatch):
        _seed()
        monkeypatch.setattr(D.subprocess, "run", lambda a, **k: subprocess.CompletedProcess(a, 1, "", "x"))
        with db.connect() as conn:
            D.reap_dead(conn, set())
            assert not db.get_dive(conn, 77)["closed"]


class TestTidepoolRestartDuringAHeadDive:
    def test_the_helpers_refusal_is_a_409_with_its_words(self, conn_db, monkeypatch):
        def refuse(verb, name):
            raise subprocess.CalledProcessError(2, ["sudo"], "", "ccc-tidepool: a Dive is using alice's "
                                                                 "Tidepool for its GPU — end it first\n")
        monkeypatch.setattr(head_tp, "act", refuse)
        r = _client(monkeypatch).post("/api/tidepool/restart")
        assert r.status_code == 409 and r.json()["detail"].startswith("a Dive is using")


# --- environment build dispatch ----------------------------------------------------

def _build_script() -> str:
    return D.DIVE_INIT.split("sh -c '", 1)[1].split("' _ ", 1)[0]


@pytest.mark.parametrize("name,ran", [
    ("pyproject.toml", "uv sync"), ("requirements.txt", "uv venv -q .venv"),
    ("environment.yml", "micromamba create -y -p .venv -f environment.yml"),
    ("conda/env.yaml", "micromamba create -y -p .venv -f conda/env.yaml")])
def test_env_build_dispatch(tmp_path, name, ran):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for tool in ("uv", "micromamba"):
        (bin_ / tool).write_text(f'#!/bin/sh\necho {tool} "$@"\n')
        (bin_ / tool).chmod(0o755)
    r = subprocess.run(["sh", "-c", _build_script(), "_", name], capture_output=True, text=True,
                       env={"PATH": f"{bin_}:/usr/bin:/bin"}, cwd=tmp_path)
    assert r.returncode == 0 and ran in r.stdout


def test_env_build_refuses_unknown(tmp_path):
    r = subprocess.run(["sh", "-c", _build_script(), "_", "setup.cfg"], capture_output=True, text=True)
    assert r.returncode == 1 and "environment.yml" in r.stdout


def test_conda_prefix_activates_with_micromamba():
    assert 'micromamba activate "$CCC_DIVE_VENV"' in D.DIVE_INIT


class TestDashboardSettings:
    def test_settings_use_the_signed_in_users_tidepool_without_preparing(self, conn_db, monkeypatch):
        """Browsing saved choices must not queue work or read another User's settings."""
        calls = []

        def helper(*args, **kw):
            calls.append((args, kw))
            return {"main": "proj", "toml": 'time = "4h"\nvram = 16\ndeps = ["data"]\n'}

        monkeypatch.setattr(D, "helper", helper)
        c = _client(monkeypatch)
        r = c.get("/api/dives/settings?main=proj&user=bob")
        assert r.status_code == 200
        assert r.json() == {"main": "proj", "settings": {"time": "4h", "vram": 16, "deps": ["data"]}}
        assert calls == [(("dive-settings", "alice", "proj"), {})]
        with db.connect() as conn:
            assert db.open_dives(conn) == []

    def test_no_saved_settings_leaves_required_choices_absent(self, conn_db, monkeypatch):
        """A new folder must not silently reserve a guessed duration or amount of VRAM."""
        monkeypatch.setattr(D, "helper", lambda *a, **kw: {"main": "new", "toml": None})
        r = _client(monkeypatch).get("/api/dives/settings?main=new")
        assert r.status_code == 200 and r.json() == {"main": "new", "settings": {}}

    @pytest.mark.parametrize("toml", ['time = ', 'vram = "sixteen"', 'gpus = true',
                                      'deps = "data"', 'features = [7]', 'time = "15d"',
                                      'time = false', 'env = 1'])
    def test_invalid_saved_settings_are_explained(self, conn_db, monkeypatch, toml):
        """Bad saved choices must not be replaced by defaults and then persisted over the original."""
        monkeypatch.setattr(D, "helper", lambda *a, **kw: {"main": "proj", "toml": toml})
        r = _client(monkeypatch).get("/api/dives/settings?main=proj")
        assert r.status_code == 422

    def test_locked_folder_is_explained(self, conn_db, monkeypatch):
        """An active main folder must not offer a second writer through the creation form."""
        _seed()
        monkeypatch.setattr(D, "helper", lambda *a, **kw: {"main": "proj", "toml": None})
        r = _client(monkeypatch).get("/api/dives/settings?main=proj")
        assert r.status_code == 409 and "held by Dive 77" in r.json()["detail"]

    def test_helper_refusal_reaches_the_form(self, conn_db, monkeypatch):
        """A refused folder must not become a fresh configuration and be submitted accidentally."""
        monkeypatch.setattr(D.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
            a, 2, "", "ccc-tidepool: not a folder in your Tidepool"))
        r = _client(monkeypatch).get("/api/dives/settings?main=missing")
        assert r.status_code == 422 and "not a folder" in r.json()["detail"]

    def test_browser_changes_and_cleared_options_are_saved(self, conn_db, monkeypatch):
        """Removing saved options must not retain an expensive GPU or software requirement."""
        h = FakeHead(monkeypatch, prep={**PREP, "toml": D.dump_toml({
            "time": "4h", "vram": 16, "gpus": 2, "deps": ["data"],
            "env": "pyproject.toml", "features": ["vsim"],
        })})
        r = _client(monkeypatch).post("/api/dives", json={"main": "proj", "time": "90m",
            "vram": 8, "gpus": 1, "deps": [], "env": "", "features": []})
        assert r.status_code == 200 and r.json()["id"] == 77
        opened = next(body for verb, body in h.calls if verb == "dive-open")
        assert D.load_toml(opened["toml"]) == {"time": "90m", "vram": 8, "gpus": 1}

    def test_settings_require_authentication(self, conn_db, monkeypatch):
        """Private folder configuration must not be exposed to an unsigned browser."""
        monkeypatch.setattr(D, "helper", lambda *a, **kw: pytest.fail("unauthenticated helper call"))
        r = TestClient(head_main.app).get("/api/dives/settings?main=proj")
        assert r.status_code == 401
