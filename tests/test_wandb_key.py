"""The per-User W&B key: the Head holds the key, the client owns the run.

Two halves, and the seam between them is the interesting part.

*At rest* — head.db is the one store CCC has that must hold a secret **recoverably** (a W&B key is
handed to `wandb` verbatim inside the Pod, so it cannot be hashed like a password). It is encrypted
under a keyfile beside the database, and every way that can fail — a restored backup with no keyfile,
a blob from a different Head — has to read as "no key", never as an error. A Head that 500s on the
Account page because a restore did what it was designed to do is a Head the restore broke.

*At submit* — the Head COMPOSES the Spawn's W&B environment rather than forwarding the client's. That
is the precedence rule, and the thing most likely to regress: a client-sent WANDB_API_KEY is discarded.
"""

import io
import os
import sqlite3
import tarfile
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import atrest, db, slurm
from ccc_head_api import main as head_main
from ccc_head_api import spawns as head_spawns
from cuttle import ui


@pytest.fixture
def head_db(monkeypatch, tmp_path):
    """An empty head.db holding one User, with its at-rest keyfile beside it. atrest derives the
    keyfile path from db.DB_PATH, so redirecting the database redirects the key with it."""
    monkeypatch.delenv("CCC_HEAD_SECRET_KEY", raising=False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    monkeypatch.setattr(atrest, "_warned", False)  # the once-per-process warning, per-test
    db.ensure_schema()
    with db.connect() as conn:
        conn.execute("INSERT INTO users (id, username, pw_hash) VALUES (1,'alice','x')")
        conn.commit()
    return tmp_path


def _stored(user_id: int = 1) -> str | None:
    with db.connect() as conn:
        return db.get_wandb_key(conn, user_id)


def _store(key: str, user_id: int = 1) -> None:
    with db.connect() as conn:
        db.set_wandb_key(conn, user_id, key)


class TestAtRest:
    def test_round_trip(self, head_db):
        blob = atrest.encrypt("wandb_deadbeef")
        assert blob != b"wandb_deadbeef"
        assert atrest.decrypt(blob) == "wandb_deadbeef"

    def test_the_keyfile_is_minted_0600(self, head_db):
        atrest.encrypt("x")
        keyfile = head_db / "secret.key"
        assert keyfile.stat().st_mode & 0o777 == 0o600
        assert len(keyfile.read_bytes()) == 32

    def test_a_nonce_is_never_reused(self, head_db):
        """GCM's one hard rule. Same plaintext, same key, two different blobs."""
        assert atrest.encrypt("same") != atrest.encrypt("same")

    def test_a_blob_from_another_head_does_not_decrypt(self, head_db):
        """Answers None rather than raising — the caller treats it as unset. This is the copied-db
        case: whoever has the bytes does not have the key that opens them."""
        blob = atrest.encrypt("wandb_deadbeef")
        (head_db / "secret.key").write_bytes(os.urandom(32))
        assert atrest.decrypt(blob) is None

    def test_a_tampered_blob_does_not_decrypt(self, head_db):
        blob = bytearray(atrest.encrypt("wandb_deadbeef"))
        blob[-1] ^= 0xFF  # flip a bit of the GCM tag
        assert atrest.decrypt(bytes(blob)) is None

    def test_the_env_override_moves_the_keyfile(self, head_db, monkeypatch, tmp_path):
        elsewhere = tmp_path / "elsewhere" / "secret.key"
        monkeypatch.setenv("CCC_HEAD_SECRET_KEY", str(elsewhere))
        atrest.encrypt("x")
        assert elsewhere.exists()
        assert not (head_db / "secret.key").exists()


class TestTheStore:
    def test_set_then_get(self, head_db):
        _store("wandb_deadbeef")
        assert _stored() == "wandb_deadbeef"

    def test_unset_is_none(self, head_db):
        assert _stored() is None
        with db.connect() as conn:
            assert db.wandb_key_status(conn, 1) == {"set": False, "last4": None}

    def test_status_masks_to_the_last_four(self, head_db):
        _store("wandb_deadbeef")
        with db.connect() as conn:
            assert db.wandb_key_status(conn, 1) == {"set": True, "last4": "beef"}

    def test_replacing_overwrites(self, head_db):
        _store("first")
        _store("second")
        assert _stored() == "second"

    def test_clearing_forgets_it(self, head_db):
        _store("wandb_deadbeef")
        with db.connect() as conn:
            db.clear_wandb_key(conn, 1)
        assert _stored() is None

    def test_a_stored_key_with_no_keyfile_reads_as_unset(self, head_db):
        """The restored-backup case, and the reason `cuttle backup` leaves the keyfile out: the
        archive is strictly less sensitive than the Head. What comes back is a head.db full of blobs
        nothing can open — which must read as "no key stored", not as a broken Head."""
        _store("wandb_deadbeef")
        (head_db / "secret.key").unlink()
        assert _stored() is None
        with db.connect() as conn:
            assert db.wandb_key_status(conn, 1) == {"set": False, "last4": None}


class TestComposition:
    """Who decides what, at submit. The Head owns the key and the mode; the client owns which W&B run
    its Spawn is (project/entity) and nothing else."""

    def test_no_stored_key_disables_wandb(self, head_db):
        """Explicitly disabled, not merely absent: an empty env leaves WANDB_MODE unset, which makes
        wandb.init() fail inside the Pod rather than skip."""
        assert head_spawns.compose_wandb_env(1, {}) == {"WANDB_MODE": "disabled"}

    def test_a_stored_key_is_injected_online(self, head_db):
        _store("wandb_deadbeef")
        env = head_spawns.compose_wandb_env(1, {})
        assert env["WANDB_API_KEY"] == "wandb_deadbeef"
        assert env["WANDB_MODE"] == "online"

    def test_the_clients_project_and_entity_survive(self, head_db):
        _store("wandb_deadbeef")
        env = head_spawns.compose_wandb_env(1, {"WANDB_PROJECT": "vision", "WANDB_ENTITY": "lab"})
        assert env["WANDB_PROJECT"] == "vision"
        assert env["WANDB_ENTITY"] == "lab"

    def test_the_project_defaults_to_ccc(self, head_db):
        _store("wandb_deadbeef")
        assert head_spawns.compose_wandb_env(1, {})["WANDB_PROJECT"] == "ccc"

    def test_an_absent_entity_stays_absent(self, head_db):
        """An EMPTY WANDB_ENTITY breaks wandb.init instead of falling back to the account default."""
        _store("wandb_deadbeef")
        assert "WANDB_ENTITY" not in head_spawns.compose_wandb_env(1, {})
        assert "WANDB_ENTITY" not in head_spawns.compose_wandb_env(1, {"WANDB_ENTITY": ""})

    def test_a_client_sent_key_is_discarded(self, head_db):
        """The precedence rule. Honouring this would let a submitter log their Spawn into whatever
        account they named — and would resurrect the old behaviour the moment some client sent one."""
        _store("mine")
        env = head_spawns.compose_wandb_env(1, {"WANDB_API_KEY": "theirs", "WANDB_MODE": "online"})
        assert env["WANDB_API_KEY"] == "mine"

    def test_off_disables_wandb_even_with_a_key_stored(self, head_db):
        """The per-Spawn off-switch. A stored key otherwise logs EVERY Spawn a User submits, so "not
        this one" has to be sayable — and it is said as intent (a boolean), never as a WANDB_MODE."""
        _store("wandb_deadbeef")
        env = head_spawns.compose_wandb_env(1, {}, False)
        assert env == {"WANDB_MODE": "disabled"}
        assert "WANDB_API_KEY" not in env  # no reason to hand a Pod a secret it was told not to use

    def test_absent_means_on(self, head_db):
        """Backward compatibility, and the reason the flag defaults true: every client that predates it
        sends nothing, and must keep getting the Spawn it always got."""
        _store("wandb_deadbeef")
        assert head_spawns.compose_wandb_env(1, {}) == head_spawns.compose_wandb_env(1, {}, True)
        assert head_spawns.compose_wandb_env(1, {})["WANDB_API_KEY"] == "wandb_deadbeef"

    def test_off_ignores_a_project_and_entity_sent_alongside(self, head_db):
        """A composer that hides its project/entity fields may still have them filled in from before the
        switch was flipped. Sending them has to be harmless, not half-on."""
        _store("wandb_deadbeef")
        env = head_spawns.compose_wandb_env(1, {"WANDB_PROJECT": "vision", "WANDB_ENTITY": "lab"}, False)
        assert env == {"WANDB_MODE": "disabled"}

    def test_a_client_cannot_enable_wandb_without_a_stored_key(self, head_db):
        """The mirror image: no key stored means disabled, whatever the client claimed."""
        env = head_spawns.compose_wandb_env(1, {"WANDB_API_KEY": "theirs", "WANDB_MODE": "online"})
        assert env == {"WANDB_MODE": "disabled"}


class TestRoutes:
    """The four routes the Dashboard's Account page has been calling all along (`head/web/src/api.ts`),
    which the Head did not serve until now."""

    @pytest.fixture
    def client(self, head_db):
        user = {"id": 1, "username": "alice", "is_admin": False, "must_change_password": False}
        head_main.app.dependency_overrides[head_main.current_user] = lambda: user
        yield TestClient(head_main.app)
        head_main.app.dependency_overrides.clear()

    def test_unset_then_set_then_cleared(self, client):
        assert client.get("/api/account/wandb").json() == {"set": False, "last4": None}
        assert client.post("/api/account/wandb", json={"key": "wandb_deadbeef"}).status_code == 200
        assert client.get("/api/account/wandb").json() == {"set": True, "last4": "beef"}
        assert client.delete("/api/account/wandb").status_code == 204
        assert client.get("/api/account/wandb").json() == {"set": False, "last4": None}

    def test_reveal_returns_the_plaintext(self, client):
        client.post("/api/account/wandb", json={"key": "wandb_deadbeef"})
        assert client.get("/api/account/wandb/reveal").json() == {"key": "wandb_deadbeef"}

    def test_reveal_with_nothing_stored_is_404(self, client):
        assert client.get("/api/account/wandb/reveal").status_code == 404

    def test_a_blank_key_is_refused(self, client):
        """Storing whitespace would report `set: true` and then disable W&B at submit."""
        assert client.post("/api/account/wandb", json={"key": "   "}).status_code == 422
        assert client.get("/api/account/wandb").json()["set"] is False

    def test_the_key_is_never_stored_in_the_clear(self, client, head_db):
        client.post("/api/account/wandb", json={"key": "wandb_deadbeef"})
        assert b"wandb_deadbeef" not in (head_db / "head.db").read_bytes()


class TestTheCliCommands:
    """`cuttle wandb set|show|clear` — the CLI-parity counterpart of the Dashboard's Account page,
    driven end to end through real commands against a real Head (see tests/conftest.py). These use the
    suite's own `head` fixture rather than this module's, so `db.DB_PATH` (and with it the keyfile) is
    the one the logged-in session was minted against."""

    def test_set_show_reveal_clear(self, signed_in):
        assert signed_in("wandb", "set", "wandb_deadbeef").exit_code == 0
        masked = signed_in("wandb", "show")
        assert "beef" in masked.output
        assert "wandb_deadbeef" not in masked.output   # masked means masked
        assert "wandb_deadbeef" in signed_in("wandb", "show", "--reveal").output
        assert signed_in("wandb", "clear").exit_code == 0
        assert "no W&B key stored" in signed_in("wandb", "show").output

    def test_show_with_nothing_stored_says_so(self, signed_in):
        assert "no W&B key stored" in signed_in("wandb", "show").output

    def test_a_cli_spawn_carries_the_stored_key(self, signed_in, fake_slurm, project, monkeypatch):
        """The seam: `cuttle run` sends only the run's project/entity, and the Head fills in the key.
        The CLI no longer holds one at all — before this, a submitter with no ~/.ccc key (and every
        Dashboard user) simply got no metrics."""
        signed_in("wandb", "set", "wandb_deadbeef")
        seen: dict[str, str] = {}
        real = head_spawns.submit

        def spy(user_id, script, gpu, args, wandb_env, *a, **kw):
            seen.update(wandb_env)
            return real(user_id, script, gpu, args, wandb_env, *a, **kw)

        monkeypatch.setattr(head_spawns, "submit", spy)
        r = signed_in("run", "train.py", "--time", "60", "--vram", "24", "--output", "out",
                      "--project", "vision")
        assert r.exit_code == 0, r.output
        assert seen["WANDB_API_KEY"] == "wandb_deadbeef"
        assert seen["WANDB_MODE"] == "online"
        assert seen["WANDB_PROJECT"] == "vision"


class TestThePinnedPath:
    """`cuttle run --node` composes IDENTICALLY to a Spawn that arrived over HTTP.

    It does not travel over HTTP at all (Phase 6 moved pinning Head-local), so it reaches
    `submit_checked` through `ccc_head_api.admin` instead — which passed a hardcoded `{}` for
    wandb_env and threw `--project/--entity` on the floor. Two roads to one submit is only safe while
    they carry the same load.
    """

    @pytest.fixture
    def tar(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("train.py")
            info.size = 0
            tf.addfile(info, io.BytesIO(b""))
        path = tmp_path / "pinned.tar.gz"
        path.write_bytes(buf.getvalue())
        return path

    @pytest.fixture
    def hint(self, monkeypatch):
        """The wandb_env the admin tool hands `submit_checked`. Stubbed at that call rather than at
        sbatch: everything past it is the shared path the HTTP tests above already cover, and what is
        under test here is whether the hint survives the argv at all."""
        captured: dict[str, str] = {}

        def spy(user_id, is_admin, script, gpu, args, wandb_env, *a, **kw):
            captured.update(wandb_env)
            return 4242

        monkeypatch.setattr(head_spawns, "submit_checked", spy)
        return captured

    def test_project_and_entity_reach_the_head(self, head_db, tar, hint, capsys):
        from ccc_head_api import admin

        _store("wandb_deadbeef")
        rc = admin.main(["submit", "--user", "alice", "--script", "train.py", "--tar", str(tar),
                         "--minutes", "60", "--vram", "24", "--node", "gpu-box",
                         "--wandb-env", '{"WANDB_PROJECT": "X", "WANDB_ENTITY": "Y"}'])
        assert rc == 0, capsys.readouterr().err
        env = head_spawns.compose_wandb_env(1, hint)
        assert env["WANDB_PROJECT"] == "X"
        assert env["WANDB_ENTITY"] == "Y"
        # The stored key still wins: the pinned road is not a way to hand the Head a key of your own.
        assert env["WANDB_API_KEY"] == "wandb_deadbeef"

    def test_a_blank_entity_never_reaches_the_head(self, head_db, tar, hint):
        """`--entity ''` is "no entity", and an empty WANDB_ENTITY breaks wandb.init(). The CLI drops
        it before the argv is built (settings.wandb_env), and nothing downstream reinstates it."""
        _store("wandb_deadbeef")
        from ccc_head_api import admin

        admin.main(["submit", "--user", "alice", "--script", "train.py", "--tar", str(tar),
                    "--minutes", "60", "--vram", "24", "--node", "gpu-box",
                    "--wandb-env", '{"WANDB_PROJECT": "X"}'])
        assert "WANDB_ENTITY" not in hint
        assert "WANDB_ENTITY" not in head_spawns.compose_wandb_env(1, hint)

    def test_the_cli_builds_the_same_env_for_both_roads(self, ccc_home, monkeypatch):
        """The symmetry itself, at the seam it broke: whatever `cuttle run` would POST is what a
        pinned Spawn hands the admin tool."""
        from cuttle import settings as cli_settings
        from cuttle import spawns as cli_spawns

        argv: list[str] = []
        monkeypatch.setattr(cli_spawns.auth, "_api", lambda *a, **kw: {"username": "alice"})
        monkeypatch.setattr(cli_spawns.auth, "_admin_argv", lambda a: a)
        monkeypatch.setattr(cli_spawns, "run", lambda a, **kw: argv.extend(a)
                            or types.SimpleNamespace(returncode=0, stdout="4242\n", stderr=""))
        cli_spawns._submit_pinned(b"", "train.py", [], 60, 24, 1, "gpu-box",
                                  cli_settings.wandb_env("X", "Y"), ["out"])
        assert '{"WANDB_PROJECT": "X", "WANDB_ENTITY": "Y"}' in argv
        argv.clear()
        cli_spawns._submit_pinned(b"", "train.py", [], 60, 24, 1, "gpu-box",
                                  cli_settings.wandb_env("X", ""), ["out"])
        assert '{"WANDB_PROJECT": "X"}' in argv


class TestTheSetupPrompt:
    """`cuttle setup`'s W&B prompts write config.yaml, and an answer that is read must be an answer
    that lands. `setdefault` evaluated the prompt and then discarded it whenever a project was already
    configured, so the default could be typed but never changed."""

    def _prompt(self, answers: list[str]) -> dict:
        from cuttle import settings as cli_settings

        ui.install_answers(ui.Answers(*answers))
        try:
            cli_settings.prompt_wandb()
        finally:
            ui.install_answers(None)
        return cli_settings.load().get("wandb", {})

    def test_a_new_project_replaces_the_configured_one(self, ccc_home):
        self._prompt(["old-project", ""])
        assert self._prompt(["new-project", ""])["project"] == "new-project"

    def test_a_blank_entity_clears_a_configured_one(self, ccc_home):
        """Blank means "the account's own default" — so it must be able to UNSET one, not only fail to
        set one. An empty stored entity is not an option: WANDB_ENTITY="" breaks wandb.init()."""
        assert self._prompt(["ccc", "lab"])["entity"] == "lab"
        assert "entity" not in self._prompt(["ccc", ""])


class TestSchemaMigration:
    """A deployed Head runs only the step it is behind on. v7 adds `users.wandb_key`, and it must land
    on a database that already holds Users without disturbing them."""

    def _at_v6(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        for step in db._MIGRATIONS[:6]:
            conn.executescript(step)
        conn.execute("PRAGMA user_version=6")
        conn.execute("INSERT INTO users (username, pw_hash) VALUES ('alice','hashed')")
        conn.commit()
        conn.close()

    def test_v6_upgrades_to_v7_in_place(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        self._at_v6(tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
            row = conn.execute("SELECT username, wandb_key FROM users").fetchone()
        assert row["username"] == "alice"   # the existing User survived
        assert row["wandb_key"] is None     # NULL is unset

    def test_the_upgraded_db_stores_a_key(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        monkeypatch.delenv("CCC_HEAD_SECRET_KEY", raising=False)
        self._at_v6(tmp_path / "head.db")
        db.ensure_schema()
        _store("wandb_deadbeef")
        assert _stored() == "wandb_deadbeef"


class TestItReachesSbatch:
    """The composed env has to arrive in the environment sbatch is launched with — that is how it
    reaches the wrapper's `wenv` line and, from there, the Pod. Everything above is theory until this
    holds."""

    @pytest.fixture
    def sbatch_env(self, head_db, monkeypatch, tmp_path):
        """Submit for real (render, unpack, Attribution row, Escrow) against a captured `sbatch`.
        Returns a dict that fills with the environment the submit handed it."""
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
        # The submit inherits os.environ, so a developer with W&B configured in their own shell would
        # otherwise see their key leak into the assertions below.
        for var in ("WANDB_API_KEY", "WANDB_MODE", "WANDB_PROJECT", "WANDB_ENTITY"):
            monkeypatch.delenv(var, raising=False)
        seen: dict[str, str] = {}

        def fake_run(argv, **kw):
            seen.update(kw["env"])
            return types.SimpleNamespace(stdout="4242\n")

        monkeypatch.setattr(head_spawns.subprocess, "run", fake_run)
        return seen

    def _submit(self, tmp_path, client_env, wandb=True):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo("train.py")
            info.size = 0
            tf.addfile(info, io.BytesIO(b""))
        tar_path = tmp_path / "upload.tar.gz"
        tar_path.write_bytes(buf.getvalue())
        head_spawns.submit_checked(1, False, "train.py", 1, [], client_env, tar_path, 60, 24, ["out"],
                                   wandb=wandb)

    def test_the_stored_key_reaches_sbatch(self, sbatch_env, tmp_path):
        _store("wandb_deadbeef")
        self._submit(tmp_path, {"WANDB_PROJECT": "vision"})
        assert sbatch_env["WANDB_API_KEY"] == "wandb_deadbeef"
        assert sbatch_env["WANDB_MODE"] == "online"
        assert sbatch_env["WANDB_PROJECT"] == "vision"

    def test_without_a_key_sbatch_sees_wandb_disabled(self, sbatch_env, tmp_path):
        self._submit(tmp_path, {})
        assert sbatch_env["WANDB_MODE"] == "disabled"
        assert "WANDB_API_KEY" not in sbatch_env

    def test_a_spawn_submitted_with_wandb_off_carries_no_key(self, sbatch_env, tmp_path):
        """The off-switch through the whole submit, not just the composer: submit_checked is where the
        env is composed, so the pinned road gets this for free."""
        _store("wandb_deadbeef")
        self._submit(tmp_path, {"WANDB_PROJECT": "vision"}, wandb=False)
        assert sbatch_env["WANDB_MODE"] == "disabled"
        assert "WANDB_API_KEY" not in sbatch_env
