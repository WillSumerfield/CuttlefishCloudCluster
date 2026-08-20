"""**Credentials**: a third-party secret the Head holds for a User, kept under that User's password.

Three halves, and the seams between them are the interesting part.

*The envelope* — head.db is the one store CCC has that must hold a secret **recoverably** (a W&B key
is handed to `wandb` verbatim inside the Pod, so it cannot be hashed like a password). It used to be
encrypted under a keyfile beside the database, which meant the Head could decrypt unattended and
anything that could read one file could read the other. It is now sealed under a DEK that is itself
wrapped twice — under the User's password, and under each live session's raw token — so **a stolen
head.db yields nothing**. The tests below are mostly about the consequences of that: what survives a
password change, what an Admin reset destroys, and what a Spawn with no Session behind it gets.

*At submit* — the Head COMPOSES the Spawn's W&B environment rather than forwarding the client's. That
is the precedence rule, and the thing most likely to regress: a client-sent WANDB_API_KEY is
discarded. It now needs the submitting session's DEK to reach the key at all, and the un-forgeability
falls out of that: a caller holding the wrong DEK gets `disabled`, not somebody else's key.

*Write-only* — no route, command, or component returns the plaintext, ever. Several tests here exist
only to keep that true, because a "just for checking what you pasted" reveal is exactly the sort of
thing that gets added back.
"""

import io
import sqlite3
import tarfile
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import credentials, db, slurm
from ccc_head_api import main as head_main
from ccc_head_api import spawns as head_spawns
from cuttle import ui

PW = "correct horse 7"
HEAD_PW = "hunter2"   # what conftest's shared `head` fixture gives alice


@pytest.fixture
def head_db(monkeypatch, tmp_path):
    """An empty head.db holding one User with a known password, and their DEK already minted.

    Minted through `unlock_dek` rather than by hand, because that is the only way a real DEK ever comes
    into existence — and a fixture that faked it could hide a login that never mints one."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        db.add_user(conn, "alice")
        db.set_password(conn, "alice", PW)
    return tmp_path


@pytest.fixture
def dek(head_db):
    """Alice's DEK, as a login would produce it."""
    with db.connect() as conn:
        return db.unlock_dek(conn, db.get_user(conn, "alice"), PW)


def _stored(dek, user_id: int = 1) -> str | None:
    with db.connect() as conn:
        return db.get_credential(conn, user_id, "wandb", dek)


def _store(key: str, dek, user_id: int = 1) -> None:
    with db.connect() as conn:
        db.set_credential(conn, user_id, "wandb", dek, key)


class TestTheEnvelope:
    """The crypto on its own, before any of it touches a database."""

    def test_a_wrap_round_trips(self):
        dek = credentials.new_dek()
        kek = credentials.password_kek(PW, credentials.new_salt())
        assert credentials.unwrap(kek, credentials.wrap(kek, dek)) == dek

    def test_a_value_round_trips(self):
        dek = credentials.new_dek()
        assert credentials.open_(dek, credentials.seal(dek, "wandb_deadbeef")) == "wandb_deadbeef"

    def test_the_wrong_password_answers_none(self):
        """Not an exception. Every failure mode here has to read as "no Credential" so a Head never
        500s over one — see the module docstring."""
        salt, dek = credentials.new_salt(), credentials.new_dek()
        blob = credentials.wrap(credentials.password_kek(PW, salt), dek)
        assert credentials.unwrap(credentials.password_kek("not it", salt), blob) is None

    def test_a_different_salt_derives_a_different_key(self):
        assert credentials.password_kek(PW, credentials.new_salt()) \
            != credentials.password_kek(PW, credentials.new_salt())

    def test_a_tampered_blob_does_not_open(self):
        """GCM's tag, doing its job: a flipped byte is caught rather than decrypted into garbage."""
        dek = credentials.new_dek()
        blob = bytearray(credentials.seal(dek, "wandb_deadbeef"))
        blob[-1] ^= 1
        assert credentials.open_(dek, bytes(blob)) is None

    def test_a_nonce_is_never_reused(self):
        dek = credentials.new_dek()
        assert credentials.seal(dek, "same") != credentials.seal(dek, "same")

    def test_the_token_kek_is_deterministic_and_token_bound(self):
        assert credentials.token_kek("tok-a") == credentials.token_kek("tok-a")
        assert credentials.token_kek("tok-a") != credentials.token_kek("tok-b")

    def test_no_dek_opens_nothing(self):
        """The single line that makes every session-less caller degrade instead of raise."""
        assert credentials.open_(None, credentials.seal(credentials.new_dek(), "x")) is None

    def test_a_hint_is_the_last_four(self):
        assert credentials.hint_for("wandb", "wandb_deadbeef") == "beef"


