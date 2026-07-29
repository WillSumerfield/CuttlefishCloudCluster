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
import logging
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, karma, ratelimit, retraction, slurm
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

# --- logging ----------------------------------------------------------------
# stdlib logging -> stdout -> journald (systemd captures the service's stdout), mirroring the Karma
# tick's setup. Its own handler with propagate=False, so our lines are independent of however uvicorn
# has (or hasn't) configured the root logger. Level from the env, INFO by default.
log = logging.getLogger("ccc.api")


def _configure_logging() -> None:
    if log.handlers:
        return                       # import is idempotent; a re-import must not double every line
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))  # journald stamps the time
    log.addHandler(h)
    log.setLevel(os.environ.get("CCC_LOG_LEVEL", "INFO").upper())
    log.propagate = False


_configure_logging()

app = FastAPI(title="CCC Head API", version="0.1.0")


@app.middleware("http")
async def access_log(request: Request, call_next):
    """One line per request: who, what, the outcome, and how long. The authenticated username is read
    from `request.state` (stashed by `current_user` during routing) — no extra DB lookup, and `-` for
    the routes that never resolve a user (healthz/readyz, a failed login). Path only, never the query
    string, headers, or body: `/passwd` and `/login` carry passwords and `/submit`'s `X-CCC-Meta` header
    carries the W&B key, none of which may reach a log. This supersedes uvicorn's own access log (the
    Head API unit runs it with `--no-access-log`)."""
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # An exception that escaped the routes (not an HTTPException — those come back as a response
        # below). Log it here with the caller and a traceback, then answer a clean 500 rather than
        # leaking a stack to the client. This is the API's only unhandled-error sink.
        dur = (time.perf_counter() - start) * 1000
        user = getattr(request.state, "username", "-")
        log.error("%s %s %s -> 500 %.0fms\n%s", user, request.method, request.url.path, dur,
                  traceback.format_exc())
        return JSONResponse({"detail": "internal server error"}, status_code=500)
    dur = (time.perf_counter() - start) * 1000
    user = getattr(request.state, "username", "-")
    status = response.status_code
    level = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
    log.log(level, "%s %s %s -> %d %.0fms", user, request.method, request.url.path, status, dur)
    return response

# Every API endpoint lives under `/api`, so no API path can ever collide with a client-side route the
# SPA owns (the Dashboard routes `/arms`, `/karma`, `/spawn/<id>` in the browser). Without this, a hard
# reload on `/karma` matched the API's `/karma` and returned raw JSON instead of the page. `/healthz`
# stays at the root — it's a liveness probe, not part of the app surface.
api = APIRouter(prefix="/api")


# --- auth models ------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


class PasswdRequest(BaseModel):
    old_password: str
    new_password: str


def password_policy_error(new: str, current: str) -> str | None:
    """The one place a new password is judged, for every caller of `/passwd` (both clients, admin
    rotations, and the forced first-login change). Returns a human reason to refuse, or None to accept.

    Deliberately minor: length + a light mix, plus "not the one you already have" so a forced rotation
    off an operator-issued temp password actually moves off it. The CLI mirrors this rule for an inline
    re-prompt (`cuttle`'s `auth._password_policy_error`) — keep the two in step; this side is the one
    that binds. `secrets.token_urlsafe` temp passwords clear it, so they are never wedged out at login.
    """
    if len(new) < 8:
        return "password must be at least 8 characters"
    classes = sum((any(c.islower() for c in new), any(c.isupper() for c in new),
                   any(c.isdigit() for c in new), any(not c.isalnum() for c in new)))
    if classes < 2:
        return "password must mix at least two of: lowercase, uppercase, digits, symbols"
    if new == current:
        return "new password must differ from your current one"
    return None


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
    # Hand the access-log middleware the resolved name (it runs outside the dependency system and would
    # otherwise have to re-look-up the session to know who called). scope["state"] is shared, so this
    # write is visible to the middleware once the route returns.
    request.state.username = user["username"]
    return {"username": user["username"], "is_admin": bool(user["is_admin"]),
            "must_change_password": bool(user["must_change_password"]), "id": user["id"]}


