"""CCC Head API app — the read board + SPA + auth + submission — served on LAN + mesh.

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
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import db, guises, karma, ratelimit, retraction, slurm
from . import features as features_mod   # `features` is the /features endpoint's own function name
from . import recipes as recipes_mod
from . import recovery as recovery_mod
from . import retention as retention_mod
from . import spawns as spawns_mod
from . import tidepool as tidepool_mod
from . import workspaces as workspaces_mod

# The built SPA, copied here by `cuttle setup --head` (world-readable). Kept out of the service user's
# home on purpose. Env var is set by the systemd unit; the default is only for fresh checkouts/tests.
WEB_DIR = Path(os.environ.get("CCC_WEB_DIR", "/opt/ccc/head-api/web"))

# This cluster's CA certificate — the public half only; the key beside it is 0600 and this process is
# the only account that can read it, which is exactly the asymmetry that makes serving this one safe.
# Mirrors cuttle.config.HEAD_CA_CERT.
CA_CERT = Path(os.environ.get("CCC_CA_CERT", "/opt/ccc/tls/ccc-ca.crt"))

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
    string, headers, or body: `/passwd` and `/login` carry passwords and `/account/wandb` carries a W&B
    key, none of which may reach a log. This supersedes uvicorn's own access log (the Head API unit
    runs it with `--no-access-log`)."""
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


class WandbKeyRequest(BaseModel):
    key: str


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


@app.get("/ca.pem", response_class=PlainTextResponse)
def ca_pem() -> str:
    """This cluster's CA certificate, so a box can trust the Head instead of clicking through a
    warning. Open, like /healthz: it is a public key, and a trust anchor nobody can fetch is one
    nobody installs.

    Serving it is not what makes it trustworthy — a caller has no reason to believe an unverified
    connection. What the caller does with it is (see cuttle/tls.ensure_ca): check that this CA actually
    signed the certificate just presented, show the operator its fingerprint, and only then install it.
    This route just has to be reachable before any of that is possible, which is why it sits on the
    main API rather than the mesh-only Enroll API — a client on the LAN has no mesh to reach.
    """
    if not CA_CERT.exists():
        raise HTTPException(status_code=503, detail="this Head has no CA certificate")
    return CA_CERT.read_text()


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


@api.get("/features")
def features(user: dict = Depends(current_user)) -> list[str]:
    """The Arm Features THIS HEAD'S VERSION supports (`cuttle.config.FEATURE_NAMES`) — what the submit
    composer offers to require.

    A constant, not state: there is nothing to mutate and no endpoint that could. It is still served
    rather than compiled into the Dashboard because a browser talking to an older Head should offer
    what that Head can actually place, which is the one thing a client cannot compute for itself."""
    return slurm.feature_catalog()


@api.get("/features/{name}/runtime", response_class=PlainTextResponse)
def feature_environment(name: str, user: dict = Depends(current_user)) -> str:
    """What CCC arranges for a Feature that has a runtime profile (`cuttle feature show`).

    Served rather than shipped in the CLI so the library list a Feature's software links against has
    exactly one copy — this one, which is also the list CCC builds into every Spawn that asks for it.

    404 for a Feature with no profile: `isaaclab` is a real, catalogued Feature that simply has no
    runtime to describe, and that is an answer rather than a failure."""
    if name not in features_mod.PROFILES:
        raise HTTPException(404, f"'{name}' has no runtime profile — nothing to describe. CCC supports "
                                 f"profiles for: {', '.join(sorted(features_mod.PROFILES))}.")
    return features_mod.runtime_summary(name)