class TestTheStore:
    def test_set_then_get(self, head_db, dek):
        _store("wandb_deadbeef", dek)
        assert _stored(dek) == "wandb_deadbeef"

    def test_unset_reads_as_unset(self, head_db, dek):
        assert _stored(dek) is None
        with db.connect() as conn:
            assert db.credential_status(conn, 1, "wandb")["set"] is False

    def test_the_status_carries_the_hint_and_never_the_value(self, head_db, dek):
        _store("wandb_deadbeef", dek)
        with db.connect() as conn:
            status = db.credential_status(conn, 1, "wandb")
        assert status["set"] is True
        assert status["hint"] == "beef"
        assert "wandb_deadbeef" not in repr(status)

    def test_the_status_needs_no_dek(self, head_db, dek):
        """`hint` is deliberately plaintext. This is what lets the Account page render, the Composer
        decide whether to show its W&B fields, and `cuttle run --node` warn — none of which should
        have to hold a key to ask "is one set?"."""
        _store("wandb_deadbeef", dek)
        with db.connect() as conn:
            assert db.credential_status(conn, 1, "wandb")["hint"] == "beef"

    def test_replacing_overwrites_value_and_hint(self, head_db, dek):
        _store("first_aaaa", dek)
        _store("second_bbbb", dek)
        assert _stored(dek) == "second_bbbb"
        with db.connect() as conn:
            assert db.credential_status(conn, 1, "wandb")["hint"] == "bbbb"

    def test_clearing_forgets_it(self, head_db, dek):
        _store("wandb_deadbeef", dek)
        with db.connect() as conn:
            db.clear_credential(conn, 1, "wandb")
        assert _stored(dek) is None

    def test_another_users_dek_does_not_open_it(self, head_db, dek):
        """The un-forgeability property, at the layer that enforces it. Everything above `get_credential`
        gets this for free, which is why the DEK — not the plaintext — is what travels to the composer."""
        _store("wandb_deadbeef", dek)
        assert _stored(credentials.new_dek()) is None

    def test_the_value_is_never_in_the_database_in_the_clear(self, head_db, dek):
        _store("wandb_deadbeef", dek)
        assert b"wandb_deadbeef" not in (head_db / "head.db").read_bytes()

    def test_every_known_kind_is_listed_set_or_not(self, head_db, dek):
        with db.connect() as conn:
            assert [s["kind"] for s in db.credential_statuses(conn, 1)] == list(credentials.KINDS)

    def test_deleting_the_user_takes_them_along(self, head_db, dek):
        _store("wandb_deadbeef", dek)
        with db.connect() as conn:
            conn.execute("DELETE FROM users WHERE id=1")
            conn.commit()
            assert conn.execute("SELECT count(*) FROM user_secrets").fetchone()[0] == 0