# --- health (open) + reads (authed) -----------------------------------------
# Reads were anonymous only because the SPA had no way to log in. It has one now, so they go behind the
# session — the board reveals who is running what, and on which donated machine. /healthz stays open:
# it is a liveness probe and says nothing.

@app.get("/healthz")
def healthz() -> dict:
    """Liveness: does the process answer at all? Deliberately cheap and dependency-free — it must stay
    200 while slurmctld restarts or head.db is briefly locked, so a monitor never bounces a healthy API
    for a downstream blip. Readiness is `/readyz`."""
    return {"ok": True}


def _db_ok() -> bool:
    try:
        with db.connect() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@app.get("/readyz")
def readyz(response: Response) -> dict:
    """Readiness: can the API actually serve? Pings its two hard dependencies — head.db and slurmctld —
    and reports each. 200 when both are green, 503 when either is down (with the culprit named), so a
    monitor can alert on the status code and a human can read which one from the body. Distinct from
    `/healthz` on purpose: a slurmctld restart makes this 503 without ever calling the process dead."""
    dep = {"db": "ok" if _db_ok() else "unreachable",
           "slurm": "ok" if slurm.reachable() else "unreachable"}
    if "unreachable" in dep.values():
        response.status_code = 503
    return dep


@api.get("/arms")
def arms(user: dict = Depends(current_user)) -> list[dict]:
    """Every compute node's live status + enrollment facts. See slurm.arms(). `retracted` is app-layer
    state slurm.arms() never sees (there is no real Slurm DRAIN behind it — see retraction.py) —
    enriched here the same way `/spawns` adds `submitted_by`."""
    rows = slurm.arms()
    with db.connect() as conn:
        retracted = db.retracted_nodes(conn)
    for r in rows:
        r["retracted"] = r["name"] in retracted
    return rows


def _arm_owner(name: str) -> str | None:
    return next((a.get("owner") for a in slurm.arms() if a["name"] == name), None)


@api.post("/arms/{name}/retract", status_code=204)
def retract_arm(name: str, user: dict = Depends(current_user)) -> Response:
    """Pause YOUR OWN Arm: stop taking new Spawns on it and end whatever is running there right now.

    App-layer (Phase 2 loose end), not real Slurm DRAIN — see retraction.py's module docstring for why
    that needs no privilege the Head API doesn't already have. Every Spawn currently RUNNING on this
    Arm is sent TERM immediately and has retraction.GRACE_SECONDS to exit before the Karma tick
    force-kills it; either way it settles at ZERO.

    **Retracting someone else's Arm is an admin action and is not reachable from here** (mirrors
    cancel_spawn): it is `cuttle arm retract <name> --any`, run on the Head. The ownership test below
    is unconditional — an Admin gets no branch.
    """
    owner = _arm_owner(name)
    if owner is None:
        raise HTTPException(404, f"unknown Arm {name!r}")
    if owner != user["username"]:
        raise HTTPException(403, "you can only retract your own Arm")
    retraction.retract(name)
    log.info("retract arm=%s by %s", name, user["username"])
    return Response(status_code=204)


@api.post("/arms/{name}/return", status_code=204)
def return_arm(name: str, user: dict = Depends(current_user)) -> Response:
    """Reopen YOUR OWN retracted Arm for new placement. Does not reprieve a Spawn already caught by the
    Retract — see retraction.py's module docstring. Same ownership rule as retract: no Admin branch."""
    owner = _arm_owner(name)
    if owner is None:
        raise HTTPException(404, f"unknown Arm {name!r}")
    if owner != user["username"]:
        raise HTTPException(403, "you can only return your own Arm")
    retraction.unretract(name)
    log.info("return arm=%s by %s", name, user["username"])
    return Response(status_code=204)