@api.post("/arms/{name}/retract", status_code=204)
def retract_arm(name: str, user: dict = Depends(current_user)) -> Response:
    """Pause YOUR OWN Arm: stop taking new Spawns on it and end whatever is running there right now.

    App-layer, not real Slurm DRAIN — see retraction.py's module docstring for why
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


def _archive_row(r: dict) -> dict:
    """Finish an archive row from db.py — the two countdowns, and whether an exit-less end was asked for.

    Shared by `/spawns/history` and `/spawns/{job_id}` so a Spawn cannot read `failed` in the list and
    `cancelled` on its own page, or show two different expiry times for the same copy on disk. The
    created_at → expires_at conversion lives here rather than in the SQL because the window lengths are
    this module's constants (recovery_mod.WINDOW_MINUTES, retention_mod.TTL_DAYS), not db.py's business.
    """
    # The ledger cannot answer cancelled-vs-failed — it records the absent exit code, not the reason —
    # so the marker is consulted, and only for the rows that lack one.
    r["cancelled"] = (r["exit_code"] is None and r["finished_at"] is not None
                      and spawns_mod.was_cancelled(r["job_id"]))
    for src, dst, delta in (("recovery_created_at", "recovery_expires_at",
                             timedelta(minutes=recovery_mod.WINDOW_MINUTES)),
                            ("output_created_at", "output_expires_at",
                             timedelta(days=retention_mod.TTL_DAYS))):
        created = r.pop(src)
        r[dst] = None
        if created is not None:
            expires = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            r[dst] = (expires + delta).strftime("%Y-%m-%dT%H:%M:%SZ")
    return r


# Declared BEFORE `/spawns/{job_id}`: FastAPI matches in order, and `history` would otherwise be parsed
# as an int job_id and 422.
@api.get("/spawns/history")
def spawns_history(days: int = 30, limit: int | None = None,
                   user: dict = Depends(current_user)) -> list[dict]:
    """The durable archive: Spawns submitted within `days`, newest first — everything the live queue has
    forgotten. Excludes what is still queued, so history and `/spawns` are disjoint (queue is now,
    history is done). Everyone's, like the queue: what ran on the shared cluster is not private. Each row
    carries the ledger's attribution, its settled cost, and the outcome the tick stamped (exit_code +
    finished_at). Also carries a Recovery Window countdown, for the History row that still has one open — `recovery_expires_at` is null for every row that never had a copy, already had
    it evicted, or whose day is up — and, on the same terms, `output_expires_at`: when the
    Spawn's declared results are deleted, 7 days after it ended (null once they are, or before the tick
    has registered them)."""
    live = [r["id"] for r in slurm.spawns() if r["id"] is not None]
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.connect() as conn:
        return [_archive_row(dict(r)) for r in db.spawn_history(conn, since, live, limit)]


@api.get("/spawns/{job_id}")
def spawn_status(job_id: int, user: dict = Depends(current_user)) -> dict:
    """A single Spawn, whole: its state, and everything the archive knows about it.

    The Attribution ledger is what distinguishes "this Spawn died" from "no such Spawn". A Spawn that
    is gone from squeue with no `exit` file never recorded one — it was scancel'd, OOM-killed, or its
    Arm dropped off the mesh before staging out. That is `failed`, not 404, and only the ledger can say
    so. Remote Arms are the flakiest nodes in the cluster (donor box sleeps, WSL shuts down), so this
    is the common case there, not a corner.

    A Spawn somebody *cancelled* lands in that same exit-code-less state, and reporting it as died-on-
    us is a lie the submitter can see through. It is told apart by the marker `DELETE /spawns/{id}`
    leaves — see spawns.mark_cancelled.

    The archive fields (cost, node, script, the shape it asked for, both expiry countdowns) ride along
    when there is a `spawns` row, which is every Spawn the API submitted. They are what let the Spawn
    page render from ONE fetch without caring whether its subject is queued, running or a year old —
    the alternative was pulling the whole cluster archive to show a single Spawn. The CLI polls this
    route and ignores them; the keys it reads (`done`, `state`, `exit`) are unchanged.

    A Spawn with no `spawns` row is not an error here: the enrollment benchmark is sbatched outside the
    API, so it is live in Slurm and absent from the ledger. It answers with state alone.
    """
    state = spawns_mod.active_state(job_id)
    rc = None if state is not None else spawns_mod.exit_code(job_id)
    with db.connect() as conn:
        row = db.spawn_detail(conn, job_id)

    if state is not None:
        out = {"id": job_id, "done": False, "state": state}
    elif rc is not None:
        out = {"id": job_id, "done": True, "state": "done", "exit": rc}
    elif row is not None:
        # `cancelled` before `failed`: both lack an exit code, but one of them somebody asked for.
        out = {"id": job_id, "done": True,
               "state": "cancelled" if spawns_mod.was_cancelled(job_id) else "failed"}
    else:
        raise HTTPException(404, f"unknown Spawn {job_id}")

    if row is not None:
        archive = _archive_row(dict(row))
        archive.pop("job_id")
        archive.pop("cancelled")  # `state` already says it, and says it for the live cases too
        out |= archive
    return out


# What a swept Spawn's logs read as. A Spawn that printed nothing also returns "", so without this the
# two are the same answer — and after retention takes the directory, every old Spawn looks silent.
_SWEPT = "this Spawn's logs and results have expired\n"


@api.get("/spawns/{job_id}/logs", response_class=PlainTextResponse)
def spawn_logs(job_id: int, user: dict = Depends(current_user)) -> str:
    """The Spawn's stdout so far (a remote Spawn's arrives only at stage-out — post-hoc)."""
    return _SWEPT if spawns_mod.swept(job_id) else (spawns_mod.out_text(job_id) or "")


@api.get("/spawns/{job_id}/stderr", response_class=PlainTextResponse)
def spawn_stderr(job_id: int, user: dict = Depends(current_user)) -> str:
    """The Spawn's stderr — where a crash's traceback lands (`/logs` serves stdout, empty on a crash)."""
    return _SWEPT if spawns_mod.swept(job_id) else (spawns_mod.err_text(job_id) or "")


@api.get("/spawns/{job_id}/outputs")
def spawn_outputs(job_id: int, user: dict = Depends(current_user)) -> Response:
    """gzip-tar of the Spawn's $CCC_OUT dir — a remote submitter's only way to get results back.

    Results are not permanent: 7 days from the Spawn's end,
    or sooner if the submitter's 32GB Allowance evicted them to make room for newer ones. Both read as
    404 here, and the detail says which of the two applies where the ledger still knows — a Spawn that
    produced nothing and one whose results were deleted are very different things to be told."""
    data = spawns_mod.outputs_tar(job_id)
    if data is None:
        with db.connect() as conn:
            known = db.get_spawn(conn, job_id) is not None
        if known and retention_mod.evicted(job_id):
            raise HTTPException(404, f"Spawn {job_id}'s outputs are no longer held — results are kept "
                                     f"for {retention_mod.TTL_DAYS} days after a Spawn ends, and only "
                                     f"until the submitter's newer results need the room")
        raise HTTPException(404, f"Spawn {job_id} wrote no outputs — a Spawn returns files by writing "
                                 f"them to $CCC_OUT inside the Pod")
    return Response(data, media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="spawn-{job_id}-outputs.tar.gz"'})


@api.get("/spawns/{job_id}/retention")
def spawn_retention(job_id: int, user: dict = Depends(current_user)) -> dict:
    """How much of Spawn `job_id`'s declared results the Head still holds, and when it deletes them.
    404 means nothing held: the Spawn produced nothing, the tick has
    not registered it yet, its 7 days are up, or the submitter's 32GB Allowance evicted it.

    Named for the policy rather than `/outputs/…` because `/spawns/{id}/outputs` is already the tar
    itself, and one path returning metadata for a GET and bytes for a sibling GET is the kind of shape
    nobody reads twice. Same permission as that route and `/recovery`: authed, no ownership check."""
    with db.connect() as conn:
        st = retention_mod.status(conn, job_id)
    if st is None:
        raise HTTPException(404, f"no outputs held for Spawn {job_id}")
    return st


@api.get("/spawns/{job_id}/recovery")
def spawn_recovery(job_id: int, user: dict = Depends(current_user)) -> dict:
    """Whether Spawn `job_id` still has an open Recovery Window copy, and if so how much of it and when
    it closes. 404 means no copy: never made (skipped for the 16GB per-Spawn cap, or the Arm dropped
    before it landed), evicted to make room for a newer copy of the submitter's own (the per-User
    Allowance, or the pool backstop behind it), or its day is up — the CLI and Dashboard both read all
    of them the same way, "nothing left to recover", and the detail stays deliberately generic: the
    Allowance is the one a User is never quoted a number for (CONTEXT.md, Recovery Window).

    Same permission shape as `/spawns/{id}/outputs` (authed, no ownership check) — this is data about a
    shared cluster's Spawn, not a secret, unlike the Tide Pool SSH keypair route, which IS ownership-
    gated for exactly that reason.
    """
    with db.connect() as conn:
        st = recovery_mod.status(conn, job_id)
    if st is None:
        raise HTTPException(404, f"no recovery copy for Spawn {job_id}")
    return st


@api.get("/spawns/{job_id}/recovery/download")
def spawn_recovery_download(job_id: int, user: dict = Depends(current_user)) -> Response:
    """gzip-tar of Spawn `job_id`'s Recovery Window copy — everything it wrote OUTSIDE its declared
    `$CCC_OUT`, while the window is still open. Distinct download from
    `/outputs`: the declared tar is permanent, this one is not, and the two are never merged into one
    artifact whose contents would otherwise silently change once the window closes."""
    data = recovery_mod.tar_bytes(job_id)
    if data is None:
        raise HTTPException(404, f"no recovery copy for Spawn {job_id}")
    return Response(data, media_type="application/gzip", headers={
        "Content-Disposition": f'attachment; filename="spawn-{job_id}-recovery.tar.gz"'})


@api.delete("/spawns/{job_id}", status_code=204)
def cancel_spawn(job_id: int, user: dict = Depends(current_user)) -> Response:
    """End YOUR OWN live Spawn early. Attribution is the ledger's, not Slurm's (every Spawn runs as
    the Mantle).

    Closes the gap FRONTEND.md names most loudly: until now nothing in CCC could stop a job from any
    client, and under Karma a User was *paying* until `--time` expired. Settles for the seconds
    actually held, by the Karma tick, when the cancelled Spawn drops out of squeue — see slurm.cancel.

    **Cancelling someone else's Spawn is an admin WRITE and is not reachable from here**: it is
    `cuttle cancel <id> --any`, run on the Head. The ownership test below is unconditional — an Admin
    gets no branch — so a stolen admin Session is worth exactly one User's Session.
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

    Throttled (see ratelimit.py): blocked by source IP or by the attempted
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
            # The one security event most worth a log: it feeds the rate-limiter and is
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