class TestTheLifecycle:
    """What a Credential survives and what kills it. This is the whole reason the change exists, so
    each of these is a property rather than a behaviour detail."""

    def test_login_mints_a_dek_and_the_session_carries_it(self, head_db):
        with db.connect() as conn:
            assert db.get_user(conn, "alice")["dek_wrapped_pw"] is None  # nothing yet
            user = db.verify_password(conn, "alice", PW)
            token = db.create_session(conn, user["id"], db.unlock_dek(conn, user, PW))
            row = db.lookup_session(conn, token)
        assert credentials.unwrap(credentials.token_kek(token), row["dek_wrapped_tok"]) is not None

    def test_a_second_login_gets_the_same_dek(self, head_db, dek):
        """Not a fresh one — otherwise every login would strand whatever the last one stored."""
        with db.connect() as conn:
            assert db.unlock_dek(conn, db.get_user(conn, "alice"), PW) == dek

    def test_the_session_wrap_cannot_be_opened_from_the_database(self, head_db, dek):
        """The load-bearing property. `sessions` stores only sha256(token); the raw token lives in the
        client. So the row's own contents plus the whole database still do not open it."""
        with db.connect() as conn:
            user = db.get_user(conn, "alice")
            token = db.create_session(conn, user["id"], dek)
            stored_hash, wrapped = conn.execute(
                "SELECT token_sha256, dek_wrapped_tok FROM sessions").fetchone()
        assert token not in stored_hash
        assert credentials.unwrap(credentials.token_kek(stored_hash), wrapped) is None

    def test_a_password_change_keeps_every_credential(self, head_db, dek):
        _store("wandb_deadbeef", dek)
        with db.connect() as conn:
            db.set_password(conn, "alice", "a new one 9", dek=dek)
            reopened = db.unlock_dek(conn, db.get_user(conn, "alice"), "a new one 9")
        assert reopened == dek
        assert _stored(reopened) == "wandb_deadbeef"

    def test_a_password_change_logs_nobody_out(self, head_db, dek):
        """The same DEK is re-wrapped, so a session minted before the change still opens it. A rotation
        that stranded live sessions would make `passwd` a far bigger act than it is."""
        with db.connect() as conn:
            token = db.create_session(conn, 1, dek)
            db.set_password(conn, "alice", "a new one 9", dek=dek)
            row = db.lookup_session(conn, token)
        assert credentials.unwrap(credentials.token_kek(token), row["dek_wrapped_tok"]) == dek

    def test_an_admin_reset_destroys_them(self, head_db, dek):
        """And there is no way for it not to: both wraps around the DEK die with the password and the
        sessions. An Admin who can reset a password deliberately cannot read what it was keeping."""
        _store("wandb_deadbeef", dek)
        with db.connect() as conn:
            temp = db.reset_password(conn, "alice")
            assert db.credential_status(conn, 1, "wandb")["set"] is False
            fresh = db.unlock_dek(conn, db.get_user(conn, "alice"), temp)
        assert fresh != dek
        assert _stored(fresh) is None

    def test_an_admin_reset_kills_every_session(self, head_db, dek):
        with db.connect() as conn:
            token = db.create_session(conn, 1, dek)
            db.reset_password(conn, "alice")
            assert db.lookup_session(conn, token) is None

    def test_a_wrap_that_will_not_open_mints_a_fresh_dek(self, head_db, dek):
        """The restored-over-a-newer-password case. Answering with a new DEK (and dropping what the old
        one held) beats refusing the login outright — the alternative is a User locked out by a
        Credential they would happily re-paste."""
        _store("wandb_deadbeef", dek)
        with db.connect() as conn:
            conn.execute("UPDATE users SET dek_wrapped_pw=? WHERE id=1", (b"junkjunkjunkjunk",))
            conn.commit()
            fresh = db.unlock_dek(conn, db.get_user(conn, "alice"), PW)
        assert fresh != dek
        assert _stored(fresh) is None
        assert _stored(dek) is None  # cleared, not left as ciphertext nothing can open

    def test_setting_a_password_with_no_dek_clears_the_wrap(self, head_db, dek):
        """Rather than leaving a stale one the new password provably cannot open, which would make the
        next login fail to unwrap and mint anyway — the same outcome, arrived at confusingly."""
        with db.connect() as conn:
            db.set_password(conn, "alice", "a new one 9")
            assert db.get_user(conn, "alice")["dek_wrapped_pw"] is None