@api.get("/spawns")
def spawns(limit: int | None = None, user: dict = Depends(current_user)) -> list[dict]:
    """The live Spawn queue (pending/running, CCC partition), enriched with Attribution. Every Spawn
    runs as the Mantle now, so Slurm's own user field says `ccc` for all of them and is useless — the
    ledger is what says who submitted it. See slurm.spawns(). `limit` caps to the front of the queue
    (rows are ascending by id — running + next-to-run first); None = every row, the Dashboard's read."""
    rows = slurm.spawns()
    if limit:
        rows = rows[:limit]
    ids = [r["id"] for r in rows if r["id"] is not None]
    with db.connect() as conn:
        submitters = db.spawn_submitters(conn, ids)
    for r in rows:
        r["submitted_by"] = submitters.get(r["id"])
    return rows


# Declared BEFORE `/spawns/{job_id}`: FastAPI matches in order, and `history` would otherwise be parsed
# as an int job_id and 422.
@api.get("/spawns/history")
def spawns_history(days: int = 30, limit: int | None = None,
                   user: dict = Depends(current_user)) -> list[dict]:
    """The durable archive: Spawns submitted within `days`, newest first — everything the live queue has
    forgotten. Excludes what is still queued, so history and `/spawns` are disjoint (queue is now,
    history is done). Everyone's, like the queue: what ran on the shared cluster is not private. Each row
    carries the ledger's attribution, its settled cost, and the outcome the tick stamped (exit_code +
    finished_at)."""
    live = [r["id"] for r in slurm.spawns() if r["id"] is not None]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.connect() as conn:
        rows = [dict(r) for r in db.spawn_history(conn, since, live, limit)]
    # The same distinction GET /spawns/{id} draws, so a Spawn does not read "failed" here and
    # "cancelled" there. The ledger cannot answer it — it records the absent exit code, not the
    # reason — so the marker is consulted, and only for the rows that lack one.
    for r in rows:
        r["cancelled"] = (r["exit_code"] is None and r["finished_at"] is not None
                          and spawns_mod.was_cancelled(r["job_id"]))
    return rows


