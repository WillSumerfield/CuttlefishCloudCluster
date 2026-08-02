"""Recipes and the 24GB wall (docs/TODO.md "Storage retention: the Recipe wall") — the third and last of
the Head's permanent consumers of disk.

What's tested here: that a save freezes code AND settings, that the wall is a hard refusal rather than
the eviction `retention.py` does, that an update is delete-then-save (charged what it grows by, and
harmless to the old snapshot when refused), that bytes are counted uncompressed so the Head and the
composer agree, that a shelf is private to its owner, and that a deleted User's shelf is collected.
NOT tested: running a Recipe — `POST /recipes/{name}/run` is deferred and answers 501, which is asserted.
"""

import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db, recipes
from ccc_head_api import main as head_main
from ccc_head_api import spawns as head_spawns

_META = {"name": "resnet", "script": "train.py", "args": ["--lr", "1e-3"], "outputs": ["checkpoints"],
         "gpus": 1, "vram_mb": 24576, "minutes": 120,
         "wandb": True, "wandb_project": "ccc", "wandb_entity": "me"}


def _targz(files: dict) -> bytes:
    """A gzip-tar the way both clients send one. Deliberately built with INCOMPRESSIBLE-free content
    (long runs of one byte), so the compressed body is far smaller than the members — which is exactly
    what makes `weigh` counting uncompressed bytes observable rather than incidental."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _project(payload_bytes: int = 64) -> bytes:
    return _targz({"train.py": b"print('hi')\n", "data.bin": b"x" * payload_bytes})


class _Base:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        monkeypatch.setattr(head_spawns, "HEAD_STATE_DIR", tmp_path)
        with db.connect() as conn:
            db.add_user(conn, "alice")
            db.add_user(conn, "bob")

    def _save(self, user_id=1, tar=None, meta=None, replacing=None, **over):
        m = {**_META, **(meta or {}), **over}
        path = self._spool(tar if tar is not None else _project())
        with db.connect() as conn:
            return recipes.save(conn, user_id, m, path, replacing=replacing)

    def _spool(self, body: bytes):
        import tempfile
        from pathlib import Path
        with tempfile.NamedTemporaryFile("wb", suffix=".tar.gz", delete=False) as f:
            f.write(body)
            return Path(f.name)


class TestSaving(_Base):
    def test_a_save_freezes_the_code_and_the_settings(self):
        saved = self._save()
        assert saved["name"] == "resnet"
        assert saved["script"] == "train.py"
        assert saved["args"] == ["--lr", "1e-3"]
        assert saved["outputs"] == ["checkpoints"]
        assert (saved["gpus"], saved["vram_mb"], saved["minutes"]) == (1, 24576, 120)
        # Experiment tracking rides with the code rather than being re-answered on every run.
        assert (saved["wandb"], saved["wandb_project"], saved["wandb_entity"]) == (True, "ccc", "me")
        tree = recipes._dir(1, "resnet")
        assert (tree / "train.py").read_bytes() == b"print('hi')\n"

    def test_a_save_runs_nothing(self):
        """The point of Recipes being their own act: no Spawn, no Escrow, no queue place. This is what
        keeps the wall out of the submit path, so a full shelf can never stop a User working."""
        self._save()
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM spawns").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM karma_escrow").fetchone()[0] == 0

    def test_bytes_are_counted_uncompressed(self):
        """The Head must answer the same question the composer asked locally from File.size. A body of
        highly-compressible bytes gzips to a fraction of itself; the allowance charges the whole thing."""
        body = _project(payload_bytes=100_000)
        saved = self._save(tar=body)
        assert len(body) < 10_000                     # the wire cost
        assert saved["size_bytes"] >= 100_000         # what the wall charges

    def test_a_second_recipe_of_the_same_name_is_refused(self):
        self._save()
        with pytest.raises(ValueError, match="already have a recipe named"):
            self._save()

    def test_two_users_may_both_hold_one_name(self):
        """Names are the shelf's key, and a shelf is private — so they are scoped per User, not global."""
        self._save(user_id=1)
        self._save(user_id=2)
        with db.connect() as conn:
            assert len(db.recipes_for(conn, 1)) == 1
            assert len(db.recipes_for(conn, 2)) == 1

    @pytest.mark.parametrize("name", ["../escape", "has/slash", ".hidden", "", "x" * 65])
    def test_a_name_that_is_not_a_directory_component_is_refused(self, name):
        with pytest.raises(ValueError, match="invalid recipe name"):
            self._save(name=name)

    def test_a_recipe_with_no_declared_outputs_is_refused(self):
        """`/submit` requires at least one, so a Recipe frozen without any could never be run — refused
        the day it is saved rather than the day it is reached for."""
        with pytest.raises(ValueError, match="at least one output folder"):
            self._save(outputs=[])

    @pytest.mark.parametrize("out", ["../escape", "has/slash", ".hidden"])
    def test_an_output_name_a_spawn_would_reject_is_refused_here_too(self, out):
        """Same regex as spawns.submit_checked, so what freezes here is exactly what will pass there."""
        with pytest.raises(ValueError, match="invalid output name"):
            self._save(outputs=[out])

    def test_an_entrypoint_not_in_the_upload_is_refused_before_anything_is_written(self):
        """A Recipe frozen around a missing script fails every time it is ever run — discovered long
        after the folder that would have fixed it is gone."""
        with pytest.raises(ValueError, match="was not found in the uploaded project"):
            self._save(tar=_targz({"other.py": b"x"}))
        assert not recipes._dir(1, "resnet").exists()


