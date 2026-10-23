"""The Dashboard's file browser: the helper's `fs` verb (podman exec as the User, argv only) and the
/api/files routes over it — listing with Dive locks, streamed download/zip, upload with clash 409."""

import subprocess

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db
from ccc_head_api import files as F
from ccc_head_api import main as head_main
from cuttle import ccc_tidepool as H
from cuttle import templates

ALICE = {"username": "alice", "is_admin": False, "must_change_password": False, "id": 1,
         "scope": "full"}


class TestTheHelperVerb:
    def test_ops_run_as_the_user_in_their_container(self):
        a = H.fs_argv("alice", "list", ["proj"])
        i = a.index("exec")
        assert a[i + 1:i + 6] == ["-u", "alice", "-w", "/home/alice", "tp-alice"]
        assert a[i + 6:i + 8] == ["find", "/home/alice/proj"]

    def test_put_reads_stdin_and_names_the_path_as_an_argument(self):
        a = H.fs_argv("alice", "put", ["a b/$(x).txt"])
        assert "-i" in a and a[-1] == "/home/alice/a b/$(x).txt" and a[-2] == "_"

    @pytest.mark.parametrize("bad", ["/etc", "../x", "a/../b", "a//b", "x\0y"])
    def test_escapes_are_refused(self, bad):
        with pytest.raises(H.Refused):
            H.fs_path("alice", bad)

    def test_home_is_dot_and_is_never_deleted_or_renamed(self):
        assert H.fs_path("alice", ".") == "/home/alice"
        for op, rels in (("delete", ["."]), ("rename", [".", "x"])):
            with pytest.raises(H.Refused):
                H.fs_argv("alice", op, rels)

    def test_unknown_ops_are_refused(self):
        with pytest.raises(H.Refused):
            H.fs_argv("alice", "chmod", ["x"])

    def test_sudoers_lets_the_api_call_exactly_these(self):
        rule = next(ln for ln in templates.render_tidepool_sudoers().splitlines() if " ^fs " in ln)
        for op in H.FS_OPS:
            assert op in rule


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        conn.execute("INSERT INTO users (id, username, is_admin, pw_hash) VALUES (1,'alice',0,'x')")
        conn.commit()
        db.record_spawn(conn, 77, 1, "", minutes=60, gpus=1, tidepool=True)
        db.create_dive(conn, 77, "a" * 32, "proj", [], 31000)
    calls = []
    files = {"proj": "d d", "data": "d d", "notes.txt": "f f", "proj/x": "f f"}

    def run(argv, **kw):
        op, rels = argv[5], argv[6:]
        calls.append((op, *rels))
        if op == "stat":
            got = files.get(rels[0])
            return subprocess.CompletedProcess(argv, 0 if got else 1, got or "", "")
        if op == "list":
            out = b"d\x004096\x001.5\x00proj\x00f\x003\x002.5\x00notes.txt\x00"
            return subprocess.CompletedProcess(argv, 0, out, b"")
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(F.subprocess, "run", run)
    head_main.app.dependency_overrides[head_main.current_user] = lambda: ALICE
    yield TestClient(head_main.app), calls
    head_main.app.dependency_overrides.clear()


class TestTheRoutes:
    def test_listing_dirs_first_with_the_dive_lock(self, api):
        c, _ = api
        r = c.get("/api/files").json()
        assert [(e["name"], e["type"], e["locked"], e["size"]) for e in r["entries"]] == \
            [("proj", "dir", True, None), ("notes.txt", "file", False, 3)]

    def test_writes_into_a_locked_main_are_refused(self, api):
        c, calls = api
        assert c.post("/api/files/mkdir", json={"path": "proj/new"}).status_code == 423
        assert c.delete("/api/files", params={"path": "proj/x"}).status_code == 423
        assert c.put("/api/files", params={"path": "proj/y"}, content=b"x").status_code == 423
        assert not [x for x in calls if x[0] in ("mkdir", "delete", "put")]

    def test_deleting_a_folder_holding_a_locked_main_is_refused(self, api, monkeypatch):
        c, _ = api
        with db.connect() as conn:
            conn.execute("UPDATE dives SET main='work/proj'")
            conn.commit()
        assert c.delete("/api/files", params={"path": "work"}).status_code == 423

    def test_delete_and_mkdir_go_to_the_helper(self, api):
        c, calls = api
        assert c.delete("/api/files", params={"path": "notes.txt"}).status_code == 204
        assert c.post("/api/files/mkdir", json={"path": "data/new"}).status_code == 204
        assert ("delete", "notes.txt") in calls and ("mkdir", "data/new") in calls

    def test_rename_onto_something_refuses(self, api):
        c, _ = api
        r = c.post("/api/files/rename", json={"path": "notes.txt", "to": "data"})
        assert r.status_code == 409

    def test_escapes_are_422(self, api):
        c, _ = api
        assert c.get("/api/files", params={"path": "../etc"}).status_code == 422

    def test_upload_clash_is_409_unless_overwrite(self, api, monkeypatch):
        c, _ = api
        assert c.put("/api/files", params={"path": "notes.txt"}, content=b"new").status_code == 409
        got = {}

        async def fake_exec(*argv, **kw):
            class P:
                class stdin:
                    @staticmethod
                    def write(b): got.setdefault("body", b"")
                    @staticmethod
                    async def drain(): pass
                    @staticmethod
                    def close(): pass

                class stderr:
                    @staticmethod
                    async def read(): return b""

                @staticmethod
                async def wait(): return 0
            got["argv"] = argv
            return P
        monkeypatch.setattr(head_main.asyncio, "create_subprocess_exec", fake_exec)
        r = c.put("/api/files", params={"path": "notes.txt", "overwrite": "true"}, content=b"new")
        assert r.status_code == 204 and got["argv"][-3:] == ("alice", "put", "notes.txt")

    def test_a_folder_downloads_as_a_zip(self, api, monkeypatch):
        c, _ = api
        seen = []
        monkeypatch.setattr(F, "stream", lambda u, op, rel: seen.append(op) or iter([b"PK"]))
        r = c.get("/api/files/download", params={"path": "proj"})
        assert r.status_code == 200 and seen == ["zip"] and "proj.zip" in r.headers["content-disposition"]
        r = c.get("/api/files/download", params={"path": "notes.txt"})
        assert seen == ["zip", "get"] and r.content == b"PK"

    def test_missing_is_404(self, api):
        c, _ = api
        assert c.get("/api/files/download", params={"path": "nope"}).status_code == 404