# --- your account: the stored W&B key (authed) ------------------------------
# The Head holds one W&B key per User and injects it into every Spawn they submit, from either client
# (see spawns.compose_wandb_env). It lives here rather than in the CLI's config file because the
# **Dashboard** is the primary submission surface and a browser has no config file to read — every
# Spawn from it ran with W&B disabled until these routes existed.
#
# Reveal takes no password re-prompt, deliberately. A stolen session can already exfiltrate the key by
# submitting a Spawn that echoes $WANDB_API_KEY, so a prompt here would guard a door with no wall
# beside it — it would cost every honest User a password every time they wanted to check what they had
# pasted, and cost an attacker nothing.

@api.get("/account/wandb")
def wandb_key(user: dict = Depends(current_user)) -> dict:
    """Masked status: whether a key is stored, and its last 4 characters. Never the plaintext."""
    with db.connect() as conn:
        return db.wandb_key_status(conn, user["id"])


@api.get("/account/wandb/reveal")
def wandb_key_reveal(user: dict = Depends(current_user)) -> dict:
    """The plaintext, for the deliberate Reveal. Its own route so the masked read — which both clients
    poll — never carries the secret at all."""
    with db.connect() as conn:
        key = db.get_wandb_key(conn, user["id"])
    if key is None:
        raise HTTPException(404, "you have no W&B key stored")
    return {"key": key}


@api.post("/account/wandb")
def set_wandb_key(req: WandbKeyRequest, user: dict = Depends(current_user)) -> dict:
    """Store (or replace) your key. Encrypted at rest — see atrest.py, which is honest about what that
    does and does not buy."""
    key = req.key.strip()
    if not key:
        raise HTTPException(422, "the key is empty")
    with db.connect() as conn:
        db.set_wandb_key(conn, user["id"], key)
    # Never the key, and never its length: this line goes to journald, which is not where a secret's
    # shape belongs. That a User set one is worth knowing; what they set is not.
    log.info("wandb key set by %s", user["username"])
    return {"ok": True}


@api.delete("/account/wandb", status_code=204)
def clear_wandb_key(user: dict = Depends(current_user)) -> Response:
    """Forget the key — the Head stops injecting it, and your Spawns go back to WANDB_MODE=disabled."""
    with db.connect() as conn:
        db.clear_wandb_key(conn, user["id"])
    log.info("wandb key cleared by %s", user["username"])
    return Response(status_code=204)


# --- Karma (authed; Override is admin-only) ---------------------------------

