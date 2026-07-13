"""`cuttle` auth: human login/session (any client box) + local user-admin (on the Head).

Two halves with different trust models:
- **Login/session** — `login`/`logout`/`whoami`/`passwd` talk to the Head API over HTTP (works from any
  box; a submitter need never have run `setup`). The bearer token lands in `~/.ccc/credentials` (0600);
  the Head URL in `~/.ccc/config`.
- **User-admin** — `user add|list|remove` run *on the Head as the `ccc` service account* and write
  head.db directly via the API venv (`python -m ccc_head_api.admin`). Being the operator on the Head
  is the authorization — no session (mirrors `cuttle arm authorize`).
"""

import json
import ssl
import urllib.error
import urllib.request

import typer

from . import config, settings, tls
from .system import run


# --- HTTP to the Head API ---------------------------------------------------

def _api(method: str, path: str, token: str | None = None, body: dict | None = None,
         auth: bool = True) -> dict:
    """A JSON call to the Head API, verified against the Head's pinned certificate (see tls.py).

    `auth=True` (the default) attaches the stored session bearer. Every route but /healthz needs one:
    reads used to be anonymous, and the browser-login slice put them behind the session.
    """
    url = _head_url()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{url}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if token is None and auth:
        token = _load_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15, context=tls.context(url)) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", e.reason)
        except Exception:
            detail = e.reason
        raise typer.BadParameter(f"{path}: {detail}") from None
    except urllib.error.URLError as e:
        # A cert mismatch lands here. Say so plainly — this is the alarm the pin exists to raise.
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            raise typer.BadParameter(
                f"{url} presented a certificate that does not match the one pinned on this machine.\n"
                f"Either the Head was rebuilt with a new certificate, or something is impersonating "
                f"it.\nIf you rebuilt it: delete the pin and log in again.") from None
        raise


def _head_url() -> str:
    url = settings.load().get("head")
    if not url:
        raise typer.BadParameter("no Head URL set — run `cuttle login --head http://<host>:8082`")
    return url


def _set_head(head: str | None) -> None:
    if head:
        data = settings.load()
        data["head"] = head.rstrip("/")
        settings.save(data)


# --- credential store (~/.ccc/credentials, 0600) ----------------------------

def _save_token(token: str) -> None:
    config.CCC_HOME.mkdir(parents=True, exist_ok=True)
    config.CREDENTIALS_FILE.write_text(token)
    config.CREDENTIALS_FILE.chmod(0o600)


def _load_token() -> str:
    if not config.CREDENTIALS_FILE.exists():
        raise typer.BadParameter("not logged in — run `cuttle login`")
    return config.CREDENTIALS_FILE.read_text().strip()


# --- login / session --------------------------------------------------------

def login(head: str | None = None) -> None:
    _set_head(head)
    # Pin BEFORE prompting: the password is the thing we are protecting, so the operator must have
    # accepted the Head's identity before it is ever typed, let alone sent.
    tls.ensure_pinned(_head_url())
    username = typer.prompt("Username")
    password = typer.prompt("Password", hide_input=True)
    resp = _api("POST", "/login", token="", body={"username": username, "password": password})
    _save_token(resp["token"])
    if resp["must_change_password"]:
        print("cuttle: password change required before first use")
        _rotate(resp["token"], old=password)
    print(f"cuttle: logged in as {username}" + (" (admin)" if resp["is_admin"] else ""))


def _rotate(token: str, old: str | None = None) -> None:
    if old is None:
        old = typer.prompt("Current password", hide_input=True)
    new = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    _api("POST", "/passwd", token=token, body={"old_password": old, "new_password": new})
    print("cuttle: password changed")


def passwd() -> None:
    _rotate(_load_token())


def whoami() -> None:
    me = _api("GET", "/me", token=_load_token())
    print(me["username"] + (" (admin)" if me["is_admin"] else ""))


def logout() -> None:
    if config.CREDENTIALS_FILE.exists():
        _api("POST", "/logout", token=_load_token())
        config.CREDENTIALS_FILE.unlink()
    print("cuttle: logged out")


# --- user-admin (on the Head, as ccc) ---------------------------------------

def _admin_argv(args: list[str]) -> list[str]:
    """Invoke the auth store's admin CLI as the `ccc` user through the API venv, with the db path set."""
    return ["sudo", "-u", config.HEAD_API_USER, "env", f"CCC_HEAD_DB={config.HEAD_DB}",
            str(config.API_VENV / "bin" / "python"), "-m", "ccc_head_api.admin", *args]


def user_add(username: str, admin: bool = False) -> None:
    run(_admin_argv(["add", *(["--admin"] if admin else []), username]))


def user_list() -> None:
    run(_admin_argv(["list"]))


def user_remove(username: str) -> None:
    run(_admin_argv(["remove", username]))


def user_exists(username: str) -> bool:
    """Does this User have an account? `cuttle arm authorize --user` checks before minting a token —
    an owner typo would otherwise strand the Arm's Karma income on a User that never existed."""
    return run(_admin_argv(["exists", username]), check=False).returncode == 0


def seed_admin(username: str) -> None:
    """First-admin seed at `setup --head`. Tolerant of re-runs (already-exists is non-fatal)."""
    run(_admin_argv(["add", "--admin", username]), check=False)
