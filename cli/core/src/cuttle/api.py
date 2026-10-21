"""The one HTTP layer between `cuttle` and the Head API.

Everything that talks to the Head funnels through here: the CLI's JSON calls, the log raw
reads, the `run` upload. One layer, so the `/api` prefix, the bearer, the pinned-cert verification
and the error shapes are decided in exactly one place.

**Why it is an object and not a module of functions.** The CLI is this project's end-to-end test
surface — the Dashboard gets no automated browser testing, so tests drive real `cuttle` commands
against the Head API running *in-process* (no socket, no server). That needs a swappable transport,
so a test points `for_head` at a client bound to the live app:

    Api(TestClient(head_main.app, base_url="http://head/api"), per_request_timeout=False)

Normal runs build the real one with `for_head()`. Nothing below this line knows which it got.

**Errors do not assume they are fatal.** A failed call raises `ApiError`, not `typer.BadParameter`.
A one-shot command wants to print the message and exit — but the interactive `setup` wizard wants to
catch it, say what went wrong, and re-prompt. Only the caller knows which, so the transport declines
to guess and the command layer translates.
"""

import json
import ssl
from typing import Any

import httpx
import typer

from . import config, settings, tls

# Timeouts by call shape. An upload is slow (a project tar over a LAN), a log read is not.
TIMEOUT_JSON = 15
TIMEOUT_RAW = 30
TIMEOUT_UPLOAD = 120

# A call whose answer nothing waits on must never delay what the user actually asked for. Two callers
# have that shape: the menu's session probe (bare `cuttle` opens a menu, and a menu must not hang) and
# teardown's session revocation (a courtesy to the Head, on a box that is being wiped either way). Both
# would otherwise spend TIMEOUT_JSON staring at a Head that is off or off-mesh, which is the one case
# they are most likely to meet.
TIMEOUT_BEST_EFFORT = 2.0

# A streamed read's budget is per-chunk, never per-read. `timeout=30` as a bare float sets all
# four phases at once, which on a body the Head streams means "give up 30s in" — the bug that made a
# working 3GB transfer read as an unreachable Head. Split out, `read` is the gap BETWEEN chunks, which
# is a correct thing to bound however long the whole transfer runs. Connect stays short: a Head that is
# off or off-mesh should say so quickly.
TIMEOUT_STREAM = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

# What a transport failure means, split by which phase it happened in. Never merged again: they have
# different causes and different fixes, and the merged wording sent a diagnosis of the timeout above
# looking for a network fault that was not there.
UNREACHABLE = ("cannot reach the Head at {where} — is it running, and are you on the same LAN or the "
               "CCC mesh?")
WENT_QUIET = ("reached the Head at {where}, but it sent nothing for {secs:.0f}s and the transfer was "
              "abandoned — it is up, so this is the Head struggling or the link dropping mid-transfer, "
              "not a missing route to it")

CERT_MISMATCH = (
    "{url} presented a certificate this machine's pinned authority did not sign.\n"
    "Either the Head was rebuilt with a new authority, or something is impersonating it.\n"
    "If you rebuilt it: delete {pin} and log in again.")

# The Head's cert is genuine but doesn't *list* the address we dialled — a different fault from a
# swap, must not borrow its alarm. setup.required_sans covers the Cluster name, LAN IP, mesh IP,
# hostname and localhost; anything else trips this. Told apart via verify_code: HOSTNAME/
# IP_ADDRESS_MISMATCH here vs a signature/issuer error on a real swap.
CERT_WRONG_ADDRESS = (
    "{url} is not an address the Head's certificate covers.\n"
    "The certificate is the right one — it just does not list this address, so TLS refuses it.\n"
    "Point this box at an address the Head serves: " + config.DOMAIN + " if this machine has a hosts "
    "entry for it, otherwise the Head's LAN IP, or its mesh IP once you are on the CCC mesh.")
# X509_V_ERR_HOSTNAME_MISMATCH / X509_V_ERR_IP_ADDRESS_MISMATCH.
_NAME_MISMATCH_CODES = {62, 64}


class ApiError(Exception):
    """A Head API call that did not succeed. Carries the status so callers can tell a *normal*
    absence (a 404) from a real failure."""

    def __init__(self, path: str, detail: str, status: int | None = None):
        self.path, self.detail, self.status = path, detail, status
        super().__init__(f"{path}: {detail}")

    def user_message(self) -> str:
        """What a command should actually print. `str(self)` leads with the internal API route path
        (`/submit: ...`) — noise to a user — and a 5xx carries only a generic phrase like "Internal
        Server Error". A 4xx `detail`, by contrast, is the Head's own human-readable reason (a policy
        violation, a bad name) and must reach the user intact. So: verbatim `detail` for a refusal or
        a transport failure (status None), a framed line for a server-side crash.

        **501 is the exception among the 5xx.** "Not Implemented" is never a crash — it is a route the
        Head deliberately serves to say a feature is not wired up yet, and its `detail` names which one
        and what to do meanwhile. Framing that as "try again in a moment"
        would be actively wrong: trying again never helps."""
        if self.status == 501:
            return self.detail
        if self.status and self.status >= 500:
            return ("the Head hit an internal error handling that request — try again in a moment; "
                    "if it keeps happening, check the Head's logs")
        return self.detail