class TestComposition:
    """Who decides what, at submit. The Head owns the key and the mode; the client owns which W&B run
    its Spawn is (project/entity) and nothing else."""

    def test_no_stored_key_disables_wandb(self, head_db, dek):
        """Explicitly disabled, not merely absent: an empty env leaves WANDB_MODE unset, which makes
        wandb.init() fail inside the Pod rather than skip."""
        assert head_spawns.compose_wandb_env(1, {}, dek=dek) == {"WANDB_MODE": "disabled"}

    def test_a_stored_key_is_injected_online(self, head_db, dek):
        _store("wandb_deadbeef", dek)
        env = head_spawns.compose_wandb_env(1, {}, dek=dek)
        assert env["WANDB_API_KEY"] == "wandb_deadbeef"
        assert env["WANDB_MODE"] == "online"

    def test_no_dek_means_no_key(self, head_db, dek):
        """A submission with no Session behind it (`cuttle run --node`). Not an error, and not a
        fallback that reaches the key some other way — that fallback is exactly what the change
        removes."""
        _store("wandb_deadbeef", dek)
        assert head_spawns.compose_wandb_env(1, {}, dek=None) == {"WANDB_MODE": "disabled"}

    def test_the_wrong_dek_means_no_key(self, head_db, dek):
        """Un-forgeability by construction: the composer does its own decrypt, so a caller who passed
        someone else's DEK gets `disabled` rather than a key it should not have."""
        _store("wandb_deadbeef", dek)
        assert head_spawns.compose_wandb_env(1, {}, dek=credentials.new_dek()) \
            == {"WANDB_MODE": "disabled"}

    def test_the_clients_project_and_entity_survive(self, head_db, dek):
        _store("wandb_deadbeef", dek)
        env = head_spawns.compose_wandb_env(1, {"WANDB_PROJECT": "vision", "WANDB_ENTITY": "lab"},
                                            dek=dek)
        assert env["WANDB_PROJECT"] == "vision"
        assert env["WANDB_ENTITY"] == "lab"

    def test_the_project_defaults_to_ccc(self, head_db, dek):
        _store("wandb_deadbeef", dek)
        assert head_spawns.compose_wandb_env(1, {}, dek=dek)["WANDB_PROJECT"] == "ccc"

    def test_an_absent_entity_stays_absent(self, head_db, dek):
        """An EMPTY WANDB_ENTITY breaks wandb.init instead of falling back to the account default."""
        _store("wandb_deadbeef", dek)
        assert "WANDB_ENTITY" not in head_spawns.compose_wandb_env(1, {}, dek=dek)
        assert "WANDB_ENTITY" not in head_spawns.compose_wandb_env(1, {"WANDB_ENTITY": ""}, dek=dek)

    def test_a_client_sent_key_is_discarded(self, head_db, dek):
        """The precedence rule. Honouring this would let a submitter log their Spawn into whatever
        account they named, the moment any client sends a key."""
        _store("mine_aaaa", dek)
        env = head_spawns.compose_wandb_env(1, {"WANDB_API_KEY": "theirs", "WANDB_MODE": "online"},
                                            dek=dek)
        assert env["WANDB_API_KEY"] == "mine_aaaa"

    def test_off_disables_wandb_even_with_a_key_stored(self, head_db, dek):
        """The per-Spawn off-switch. A stored key otherwise logs EVERY Spawn a User submits, so "not
        this one" has to be sayable — and it is said as intent (a boolean), never as a WANDB_MODE."""
        _store("wandb_deadbeef", dek)
        env = head_spawns.compose_wandb_env(1, {}, False, dek)
        assert env == {"WANDB_MODE": "disabled"}
        assert "WANDB_API_KEY" not in env  # no reason to hand a Pod a secret it was told not to use

    def test_absent_means_on(self, head_db, dek):
        """Backward compatibility, and the reason the flag defaults true: every client that predates it
        sends nothing, and must keep getting the Spawn it always got."""
        _store("wandb_deadbeef", dek)
        assert head_spawns.compose_wandb_env(1, {}, dek=dek) \
            == head_spawns.compose_wandb_env(1, {}, True, dek)

    def test_off_ignores_a_project_and_entity_sent_alongside(self, head_db, dek):
        """A composer that hides its project/entity fields may still have them filled in from before the
        switch was flipped. Sending them has to be harmless, not half-on."""
        _store("wandb_deadbeef", dek)
        env = head_spawns.compose_wandb_env(1, {"WANDB_PROJECT": "vision", "WANDB_ENTITY": "lab"},
                                            False, dek)
        assert env == {"WANDB_MODE": "disabled"}

    def test_a_client_cannot_enable_wandb_without_a_stored_key(self, head_db, dek):
        """The mirror image: no key stored means disabled, whatever the client claimed."""
        env = head_spawns.compose_wandb_env(1, {"WANDB_API_KEY": "theirs", "WANDB_MODE": "online"},
                                            dek=dek)
        assert env == {"WANDB_MODE": "disabled"}


