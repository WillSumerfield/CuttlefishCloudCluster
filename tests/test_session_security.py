"""Two session-layer hardenings:

- F4: changing your password is a security act — it evicts every OTHER session (a stolen or stale
  token, any scope), keeping only the caller's own so the client stays signed in.
- F5: login CSRF — /login mints a session before one exists, so SameSite can't help it; a cross-site
  form post is refused by an Origin/Referer same-origin check. The CLI (no such header) still logs in.
"""

from pathlib import Path
import tempfile

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db, ratelimit
from ccc_head_api import main as head_main


@pytest.fixture(autouse=True)
def _clean():
    ratelimit.reset()
    yield
    ratelimit.reset()
    head_main.app.dependency_overrides.clear()


def _real_db(monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", Path(tempfile.mkdtemp()) / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        db.add_user(conn, "alice", is_admin=False)
        db.set_password(conn, "alice", "hunter2")


# --- F4: /passwd revokes every other session -------------------------------------

class TestRevokeOtherSessions:
    def test_db_revoke_keeps_only_the_caller_token(self, monkeypatch):
        _real_db(monkeypatch)
        with db.connect() as conn:
            uid = db.get_user(conn, "alice")["id"]
            keep = db.create_session(conn, uid)
            other = db.create_session(conn, uid)
            tide = db.create_session(conn, uid, scope="tidepool")
            removed = db.revoke_other_sessions(conn, uid, keep)
            assert removed == 2                                   # other + tidepool, all scopes
            assert db.lookup_session(conn, keep) is not None
            assert db.lookup_session(conn, other) is None
            assert db.lookup_session(conn, tide) is None

    def test_db_revoke_leaves_a_different_users_sessions(self, monkeypatch):
        _real_db(monkeypatch)
        with db.connect() as conn:
            db.add_user(conn, "bob", is_admin=False)
            alice = db.get_user(conn, "alice")["id"]
            bob = db.get_user(conn, "bob")["id"]
            a_tok = db.create_session(conn, alice)
            db.create_session(conn, alice)
            b_tok = db.create_session(conn, bob)
            db.revoke_other_sessions(conn, alice, a_tok)
            assert db.lookup_session(conn, b_tok) is not None     # bob untouched

    def test_passwd_evicts_other_sessions_but_keeps_caller(self, monkeypatch):
        _real_db(monkeypatch)
        c = TestClient(head_main.app)
        a = c.post("/api/login", json={"username": "alice", "password": "hunter2"}).json()["token"]
        b = c.post("/api/login", json={"username": "alice", "password": "hunter2"}).json()["token"]
        r = c.post("/api/passwd",
                   json={"old_password": "hunter2", "new_password": "a-much-longer-pw-1"},
                   headers={"Authorization": f"Bearer {a}"})
        assert r.status_code == 200
        # caller's own token still valid, the other is dead
        assert c.get("/api/me", headers={"Authorization": f"Bearer {a}"}).status_code == 200
        assert c.get("/api/me", headers={"Authorization": f"Bearer {b}"}).status_code == 401


# --- F5: login CSRF (same-origin) ------------------------------------------------

class TestLoginSameOrigin:
    def _login(self, c, headers=None):
        return c.post("/api/login", json={"username": "alice", "password": "hunter2"},
                      headers=headers or {})

    def test_no_origin_header_is_allowed(self, monkeypatch):
        """The CLI sends neither Origin nor Referer — it must still log in."""
        _real_db(monkeypatch)
        assert self._login(TestClient(head_main.app)).status_code == 200

    def test_cross_site_origin_is_refused(self, monkeypatch):
        _real_db(monkeypatch)
        r = self._login(TestClient(head_main.app), {"Origin": "https://evil.example"})
        assert r.status_code == 403

    def test_same_origin_is_allowed(self, monkeypatch):
        _real_db(monkeypatch)
        r = self._login(TestClient(head_main.app), {"Origin": "http://testserver"})
        assert r.status_code == 200

    def test_cross_site_referer_is_refused(self, monkeypatch):
        """Origin absent → fall back to Referer; a cross-site one still blocks."""
        _real_db(monkeypatch)
        r = self._login(TestClient(head_main.app), {"Referer": "https://evil.example/x"})
        assert r.status_code == 403

    def test_cross_site_login_never_checks_the_password(self, monkeypatch):
        """Refused before the credential check, so it is not a rate-limited guess either."""
        _real_db(monkeypatch)
        c = TestClient(head_main.app)
        r = c.post("/api/login", json={"username": "alice", "password": "wrong"},
                   headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
