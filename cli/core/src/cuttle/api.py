"""The one HTTP layer between `cuttle` and the Head API.

Everything that talks to the Head funnels through here: the CLI's JSON calls, the log/outputs raw
reads, the `run` upload. One layer, so the `/api` prefix, the bearer, the pinned-cert verification
and the error shapes are decided in exactly one place.

**Why it is an object and not a module of functions.** The CLI is this project's end-to-end test
surface (ROADMAP Phase 6): CCC's Dashboard gets no automated browser testing, so a test drives real
`cuttle` commands against the Head API running *in-process* — no socket, no server. That only works
if the transport can be swapped, so a test points `for_head` at a client bound to the live app:

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

from . import settings, tls

# Timeouts by call shape. An upload is slow (a project tar over a LAN), a log read is not.
TIMEOUT_JSON = 15
TIMEOUT_RAW = 30
TIMEOUT_UPLOAD = 120

CERT_MISMATCH = (
    "{url} presented a certificate that does not match the one pinned on this machine.\n"
    "Either the Head was rebuilt with a new certificate, or something is impersonating it.\n"
    "If you rebuilt it: delete the pin and log in again.")


class ApiError(Exception):
    """A Head API call that did not succeed. Carries the status so callers can tell a *normal*
    absence (404 on a Spawn that wrote no outputs) from a real failure."""

    def __init__(self, path: str, detail: str, status: int | None = None):
        self.path, self.detail, self.status = path, detail, status
        super().__init__(f"{path}: {detail}")


class Api:
    """A session against one Head. Holds the bearer so the TUI can log in mid-run without touching
    disk; the CLI leaves it None and lets `for_head()`'s loader read `~/.ccc/credentials`."""

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
        """A GET of a raw body (log text, outputs tar). `missing_ok` turns a 404 into None — a Spawn
        that wrote nothing to $CCC_OUT is a normal outcome, not a malformed command."""
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
                # A swapped certificate lands here. Say so plainly — this is the alarm the pin exists
                # to raise, and "connection error" would bury it.
                raise ApiError(path, CERT_MISMATCH.format(url=self.url or "the Head")) from None
            where = self.url or "the Head"
            raise ApiError(path, f"cannot reach the Head at {where} "
                                 f"({e.__class__.__name__}: {e or 'no response'})") from None
        if r.is_error:
            raise ApiError(path, _detail(r), r.status_code)
        return r


def _detail(r: httpx.Response) -> str:
    """FastAPI puts the human-readable half of an error in `detail`; fall back to the status line."""
    try:
        return json.loads(r.content).get("detail", r.reason_phrase)
    except Exception:
        return r.reason_phrase


def _head_url() -> str:
    url = settings.load().get("head")
    if not url:
        raise ApiError("config", "no Head URL set — run `cuttle login --head http://<host>:8082`")
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