@api.get("/spawns/{job_id}")
def spawn_status(job_id: int, user: dict = Depends(current_user)) -> dict:
    """A single Spawn's state: still queued/running, or done with an exit code.

    The Attribution ledger is what distinguishes "this Spawn died" from "no such Spawn". A Spawn that
    is gone from squeue with no `exit` file never recorded one — it was scancel'd, OOM-killed, or its
    Arm dropped off the mesh before staging out. That is `failed`, not 404, and only the ledger can say
    so. Remote Arms are the flakiest nodes in the cluster (donor box sleeps, WSL shuts down), so this
    is the common case there, not a corner.

    A Spawn somebody *cancelled* lands in that same exit-code-less state, and reporting it as died-on-
    us is a lie the submitter can see through. It is told apart by the marker `DELETE /spawns/{id}`
    leaves — see spawns.mark_cancelled.
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
        # `cancelled` before `failed`: both lack an exit code, but one of them somebody asked for.
        if spawns_mod.was_cancelled(job_id):
            return {"id": job_id, "done": True, "state": "cancelled"}
        return {"id": job_id, "done": True, "state": "failed"}
    raise HTTPException(404, f"unknown Spawn {job_id}")


@api.get("/spawns/{job_id}/logs", response_class=PlainTextResponse)
def spawn_logs(job_id: int, user: dict = Depends(current_user)) -> str:
    """The Spawn's stdout so far (a remote Spawn's arrives only at stage-out — post-hoc)."""
    return spawns_mod.out_text(job_id) or ""


@api.get("/spawns/{job_id}/stderr", response_class=PlainTextResponse)
def spawn_stderr(job_id: int, user: dict = Depends(current_user)) -> str:
    """The Spawn's stderr — where a crash's traceback lands (`/logs` serves stdout, empty on a crash)."""
    return spawns_mod.err_text(job_id) or ""


@api.get("/spawns/{job_id}/outputs")
def spawn_outputs(job_id: int, user: dict = Depends(current_user)) -> Response:
    """gzip-tar of the Spawn's $CCC_OUT dir — a remote submitter's only way to get results back."""
    data = spawns_mod.outputs_tar(job_id)
    if data is None:
        raise HTTPException(404, f"no outputs for Spawn {job_id}")
    return Response(data, media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="spawn-{job_id}-outputs.tar.gz"'})


@api.delete("/spawns/{job_id}", status_code=204)
def cancel_spawn(job_id: int, user: dict = Depends(current_user)) -> Response:
    """End YOUR OWN live Spawn early. Attribution is the ledger's, not Slurm's (every Spawn runs as
    the Mantle).

    Closes the gap FRONTEND.md names most loudly: until now nothing in CCC could stop a job from any
    client, and under Karma a User was *paying* until `--time` expired. Settles for the seconds
    actually held, by the Karma tick, when the cancelled Spawn drops out of squeue — see slurm.cancel.

    **Cancelling someone else's Spawn is an admin WRITE and is not reachable from here** (Phase 6):
    it is `cuttle cancel <id> --any`, run on the Head. The ownership test below is unconditional —
    an Admin gets no branch — so a stolen admin Session is worth exactly one User's Session.
    """
    with db.connect() as conn:
        row = db.get_spawn(conn, job_id)
    # Only Spawns the Head actually recorded can be cancelled through here. An id squeue knows but the
    # ledger doesn't is not a CCC Spawn (or predates attribution); refuse rather than scancel blindly.
    if row is None:
        raise HTTPException(404, f"unknown Spawn {job_id}")
    if row["user_id"] != user["id"]:
        raise HTTPException(403, "you can only cancel your own Spawns")
    slurm.cancel(job_id)
    # AFTER the scancel, never before: the marker only changes how this Spawn is *described*, and a
    # Spawn left running because we could not write a file would be a real cost to its submitter. The
    # window between the two is a status read that says "failed" — the answer given before this
    # existed at all.
    spawns_mod.mark_cancelled(job_id)
    log.info("cancel id=%s by %s", job_id, user["username"])
    return Response(status_code=204)


# --- auth routes ------------------------------------------------------------

def _human_wait(seconds: float) -> str:
    seconds = int(seconds) + 1
    return f"{(seconds + 59) // 60}m" if seconds >= 60 else f"{seconds}s"


@api.post("/login")
def login(req: LoginRequest, request: Request, response: Response) -> dict:
    """Verify credentials → mint a session. Returns the bearer token (CLI) and sets the HttpOnly
    cookie (browser). A `must_change_password` user still gets a session so it can call /passwd.

    Throttled (Phase 8 loose end — see ratelimit.py): blocked by source IP or by the attempted
    username, whichever trips first, so neither one attacker IP spraying usernames nor a distributed
    guess against one account slips through."""
    ip = request.client.host if request.client else "?"
    wait = ratelimit.check(f"ip:{ip}", f"user:{req.username}")
    if wait is not None:
        raise HTTPException(429, f"too many failed attempts — try again in {_human_wait(wait)}",
                            headers={"Retry-After": str(int(wait) + 1)})
    with db.connect() as conn:
        user = db.verify_password(conn, req.username, req.password)
        if user is None:
            ratelimit.record_failure(f"ip:{ip}", f"user:{req.username}")
            # The one security event most worth a log: it feeds the rate-limiting item (Phase 8) and is
            # the signal a credential is being guessed. The attempted username and source, never the
            # password. WARNING, not ERROR — a fat-fingered password is not an incident.
            log.warning("login FAILED user=%s from %s", req.username, ip)
            raise HTTPException(401, "bad username or password")
        token = db.create_session(conn, user["id"])
    ratelimit.clear(f"ip:{ip}", f"user:{req.username}")
    log.info("login OK user=%s", user["username"])
    response.set_cookie(_COOKIE_NAME, token, max_age=_COOKIE_MAX_AGE, httponly=True,
                        samesite="lax", secure=_COOKIE_SECURE)
    return {"token": token, "username": user["username"], "is_admin": bool(user["is_admin"]),
            "must_change_password": bool(user["must_change_password"])}


@api.post("/logout")
def logout(request: Request, response: Response) -> dict:
    """Revoke the presented session (delete the row) and clear the cookie. Idempotent."""
    token = _token_from(request)
    if token:
        with db.connect() as conn:
            db.delete_session(conn, token)
    response.delete_cookie(_COOKIE_NAME)
    log.info("logout user=%s", getattr(request.state, "username", "-"))
    return {"ok": True}


@api.get("/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {"username": user["username"], "is_admin": user["is_admin"],
            "must_change_password": user["must_change_password"]}


@api.post("/passwd")
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
        # Policy is checked only after the old password is proven: a 422 otherwise would tell an
        # unauthenticated caller which candidate strings are well-formed. 422 (not 403): the request is
        # authenticated and the act is allowed — the payload just isn't acceptable.
        reason = password_policy_error(req.new_password, req.old_password)
        if reason is not None:
            raise HTTPException(422, reason)
        db.set_password(conn, user["username"], req.new_password)
    return {"ok": True}


# --- Karma (authed; Override is admin-only) ---------------------------------

@api.get("/karma")
def karma_standings(user: dict = Depends(current_user)) -> list[dict]:
    """Every User's standing — Balance, Escrow, Share, and the nice it implies — best first.

    Everyone's, not just your own: a Balance is meaningless in isolation. Karma ranks Users *against
    each other*, so a User who cannot see the others cannot see their own standing either.
    """
    with db.connect() as conn:
        return karma.standings(conn)


@api.get("/karma/ledger")
def karma_ledger(days: int = 30, user: dict = Depends(current_user)) -> dict:
    """**Your own** ledger: the real rows you were a party to, plus the Balance curve they imply.

    Yours alone, unlike `/karma` — the standings exist so a User can see where they stand *relative* to
    others, which needs everyone's number; a transaction history needs nobody else's.

    There are no Decay rows in `rows`, and the absence is the point: Decay is the sag of `curve` between
    the events, not an event. See karma.ledger().
    """
    with db.connect() as conn:
        return karma.ledger(conn, user["id"], days=days)


@api.get("/karma/demand")
def karma_demand(days: int = 7, user: dict = Depends(current_user)) -> list[dict]:
    """Demand per Arm over time — how badly each card has been wanted, hour by hour.

    This is the **only** surface the Tide is ever seen through, and it is deliberately not a forecast: a
    User reads the shape and infers the quiet hours for themselves. Nothing quotes them a predicted
    price, warns them, or asks them to confirm — *Karma ranks, it never gates*, and a blocking "this
    will cost you N" would have been the first thing in the model able to stop a Spawn.
    """
    with db.connect() as conn:
        return [dict(r) for r in db.demand_series(conn, days)]


# `POST /karma/override` USED TO LIVE HERE and was removed in Phase 6.
#
# It was defensible on its own terms: an Override touches nothing but head.db, which this API owns, so
# `is_admin` was the only privilege it needed. What it could not survive is the invariant the phase
# settled on — **the Head API exposes no admin-write endpoints at all**. Minting Karma from a stolen
# Session is the single most valuable thing an attacker could do with one, and no amount of
# authorization checking makes a remote write as safe as one that cannot be requested remotely.
#
# It is now `cuttle karma override`, on the Head, writing head.db through the API venv — the same
# shape as `cuttle user add` and `cuttle karma bump`. See ccc_head_api.admin.


# --- submission (authed) ----------------------------------------------------

@api.post("/submit")
async def submit(request: Request, user: dict = Depends(current_user)) -> dict:
    """Accept an uploaded project + metadata and launch a Spawn attributed to the caller. The only
    authed mutation in slice 2. Body = gzip-tar of the project; `X-CCC-Meta` header = JSON
    {script, gpu, args, node, wandb_env} — metadata in a header (not query params) keeps the W&B key
    out of access logs. Runs the render/sbatch/Attribution as the Mantle; returns the Spawn id.

    `minutes` and `vram` are **required** — a Spawn with no declared length cannot be priced, and one
    with no declared need cannot be placed. `vram` is OR-expanded here into a Slurm constraint over the
    fleet's real cards (slurm.vram_constraint); the client can't do it, because only the Head knows
    what hardware exists.

    Pinning a Spawn to a named Arm is **not possible over this route** (Phase 6). It was an
    admin-only `node` field until the phase settled that the Head API exposes no admin writes at all;
    a `node` in the metadata is now refused outright rather than honoured for an Admin, because a
    field that is silently ignored is worse than one that says no. It lives on as
    `cuttle run --node`, run on the Head. Users declare what they need (`--vram`) and let Slurm
    choose — naming a machine sidesteps eligibility, and with it the Demand the Spawn would have
    added to every card that could have run it.
    """
    if user["must_change_password"]:
        raise HTTPException(403, "change your password before submitting (run `cuttle passwd`)")
    meta = json.loads(request.headers.get("x-ccc-meta") or "{}")
    if not meta.get("script"):
        raise HTTPException(422, "missing 'script' in X-CCC-Meta — the entrypoint script is required")
    for field in ("minutes", "vram"):
        if not meta.get(field):
            raise HTTPException(422, f"missing '{field}' in X-CCC-Meta — --time and --vram are required")
    if meta.get("node"):
        raise HTTPException(403, "pinning a Spawn to an Arm is not available over the API — it is "
                                 "`cuttle run --node`, on the Head.")
    minutes, vram_gb, gpus = int(meta["minutes"]), int(meta["vram"]), int(meta.get("gpu", 1))
    tar_path = await _spool_upload(request)
    try:
        # Placement, pricing and the Attribution row all live in submit_checked, shared with the
        # Head-local pinned path — so a Spawn is quoted and recorded identically however it arrived.
        # ValueError covers both the unknown-Arm case (Head-local only in practice — `node` can never
        # reach here, see above) and the per-User concurrent-Spawn cap (Phase 8 loose end).
        job_id = spawns_mod.submit_checked(
            user["id"], user["is_admin"], meta["script"], gpus, meta.get("args", []),
            meta.get("wandb_env", {}), tar_path, minutes, vram_gb)
    except ValueError as e:
        raise HTTPException(422, str(e))
    finally:
        tar_path.unlink(missing_ok=True)
    log.info("submit id=%s by %s (gpu=%d vram=%d min=%d)", job_id, user["username"], gpus, vram_gb,
             minutes)
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


app.include_router(api)


class SPAFiles(StaticFiles):
    """Static files, but unknown paths fall back to index.html.

    The Dashboard routes client-side (/arms, /karma, /spawn/<id>). Those paths are not files, so a hard
    reload or a pasted link would otherwise 404 — the SPA never gets a chance to run and read the URL.
    This is why the API lives under `/api`: a client route like `/karma` reaches this fallback and gets
    the SPA, instead of being shadowed by an identically-named endpoint and returning JSON.
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


# Mounted LAST so the `/api` routes and `/healthz` win. html=True serves index.html at `/`. Guarded so
# the app still imports/serves the API before the SPA has been built (fresh checkout, tests).
if (WEB_DIR / "index.html").exists():
    app.mount("/", SPAFiles(directory=str(WEB_DIR), html=True), name="spa")
