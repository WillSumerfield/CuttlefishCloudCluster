"""The one HTTP layer between `cuttle` and the Head API.

Everything that talks to the Head funnels through here: the CLI's JSON calls, the log/outputs raw
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
import os
import shutil
import ssl
import tarfile
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import typer

from . import config, settings, tls

# Timeouts by call shape. An upload is slow (a project tar over a LAN), a log read is not.
TIMEOUT_JSON = 15
TIMEOUT_RAW = 30
TIMEOUT_UPLOAD = 120

# A streamed download's budget is per-chunk, never per-download. `timeout=30` as a bare float sets all
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
    absence (404 on a Spawn that wrote no outputs) from a real failure."""

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
        and what to do meanwhile (`POST /recipes/{name}/run`). Framing that as "try again in a moment"
        would be actively wrong: trying again never helps."""
        if self.status == 501:
            return self.detail
        if self.status and self.status >= 500:
            return ("the Head hit an internal error handling that request — try again in a moment; "
                    "if it keeps happening, check the Head's logs")
        return self.detail


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
             auth: bool = True, timeout: float | None = None, missing_ok: bool = False) -> Any:
        """A JSON call. Returns the decoded body ({} when empty, e.g. a 204). `timeout` overrides the
        default budget — the menu's session probe wants a short one so opening `cuttle` never hangs.

        `missing_ok` mirrors `raw()`'s: a 404 becomes None instead of raising, for a caller like the
        Recovery Window status route where "nothing to report" is a normal outcome, not a failure."""
        headers = self._headers(auth, token, {"Content-Type": "application/json"})
        content = json.dumps(body).encode() if body is not None else None
        try:
            r = self._send(method, path, content=content, headers=headers,
                           timeout=timeout if timeout is not None else TIMEOUT_JSON)
        except ApiError as e:
            if missing_ok and e.status == 404:
                return None
            raise
        return json.loads(r.content or b"{}")

    def raw(self, path: str, missing_ok: bool = False) -> bytes | None:
        """A GET of a raw body (log text, outputs tar). `missing_ok` turns a 404 into None — a Spawn
        that wrote nothing to its declared outputs is a normal outcome, not a malformed command."""
        try:
            r = self._send("GET", path, headers=self._headers(True, None), timeout=TIMEOUT_RAW)
        except ApiError as e:
            if missing_ok and e.status == 404:
                return None
            raise
        return r.content

    @contextmanager
    def _stream(self, path: str):
        """The GET both streaming readers share: opened, with any refusal raised as a status.

        A streamed response has not read its body yet, so an error has to be `read()` before `_detail`
        can find the Head's own reason in it. Doing that here is what keeps a 404 arriving as a 404 on
        a streamed route rather than as an empty download.
        """
        extra = {"timeout": TIMEOUT_STREAM} if self._per_request_timeout else {}
        with self._client.stream("GET", path, headers=self._headers(True, None), **extra) as r:
            if r.is_error:
                r.read()
                raise ApiError(path, _detail(r), r.status_code)
            yield r

    def download(self, path: str, dest: Path, on_progress=None) -> int:
        """GET a raw body straight to a file, in chunks. Returns the bytes written.

        `raw()` above holds a whole body in this process, which is fine for a log and wrong for
        anything a tree's size decides: a **Workspace**'s **Space** runs to 16GB and up, and the Head
        streams it for exactly that reason, which buys nothing if the client turns it back into one
        buffer on arrival.

        Written to a `.part` beside the target and renamed on success, so an interrupted download never
        leaves a truncated tar sitting under the name of a complete one.

        `on_progress(done, total)` is called with the UNCOMPRESSED bytes so far and the size the Head
        declared in `X-CCC-Bytes`. The file on disk is the compressed tar, byte for byte as it arrived
        — the decompression here exists only so the bar's numerator and denominator are the same
        quantity, since a compressed count against an uncompressed total would show a 16GB Workspace
        finishing at 45%. A gzip decompress at level 1 runs an order of magnitude faster than the mesh
        moves, so it costs nothing anybody can see.

        It also, finally, checks the gzip CRC32 — the one integrity check `download_extract` explicitly
        cannot reach, because tarfile stops at the archive's end-of-file blocks and never feeds the
        trailer through.

        The count is of tar-stream bytes, so it runs slightly ahead of the file sizes the Head summed:
        every member carries a 512-byte header and pads to a 512-byte boundary. On a tree of many tiny
        files that is a real gap, and the bar simply arrives at 100% a little early rather than
        reporting past it — worth it to keep `X-CCC-Bytes` meaning "what this tree holds", the same
        number a listing quotes, instead of a wire artifact nothing else could use.
        """
        part = dest.with_suffix(dest.suffix + ".part")
        written = 0
        plain = 0
        try:
            with self._stream(path) as r, part.open("wb") as f:
                total = _declared_bytes(r)
                gz = zlib.decompressobj(zlib.MAX_WBITS | 16)
                for chunk in r.iter_bytes():
                    written += f.write(chunk)
                    plain += len(gz.decompress(chunk, 0))
                    if on_progress:
                        on_progress(plain, total)
                plain += len(gz.flush())
                if on_progress:
                    on_progress(plain, total)
        except zlib.error as e:
            part.unlink(missing_ok=True)
            raise ApiError(path, f"the download arrived corrupted and was discarded ({e}) — nothing "
                                 f"was written; try again") from None
        except httpx.RequestError as e:
            part.unlink(missing_ok=True)
            raise ApiError(path, self._transport_detail(e)) from None
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        part.replace(dest)
        return written

    def download_extract(self, path: str, dest: Path, member=None, on_progress=None) -> int:
        """GET a gzip tar and unpack it into `dest` as it arrives. Returns the uncompressed bytes.

        The tar is never a file on this machine. It is a transport container — what a User asked for is
        the tree inside it — and materialising it first would mean 1.5GB written and read again for
        nothing, plus a second pass they wait through after the transfer already finished.

        Streaming mode (`r|gz`) is the reason the loop looks like this: the archive is not seekable, so
        each member must be extracted as it is encountered rather than picked from an index at the end.
        That constraint and the progress reporting want the same loop, so they share one.

        **Integrity comes free and is not re-implemented.** A cut transfer ends mid-member and the read
        below raises rather than returning a short tree; corrupted bytes fail zlib's own per-block
        checks first. Between them that covers the failure this actually has — a link that dropped —
        and not the one a sha256 manifest would add cover for, a Head that lies about its own bytes.
        (Note what is NOT relied on: the gzip trailer's CRC32 is never reached, because tarfile stops
        at the archive's end-of-file blocks and never feeds the trailer through.)

        `dest` is replaced, not merged: it lands in a sibling `.partial` directory and is renamed over
        the top only once the whole stream has been read. So the destination holds exactly what the Head
        holds or the previous attempt's contents, never a half-written mixture of the two — and a retry
        after a failed download is a clean retry rather than an accumulation.
        """
        work = dest.with_name(dest.name + ".partial")
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        seen = 0
        try:
            with self._stream(path) as r:
                # Called once with nothing done yet, and only now: the caller's progress line is what
                # this fires, and a route that answered 404 must not have drawn one first. The size
                # rides along from here on, because the header it comes from is only readable once the
                # response is open — which is after the caller decided whether to draw a line at all.
                total = _declared_bytes(r)
                if on_progress:
                    on_progress(0, total)
                reader = _ChunkReader(r.iter_bytes())
                keep = member or tarfile.data_filter
                with tarfile.open(fileobj=reader, mode="r|gz") as tf:
                    for m in tf:
                        # The filter is applied here rather than handed to `extract`, because the
                        # progress below needs the member open. It is the same gate either way: a
                        # filter runs on the TarInfo, before a byte is written, and may rewrite the
                        # member (`data_filter` clamps the mode) or refuse it outright.
                        info = keep(m, str(work))
                        if info is None:
                            continue
                        if not info.isreg():
                            # Directories, symlinks, the rest: no contents to count, and nothing to
                            # gain from reimplementing what tarfile already does. Filtered already,
                            # so the identity filter here is not a hole.
                            tf.extract(info, work, filter=lambda i, _p: i)
                            continue
                        # Copied a megabyte at a time rather than through `extract`, which returns
                        # only once the whole member has landed. That was the bar's bug: progress
                        # advanced per FILE, so a Spawn whose output is one 300MB blob sat at 0%
                        # for the entire transfer and then jumped to done.
                        target = work / info.name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        src = tf.extractfile(info)
                        with target.open("wb") as out:
                            while chunk := src.read(1 << 20):
                                out.write(chunk)
                                seen += len(chunk)
                                if on_progress:
                                    on_progress(seen, total)
                        if info.mode is not None:
                            os.chmod(target, info.mode)
        except httpx.RequestError as e:
            shutil.rmtree(work, ignore_errors=True)
            raise ApiError(path, self._transport_detail(e)) from None
        except (tarfile.ReadError, EOFError, zlib.error) as e:
            # The gzip trailer did not check out, or the archive ended mid-member. Said as truncation
            # rather than as a corrupt file: the Head built this one member at a time and never had a
            # bad copy to send, so what arrived short is what got cut on the way.
            shutil.rmtree(work, ignore_errors=True)
            raise ApiError(path, f"the download arrived incomplete and was discarded ({e}) — nothing "
                                 f"was written; try again") from None
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        # Only now is the old copy touched. Removed rather than merged into: a file the Spawn no longer
        # produces would otherwise sit in the results forever, indistinguishable from one it does.
        shutil.rmtree(dest, ignore_errors=True)
        work.replace(dest)
        return seen

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


class _ChunkReader:
    """An `iter_bytes()` stream, wearing the `.read(n)` face `tarfile` expects.

    `tarfile` in stream mode pulls fixed-size blocks; httpx hands over whatever chunks arrived. One
    small buffer bridges the two — and, being the only thing between them, is also the reason nothing
    larger than a chunk is ever held in memory on this side either.
    """

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._buf = b""

    def read(self, n: int = -1) -> bytes:
        while n < 0 or len(self._buf) < n:
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                break
        if n < 0:
            out, self._buf = self._buf, b""
            return out
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


def _declared_bytes(r: httpx.Response) -> int | None:
    """What the Head says this download weighs, from `X-CCC-Bytes`, or None if it will not say.

    Never a reason to fail a transfer. The only thing riding on it is whether the progress line can
    draw a bar or must count upwards, so an old Head that does not send the header, or one that sends
    something unparseable, degrades to the line that was there before the header existed.

    A streamed body has no `Content-Length` to read instead — and would not want one, since the useful
    number is the uncompressed size and the compressed length is unknown until the last chunk.
    """
    raw = r.headers.get("X-CCC-Bytes")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


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
