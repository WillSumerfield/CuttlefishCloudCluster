"""CCC Head API app (Phase 1 ⑥ read board + SPA; Phase 3a auth + submission) — served on LAN + mesh.

The LAN+mesh service backing both the web Dashboard and the CLI (glossary: *Head API*). Deliberately
a **separate** service from the enroll app (`ccc_head_api.enroll`): they have opposite exposure needs
— enrollment hands out the munge key and stays bound to the mesh only, while this must reach the LAN
so anyone on the CCC mesh *or* the local network can open the board. Binds `0.0.0.0`.

Runs as the **Mantle** (the `ccc` account) and owns the auth store (`head.db`). Every route except
`/healthz` is behind a session: reads were anonymous while the SPA had no way to log in, and the
browser-login slice closes that. Same origin for API + SPA, so the front-end needs no CORS.

Transit: HTTPS, with a cert the Head signs itself (no CA will sign for a box with no public domain —
that's the deferred item #8). The CLI pins that cert on first use; browsers show their self-signed
warning once. The session cookie is therefore Secure.
"""

import json
import os
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, karma, slurm
from . import spawns as spawns_mod

# The built SPA, copied here by `cuttle setup --head` (world-readable). Kept out of the service user's
# home on purpose. Env var is set by the systemd unit; the default is only for fresh checkouts/tests.
WEB_DIR = Path(os.environ.get("CCC_WEB_DIR", "/opt/ccc/head-api/web"))

# Cap the upload instead of trusting whatever arrives. The body is streamed to disk (never buffered in
# this process): the Head is the one box that must stay up, and a browser folder-picker makes a huge
# accidental selection a single click. Mirrors cuttle.config.MAX_UPLOAD_BYTES.
MAX_UPLOAD_BYTES = int(os.environ.get("CCC_MAX_UPLOAD", 4 * 1024**3))

# Session cookie: HttpOnly + SameSite=Lax + Secure (the Head is HTTPS now — see module docstring).
_COOKIE_NAME = "ccc_session"
_COOKIE_SECURE = True
_COOKIE_MAX_AGE = db.SESSION_TTL_DAYS * 86400

app = FastAPI(title="CCC Head API", version="0.1.0")


# --- auth models ------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class PasswdRequest(BaseModel):
    old_password: str
    new_password: str


class OverrideRequest(BaseModel):
    username: str
    amount: float
    note: str | None = None


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


# --- health (open) + reads (authed) -----------------------------------------
# Reads were anonymous only because the SPA had no way to log in. It has one now, so they go behind the
# session — the board reveals who is running what, and on which donated machine. /healthz stays open:
# it is a liveness probe and says nothing.

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/arms")
def arms(user: dict = Depends(current_user)) -> list[dict]:
    """Every compute node's live status + enrollment facts. See slurm.arms()."""
    return slurm.arms()


@app.get("/spawns")
def spawns(user: dict = Depends(current_user)) -> list[dict]:
    """The live Spawn queue (pending/running, CCC partition), enriched with Attribution. Every Spawn
    runs as the Mantle now, so Slurm's own user field says `ccc` for all of them and is useless — the
    ledger is what says who submitted it. See slurm.spawns()."""
    rows = slurm.spawns()
    ids = [r["id"] for r in rows if r["id"] is not None]
    with db.connect() as conn:
        submitters = db.spawn_submitters(conn, ids)
    for r in rows:
        r["submitted_by"] = submitters.get(r["id"])
    return rows


@app.get("/spawns/{job_id}")
def spawn_status(job_id: int, user: dict = Depends(current_user)) -> dict:
    """A single Spawn's state: still queued/running, or done with an exit code.

    The Attribution ledger is what distinguishes "this Spawn died" from "no such Spawn". A Spawn that
    is gone from squeue with no `exit` file never recorded one — it was scancel'd, OOM-killed, or its
    Arm dropped off the mesh before staging out. That is `failed`, not 404, and only the ledger can say
    so. Remote Arms are the flakiest nodes in the cluster (donor box sleeps, WSL shuts down), so this
    is the common case there, not a corner.
    """
    state = spawns_mod.active_state(job_id)
    if state is not None:
        return {"id": job_id, "done": False, "state": state}
    rc = spawns_mod.exit_code(job_id)
    if rc is not None:
        return {"id": job_id, "done": True, "state": "done", "exit": rc}
    with db.connect() as conn:
        known = db.get_spawn(conn, job_id) is not None
    if known:
        return {"id": job_id, "done": True, "state": "failed"}
    raise HTTPException(404, f"unknown spawn {job_id}")


@app.get("/spawns/{job_id}/logs", response_class=PlainTextResponse)
def spawn_logs(job_id: int, user: dict = Depends(current_user)) -> str:
    """The Spawn's stdout so far (a remote Spawn's arrives only at stage-out — post-hoc)."""
    return spawns_mod.out_text(job_id) or ""