class TestTheWall(_Base):
    """A hard wall, never an eviction (CONTEXT.md, **Allowance**). `retention.py` always accepts a result
    and silently drops the oldest to fit; a Recipe is a deliberate act with a person waiting on it, so
    here the SAVE is what fails, and their existing Recipes are never touched."""

    def test_a_save_past_the_wall_is_refused(self, monkeypatch):
        monkeypatch.setattr(recipes, "MAX_USER_BYTES", 5_000)
        with pytest.raises(recipes.WallExceeded):
            self._save(tar=_project(payload_bytes=10_000))

    def test_a_refused_save_evicts_nothing_and_writes_nothing(self, monkeypatch):
        monkeypatch.setattr(recipes, "MAX_USER_BYTES", 12_000)
        self._save(name="keeper", tar=_project(payload_bytes=8_000))
        with pytest.raises(recipes.WallExceeded):
            self._save(name="newcomer", tar=_project(payload_bytes=8_000))
        with db.connect() as conn:
            assert [r["name"] for r in db.recipes_for(conn, 1)] == ["keeper"]
        assert recipes._dir(1, "keeper").exists()
        assert not recipes._dir(1, "newcomer").exists()

    def test_the_wall_is_per_user(self):
        """One User's full shelf can never cost another a byte."""
        with db.connect() as conn:
            self._save(user_id=1, tar=_project(payload_bytes=10_000))
            assert db.user_recipe_bytes(conn, 2) == 0

    def test_deleting_frees_the_bytes_back(self):
        self._save(tar=_project(payload_bytes=10_000))
        with db.connect() as conn:
            assert db.user_recipe_bytes(conn, 1) > 10_000
            assert recipes.delete(conn, 1, "resnet")
            assert db.user_recipe_bytes(conn, 1) == 0
        assert not recipes._dir(1, "resnet").exists()


class TestUpdate(_Base):
    """Delete-then-save: the old snapshot is released before the new one is charged, so an update is
    weighed on what it GROWS BY — but nothing on disk moves until the wall has said yes."""

    def test_an_update_re_captures_the_code(self):
        self._save()
        self._save(tar=_targz({"train.py": b"new code\n"}), replacing="resnet")
        assert (recipes._dir(1, "resnet") / "train.py").read_bytes() == b"new code\n"
        with db.connect() as conn:
            assert len(db.recipes_for(conn, 1)) == 1  # replaced in place, never duplicated

    def test_an_update_is_charged_the_delta_not_the_peak(self, monkeypatch):
        """The rule's whole point. At 8k held of a 12k wall, re-capturing that same Recipe at 9k fits —
        it grows by 1k. Charging the peak would need 17k and refuse it."""
        monkeypatch.setattr(recipes, "MAX_USER_BYTES", 12_000)
        self._save(tar=_project(payload_bytes=8_000))
        saved = self._save(tar=_project(payload_bytes=9_000), replacing="resnet")
        assert saved["size_bytes"] > 9_000

    def test_a_refused_update_leaves_the_old_snapshot_intact(self, monkeypatch):
        """The "neither version" case is a crash mid-extract, never a save the Head said no to: the wall
        is weighed before a byte is written, so a refusal costs the owner nothing."""
        monkeypatch.setattr(recipes, "MAX_USER_BYTES", 12_000)
        self._save(tar=_targz({"train.py": b"original\n", "data.bin": b"x" * 8_000}))
        with pytest.raises(recipes.WallExceeded):
            self._save(tar=_project(payload_bytes=20_000), replacing="resnet")
        assert (recipes._dir(1, "resnet") / "train.py").read_bytes() == b"original\n"
        with db.connect() as conn:
            assert len(db.recipes_for(conn, 1)) == 1

    def test_updating_a_recipe_that_does_not_exist_is_a_miss(self):
        with pytest.raises(ValueError, match="no recipe named"):
            self._save(replacing="ghost")


