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

import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, slurm
from . import spawns as spawns_mod

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
    """The live Spawn queue (pending/running, CCC partition), enriched with app-level attribution.
    Every job runs as `ccc` now, so Slurm's own user field is useless — the ledger says who submitted
    it. See slurm.spawns()."""
    rows = slurm.spawns()
    ids = [r["id"] for r in rows if r["id"] is not None]
    with db.connect() as conn:
        submitters = db.spawn_submitters(conn, ids)
    for r in rows:
        r["submitted_by"] = submitters.get(r["id"])
    return rows


@app.get("/spawns/{job_id}")
def spawn_status(job_id: int) -> dict:
    """A single Spawn's state: still queued/running, or done with an exit code. Anonymous in slice 2
    (reads stay open until the browser-login slice gates them)."""
    state = spawns_mod.active_state(job_id)
    if state is not None:
        return {"id": job_id, "done": False, "state": state}
    rc = spawns_mod.exit_code(job_id)
    if rc is not None:
        return {"id": job_id, "done": True, "state": "done", "exit": rc}
    raise HTTPException(404, f"unknown spawn {job_id} (not queued; no exit record)")


@app.get("/spawns/{job_id}/logs", response_class=PlainTextResponse)
def spawn_logs(job_id: int) -> str:
    """The Spawn's stdout so far (a remote Spawn's arrives only at stage-out — post-hoc)."""
    return spawns_mod.out_text(job_id) or ""


@app.get("/spawns/{job_id}/outputs")
def spawn_outputs(job_id: int) -> Response:
    """gzip-tar of the Spawn's $CCC_OUT dir — a remote submitter's only way to get results back."""
    data = spawns_mod.outputs_tar(job_id)
    if data is None:
        raise HTTPException(404, f"no outputs for spawn {job_id}")
    return Response(data, media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="spawn-{job_id}-outputs.tar.gz"'})


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


# --- submission (authed) ----------------------------------------------------

@app.post("/submit")
async def submit(request: Request, user: dict = Depends(current_user)) -> dict:
    """Accept an uploaded project + metadata and launch a Spawn attributed to the caller. The only
    authed mutation in slice 2. Body = gzip-tar of the project; `X-CCC-Meta` header = JSON
    {script, gpu, args, wandb_env} — metadata in a header (not query params) keeps the W&B key out of
    access logs. Runs the render/sbatch/ledger as ccc; returns the Spawn id."""
    if user["must_change_password"]:
        raise HTTPException(403, "change your password before submitting (run `cuttle passwd`)")
    meta = json.loads(request.headers.get("x-ccc-meta") or "{}")
    if not meta.get("script"):
        raise HTTPException(422, "missing 'script' in X-CCC-Meta")
    tar_bytes = await request.body()
    job_id = spawns_mod.submit(user["id"], meta["script"], int(meta.get("gpu", 1)),
                               meta.get("args", []), meta.get("wandb_env", {}), tar_bytes)
    return {"id": job_id}


# Mounted LAST so the API routes above win. html=True serves index.html at `/`. Guarded so the app
# still imports/serves the API before the SPA has been built (fresh checkout, tests).
if (WEB_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="spa")