@app.get("/spawns/{job_id}/outputs")
def spawn_outputs(job_id: int, user: dict = Depends(current_user)) -> Response:
    """gzip-tar of the Spawn's $CCC_OUT dir — a remote submitter's only way to get results back."""
    data = spawns_mod.outputs_tar(job_id)
    if data is None:
        raise HTTPException(404, f"no outputs for spawn {job_id}")
    return Response(data, media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="spawn-{job_id}-outputs.tar.gz"'})


@app.delete("/spawns/{job_id}", status_code=204)
def cancel_spawn(job_id: int, user: dict = Depends(current_user)) -> Response:
    """End a live Spawn early. A User may cancel their OWN outstanding Spawn; an Admin may cancel
    anyone's — Attribution is the ledger's, not Slurm's (every Spawn runs as the Mantle).

    Closes the gap FRONTEND.md names most loudly: until now nothing in CCC could stop a job from any
    client, and under Karma a User was *paying* until `--time` expired. Settles for the seconds
    actually held, by the Karma tick, when the cancelled Spawn drops out of squeue — see slurm.cancel.
    """
    with db.connect() as conn:
        row = db.get_spawn(conn, job_id)
    # Only Spawns the Head actually recorded can be cancelled through here. An id squeue knows but the
    # ledger doesn't is not a CCC Spawn (or predates attribution); refuse rather than scancel blindly.
    if row is None:
        raise HTTPException(404, f"unknown spawn {job_id}")
    if not user["is_admin"] and row["user_id"] != user["id"]:
        raise HTTPException(403, "not your spawn")
    slurm.cancel(job_id)
    return Response(status_code=204)


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
    must-change flag. Existing sessions stay valid — the client keeps using its token.

    A wrong old password is **403, not 401**: the caller's session is fine, this one act is refused.
    401 is the clients' "your session is dead, log in again" signal (the SPA drops to its login form
    on any 401), so answering it here made a typo'd password report itself as `not logged in`.
    """
    with db.connect() as conn:
        if db.verify_password(conn, user["username"], req.old_password) is None:
            raise HTTPException(403, "current password is wrong")
        db.set_password(conn, user["username"], req.new_password)
    return {"ok": True}


# --- Karma (authed; Override is admin-only) ---------------------------------

@app.get("/karma")
def karma_standings(user: dict = Depends(current_user)) -> list[dict]:
    """Every User's standing — Balance, Escrow, Share, and the nice it implies — best first.

    Everyone's, not just your own: a Balance is meaningless in isolation. Karma ranks Users *against
    each other*, so a User who cannot see the others cannot see their own standing either.
    """
    with db.connect() as conn:
        return karma.standings(conn)


@app.get("/karma/ledger")
def karma_ledger(days: int = 30, user: dict = Depends(current_user)) -> dict:
    """**Your own** ledger: the real rows you were a party to, plus the Balance curve they imply.

    Yours alone, unlike `/karma` — the standings exist so a User can see where they stand *relative* to
    others, which needs everyone's number; a transaction history needs nobody else's.

    There are no Decay rows in `rows`, and the absence is the point: Decay is the sag of `curve` between
    the events, not an event. See karma.ledger().
    """
    with db.connect() as conn:
        return karma.ledger(conn, user["id"], days=days)


@app.get("/karma/demand")
def karma_demand(days: int = 7, user: dict = Depends(current_user)) -> list[dict]:
    """Demand per Arm over time — how badly each card has been wanted, hour by hour.

    This is the **only** surface the Tide is ever seen through, and it is deliberately not a forecast: a
    User reads the shape and infers the quiet hours for themselves. Nothing quotes them a predicted
    price, warns them, or asks them to confirm — *Karma ranks, it never gates*, and a blocking "this
    will cost you N" would have been the first thing in the model able to stop a Spawn.
    """
    with db.connect() as conn:
        return [dict(r) for r in db.demand_series(conn, days)]


@app.post("/karma/override")
def karma_override(req: OverrideRequest, user: dict = Depends(current_user)) -> dict:
    """Admin add/remove of a User's Balance. The only flow that isn't a Transfer — it conjures Karma or
    destroys it, and Decay absorbs the imbalance.

    Needs no privilege beyond `is_admin`, because it touches nothing but head.db, which this API owns.
    That is exactly why it lives here while a **Queue bump** cannot: a bump writes a negative nice,
    which Slurm grants only to an operator, and Slurm has never heard of CCC's Admin (every Spawn — and
    every scontrol call this process makes — is the one `ccc` uid). See `cuttle karma bump`.
    """
    if not user["is_admin"]:
        raise HTTPException(403, "Karma override is admin-only")
    with db.connect() as conn:
        try:
            balance = karma.override(conn, req.username, req.amount, req.note)
        except KeyError:
            raise HTTPException(404, f"no such user '{req.username}'")
        except ValueError as e:
            raise HTTPException(422, str(e))  # the target is an Admin — outside the economy
    return {"username": req.username, "balance": round(balance, 2)}


# --- submission (authed) ----------------------------------------------------

@app.post("/submit")
async def submit(request: Request, user: dict = Depends(current_user)) -> dict:
    """Accept an uploaded project + metadata and launch a Spawn attributed to the caller. The only
    authed mutation in slice 2. Body = gzip-tar of the project; `X-CCC-Meta` header = JSON
    {script, gpu, args, node, wandb_env} — metadata in a header (not query params) keeps the W&B key
    out of access logs. Runs the render/sbatch/Attribution as the Mantle; returns the Spawn id.

    `minutes` and `vram` are **required** — a Spawn with no declared length cannot be priced, and one
    with no declared need cannot be placed. `vram` is OR-expanded here into a Slurm constraint over the
    fleet's real cards (slurm.vram_constraint); the client can't do it, because only the Head knows
    what hardware exists.

    `node` pins the Spawn to one Arm and is **admin-only**. Users declare what they need and let Slurm
    choose; naming a machine sidesteps eligibility (and so the Demand it would have added to every card
    that could have run it). We reject an unknown name here rather than letting Slurm do it: sbatch's
    error for a bad --nodelist is opaque, and we already know the real node list.
    """
    if user["must_change_password"]:
        raise HTTPException(403, "change your password before submitting (run `cuttle passwd`)")
    meta = json.loads(request.headers.get("x-ccc-meta") or "{}")
    if not meta.get("script"):
        raise HTTPException(422, "missing 'script' in X-CCC-Meta")
    for field in ("minutes", "vram"):
        if not meta.get(field):
            raise HTTPException(422, f"missing '{field}' in X-CCC-Meta — --time and --vram are required")
    node = meta.get("node")
    if node:
        if not user["is_admin"]:
            raise HTTPException(403, "--node is admin-only. Say what you need (--vram) and Slurm will "
                                     "give you the fastest free Arm that has it.")
        known = [a["name"] for a in slurm.arms()]
        if node not in known:
            raise HTTPException(422, f"unknown Arm '{node}' — known Arms: {', '.join(known)}")
    constraint = slurm.vram_constraint(int(meta["vram"]))

    # Quote the Escrow here, where the fleet is known. quote_hold is the one place a Spawn is priced —
    # shared verbatim with the Fake Cluster (slice 4) so the test cannot drift from what really runs. It
    # returns None for an Admin (outside the economy), else the hold over the priciest eligible Arm at
    # the Tide of the hours the Spawn would span.
    minutes, vram_gb, gpus = int(meta["minutes"]), int(meta["vram"]), int(meta.get("gpu", 1))
    need = {"node": node, "vram_gb": vram_gb, "gpus": gpus}
    with db.connect() as conn:
        hold = karma.quote_hold(conn, slurm.arms(), need, minutes, gpus, user["is_admin"])

    tar_path = await _spool_upload(request)
    try:
        job_id = spawns_mod.submit(user["id"], meta["script"], gpus,
                                   meta.get("args", []), meta.get("wandb_env", {}), tar_path,
                                   minutes, constraint, node, vram_gb=vram_gb, hold=hold)
    finally:
        tar_path.unlink(missing_ok=True)
    return {"id": job_id}


def _human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return f"{n} B"  # unreachable


async def _spool_upload(request: Request) -> Path:
    """Stream the request body to a temp file, aborting past MAX_UPLOAD_BYTES.

    Never `await request.body()`: that holds the entire project in this process's memory (and
    `tarfile` then holds a decompressed copy on top). The Head is the one machine that must stay up,
    and a browser folder-picker makes selecting a folder with a huge `data/` or a venv a single click.
    Streaming to disk means a too-large upload costs disk we then release, not RAM we might not have.
    """
    total = 0
    with tempfile.NamedTemporaryFile("wb", suffix=".tar.gz", delete=False) as f:
        path = Path(f.name)
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                path.unlink(missing_ok=True)
                raise HTTPException(413, f"upload exceeds {_human(MAX_UPLOAD_BYTES)} — "
                                         "exclude data/checkpoints (honor .gitignore) and retry")
            f.write(chunk)
    return path


class SPAFiles(StaticFiles):
    """Static files, but unknown paths fall back to index.html.

    The Dashboard routes client-side (/arms, /karma, /spawn/<id>). Those paths are not files, so a hard
    reload or a pasted link would otherwise 404 — the SPA never gets a chance to run and read the URL.
    Real misses (a missing .js, a bad asset hash) still 404 from `get_response`; only *extensionless*
    paths are rewritten, so a broken bundle reference fails loudly instead of silently serving HTML.
    """

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)


# Mounted LAST so the API routes above win. html=True serves index.html at `/`. Guarded so the app
# still imports/serves the API before the SPA has been built (fresh checkout, tests).
if (WEB_DIR / "index.html").exists():
    app.mount("/", SPAFiles(directory=str(WEB_DIR), html=True), name="spa")
