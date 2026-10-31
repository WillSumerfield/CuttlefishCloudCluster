"""The Head API observes itself: a real readiness probe, and the security events worth a
journal line.

Two surfaces under test. `/readyz` is a *contract* (a monitor branches on its status code), so
it's tested by status code and body. Log lines aren't a contract — wording may change freely — so
only the two carrying a security signal are pinned, by logger/level/substring: a login failure
must be recorded (feeds rate-limiting, is the guessing signal), and an unhandled route error must
never vanish silently.
"""

import logging
from pathlib import Path
import tempfile

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db, karma, ratelimit
from ccc_head_api import main as head_main
from ccc_head_api import slurm


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


@pytest.fixture
def api_caplog(caplog, monkeypatch):
    """`ccc.api` logs with propagate=False (its lines are independent of uvicorn's root config), so
    pytest's caplog — which listens at the root — sees nothing until we let this pass through for the
    duration of a test."""
    monkeypatch.setattr(head_main.log, "propagate", True)
    caplog.set_level(logging.INFO, logger="ccc.api")
    return caplog


def _real_db(monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", Path(tempfile.mkdtemp()) / "head.db")
    db.ensure_schema()


class TestReadyz:
    """Readiness = can the API actually serve? 200 iff both hard dependencies answer; 503 with the
    culprit named otherwise. Distinct from /healthz, which stays 200 through a downstream blip."""

    def test_all_green_is_200(self, monkeypatch):
        _real_db(monkeypatch)                       # exercises the real _db_ok against a live store
        monkeypatch.setattr(slurm, "reachable", lambda: True)
        r = TestClient(head_main.app).get("/readyz")
        assert r.status_code == 200
        assert r.json() == {"db": "ok", "slurm": "ok"}

    def test_db_down_is_503(self, monkeypatch):
        monkeypatch.setattr(head_main, "_db_ok", lambda: False)
        monkeypatch.setattr(slurm, "reachable", lambda: True)
        r = TestClient(head_main.app).get("/readyz")
        assert r.status_code == 503
        assert r.json() == {"db": "unreachable", "slurm": "ok"}

    def test_slurm_down_is_503(self, monkeypatch):
        _real_db(monkeypatch)
        monkeypatch.setattr(slurm, "reachable", lambda: False)  # slurmctld restarting
        r = TestClient(head_main.app).get("/readyz")
        assert r.status_code == 503
        assert r.json() == {"db": "ok", "slurm": "unreachable"}


def test_healthz_is_liveness_only(monkeypatch):
    """Liveness stays 200 even with both dependencies down — the process answers, so a monitor must
    not bounce it. This is exactly the case that would make /readyz 503."""
    monkeypatch.setattr(head_main, "_db_ok", lambda: False)
    monkeypatch.setattr(slurm, "reachable", lambda: False)
    r = TestClient(head_main.app).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_login_failure_is_logged(monkeypatch, api_caplog):
    """The one auth event worth pinning: a failed login records the attempted username at WARNING (never
    the password), so a guessing attempt leaves a trail and rate-limiting has something to read."""
    _real_db(monkeypatch)
    with db.connect() as conn:
        db.add_user(conn, "alice", is_admin=False)
        db.set_password(conn, "alice", "hunter2")

    r = TestClient(head_main.app).post("/api/login",
                                       json={"username": "alice", "password": "wrong"})
    assert r.status_code == 401
    rec = [x for x in api_caplog.records if x.levelno == logging.WARNING and "login FAILED" in x.msg]
    assert rec, "a failed login must be logged at WARNING"
    assert "alice" in rec[0].getMessage()
    assert "wrong" not in rec[0].getMessage()  # never the password


class TestLoginThrottleWiring:
    """`ratelimit.py` owns the algorithm (see test_ratelimit.py); this only checks `/login` and
    `/passwd` are wired to it — the block fires, it answers 429 + Retry-After, and what a success clears."""

    def _seed_alice(self, monkeypatch):
        _real_db(monkeypatch)
        with db.connect() as conn:
            db.add_user(conn, "alice", is_admin=False)
            db.set_password(conn, "alice", "hunter2")

    def _bad_logins(self, c, n, user="alice"):
        for _ in range(n):
            c.post("/api/login", json={"username": user, "password": "wrong"})

    def test_too_many_failures_becomes_429_with_retry_after(self, monkeypatch):
        self._seed_alice(monkeypatch)
        c = TestClient(head_main.app)
        self._bad_logins(c, ratelimit.USER_LIMIT)
        r = c.post("/api/login", json={"username": "alice", "password": "wrong"})
        assert r.status_code == 429
        assert "Retry-After" in r.headers

    def test_the_right_password_is_blocked_once_tripped(self, monkeypatch):
        """The point: once tripped, even the correct password is refused until the window ends."""
        self._seed_alice(monkeypatch)
        c = TestClient(head_main.app)
        self._bad_logins(c, ratelimit.USER_LIMIT)
        r = c.post("/api/login", json={"username": "alice", "password": "hunter2"})
        assert r.status_code == 429

    def test_success_clears_the_username(self, monkeypatch):
        self._seed_alice(monkeypatch)
        c = TestClient(head_main.app)
        self._bad_logins(c, ratelimit.USER_LIMIT - 1)
        assert c.post("/api/login",
                      json={"username": "alice", "password": "hunter2"}).status_code == 200
        # a fresh run of failures after a clean success needs the FULL limit again
        for _ in range(ratelimit.USER_LIMIT - 1):
            r = c.post("/api/login", json={"username": "alice", "password": "wrong"})
            assert r.status_code == 401

    def test_success_does_not_clear_the_ip(self, monkeypatch):
        """Else an attacker logs into its own account between guesses and its IP never trips."""
        self._seed_alice(monkeypatch)
        c = TestClient(head_main.app)
        for i in range(ratelimit.IP_LIMIT - 1):
            c.post("/api/login", json={"username": f"u{i}", "password": "wrong"})
        assert c.post("/api/login",
                      json={"username": "alice", "password": "hunter2"}).status_code == 200
        c.post("/api/login", json={"username": "nobody", "password": "wrong"})
        assert c.post("/api/login",
                      json={"username": "alice", "password": "hunter2"}).status_code == 429

    def _passwd(self, c, tok, old):
        return c.post("/api/passwd", json={"old_password": old, "new_password": "a-much-longer-pw-1"},
                      headers={"Authorization": f"Bearer {tok}"})

    def test_passwd_wrong_old_passwords_block_login_too(self, monkeypatch):
        """Same keys as /login: a stolen session gets no extra guesses at the password."""
        self._seed_alice(monkeypatch)
        c = TestClient(head_main.app)
        tok = c.post("/api/login", json={"username": "alice", "password": "hunter2"}).json()["token"]
        for _ in range(ratelimit.USER_LIMIT):
            assert self._passwd(c, tok, "wrong").status_code == 403
        assert self._passwd(c, tok, "hunter2").status_code == 429
        assert c.post("/api/login",
                      json={"username": "alice", "password": "hunter2"}).status_code == 429


def test_unhandled_route_error_is_logged_and_500(monkeypatch, api_caplog):
    """An exception that escapes a route must not vanish: the middleware logs it at ERROR with a
    traceback and answers a clean 500 (no stack to the client)."""
    _real_db(monkeypatch)                           # so db.connect() succeeds and `boom` is what raises
    head_main.app.dependency_overrides[head_main.current_user] = lambda: {
        "id": 1, "username": "alice", "is_admin": False, "must_change_password": False}

    def boom(_conn):
        raise RuntimeError("karma exploded")
    monkeypatch.setattr(karma, "standings", boom)

    r = TestClient(head_main.app).get("/api/karma")
    assert r.status_code == 500
    assert r.json() == {"detail": "internal server error"}
    rec = [x for x in api_caplog.records if x.levelno == logging.ERROR]
    assert rec, "an unhandled route error must be logged at ERROR"
    assert "RuntimeError" in rec[0].getMessage() or "karma exploded" in rec[0].getMessage()