class TestRoutes:
    """`/account/credentials{,/{kind}}` — keyed by kind rather than named per secret, so adding one is
    a `credentials.KINDS` entry rather than a fourth set of endpoints."""

    @pytest.fixture
    def client(self, head_db, dek):
        user = {"id": 1, "username": "alice", "is_admin": False, "must_change_password": False,
                "dek": dek}
        head_main.app.dependency_overrides[head_main.current_user] = lambda: user
        yield TestClient(head_main.app)
        head_main.app.dependency_overrides.clear()

    def test_unset_then_set_then_cleared(self, client):
        assert client.get("/api/account/credentials/wandb").json()["set"] is False
        assert client.post("/api/account/credentials/wandb",
                           json={"value": "wandb_deadbeef"}).status_code == 200
        got = client.get("/api/account/credentials/wandb").json()
        assert (got["set"], got["hint"]) == (True, "beef")
        assert client.delete("/api/account/credentials/wandb").status_code == 204
        assert client.get("/api/account/credentials/wandb").json()["set"] is False

    def test_the_list_names_every_kind(self, client):
        assert [c["kind"] for c in client.get("/api/account/credentials").json()] \
            == list(credentials.KINDS)

    def test_there_is_no_reveal_route(self, client):
        """The property this whole change rests on, guarded as a test because "just for checking what
        you pasted" is exactly the sort of convenience that gets added back."""
        client.post("/api/account/credentials/wandb", json={"value": "wandb_deadbeef"})
        assert client.get("/api/account/wandb/reveal").status_code == 404
        assert client.get("/api/account/credentials/wandb/reveal").status_code == 404
        assert not any("reveal" in r.path for r in head_main.api.routes)

    def test_no_response_body_ever_carries_the_value(self, client):
        client.post("/api/account/credentials/wandb", json={"value": "wandb_deadbeef"})
        for path in ("/api/account/credentials", "/api/account/credentials/wandb"):
            assert b"wandb_deadbeef" not in client.get(path).content

    def test_an_unknown_kind_is_404(self, client):
        assert client.get("/api/account/credentials/github").status_code == 404
        assert client.post("/api/account/credentials/github", json={"value": "x"}).status_code == 404
        assert client.delete("/api/account/credentials/github").status_code == 404

    def test_a_blank_value_is_refused(self, client):
        """Storing whitespace would report `set: true` and then disable W&B at submit."""
        assert client.post("/api/account/credentials/wandb",
                           json={"value": "   "}).status_code == 422
        assert client.get("/api/account/credentials/wandb").json()["set"] is False

    def test_a_session_with_no_dek_cannot_store_one(self, head_db):
        """Belt and braces on the invariant. Refusing beats storing something under a key nobody can
        reproduce, which would look stored and never work."""
        user = {"id": 1, "username": "alice", "is_admin": False, "must_change_password": False,
                "dek": None}
        head_main.app.dependency_overrides[head_main.current_user] = lambda: user
        try:
            c = TestClient(head_main.app)
            assert c.post("/api/account/credentials/wandb",
                          json={"value": "wandb_deadbeef"}).status_code == 409
        finally:
            head_main.app.dependency_overrides.clear()

    def test_the_value_is_never_stored_in_the_clear(self, client, head_db):
        client.post("/api/account/credentials/wandb", json={"value": "wandb_deadbeef"})
        assert b"wandb_deadbeef" not in (head_db / "head.db").read_bytes()


class TestTheLoginRoute:
    """`/login` is where the envelope is actually opened, so it gets its own end-to-end pass against a
    real Head rather than an overridden dependency."""

    def test_a_key_set_in_one_session_is_readable_in_the_next(self, head):
        first = head.post("/login", json={"username": "alice", "password": HEAD_PW}).json()["token"]
        head.post("/account/credentials/wandb", json={"value": "wandb_deadbeef"},
                  headers={"Authorization": f"Bearer {first}"})
        second = head.post("/login", json={"username": "alice", "password": HEAD_PW}).json()["token"]
        got = head.get("/account/credentials/wandb",
                       headers={"Authorization": f"Bearer {second}"}).json()
        assert (got["set"], got["hint"]) == (True, "beef")

    def test_a_password_change_over_http_keeps_the_key(self, head):
        tok = head.post("/login", json={"username": "alice", "password": HEAD_PW}).json()["token"]
        H = {"Authorization": f"Bearer {tok}"}
        head.post("/account/credentials/wandb", json={"value": "wandb_deadbeef"}, headers=H)
        assert head.post("/passwd", json={"old_password": HEAD_PW,
                                          "new_password": "Brand New 9"}, headers=H).status_code == 200
        assert head.get("/account/credentials/wandb", headers=H).json()["hint"] == "beef"
        later = head.post("/login", json={"username": "alice",
                                          "password": "Brand New 9"}).json()["token"]
        assert head.get("/account/credentials/wandb",
                        headers={"Authorization": f"Bearer {later}"}).json()["hint"] == "beef"