class TestDeletedUsers(_Base):
    """A deleted User's shelf goes with them — outright. Recipes have no TTL to eventually collect them
    the way an expired output has, so without this their code would sit on the Head forever."""

    def test_the_rows_cascade_and_the_sweep_takes_the_files(self):
        self._save(user_id=2)
        tree = recipes._dir(2, "resnet")
        assert tree.exists()
        with db.connect() as conn:
            db.remove_user(conn, "bob")
            assert db.recipes_for(conn, 2) == []   # the CASCADE
            assert recipes.sweep_orphans(conn) == 1
        assert not tree.exists()

    def test_the_sweep_leaves_living_users_alone(self):
        self._save(user_id=1)
        with db.connect() as conn:
            assert recipes.sweep_orphans(conn) == 0
        assert recipes._dir(1, "resnet").exists()

    def test_the_sweep_is_quiet_when_nobody_has_ever_saved_one(self):
        with db.connect() as conn:
            assert recipes.sweep_orphans(conn) == 0


# --- the API ------------------------------------------------------------------
# Same in-process rig test_retention.py uses.

def _client(monkeypatch, tmp_path, user_id=1) -> TestClient:
    user = {"id": user_id, "username": "alice", "is_admin": False, "must_change_password": False}
    head_main.app.dependency_overrides[head_main.current_user] = lambda: user
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        db.add_user(conn, "alice")
        db.add_user(conn, "bob")
    monkeypatch.setattr(head_spawns, "HEAD_STATE_DIR", tmp_path)
    return TestClient(head_main.app)


def _post(c: TestClient, path: str, body: bytes, **meta):
    return c.post(path, content=body, headers={"X-CCC-Meta": json.dumps({**_META, **meta}),
                                               "Content-Type": "application/gzip"})


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


class TestApi:
    def test_save_then_list(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert _post(c, "/api/recipes", _project()).status_code == 201
        r = c.get("/api/recipes")
        assert r.status_code == 200
        assert [x["name"] for x in r.json()] == ["resnet"]

    def test_the_wall_answers_413_and_says_what_to_do(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        monkeypatch.setattr(recipes, "MAX_USER_BYTES", 5_000)
        r = _post(c, "/api/recipes", _project(payload_bytes=10_000))
        assert r.status_code == 413
        # Warn-then-refuse: the remedy here is entirely the User's, so the refusal has to name it.
        assert "delete a recipe" in r.json()["detail"]

    def test_a_shelf_is_private(self, monkeypatch, tmp_path):
        """Bob's Recipe is invisible to Alice, and she cannot delete it by name."""
        c = _client(monkeypatch, tmp_path)
        with db.connect() as conn:
            monkeypatch.setattr(head_spawns, "HEAD_STATE_DIR", tmp_path)
            recipes.save(conn, 2, {**_META, "name": "bobs"}, _spool_body(_project()))
        assert c.get("/api/recipes").json() == []
        assert c.delete("/api/recipes/bobs").status_code == 404

    def test_update_re_captures_over_the_same_name(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, "/api/recipes", _project())
        r = _post(c, "/api/recipes/resnet/update", _targz({"train.py": b"v2\n"}))
        assert r.status_code == 200
        assert (recipes._dir(1, "resnet") / "train.py").read_bytes() == b"v2\n"

    def test_updating_a_missing_recipe_404s(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert _post(c, "/api/recipes/ghost/update", _project()).status_code == 404

    def test_delete_removes_it(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, "/api/recipes", _project())
        assert c.delete("/api/recipes/resnet").status_code == 204
        assert c.get("/api/recipes").json() == []

    def test_deleting_something_that_is_not_there_404s(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert c.delete("/api/recipes/ghost").status_code == 404

    def test_run_is_deferred_and_says_so(self, monkeypatch, tmp_path):
        """Storage and the wall are built; spawning from a stored snapshot is not. A route that 501s is
        discoverable — one that silently does nothing is not."""
        c = _client(monkeypatch, tmp_path)
        _post(c, "/api/recipes", _project())
        r = c.post("/api/recipes/resnet/run")
        assert r.status_code == 501
        assert "saved and safe" in r.json()["detail"]

    def test_a_bad_entrypoint_is_422_not_413(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert _post(c, "/api/recipes", _targz({"other.py": b"x"})).status_code == 422

    def test_the_routes_need_a_session(self, tmp_path):
        head_main.app.dependency_overrides.clear()
        assert TestClient(head_main.app).get("/api/recipes").status_code == 401


def _spool_body(body: bytes):
    import tempfile
    from pathlib import Path
    with tempfile.NamedTemporaryFile("wb", suffix=".tar.gz", delete=False) as f:
        f.write(body)
        return Path(f.name)
