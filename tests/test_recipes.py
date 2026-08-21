"""Recipes and the 32GB wall (docs/TODO.md "Storage retention: the Recipe wall") — the third and last of
the Head's permanent consumers of disk.

What's tested here: that a save freezes code AND settings, that the wall is a hard refusal rather than
the eviction `retention.py` does, that an update is delete-then-save (charged what it grows by, and
harmless to the old snapshot when refused), that bytes are counted uncompressed so the Head and the
composer agree, that a shelf is private to its owner, that a deleted User's shelf is collected, that
running a Recipe hands `spawns.submit_checked` a copy of the stored snapshot rather than the snapshot
itself, and that a live Spawn from a Recipe is what makes `running` true on the shelf.
"""

import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db, recipes, slurm
from ccc_head_api import main as head_main
from ccc_head_api import spawns as head_spawns

_META = {"name": "resnet", "script": "train.py", "args": ["--lr", "1e-3"], "outputs": ["checkpoints"],
         "gpus": 1, "vram_mb": 24576, "minutes": 120,
         "wandb": True, "wandb_project": "ccc", "wandb_entity": "me",
         # A Recipe freezes the **Environment** its work runs in, not just the code, so naming one is
         # required to save at all — see test_saving_without_a_manifest_is_refused.
         "manifest": "requirements.txt"}


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
    return _targz({"train.py": b"print('hi')\n", "requirements.txt": b"",
                   "data.bin": b"x" * payload_bytes})


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

    @pytest.mark.parametrize("out", ["../escape", ".hidden", "a/../b", ""])
    def test_an_output_path_a_spawn_would_reject_is_refused_here_too(self, out):
        """The same `output_plan` a submit runs, so what freezes here is exactly what will pass there."""
        with pytest.raises(ValueError, match="invalid output path"):
            self._save(outputs=[out])

    @pytest.mark.parametrize("out", ["runs", "out/ckpt", "/ccc/vlwd/logs"])
    def test_a_nested_or_absolute_output_path_freezes_fine(self, out):
        """An output is a PATH now — a nested one, or a `/`-prefixed absolute one for what an Arm
        Feature writes outside the project. `has/slash` used to be a rejection here."""
        self._save(outputs=[out])

    def test_two_outputs_that_would_be_kept_in_one_place_are_refused(self):
        """`out/x` and `/out/x` are different directories in the Pod and one directory on the Head, so
        one would silently overwrite the other in the tar."""
        with pytest.raises(ValueError, match="would both be kept"):
            self._save(outputs=["out/x", "/out/x"])

    def test_an_entrypoint_not_in_the_upload_is_refused_before_anything_is_written(self):
        """A Recipe frozen around a missing script fails every time it is ever run — discovered long
        after the folder that would have fixed it is gone."""
        with pytest.raises(ValueError, match="was not found in the uploaded project"):
            self._save(tar=_targz({"other.py": b"x"}))
        assert not recipes._dir(1, "resnet").exists()

    def test_manifest_and_features_freeze_with_the_code(self):
        """The two declared-need fields `/submit` already carried that a Recipe couldn't freeze until
        now. `features` is structural only, same as vram_mb/gpus: never checked against the fleet here.
        `manifest` is checked against the upload, because a Recipe pointed at a file it has not got is
        one that fails every time it is ever run."""
        saved = self._save(meta={"manifest": "requirements.txt", "features": ["vlearn"]})
        assert saved["manifest"] == "requirements.txt"
        assert saved["features"] == ["vlearn"]

    def test_features_default_to_unset(self):
        """Unlike `manifest`: most Recipes require no Arm Feature, and every Recipe has an
        Environment."""
        saved = self._save()
        assert saved["features"] == []

    def test_saving_without_a_manifest_is_refused(self):
        """A Recipe captures its **Environment** as well as its code (CONTEXT.md, **Recipe**), so one
        saved without a manifest could never be run — the same defect an empty `outputs` is refused
        for, caught at save time rather than the day it is needed."""
        for nothing in (None, ""):
            with pytest.raises(ValueError, match="manifest"):
                self._save(manifest=nothing)
        assert not recipes._dir(1, "resnet").exists()

    def test_a_manifest_not_in_the_upload_is_refused_before_anything_is_written(self):
        """Same check the entrypoint gets, and for the same reason. A `.gitignore` that drops the
        dependency file is exactly how this happens."""
        with pytest.raises(ValueError, match="manifest 'requirements.txt' was not found"):
            self._save(tar=_targz({"train.py": b"x"}))
        assert not recipes._dir(1, "resnet").exists()

    def test_features_must_be_a_list_of_strings(self):
        with pytest.raises(ValueError, match="'features'"):
            self._save(meta={"features": "vlearn"})


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
        self._save(tar=_targz({"train.py": b"new code\n", "requirements.txt": b""}), replacing="resnet")
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
        self._save(tar=_targz({"train.py": b"original\n", "requirements.txt": b"", "data.bin": b"x" * 8_000}))
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