class TestTheCliCommands:
    """`cuttle wandb set|show|clear` — the CLI-parity counterpart of the Dashboard's Account page,
    driven end to end through real commands against a real Head (see tests/conftest.py). The verbs
    stay per-tool even though the storage underneath is generic: a scientist reaches for the name of
    the thing they have a key *for*."""

    def test_set_show_clear(self, signed_in):
        assert signed_in("wandb", "set", "wandb_deadbeef").exit_code == 0
        masked = signed_in("wandb", "show")
        assert "beef" in masked.output
        assert "wandb_deadbeef" not in masked.output   # masked means masked
        assert signed_in("wandb", "clear").exit_code == 0
        assert "no W&B key stored" in signed_in("wandb", "show").output

    def test_there_is_no_reveal_flag(self, signed_in):
        signed_in("wandb", "set", "wandb_deadbeef")
        result = signed_in("wandb", "show", "--reveal")
        assert result.exit_code != 0
        assert "wandb_deadbeef" not in result.output

    def test_stdin_keeps_the_key_out_of_argv(self, signed_in):
        """The scripted path. A positional is redacted from the CLI's own run log, but `ps` and shell
        history see it regardless — this is the spelling that avoids all three."""
        assert signed_in("wandb", "set", "--stdin", input="wandb_deadbeef\n").exit_code == 0
        assert "beef" in signed_in("wandb", "show").output

    def test_show_with_nothing_stored_says_so(self, signed_in):
        assert "no W&B key stored" in signed_in("wandb", "show").output

    def test_a_cli_spawn_carries_the_stored_key(self, signed_in, fake_slurm, project, monkeypatch):
        """The seam, and now the longest thread in the change: the key is opened with the DEK carried
        by the session this very command is authenticated with, from `current_user` all the way down
        into the composer."""
        signed_in("wandb", "set", "wandb_deadbeef")
        seen: dict[str, str] = {}
        real = head_spawns.submit

        def spy(user_id, script, gpu, args, wandb_env, *a, **kw):
            seen.update(wandb_env)
            return real(user_id, script, gpu, args, wandb_env, *a, **kw)

        monkeypatch.setattr(head_spawns, "submit", spy)
        r = signed_in("run", "train.py", "--name", "a spawn", "--time", "60", "--vram", "24",
                      "--output", "out", "--manifest", "requirements.txt", "--project", "vision")
        assert r.exit_code == 0, r.output
        assert seen["WANDB_API_KEY"] == "wandb_deadbeef"
        assert seen["WANDB_MODE"] == "online"
        assert seen["WANDB_PROJECT"] == "vision"