class Api:
    """A session against one Head. Holds the bearer so the TUI can log in mid-run without touching
    disk; the CLI leaves it None and lets `for_head()`'s loader read `~/.ccc/session`."""

    def __init__(self, client: httpx.Client, token: str | None = None, url: str = "",
                 per_request_timeout: bool = True):
        self._client = client
        self._token = token
        self.url = url  # for messages only — the client already knows its base
        # Starlette's TestClient is an httpx.Client that runs the app in-process, where a wall-clock
        # timeout is meaningless — it warns if handed one. The in-process test rig sets this False.
        self._per_request_timeout = per_request_timeout

    # --- token ---------------------------------------------------------------

    @property
    def token(self) -> str | None:
        return self._token

    @token.setter
    def token(self, value: str | None) -> None:
        self._token = value

    def _headers(self, auth: bool, token: str | None, extra: dict | None = None) -> dict:
        h = dict(extra or {})
        # An explicit token wins (login hands us one before it is stored); `auth=False` sends none.
        tok = token if token is not None else (self._token if auth else None)
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    # --- calls ---------------------------------------------------------------

    def json(self, method: str, path: str, body: dict | None = None, token: str | None = None,
             auth: bool = True, timeout: float | None = None) -> Any:
        """A JSON call. Returns the decoded body ({} when empty, e.g. a 204). `timeout` overrides the
        default budget — the menu's session probe wants a short one so opening `cuttle` never hangs."""
        headers = self._headers(auth, token, {"Content-Type": "application/json"})
        content = json.dumps(body).encode() if body is not None else None
        r = self._send(method, path, content=content, headers=headers,
                       timeout=timeout if timeout is not None else TIMEOUT_JSON)
        return json.loads(r.content or b"{}")

    def raw(self, path: str, missing_ok: bool = False) -> bytes | None:
        """A GET of a raw body (log text). `missing_ok` turns a 404 into None."""
        try:
            r = self._send("GET", path, headers=self._headers(True, None), timeout=TIMEOUT_RAW)
        except ApiError as e:
            if missing_ok and e.status == 404:
                return None
            raise
        return r.content

    def upload(self, path: str, data: bytes, meta: dict) -> Any:
        """POST a gzip body with its metadata in a header — the `cuttle run` project upload."""
        headers = self._headers(True, None, {
            "Content-Type": "application/gzip", "X-CCC-Meta": json.dumps(meta)})
        r = self._send("POST", path, content=data, headers=headers, timeout=TIMEOUT_UPLOAD)
        return json.loads(r.content or b"{}")

    # --- transport -----------------------------------------------------------

    def _send(self, method: str, path: str, headers: dict, content: bytes | None = None,
              timeout: float = TIMEOUT_JSON) -> httpx.Response:
        extra = {"timeout": timeout} if self._per_request_timeout else {}
        try:
            r = self._client.request(method, path, content=content, headers=headers, **extra)
        except httpx.RequestError as e:
            # Every transport failure, not just ConnectError: a timeout is the *common* one when the
            # Head is off or off-mesh, and left uncaught it reaches the user as a bare "timed out"
            # that names neither the Head nor the fact that it was unreachable.
            if isinstance(e.__cause__, ssl.SSLCertVerificationError):
                # A swapped certificate lands here — say so plainly, it is the alarm the pin exists to
                # raise and "connection error" would bury it. But so does dialling an address the
                # genuine cert doesn't list, which is not an alarm at all; separate them.
                where = self.url or "the Head"
                template = (CERT_WRONG_ADDRESS
                            if e.__cause__.verify_code in _NAME_MISMATCH_CODES else CERT_MISMATCH)
                # Name the pin file rather than saying "the pin": after a rebuild this is the one
                # thing the user has to delete, and PINS_DIR holds one per Head they have ever met.
                raise ApiError(path, template.format(
                    url=where, pin=tls._pin_path(self.url) if self.url else config.PINS_DIR)) from None
            raise ApiError(path, self._transport_detail(e)) from None
        if r.is_error:
            raise ApiError(path, _detail(r), r.status_code)
        return r

    def _transport_detail(self, e: httpx.RequestError, read_secs: float | None = None) -> str:
        """What to tell a User about a call that never came back. Two answers, not one.

        No httpx class name (`ConnectTimeout`, `ReadError`) in the user's face — it names a library,
        not a fix. But WHICH phase failed is not library trivia, it is the whole diagnosis: never
        having reached the Head and having reached it and lost it mid-body are different faults with
        different things to go check. The merged wording cost exactly that once already.
        """
        where = self.url or "the Head"
        if isinstance(e, (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError)):
            return WENT_QUIET.format(where=where, secs=read_secs if read_secs else TIMEOUT_STREAM.read)
        return UNREACHABLE.format(where=where)


def _detail(r: httpx.Response) -> str:
    """FastAPI puts the human-readable half of an error in `detail`; fall back to the status line."""
    try:
        return json.loads(r.content).get("detail", r.reason_phrase)
    except Exception:
        return r.reason_phrase


def _head_url() -> str:
    url = settings.load().get("head")
    if not url:
        raise ApiError("config", "no Head URL set — run `cuttle login --head <host>`")
    return url


def for_head(url: str | None = None, token: str | None = None) -> Api:
    """The real client: this machine's configured Head, verified against its pinned certificate."""
    url = (url or _head_url()).rstrip("/")
    try:
        verify = tls.context(url) or True  # None == plain HTTP, where verify is moot
    except typer.BadParameter as e:
        # tls raises Typer errors because it is mostly a CLI concern, but this module promises its
        # callers one exception type. "No pin yet" is a state the setup wizard must be able to
        # explain and recover from, not one that should abort the process from three layers down.
        raise ApiError("tls", str(e)) from None
    client = httpx.Client(base_url=f"{url}/api", verify=verify, follow_redirects=True)
    return Api(client, token=token, url=url)
