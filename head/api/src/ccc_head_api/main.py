"""CCC Head API app (Phase 1 ⑥ read board + SPA; Phase 3a auth + submission) — served on LAN + mesh.

The LAN+mesh service backing both the web Dashboard and the CLI (glossary: *Head API*). Deliberately
a **separate** service from the enroll app (`ccc_head_api.enroll`): they have opposite exposure needs
— enrollment hands out the munge key and stays bound to the mesh only, while this must reach the LAN
so anyone on the CCC mesh *or* the local network can open the board. Binds `0.0.0.0`.

Phase 3a (identity spine): runs as the `ccc` service account and owns the auth store (`head.db`).
Reads (`/arms`, `/spawns`) stay **anonymous** on the trusted net; login/session routes are added here.
Mutations (upload/submit) arrive in later slices behind the session. Same origin for API + SPA, so
the front-end needs no CORS.

Transit: served plain HTTP for now (LAN TLS is a later slice — self-signed + CLI pinning). The
session cookie is therefore not yet marked Secure; flip `_COOKIE_SECURE` when TLS lands.
"""

import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, slurm

# The built SPA, copied here by `cuttle setup --head` (world-readable). Kept out of the service user's
# home on purpose. Env var is set by the systemd unit; the default is only for fresh checkouts/tests.
WEB_DIR = Path(os.environ.get("CCC_WEB_DIR", "/opt/ccc/head-api/web"))

# Session cookie: HttpOnly + SameSite=Lax. Not Secure until LAN TLS lands (see module docstring).
_COOKIE_NAME = "ccc_session"
_COOKIE_SECURE = False
_COOKIE_MAX_AGE = db.SESSION_TTL_DAYS * 86400

app = FastAPI(title="CCC Head API", version="0.1.0")


# --- auth models ------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class PasswdRequest(BaseModel):
    old_password: str
    new_password: str


def _token_from(request: Request) -> str | None:
    """A client presents its session either as a bearer (CLI) or the HttpOnly cookie (browser)."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get(_COOKIE_NAME)


def current_user(request: Request) -> dict:
    """Dependency: resolve the session to a user, or 401. Returns a plain dict (row detached from the
    per-request connection)."""
    token = _token_from(request)
    if not token:
        raise HTTPException(401, "not authenticated")
    with db.connect() as conn:
        user = db.lookup_session(conn, token)
    if user is None:
        raise HTTPException(401, "invalid or expired session")
    return {"username": user["username"], "is_admin": bool(user["is_admin"]),
            "must_change_password": bool(user["must_change_password"]), "id": user["id"]}


# --- health + reads (anonymous) ---------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/arms")
def arms() -> list[dict]:
    """Every compute node's live status + enrollment facts. See slurm.arms()."""
    return slurm.arms()


@app.get("/spawns")
def spawns() -> list[dict]:
    """The live Spawn queue (pending/running, CCC partition). See slurm.spawns()."""
    return slurm.spawns()


# --- auth routes ------------------------------------------------------------

@app.post("/login")
def login(req: LoginRequest, response: Response) -> dict:
    """Verify credentials → mint a session. Returns the bearer token (CLI) and sets the HttpOnly
    cookie (browser). A `must_change_password` user still gets a session so it can call /passwd."""
    with db.connect() as conn:
        user = db.verify_password(conn, req.username, req.password)
        if user is None:
            raise HTTPException(401, "bad username or password")
        token = db.create_session(conn, user["id"])
    response.set_cookie(_COOKIE_NAME, token, max_age=_COOKIE_MAX_AGE, httponly=True,
                        samesite="lax", secure=_COOKIE_SECURE)
    return {"token": token, "username": user["username"], "is_admin": bool(user["is_admin"]),
            "must_change_password": bool(user["must_change_password"])}


@app.post("/logout")
def logout(request: Request, response: Response) -> dict:
    """Revoke the presented session (delete the row) and clear the cookie. Idempotent."""
    token = _token_from(request)
    if token:
        with db.connect() as conn:
            db.delete_session(conn, token)
    response.delete_cookie(_COOKIE_NAME)
    return {"ok": True}


@app.get("/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {"username": user["username"], "is_admin": user["is_admin"],
            "must_change_password": user["must_change_password"]}


@app.post("/passwd")
def passwd(req: PasswdRequest, user: dict = Depends(current_user)) -> dict:
    """Rotate own password (also the forced-first-login path). Verifies the old password, clears the
    must-change flag. Existing sessions stay valid — the client keeps using its token."""
    with db.connect() as conn:
        if db.verify_password(conn, user["username"], req.old_password) is None:
            raise HTTPException(401, "current password is wrong")
        db.set_password(conn, user["username"], req.new_password)
    return {"ok": True}


# Mounted LAST so the API routes above win. html=True serves index.html at `/`. Guarded so the app
# still imports/serves the API before the SPA has been built (fresh checkout, tests).
if (WEB_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="spa")