class TestThePinnedPath:
    """`cuttle run --node` composes identically to a Spawn that arrived over HTTP — **except for the
    key**, which it cannot reach.

    It doesn't travel over HTTP at all: pinning is Head-local, reaching `submit_checked` through
    `ccc_head_api.admin`. That means it has a User to attribute to but no Session, and therefore no
    DEK. Its Spawns run with W&B disabled, and the command says so on stderr rather than letting it be
    discovered afterwards in a run that logged nothing. Restoring the key here would mean escrowing a
    copy somewhere the Head could reach unattended, which is the whole thing this change removes.
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
        """The wandb_env and dek the admin tool hands `submit_checked`. Stubbed at that call rather
        than at sbatch: everything past it is the shared path the tests above already cover, and what
        is under test here is what survives the argv at all."""
        captured: dict = {}

        def spy(user_id, is_admin, script, gpu, args, wandb_env, *a, **kw):
            captured.update(wandb_env)
            captured["__dek"] = kw.get("dek", "absent")
            return 4242

        monkeypatch.setattr(head_spawns, "submit_checked", spy)
        return captured

    def test_project_and_entity_reach_the_head(self, head_db, dek, tar, hint, capsys):
        from ccc_head_api import admin

        rc = admin.main(["submit", "--user", "alice", "--name", "a spawn", "--script", "train.py",
                         "--tar", str(tar), "--minutes", "60", "--vram", "24", "--node", "gpu-box",
                         "--wandb-env", '{"WANDB_PROJECT": "X", "WANDB_ENTITY": "Y"}'])
        assert rc == 0, capsys.readouterr().err
        assert hint["WANDB_PROJECT"] == "X"
        assert hint["WANDB_ENTITY"] == "Y"

    def test_it_passes_no_dek_so_the_key_is_unreachable(self, head_db, dek, tar, hint):
        from ccc_head_api import admin

        _store("wandb_deadbeef", dek)
        admin.main(["submit", "--user", "alice", "--name", "a spawn", "--script", "train.py",
                    "--tar", str(tar), "--minutes", "60", "--vram", "24", "--node", "gpu-box",
                    "--wandb-env", "{}"])
        assert hint["__dek"] is None
        assert head_spawns.compose_wandb_env(1, hint, dek=hint["__dek"]) == {"WANDB_MODE": "disabled"}

    def test_it_says_so_when_there_was_a_key_to_lose(self, head_db, dek, tar, hint, capsys):
        from ccc_head_api import admin

        _store("wandb_deadbeef", dek)
        admin.main(["submit", "--user", "alice", "--name", "a spawn", "--script", "train.py",
                    "--tar", str(tar), "--minutes", "60", "--vram", "24", "--node", "gpu-box",
                    "--wandb-env", "{}"])
        err = capsys.readouterr().err
        assert "W&B disabled" in err
        assert "wandb_deadbeef" not in err

    def test_it_stays_quiet_when_there_was_not(self, head_db, dek, tar, hint, capsys):
        """The masked status needs no DEK, which is exactly what lets this tell "you had a key and it
        wasn't used" apart from "you never had one"."""
        from ccc_head_api import admin

        admin.main(["submit", "--user", "alice", "--name", "a spawn", "--script", "train.py",
                    "--tar", str(tar), "--minutes", "60", "--vram", "24", "--node", "gpu-box",
                    "--wandb-env", "{}"])
        assert "W&B disabled" not in capsys.readouterr().err

    def test_a_blank_entity_never_reaches_the_head(self, head_db, dek, tar, hint):
        """`--entity ''` is "no entity", and an empty WANDB_ENTITY breaks wandb.init(). The CLI drops
        it before the argv is built (settings.wandb_env), and nothing downstream reinstates it."""
        from ccc_head_api import admin

        admin.main(["submit", "--user", "alice", "--name", "a spawn", "--script", "train.py",
                    "--tar", str(tar), "--minutes", "60", "--vram", "24", "--node", "gpu-box",
                    "--wandb-env", '{"WANDB_PROJECT": "X"}'])
        assert "WANDB_ENTITY" not in hint

    def test_the_cli_builds_the_same_env_for_both_roads(self, ccc_home, monkeypatch):
        """The symmetry itself, at the seam it broke: whatever `cuttle run` would POST is what a
        pinned Spawn hands the admin tool. Still true — only the key differs between the two roads."""
        from cuttle import settings as cli_settings
        from cuttle import spawns as cli_spawns

        argv: list[str] = []
        monkeypatch.setattr(cli_spawns.auth, "_api", lambda *a, **kw: {"username": "alice"})
        monkeypatch.setattr(cli_spawns.auth, "_admin_argv", lambda a, env=None: a)
        monkeypatch.setattr(cli_spawns.mesh, "tailscale_ip", lambda: "100.99.0.1")
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
    """v20 is the destructive one, and deliberately so: it drops `users.wandb_key` **unrecovered** and
    deletes every session.

    Neither is salvageable. Re-encrypting a stored key would mean first decrypting it with the keyfile
    — the exact unattended decrypt this change exists to make impossible — and a live session predating
    the change has no wrapped DEK to carry forward. Users re-paste; everyone logs in again."""

    def _at_v19(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        for step in db._MIGRATIONS[:19]:
            conn.executescript(step)
        conn.execute("PRAGMA user_version=19")
        conn.execute("INSERT INTO users (id, username, pw_hash, wandb_key) "
                     "VALUES (1,'alice','hashed',x'0badc0de')")
        conn.execute("INSERT INTO sessions (token_sha256, user_id, expires_at) "
                     "VALUES ('abc',1,'2099-01-01T00:00:00Z')")
        conn.commit()
        conn.close()

    def test_it_upgrades_in_place(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        self._at_v19(tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
            user = db.get_user(conn, "alice")
            assert user is not None                    # the existing User survived
            assert user["dek_wrapped_pw"] is None      # NULL until their next login
            assert "wandb_key" not in user.keys()      # the old column is gone

    def test_it_logs_everyone_out(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        self._at_v19(tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0

    def test_the_upgraded_db_stores_a_credential(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        self._at_v19(tmp_path / "head.db")
        db.ensure_schema()
        with db.connect() as conn:
            db.set_password(conn, "alice", PW)
            dek = db.unlock_dek(conn, db.get_user(conn, "alice"), PW)
        _store("wandb_deadbeef", dek)
        assert _stored(dek) == "wandb_deadbeef"


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
        monkeypatch.setattr(slurm, "vram_constraint", lambda gb, gpus=1: "vram24")
        monkeypatch.setattr(slurm, "queue", lambda: [])
        # The submit inherits os.environ, so a developer with W&B configured in their own shell would
        # otherwise see their key leak into the assertions below.
        for var in ("WANDB_API_KEY", "WANDB_MODE", "WANDB_PROJECT", "WANDB_ENTITY"):
            monkeypatch.delenv(var, raising=False)
        seen: dict[str, str] = {}

        def fake_run(argv, **kw):
            # `setfacl` (the project lend) comes through here too and carries no env — only the
            # submission's is what this fixture is about.
            if "sbatch" in argv:
                seen.update(kw["env"])
            return types.SimpleNamespace(stdout="4242\n", stderr="", returncode=0)

        monkeypatch.setattr(head_spawns.subprocess, "run", fake_run)
        return seen

    def _submit(self, tmp_path, client_env, wandb=True, dek=None):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name in ("train.py", "requirements.txt"):
                info = tarfile.TarInfo(name)
                info.size = 0
                tf.addfile(info, io.BytesIO(b""))
        tar_path = tmp_path / "upload.tar.gz"
        tar_path.write_bytes(buf.getvalue())
        head_spawns.submit_checked(1, False, "train.py", 1, [], client_env, tar_path, 60, 24, ["out"],
                                   wandb=wandb, manifest="requirements.txt", name="a spawn", dek=dek)

    def test_the_stored_key_reaches_sbatch(self, sbatch_env, tmp_path, dek):
        _store("wandb_deadbeef", dek)
        self._submit(tmp_path, {"WANDB_PROJECT": "vision"}, dek=dek)
        assert sbatch_env["WANDB_API_KEY"] == "wandb_deadbeef"
        assert sbatch_env["WANDB_MODE"] == "online"
        assert sbatch_env["WANDB_PROJECT"] == "vision"

    def test_without_a_key_sbatch_sees_wandb_disabled(self, sbatch_env, tmp_path, dek):
        self._submit(tmp_path, {}, dek=dek)
        assert sbatch_env["WANDB_MODE"] == "disabled"
        assert "WANDB_API_KEY" not in sbatch_env

    def test_without_a_dek_sbatch_sees_wandb_disabled(self, sbatch_env, tmp_path, dek):
        """The pinned path, all the way to sbatch: a stored key that nothing in this submission can
        open leaves the Pod with an explicit disabled, not a half-configured W&B."""
        _store("wandb_deadbeef", dek)
        self._submit(tmp_path, {"WANDB_PROJECT": "vision"}, dek=None)
        assert sbatch_env["WANDB_MODE"] == "disabled"
        assert "WANDB_API_KEY" not in sbatch_env

    def test_a_spawn_submitted_with_wandb_off_carries_no_key(self, sbatch_env, tmp_path, dek):
        """The off-switch through the whole submit, not just the composer: submit_checked is where the
        env is composed, so the pinned road gets this for free."""
        _store("wandb_deadbeef", dek)
        self._submit(tmp_path, {"WANDB_PROJECT": "vision"}, wandb=False, dek=dek)
        assert sbatch_env["WANDB_MODE"] == "disabled"
        assert "WANDB_API_KEY" not in sbatch_env
