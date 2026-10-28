"""`cuttle` inside the containers: the in-container env, and the **ambient session** that lets it talk
to the Head with no login — narrow (no Admin, read-only but for the caller's own Dives), minted per
Tidepool (rotated at each start) and per Dive (revoked at its close)."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db
from ccc_head_api import main as head_main
from ccc_head_api import tidepool as head_tp
from cuttle import api, auth, ccc_tidepool as H, config, incontainer, settings, tls

from conftest import USERS


def _ambient(head, scope="tidepool", user="root"):
    with db.connect() as conn:
        uid = conn.execute("SELECT id FROM users WHERE username=?", (user,)).fetchone()[0]
        return db.create_session(conn, uid, scope=scope)


def _full(head, user="root"):
    return head.post("/login", json={"username": user, "password": USERS[user]}).json()["token"]


class TestTheScope:
    def test_it_reads(self, head):
        r = head.get("/me", headers={"Authorization": f"Bearer {_ambient(head)}"})
        assert r.status_code == 200 and r.json()["username"] == "root"

    def test_it_never_carries_admin(self, head):
        assert head.get("/me", headers={"Authorization": f"Bearer {_ambient(head)}"}).json()["is_admin"] is False
        assert head.get("/me", headers={"Authorization": f"Bearer {_full(head)}"}).json()["is_admin"] is True

    def test_it_cannot_act_outside_its_own_dives(self, head):
        h = {"Authorization": f"Bearer {_ambient(head)}"}
        assert head.post("/passwd", json={"old": "toor", "new": "a-much-longer-pw-1"}, headers=h).status_code == 403
        assert head.post("/ssh-keys", json={"pubkey": "ssh-ed25519 AAAA x"}, headers=h).status_code == 403
        assert head.post("/tidepool/reset", headers=h).status_code == 403

    def test_its_dive_routes_take_it(self):
        deps = {r.path: [d.call for d in r.dependant.dependencies] for r in head_main.api.routes
                if r.path in ("/api/dives", "/api/dives/{job_id}") and "GET" not in r.methods}
        assert len(deps) == 2 and all(head_main.ambient_user in d for d in deps.values())

    def test_logout_cannot_revoke_it(self, head):
        tok = _ambient(head)
        head.post("/logout", headers={"Authorization": f"Bearer {tok}"})
        assert head.get("/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


class TestTheTidepoolSession:
    @pytest.fixture
    def keys(self, head, monkeypatch, tmp_path):
        monkeypatch.setattr(head_tp, "KEYS_ROOT", tmp_path)
        (tmp_path / "alice").mkdir()
        return tmp_path / "alice"

    def _sync(self):
        with db.connect() as conn:
            head_tp.sync_sessions(conn)

    def test_minted_into_the_keys_dir_owner_and_group_only(self, keys):
        self._sync()
        f = keys / "session"
        assert f.stat().st_mode & 0o777 == 0o640
        with db.connect() as conn:
            assert db.lookup_session(conn, f.read_text().strip())["session_scope"] == "tidepool"

    def test_a_fresh_one_is_kept(self, keys):
        self._sync()
        first = (keys / "session").read_text()
        self._sync()
        assert (keys / "session").read_text() == first

    def test_a_restart_rotates_it(self, keys):
        self._sync()
        old = (keys / "session").read_text().strip()
        (keys / "session").unlink()           # what the helper's `started` does
        self._sync()
        with db.connect() as conn:
            assert db.lookup_session(conn, old) is None
            assert db.lookup_session(conn, (keys / "session").read_text().strip())

    def test_no_tidepool_no_session(self, keys):
        self._sync()
        assert not (keys.parent / "root" / "session").exists()

    def test_the_helper_deletes_it_at_start(self, monkeypatch, tmp_path):
        monkeypatch.setattr(H, "KEYS_ROOT", tmp_path)
        (tmp_path / "bob").mkdir()
        (tmp_path / "bob" / "session").write_text("x")
        monkeypatch.setattr(H, "_ctr_pid", lambda u, wait_s=0: None)
        H.started("bob")
        assert not (tmp_path / "bob" / "session").exists()


class TestCuttleInside:
    @pytest.fixture
    def inside(self, ccc_home, monkeypatch, tmp_path):
        sess = tmp_path / "keys-session"
        sess.write_text("ambient-token\n")
        ca = tmp_path / "ca.crt"
        ca.write_text("pem")
        monkeypatch.setattr(config, "AMBIENT_SESSIONS", (sess,))
        monkeypatch.setattr(config, "CONTAINER_CA", ca)
        return sess

    def test_the_ambient_token_is_used_without_a_login(self, inside):
        assert auth._load_token() == "ambient-token"

    def test_a_login_inside_wins(self, inside):
        auth._save_token("mine")
        assert auth._load_token() == "mine"

    def test_the_head_is_the_cluster_name(self, inside):
        settings.save({})
        assert api._head_url() == "https://ccc.lab"

    def test_the_mounted_ca_is_trusted(self, inside, monkeypatch):
        seen = []
        monkeypatch.setattr(tls.ssl, "create_default_context", lambda cafile: seen.append(cafile))
        tls.context("https://ccc.lab")
        assert seen == [str(config.CONTAINER_CA)]

    def test_outside_nothing_changes(self, ccc_home, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "AMBIENT_SESSIONS", (tmp_path / "absent",))
        assert config.ambient_session() is None
        with pytest.raises(Exception, match="not logged in"):
            auth._load_token()


class TestTheEnv:
    def test_deps_pinned_to_this_install_without_cuttle(self):
        reqs = incontainer.requirements().splitlines()
        names = {r.split("==")[0] for r in reqs}
        assert "typer" in names and "httpx" in names and "cuttle" not in names
        assert not names & {"ccc-head-api", "pytest", "fastapi"}   # dev-venv extras, not cuttle's deps
        assert all("==" in r for r in reqs)

    def test_built_in_a_base_container_then_swapped_in_place(self, monkeypatch):
        ran = []
        monkeypatch.setattr(incontainer, "run", lambda argv, **kw: ran.append(argv))
        incontainer.build("ccc.lab:5000/ccc/base@sha256:" + "a" * 64)
        dest = str(config.CONTAINER_CUTTLE)
        pod = next(a for a in ran if a[0] == "podman")
        assert "ccc.lab:5000/ccc/base@sha256:" + "a" * 64 in pod and f"{dest}.new:{dest}" in pod
        assert ["rsync", "-a", "--delete", f"{dest}.new/", f"{dest}/"] in ran
        assert not any(a[0] == "mv" for a in ran)    # a bind mount pins the inode

    def test_the_script_puts_only_cuttle_on_path(self):
        sh = incontainer.build_script(Path("/usr/local/lib/ccc/cuttle"))
        assert "/usr/local/lib/ccc/cuttle/path/cuttle" in sh and "--python /usr/bin/python3" in sh

    def test_paths_agree(self):
        assert str(H.CUTTLE_ENV) == str(config.CONTAINER_CUTTLE)
        from cuttle import ccc_launch as L
        assert L.CUTTLE_ENV == str(config.CONTAINER_CUTTLE) and L.HEAD_CA == str(config.HEAD_CA_TRUST_PATH)
        assert head_tp.CLUSTER_NAME_FILE == config.CLUSTER_NAME_FILE