def _client(monkeypatch, tmp_path, user_id=1, is_admin=False) -> TestClient:
    user = {"id": user_id, "username": "alice", "is_admin": is_admin, "must_change_password": False, "dek": None}
    head_main.app.dependency_overrides[head_main.current_user] = lambda: user
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        db.add_user(conn, "alice")
        db.add_user(conn, "bob")
    monkeypatch.setattr(head_spawns, "HEAD_STATE_DIR", tmp_path)
    # Only touched by the run/running tests below — an empty queue and one eligible Arm, so
    # submit_checked's placement/pricing checks have something to chew on without a real Slurm or
    # Arm registry file.
    monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box"}])
    monkeypatch.setattr(slurm, "queue", lambda: [])
    monkeypatch.setattr(slurm, "vram_constraint", lambda gb, gpus=1: f"vram{gb}")
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
        r = _post(c, "/api/recipes/resnet/update", _targz({"train.py": b"v2\n", "requirements.txt": b""}))
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

    def test_a_bad_entrypoint_is_422_not_413(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert _post(c, "/api/recipes", _targz({"other.py": b"x"})).status_code == 422

    def test_the_routes_need_a_session(self, tmp_path):
        head_main.app.dependency_overrides.clear()
        assert TestClient(head_main.app).get("/api/recipes").status_code == 401


class TestRunRoute:
    """`spawns.submit` is stubbed throughout — placement/pricing/staging are `submit_checked`'s and
    `submit`'s own tests (test_submit.py), not this route's. What belongs here is that `recipes.run`
    hands it the right things: a COPY of the stored snapshot (never the snapshot itself) and the frozen
    settings, nothing re-asked."""

    def test_run_spawns_a_job_from_the_stored_snapshot(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, "/api/recipes", _project())
        calls = []
        monkeypatch.setattr(head_spawns, "submit", lambda *a, **kw: (calls.append(kw), 4242)[1])
        r = c.post("/api/recipes/resnet/run")
        assert r.status_code == 200, r.text
        assert r.json() == {"id": 4242}
        assert calls[0]["recipe_name"] == "resnet"
        assert calls[0]["source_dir"] == recipes._dir(1, "resnet")

    def test_run_carries_the_frozen_settings_not_new_ones(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, "/api/recipes", _project(), gpus=2, vram_mb=49152, minutes=45,
             features=["vlearn"], manifest="requirements.txt")
        monkeypatch.setattr(slurm, "arms_with_features", lambda names: [{"name": "gpu-box"}])
        captured = {}

        def fake_submit(user_id, script, gpu, args, wandb_env, tar_path, minutes, constraint, outputs,
                        node=None, **kw):
            captured.update(script=script, gpu=gpu, minutes=minutes, outputs=outputs, **kw)
            return 99

        monkeypatch.setattr(head_spawns, "submit", fake_submit)
        r = c.post("/api/recipes/resnet/run")
        assert r.status_code == 200, r.text
        assert (captured["script"], captured["gpu"], captured["minutes"]) == ("train.py", 2, 45)
        assert captured["outputs"] == ["checkpoints"]
        assert captured["features"] == ["vlearn"]
        assert captured["manifest"] == "requirements.txt"
        # The Spawn's **Name** is INHERITED from the Recipe, never asked for again: a person chose it
        # when they saved the Recipe, so the "every Spawn is named by a human" rule is already met and
        # `POST /recipes/{name}/run` stays a call that sends nothing but the name.
        assert captured["name"] == "resnet"

    def test_running_a_missing_recipe_404s(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert c.post("/api/recipes/ghost/run").status_code == 404

    def test_a_request_submit_checked_refuses_is_422(self, monkeypatch, tmp_path):
        """The concurrent-Spawn cap and an unsatisfiable feature request are real ways `submit_checked`
        can refuse a run — surfaced as a 422, the same as any other Spawn's."""
        c = _client(monkeypatch, tmp_path)
        _post(c, "/api/recipes", _project(), features=["vsim"])
        monkeypatch.setattr(slurm, "arms_with_features", lambda names: [])
        monkeypatch.setattr(slurm, "all_feature_names", lambda: [])
        r = c.post("/api/recipes/resnet/run")
        assert r.status_code == 422


class TestRunningStatus:
    """`running` was always a stub before `/run` existed. Now it reads `db.running_recipe_names` against
    the live Slurm queue — proven here at the db/route seam, independent of how a Spawn got its
    `recipe_name` (that plumbing is `recipes.run`'s own tests above, and `submit()`'s in test_submit.py)."""

    def test_a_live_spawn_from_this_recipe_makes_it_running(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, "/api/recipes", _project())
        with db.connect() as conn:
            db.record_spawn(conn, 777, 1, "train.py", "uuid-1", recipe_name="resnet")
        monkeypatch.setattr(slurm, "queue", lambda: [{"id": 777}])
        assert c.get("/api/recipes").json()[0]["running"] is True

    def test_no_live_spawn_means_not_running(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, "/api/recipes", _project())
        with db.connect() as conn:
            db.record_spawn(conn, 777, 1, "train.py", "uuid-1", recipe_name="resnet")
        monkeypatch.setattr(slurm, "queue", lambda: [])  # already settled — off the queue
        assert c.get("/api/recipes").json()[0]["running"] is False

    def test_another_users_spawn_never_marks_it_running(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, "/api/recipes", _project())
        with db.connect() as conn:
            db.record_spawn(conn, 777, 2, "train.py", "uuid-1", recipe_name="resnet")  # bob's
        monkeypatch.setattr(slurm, "queue", lambda: [{"id": 777}])
        assert c.get("/api/recipes").json()[0]["running"] is False


def _spool_body(body: bytes):
    import tempfile
    from pathlib import Path
    with tempfile.NamedTemporaryFile("wb", suffix=".tar.gz", delete=False) as f:
        f.write(body)
        return Path(f.name)


class TestTheGitHubKeyIsFrozenToo:
    """A Recipe remembers whether its runs carry the owner's **GitHub key**, exactly as it remembers
    whether they log to W&B.

    Not a per-run question, because a Recipe is durable config: "run this without my key" is part of
    what the Recipe IS. The flag a Spawn passes once has to survive being saved, or the Composer would
    offer a choice that silently stopped meaning anything the moment the work was frozen."""

    def test_a_recipe_defaults_to_carrying_it(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert _post(c, "/api/recipes", _project()).json()["git"] is True

    def test_a_recipe_can_decline_it(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert _post(c, "/api/recipes", _project(), git=False).json()["git"] is False

    def test_the_choice_reaches_the_spawn_it_runs(self, monkeypatch, tmp_path):
        """The whole point of freezing it — a stored `false` that never arrived at `submit_checked`
        would be a setting the Dashboard shows and the cluster ignores."""
        c = _client(monkeypatch, tmp_path)
        _post(c, "/api/recipes", _project(), git=False)
        calls = []
        monkeypatch.setattr(head_spawns, "submit", lambda *a, **kw: (calls.append(kw), 1)[1])
        assert c.post("/api/recipes/resnet/run").status_code == 200
        assert calls[0]["git_key"] == ""

    def test_a_recipe_frozen_before_this_existed_carries_it(self, monkeypatch, tmp_path):
        """`DEFAULT 1` on the migration, and an absent `git` in the meta reads the same way — the
        behaviour those Recipes already had, and the same answer `/submit` gives a client that has
        never heard of the flag."""
        c = _client(monkeypatch, tmp_path)
        _post(c, "/api/recipes", _project())
        with db.connect() as conn:
            assert db.get_recipe(conn, 1, "resnet")["git"] == 1

    def test_editing_settings_keeps_a_declined_key_declined(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, "/api/recipes", _project(), git=False)
        body = {**_META, "git": False, "outputs": ["out"]}
        assert c.patch("/api/recipes/resnet", json=body).json()["git"] is False