@api.get("/karma")
def karma_standings(user: dict = Depends(current_user)) -> list[dict]:
    """Every User's standing — Balance, Escrow, Share, and the nice it implies — best first.

    Everyone's, not just your own: a Balance is meaningless in isolation. Karma ranks Users *against
    each other*, so a User who cannot see the others cannot see their own standing either.

    `escrow_spawns` breaks the Escrow total into the individual claims holding it, dearest first — the
    only form of that number a User can act on, since one of them is usually the sweep they forgot to
    cancel. It sums to `escrow` by construction (same ratchet, un-aggregated).
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


@api.get("/karma/usage")
def karma_usage(days: int = 30, user: dict = Depends(current_user)) -> list[dict]:
    """Who spent the cluster: one daily Karma-spend series per User over `days`, heaviest first.

    The people-shaped counterpart to `/karma/demand`, which is card-shaped, and read against it — a
    month of high Demand is explained by whoever's band swallows this plot. Everyone's, on exactly the
    terms the standings are: usage is only legible in proportion, so a User shown their own series
    alone would learn nothing from it.

    Spend, not GPU-hours: a settle row already weights an hour by the card's Power and the Demand it ran
    against, and raw hours would call the quiet little card's hour equal to the coveted one's.
    """
    with db.connect() as conn:
        return karma.usage(conn, days=days)


# `POST /karma/override` does not exist here — an admin write, and **the Head API exposes no
# admin-write endpoints at all**.
#
# It would be defensible on its own terms: an Override touches nothing but head.db, which this API
# owns, so `is_admin` would be the only privilege it needed. But minting Karma from a stolen Session is
# the single most valuable thing an attacker could do with one, and no amount of authorization checking
# makes a remote write as safe as one that cannot be requested remotely.
#
# It is `cuttle karma override`, on the Head, writing head.db through the API venv — the same shape as
# `cuttle user add` and `cuttle karma bump`. See ccc_head_api.admin.


# --- submission (authed) ----------------------------------------------------

@api.post("/submit")
async def submit(request: Request, user: dict = Depends(current_user)) -> dict:
    """Accept an uploaded project + metadata and launch a Spawn attributed to the caller. The only
    authed mutation. Body = gzip-tar of the project; `X-CCC-Meta` header = JSON
    {script, gpu, args, node, wandb_env, outputs, features}. Runs the render/sbatch/Attribution as the
    Mantle; returns the Spawn id.

    `features` is optional, and empty when absent — a Spawn that requests none is placed exactly as it
    always was. Each name is checked against every currently admitted Arm's declared features
    (`slurm.arms_with_features`); a request no Arm can satisfy is refused with 422 before ever reaching
    sbatch, same hard-validation stance as `vram`.

    `outputs` is a non-empty list of subdir names made under the one `$CCC_OUT` root before the payload
    runs — required, with no default: a Spawn nobody named an output for would produce results with
    nowhere retrievable to land.

    `wandb_env` no longer carries a key — it never could from the browser, which is why the Dashboard
    logged no metrics at all. The caller's W&B *project/entity* are honoured and everything else in it
    is discarded; the key is the Head's, per User (see spawns.compose_wandb_env, /account/wandb).
    Whether to log at all is the separate `wandb` flag, defaulting to true when absent — it rides
    beside `wandb_env` rather than inside it so a client can still say "not this one" without being
    handed back the WANDB_MODE the Head just took off it.

    `minutes` and `vram` are **required** — a Spawn with no declared length cannot be priced, and one
    with no declared need cannot be placed. `vram` is the **total** across the `gpus` Devices asked
    for, and is OR-expanded here into a Slurm constraint over the fleet's real cards
    (slurm.vram_constraint); the client can't do it, because only the Head knows what hardware exists.

    Pinning a Spawn to a named Arm is **not possible over this route**: the Head API exposes no admin
    writes at all, so a `node` in the metadata is refused outright rather than honoured for an Admin —
    a field that is silently ignored is worse than one that says no. It lives on as `cuttle run --node`,
    run on the Head. Users declare what they need (`--vram`) and let Slurm choose — naming a machine
    sidesteps eligibility, and with it the Demand the Spawn would have added to every card that could
    have run it.

    `tidepool` is the one flag that changes what the other fields mean: a
    live shell instead of a script. It relaxes exactly two of the checks below — `script` and `outputs`
    — and nothing else; time/vram/gpu/features/pricing/placement/Attribution are all identical to any
    other Spawn. The Dashboard's Tide Pool mode sends an EMPTY body (no project — there is nothing to
    upload) and omits `script`; the CLI does the same for `cuttle run --tidepool`.
    """
    if user["must_change_password"]:
        raise HTTPException(403, "change your password before submitting (run `cuttle passwd`)")
    meta = json.loads(request.headers.get("x-ccc-meta") or "{}")
    tidepool = bool(meta.get("tidepool"))
    if not meta.get("script") and not tidepool:
        raise HTTPException(422, "missing 'script' in X-CCC-Meta — the entrypoint script is required")
    for field in ("minutes", "vram"):
        if not meta.get(field):
            raise HTTPException(422, f"missing '{field}' in X-CCC-Meta — --time and --vram are required")
    # At least one declared output subdir: a Spawn nobody named an output for is one whose results have
    # nowhere retrievable to land, which is worse discovered after the fact than refused up front.
    # `outputs` also gets the shell-injection check (OUTPUT_NAME_RE) in spawns.submit — this is only the
    # "did the client send any" gate. Skipped for a Tide Pool alone — see submit_checked's docstring.
    outputs = meta.get("outputs") or []
    if not isinstance(outputs, list):
        raise HTTPException(422, "'outputs' in X-CCC-Meta must be a list")
    if not outputs and not tidepool:
        raise HTTPException(422, "missing 'outputs' in X-CCC-Meta — at least one output folder name is "
                                 "required (--output/-o)")
    if meta.get("node"):
        raise HTTPException(403, "pinning a Spawn to an Arm is not available over the API — it is "
                                 "`cuttle run --node`, on the Head.")
    # The **Environment** every Spawn declares (ccc_head_api.environments). Gated here, beside the
    # outputs gate and for the same reason: refused before the upload is spooled to disk rather than
    # after. `spawns.submit` re-checks it against the extracted project — this is only the "did the
    # client send one" half. Skipped for a Tide Pool alone, the one Spawn that may decline to declare.
    if not (meta.get("manifest") or "").strip() and not tidepool:
        raise HTTPException(422, "missing 'manifest' in X-CCC-Meta — name the dependency file to build "
                                 "this Spawn's environment from (--manifest), e.g. pyproject.toml, "
                                 "uv.lock, requirements.txt or environment.yml")
    # `gpus` is the current name; `gpu` is what older clients send. Either is accepted — see
    # spawns.submit_checked, which is where a count no Arm can host is refused.
    minutes, vram_gb = int(meta["minutes"]), int(meta["vram"])
    gpus = int(meta.get("gpus", meta.get("gpu", 1)))
    tar_path = await _spool_upload(request)
    try:
        # Placement, pricing and the Attribution row all live in submit_checked, shared with the
        # Head-local pinned path — so a Spawn is quoted and recorded identically however it arrived.
        # ValueError covers both the unknown-Arm case (Head-local only in practice — `node` can never
        # reach here, see above), the per-User concurrent-Spawn cap, an invalid output name, and a
        # `script` that isn't actually in the uploaded tar.
        job_id = spawns_mod.submit_checked(
            user["id"], user["is_admin"], meta.get("script"), gpus, meta.get("args", []),
            meta.get("wandb_env", {}), tar_path, minutes, vram_gb, outputs,
            wandb=bool(meta.get("wandb", True)), features=meta.get("features") or [],
            tidepool=tidepool, manifest=meta.get("manifest"))
    except ValueError as e:
        raise HTTPException(422, str(e))
    finally:
        tar_path.unlink(missing_ok=True)
    log.info("submit id=%s by %s (gpu=%d vram=%d min=%d tidepool=%s)", job_id, user["username"], gpus,
             vram_gb, minutes, tidepool)
    # `environment` rides back on the submit response so both clients can echo it in the same breath as
    # the id — the point of the echo is that a User sees which dependency file was picked up (or that
    # none was) BEFORE they walk away, not when they come back to a Spawn that ran against the wrong one.
    return {"id": job_id, "environment": spawns_mod.environment_note(job_id)}


# --- recipes (authed) -------------------------------------------------------
# A **Recipe** is a User's frozen, re-runnable snapshot, and these routes are the whole of its lifecycle
# except one: `run` is deferred (see below). Every route is scoped to the caller — a Recipe is private to
# its owner, so there is no roster read here and no `user_id` any client can name. See recipes.py for the
# 32GB wall these hang off, and why it is a wall rather than the eviction `retention.py` does.
#
# Uploads here are spooled against the Recipe **Allowance**, not `MAX_UPLOAD_BYTES`: the 4GB cap is a
# Spawn's, and a Recipe is bounded by the wall it is about to be weighed against instead. Both are the
# same 413 to a client — this one just says which limit was hit.

@api.get("/recipes")
def recipes(user: dict = Depends(current_user)) -> list[dict]:
    """The caller's own shelf. Never anyone else's: there is no admin view of Recipes, because a Recipe
    holds a User's actual source code rather than a fact about the cluster.

    `running` is best-effort: it asks the live Slurm queue, and a shelf a User can save/update/delete
    entirely without Slurm must not start 503ing because the scheduler hiccuped answering an ENRICHMENT
    to it. A Recipe just says `running: false` on a Head that can't be asked right now — the same as
    before this existed — rather than hiding the whole shelf."""
    try:
        live_ids = [j["id"] for j in slurm.queue() if j["id"] is not None]
    except HTTPException:
        live_ids = []
    with db.connect() as conn:
        rows = db.recipes_for(conn, user["id"])
        running = db.running_recipe_names(conn, user["id"], live_ids)
    return [recipes_mod.to_json(r, running=r["name"] in running) for r in rows]


@api.post("/recipes", status_code=201)
async def create_recipe(request: Request, user: dict = Depends(current_user)) -> dict:
    """Freeze an upload into a new Recipe. Same wire shape as `/submit` — gzip-tar body, `X-CCC-Meta`
    header — because it is the same upload, persisted instead of run once.

    Runs **nothing**: no Spawn, no Escrow, no queue place. That is deliberate and it is what keeps the
    wall out of the submit path, so a full shelf can never stop a User working (CONTEXT.md,
    **Allowance**)."""
    if user["must_change_password"]:
        raise HTTPException(403, "change your password before saving a recipe (run `cuttle passwd`)")
    meta = json.loads(request.headers.get("x-ccc-meta") or "{}")
    tar_path = await _spool_upload(request, limit=recipes_mod.MAX_USER_BYTES)
    try:
        with db.connect() as conn:
            saved = recipes_mod.save(conn, user["id"], meta, tar_path)
    except recipes_mod.WallExceeded as e:
        raise HTTPException(413, _wall_message(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    finally:
        tar_path.unlink(missing_ok=True)
    log.info("recipe save %r by %s (%s)", saved["name"], user["username"], _human(saved["size_bytes"]))
    return saved


@api.post("/recipes/{name}/update")
async def update_recipe(name: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """Re-capture a Recipe around fresh code — the one moment a frozen thing moves.

    Delete-then-save: the old snapshot's bytes are released before the new ones are weighed, so this is
    charged what it GROWS BY rather than needing room for both at once. The wall is still weighed before
    anything is written, so an update this refuses leaves the old version intact and still runnable."""
    if user["must_change_password"]:
        raise HTTPException(403, "change your password before updating a recipe (run `cuttle passwd`)")
    meta = json.loads(request.headers.get("x-ccc-meta") or "{}")
    meta.setdefault("name", name)
    tar_path = await _spool_upload(request, limit=recipes_mod.MAX_USER_BYTES)
    try:
        with db.connect() as conn:
            saved = recipes_mod.save(conn, user["id"], meta, tar_path, replacing=name)
    except recipes_mod.WallExceeded as e:
        raise HTTPException(413, _wall_message(e))
    except ValueError as e:
        # "no recipe named X" is the one ValueError here that is a miss rather than a bad request.
        raise HTTPException(404 if str(e).startswith("no recipe named") else 422, str(e))
    finally:
        tar_path.unlink(missing_ok=True)
    log.info("recipe update %r by %s (%s)", name, user["username"], _human(saved["size_bytes"]))
    return saved


@api.patch("/recipes/{name}")
async def update_recipe_settings(name: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """Change a Recipe's settings without re-freezing its code — a plain JSON body, not the gzip-tar
    `X-CCC-Meta` upload `create_recipe`/`update_recipe` share, since nothing here touches the 32GB wall or
    `bytes`/`frozen_at`. What the Dashboard's "Edit" hits when no folder was dropped alongside it; a
    dropped folder instead goes to `update_recipe` above, which DOES re-freeze the code."""
    if user["must_change_password"]:
        raise HTTPException(403, "change your password before editing a recipe (run `cuttle passwd`)")
    meta = await request.json()
    try:
        with db.connect() as conn:
            saved = recipes_mod.update_meta(conn, user["id"], name, meta)
    except ValueError as e:
        raise HTTPException(404 if str(e).startswith("no recipe named") else 422, str(e))
    log.info("recipe settings edit %r by %s", name, user["username"])
    return saved


@api.post("/recipes/{name}/run")
def run_recipe(name: str, user: dict = Depends(current_user)) -> dict:
    """Run the frozen snapshot as a new Spawn — nothing to upload, nothing to hand, from any machine.
    Every setting is frozen with the Recipe already, so this takes no body at all; see `recipes.run`."""
    if user["must_change_password"]:
        raise HTTPException(403, "change your password before running a recipe (run `cuttle passwd`)")
    try:
        with db.connect() as conn:
            job_id = recipes_mod.run(conn, user["id"], name, user["is_admin"])
    except ValueError as e:
        raise HTTPException(404 if str(e).startswith("no recipe named") else 422, str(e))
    log.info("recipe run %r by %s -> spawn %s", name, user["username"], job_id)
    return {"id": job_id}


@api.delete("/recipes/{name}", status_code=204)
def delete_recipe(name: str, user: dict = Depends(current_user)) -> Response:
    """The only way bytes ever leave a shelf. Nothing on the Head deletes a Recipe on a User's behalf —
    no TTL, no eviction — so this is what the wall sends them to do."""
    with db.connect() as conn:
        if not recipes_mod.delete(conn, user["id"], name):
            raise HTTPException(404, f"you have no recipe named {name!r}")
    log.info("recipe delete %r by %s", name, user["username"])
    return Response(status_code=204)


# --- workspaces (authed) ----------------------------------------------------
# A **Workspace** is a durable working directory — the Tide Pool's persistent form. These routes are its
# whole lifecycle except the visit itself (`open`, which submits a Tide Pool bound to it). Scoped to the
# caller like the Recipe routes above and for the same reason: a Workspace holds a User's actual files,
# so there is no roster read and no `user_id` any client can name.
#
# The seed upload is spooled against the Workspace **Allowance** rather than `MAX_UPLOAD_BYTES`: the 4GB
# cap is a Spawn's, and a Workspace is bounded by the Storage it is about to be weighed against.

@api.get("/workspaces")
def workspaces(user: dict = Depends(current_user)) -> list[dict]:
    """The caller's own Workspaces, with what each holds against its **Storage**.

    `used_bytes` is measured off the disk here and now — see workspaces.usage for why nothing caches
    it. One walk per Workspace, on a shelf that is a handful of entries.
    """
    with db.connect() as conn:
        rows = db.workspaces_for(conn, user["id"])
    return [workspaces_mod.to_json(r, workspaces_mod.usage(user["id"], r["name"])) for r in rows]


@api.post("/workspaces", status_code=201)
async def create_workspace(request: Request, user: dict = Depends(current_user)) -> dict:
    """Seed a directory into a new Workspace — the only upload one ever takes. Same wire shape as
    `/submit` and `/recipes`: gzip-tar body, `X-CCC-Meta` header.

    Runs nothing and costs no Karma. Storage never does; the visits do."""
    if user["must_change_password"]:
        raise HTTPException(403, "change your password before creating a workspace (run `cuttle passwd`)")
    meta = json.loads(request.headers.get("x-ccc-meta") or "{}")
    tar_path = await _spool_upload(request, limit=workspaces_mod.MAX_USER_BYTES)
    try:
        with db.connect() as conn:
            made = workspaces_mod.create(conn, user["id"], meta, tar_path)
    except workspaces_mod.PoolExceeded as e:
        raise HTTPException(413, _pool_message(e))
    except workspaces_mod.SeedTooBig as e:
        raise HTTPException(413, f"this directory is {_human(e.size)} but the workspace reserves a "
                                 f"{_human(e.storage)} storage — ask for a bigger --storage, or seed less")
    except ValueError as e:
        raise HTTPException(422, str(e))
    finally:
        tar_path.unlink(missing_ok=True)
    log.info("workspace create %r by %s (%s storage)", made["name"], user["username"],
             _human(made["storage_bytes"]))
    return made


@api.patch("/workspaces/{name}")
async def update_workspace(name: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """Change what a visit asks for, and the **Storage** the tree may fill. Never the files: a Workspace is
    seeded once and written to from inside its own sessions thereafter.

    A resize while a visit is live is accepted and takes effect on the next one — the running session's
    gate is baked into its job script (see workspaces.update_meta)."""
    if user["must_change_password"]:
        raise HTTPException(403, "change your password before editing a workspace (run `cuttle passwd`)")
    meta = await request.json()
    try:
        with db.connect() as conn:
            saved = workspaces_mod.update_meta(conn, user["id"], name, meta)
    except workspaces_mod.PoolExceeded as e:
        raise HTTPException(413, _pool_message(e))
    except workspaces_mod.StorageTooSmall as e:
        raise HTTPException(422, f"this workspace already holds {_human(e.used)}, so its storage cannot "
                                 f"be set to {_human(e.asked)} — free space inside it first "
                                 f"(`cuttle workspace open`), or ask for more")
    except ValueError as e:
        raise HTTPException(404 if str(e).startswith("no workspace named") else 422, str(e))
    log.info("workspace edit %r by %s", name, user["username"])
    return saved


@api.post("/workspaces/{name}/open")
async def open_workspace(name: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """Start a visit, or hand back the one already running.

    A visit is a **Tide Pool** bound to this Workspace — same queue, same Eligibility, same Escrow — so
    the client attaches to it through `GET /spawns/{id}/tidepool` exactly as it would any other. What
    differs is that the session starts from the durable tree and mirrors back into it.

    A Workspace holds ONE visit at a time, so this never queues a second Spawn behind a live one: it
    returns that one's id with `attached: true`. The body is optional per-visit overrides (vram, time,
    gpus, features, outputs, manifest) — they change this session only and are never written back."""
    if user["must_change_password"]:
        raise HTTPException(403, "change your password before opening a workspace (run `cuttle passwd`)")
    try:
        overrides = await request.json()
    except Exception:
        overrides = {}   # no body at all is the ordinary case: open it as it was declared
    try:
        with db.connect() as conn:
            job_id, attached = workspaces_mod.visit(conn, user["id"], name, user["is_admin"],
                                                    overrides if isinstance(overrides, dict) else {})
    except ValueError as e:
        raise HTTPException(404 if str(e).startswith("no workspace named") else 422, str(e))
    log.info("workspace open %r by %s -> spawn %s%s", name, user["username"], job_id,
             " (attached)" if attached else "")
    return {"id": job_id, "attached": attached}


@api.get("/workspaces/{name}/full")
def workspace_full(name: str, user: dict = Depends(current_user)) -> dict:
    """Whether the live visit's last mirror found the tree over its **Storage**, and by how much.

    The Head cannot see what the Arm is holding — only what the session's 60s mirror tells it — so this
    reads the marker that comes home through `[out-…]`. `bytes` is 0 when the visit is under its Storage,
    which is what makes recovering from full visible rather than merely the absence of a warning. The
    CLI polls it during a session to warn without ending it (nothing is ever evicted)."""
    with db.connect() as conn:
        row = db.get_workspace(conn, user["id"], name)
    if row is None:
        raise HTTPException(404, f"you have no workspace named {name!r}")
    job_id = row["live_job_id"] if (row["live_job_id"] or 0) > 0 else None
    over = workspaces_mod.full_marker(job_id) if job_id else 0
    return {"live": job_id, "full": over > 0, "bytes": over, "storage_bytes": row["storage_bytes"]}


@api.get("/workspaces/{name}/download")
def download_workspace(name: str, path: str | None = None,
                       user: dict = Depends(current_user)) -> Response:
    """The whole tree as a gzip-tar, or the subtree named by `?path=`. Live or idle — a Workspace being
    visited is still readable, it is just a moment older than what the session sees.

    Streamed, never buffered (`workspaces.tar_stream`): a Storage runs to 16GB and up, so the in-memory
    shape `/spawns/{id}/outputs` uses would put the whole tree in this process."""
    with db.connect() as conn:
        row = db.get_workspace(conn, user["id"], name)
    if row is None:
        raise HTTPException(404, f"you have no workspace named {name!r}")
    try:
        target = workspaces_mod.resolve(user["id"], name, path)
    except ValueError as e:
        raise HTTPException(404, str(e))
    stem = f"workspace-{name}" + (f"-{Path(path).name}" if path else "")
    return StreamingResponse(
        workspaces_mod.tar_stream(target, stem), media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{stem}.tar.gz"'})


@api.delete("/workspaces/{name}", status_code=204)
def delete_workspace(name: str, user: dict = Depends(current_user)) -> Response:
    """Delete a Workspace and free its **Storage**. The only thing that ever removes one — no TTL, no
    eviction. Refused while a visit is live: the tree is what that session is mirroring into."""
    try:
        with db.connect() as conn:
            if not workspaces_mod.delete(conn, user["id"], name):
                raise HTTPException(404, f"you have no workspace named {name!r}")
    except workspaces_mod.Live as e:
        raise HTTPException(409, f"workspace {name!r} is being visited by spawn {e.job_id} — end that "
                                 f"session first (`cuttle cancel {e.job_id}`)")
    log.info("workspace delete %r by %s", name, user["username"])
    return Response(status_code=204)


def _pool_message(e: "workspaces_mod.PoolExceeded") -> str:
    """The Workspace Allowance is the one its holder divides up themselves (CONTEXT.md, **Storage**), so
    the refusal has to say what is left of the pool as well as what was asked for."""
    return (f"this workspace asks for {_human(e.incoming)} but only "
            f"{_human(max(workspaces_mod.MAX_USER_BYTES - e.used, 0))} of your "
            f"{_human(workspaces_mod.MAX_USER_BYTES)} workspace allowance is free — delete a workspace, "
            f"or ask for a smaller storage")


def _wall_message(e: "recipes_mod.WallExceeded") -> str:
    """Say what was asked for, what is free, and what to do — a wall a User cannot see the far side of
    is one they cannot act on, and this is the only refusal in CCC whose remedy is entirely theirs."""
    return (f"this recipe needs {_human(e.incoming)} but only "
            f"{_human(max(recipes_mod.MAX_USER_BYTES - e.used, 0))} of your "
            f"{_human(recipes_mod.MAX_USER_BYTES)} recipe allowance is free — delete a recipe and retry")


@api.get("/spawns/{job_id}/tidepool")
def spawn_tidepool(job_id: int, user: dict = Depends(current_user)) -> dict:
    """The one authenticated channel a Tide Pool's ephemeral SSH keypair ever travels over — the CLI
    polls this after submitting (or when reattaching to an existing one) until `ready` is true, per the
    "no user ever manages an SSH key themselves" rule.

    `ready` is false while the Spawn is not yet RUNNING, or RUNNING but nothing is yet accepting on its
    port — this call is what BRINGS the relay up (`tidepool.ensure_relay`, idempotent), so polling here
    is what makes attach converge in well under the karma tick's 30s cadence rather than waiting on it.
    On the Head's own co-located Arm there is no relay to bring up (and starting one breaks the Tide
    Pool outright — see `ensure_relay`); `ready` there means the Pod's own sshd has finished booting.

    The SAME port + private key are returned every time, for the Spawn's whole life: a dropped
    connection or a Dashboard-requested Tide Pool both reattach with `cuttle run --tidepool <id>`, and
    both need the identical credential back, not a fresh one.

    `user` is the account to ssh in AS — the owner's **Guise** (temp/spawn-isolation.md). It
    is on the wire because the CLI cannot derive it: it is `ccc-u<uid>`, and a uid is a Head-side fact.
    The User never types it and never sees it; nobody is expected to know their own uid.
    """
    with db.connect() as conn:
        row = db.get_spawn(conn, job_id)
        tp = db.get_tidepool(conn, job_id) if row is not None else None
    if row is None or not row["tidepool"]:
        raise HTTPException(404, f"Spawn {job_id} is not a Tide Pool")
    if row["user_id"] != user["id"]:
        raise HTTPException(403, "you can only attach to your own Tide Pool")
    if tp is None:
        raise HTTPException(404, f"Tide Pool {job_id} has no connection state (already ended?)")
    # Only past the ownership check: this is the caller's OWN Guise, so there is nothing to look up
    # until we know the Tide Pool is theirs.
    with db.connect() as conn:
        guise = guises.name(db.allocate_guise(conn, user["id"]))
    if spawns_mod.active_state(job_id) != "RUNNING":
        return {"ready": False, "port": None, "private_key": None, "user": guise}
    node_name = next((s["node"] for s in slurm.spawns() if s["id"] == job_id), None)
    arm = next((a for a in slurm.arms() if a["name"] == node_name), None) if node_name else None
    if arm is None or not arm.get("mesh_ip"):
        return {"ready": False, "port": None, "private_key": None, "user": guise}
    with db.connect() as conn:
        up = tidepool_mod.ensure_relay(conn, job_id, arm["mesh_ip"], node_name,
                                       head_arm=bool(arm.get("head")))
        key = db.tidepool_private_key(conn, job_id)
    return {"ready": up and key is not None, "port": tp["port"], "private_key": key, "user": guise}


def _human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return f"{n} B"  # unreachable


async def _spool_upload(request: Request, limit: int | None = None) -> Path:
    """Stream the request body to a temp file, aborting past `limit` (MAX_UPLOAD_BYTES by default).

    Never `await request.body()`: that holds the entire project in this process's memory (and
    `tarfile` then holds a decompressed copy on top). The Head is the one machine that must stay up,
    and a browser folder-picker makes selecting a folder with a huge `data/` or a venv a single click.
    Streaming to disk means a too-large upload costs disk we then release, not RAM we might not have.

    `limit` exists for Recipes, which are bounded by their own **Allowance** rather than by a Spawn's
    4GB cap. It is a floor on wasted disk, not the real check: the wall is weighed properly, against
    what the caller already holds, once the tar can be read (see recipes.save).
    """
    cap = MAX_UPLOAD_BYTES if limit is None else limit
    total = 0
    with tempfile.NamedTemporaryFile("wb", suffix=".tar.gz", delete=False) as f:
        path = Path(f.name)
        # `delete=False` means NOTHING here reaps this file but us. It is spooled to the system temp
        # dir, which is outside every path CCC tracks and has no reaper of its own, so any way out of
        # this loop that is not `return` has to unlink — not just the over-cap branch. A client that
        # Ctrl-Cs a multi-GB folder upload mid-stream raises out of `request.stream()`, and without
        # this it strands the partial .tar.gz until the OS cleans /tmp, if it ever does.
        try:
            async for chunk in request.stream():
                total += len(chunk)
                if total > cap:
                    raise HTTPException(413, f"upload exceeds {_human(cap)} — "
                                             "exclude data/checkpoints (honor .gitignore) and retry")
                f.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
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
