"""`cuttle` auth: human login/session (any client box) + local user-admin (on the Head).

Two halves with different trust models:
- **Login/session** — `login`/`logout`/`whoami`/`passwd` talk to the Head API over HTTP (works from any
  box; a submitter need never have run `setup`). The bearer token lands in `~/.ccc/session` (0600);
  the Head URL in `~/.ccc/config`.
- **User-admin** — `user add|list|remove|reset` run *on the Head as the `ccc` service account* and write
  head.db directly via the API venv (`python -m ccc_head_api.admin`). Being the operator on the Head
  is the authorization — no session (mirrors `cuttle arm authorize`).
"""

import time

import typer

from . import api, config, headapi, settings, tls, ui, watch
from .system import run


# --- HTTP to the Head API ---------------------------------------------------
# The transport lives in api.py; this is the CLI's thin shim over it.
#
# `ApiError` is deliberately NOT translated into `typer.BadParameter` here — that renders as a click
# *usage* error (a "Usage: cuttle status ..." box telling you to try `--help`), which is wrong for a
# failure that has nothing to do with how the command was typed. It propagates to `ui.run_app` instead,
# which prints one clear line.

_CLIENT: api.Api | None = None


def _client() -> api.Api:
    """One client per process — keeps the TLS handshake and connection across a command's calls."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = api.for_head()
    return _CLIENT


def _api(method: str, path: str, token: str | None = None, body: dict | None = None,
         auth: bool = True, refused_ok: bool = False) -> dict | None:
    """A JSON call to the Head API, verified against the Head's pinned certificate (see tls.py).

    `auth=True` (the default) attaches the stored session bearer. Every route but /healthz needs one.

    `refused_ok` returns None on a **403** instead of aborting — mirroring `api.Client.json`'s
    `missing_ok` for a 404. It is for the two commands that have a second road on the Head (`cuttle
    cancel`, `cuttle arm retract`/`return`): there, "not yours" is the Head performing the ownership
    test for us, and the answer is which road to take rather than what to print. Nothing else should
    use it — for every other command a 403 IS the outcome.
    """
    # Loaded here, not in api.py: reading the session file is what makes "not logged in" a
    # *command* error, and `auth=False` callers (login) must never trigger it.
    if token is None and auth:
        token = _load_token()
    try:
        return _client().json(method, path, body=body, token=token, auth=auth)
    except api.ApiError as e:
        if refused_ok and e.status == 403:
            return None                # a branch, not a failure — checked before --debug for that reason
        if ui.DEBUG:
            raise                      # --debug wants the stack, not a tidy summary of it
        if e.status == 401 and auth and token:
            # We sent a stored bearer and the Head rejected it: the session expired or was revoked, not
            # a route that happens to answer 401. `auth=False` login (bad password) never reaches here,
            # and a `token=""` call has nothing to have expired. Say the one thing that fixes it rather
            # than echoing the Head's raw "Unauthorized".
            raise ui.abort("session expired — run `cuttle login`") from None
        raise ui.abort(e.user_message()) from None


def _head_url() -> str:
    try:
        return api._head_url()
    except api.ApiError as e:
        raise typer.BadParameter(e.detail) from None


def _set_head(head: str | None) -> None:
    """Record which Head this box talks to. Accepts a bare address as well as a URL — `--head` is a
    flag, but what a person has to hand is an address, and the Head API's port is https's own default
    now (config.HEAD_API_PORT), so there is nothing left worth making them type. Stored without the
    scheme it would be parsed with, `urlparse` reads the whole thing as a path and every later use —
    the pin's filename, the dial — gets an empty hostname."""
    if head:
        data = settings.load()
        data["head"] = head.rstrip("/") if "://" in head else headapi.head_url(head.rstrip("/"))
        settings.save(data)


# --- session store (~/.ccc/session, 0600) -----------------------------------

def _save_token(token: str) -> None:
    # mode explicitly, not the ambient umask: the file below is 0600, and a 0755 directory around it
    # still tells every local user that this box has a CCC session and when it was obtained.
    config.ensure_ccc_home()
    config.SESSION_FILE.write_text(token)
    config.SESSION_FILE.chmod(0o600)


def _load_token() -> str:
    if not config.SESSION_FILE.exists():
        raise typer.BadParameter("not logged in — run `cuttle login`")
    return config.SESSION_FILE.read_text().strip()


def have_session() -> bool:
    """Whether this box holds a token at all. Deliberately NOT a validity check — `setup` uses it to
    decide whether it can go on and ask the account questions, and the routes it would then call say
    "session expired" perfectly well on their own."""
    return config.SESSION_FILE.exists()


# --- login / session --------------------------------------------------------

def login(head: str | None = None) -> str:
    """Prompt for credentials, mint a session. Returns the logged-in display name (with an admin
    marker) — the caller frames the flow and uses it as the box's summary line."""
    _set_head(head)
    # Trust BEFORE prompting: the password is the thing we are protecting, so the operator must have
    # accepted the Head's identity before it is ever typed, let alone sent.
    tls.ensure_ca(_head_url())
    return _login_with(ui.ask_text("Username"), ui.ask_password("Password"))


def login_seeded(username: str, temp_password: str) -> str:
    """Log in with a password nobody typed — the one `setup --head` just seeded this account with.

    Split from `login` because the two prompts are exactly what must NOT happen here: setup knows the
    username (there is only ever one first Admin) and holds the temp password in memory, and asking
    for either would mean printing the secret first so it could be typed back in. The forced rotation
    below is where the operator does get asked — for a password of their own, once, while they are
    still standing at the terminal that ran setup.
    """
    return _login_with(username, temp_password)


def _login_with(username: str, password: str) -> str:
    resp = _api("POST", "/login", token="", body={"username": username, "password": password})
    _save_token(resp["token"])
    if resp["must_change_password"]:
        # A forced first-login change earns a frame: it is a step of its own, and the operator-issued
        # temp password needs one line saying why you are being asked. Voluntary `cuttle passwd` reuses
        # `_rotate` without the frame.
        with ui.section("Set your password", lambda: "password set"):
            ui.say("This account was created with a temporary password. "
                   "Choose your own to continue.")
            _rotate(resp["token"], old=password)
    who = username + (" (admin)" if resp["is_admin"] else "")
    ui.ok(f"logged in as {who}")
    return who


def _password_policy_error(new: str, current: str) -> str | None:
    """Mirror of the Head's `password_policy_error` (ccc_head_api.main), so a weak password is caught at
    the prompt with an inline re-ask instead of a round-trip and a 422. The Head is authoritative — if
    the two ever drift it still refuses; this only saves the trip."""
    if len(new) < 8:
        return "at least 8 characters"
    classes = sum((any(c.islower() for c in new), any(c.isupper() for c in new),
                   any(c.isdigit() for c in new), any(not c.isalnum() for c in new)))
    if classes < 2:
        return "a mix of at least two of: lowercase, uppercase, digits, symbols"
    if new == current:
        return "different from your current password"
    return None


def _rotate(token: str, old: str | None = None) -> None:
    if old is None:
        old = ui.ask_password("Current password")
    # Typed twice, because it is never echoed and a typo would lock the account out of itself.
    while True:
        new = ui.ask_password("New password")
        if new != ui.ask_password("New password (again)"):
            ui.warn("they did not match — try again")
            continue
        reason = _password_policy_error(new, old)
        if reason is not None:
            ui.warn(f"password must be {reason} — try again")
            continue
        break
    _api("POST", "/passwd", token=token, body={"old_password": old, "new_password": new})
    ui.ok("password changed")


def passwd() -> None:
    _rotate(_load_token())


def whoami(as_json: bool = False) -> None:
    me = _api("GET", "/me", token=_load_token())
    # stdout, so `u=$(cuttle whoami)` works; the admin marker is decoration and stays out of it.
    ui.emit(me, as_json, lambda: ui.data(me["username"] + (" (admin)" if me["is_admin"] else "")))


def account(as_json: bool = False) -> None:
    """This session's User in full: identity from /me, plus where they stand in the economy.

    Richer than `whoami` (which prints a bare username for `u=$(cuttle whoami)`). Balance/Escrow/Share
    and the queue nice they imply come out of the *standings*, the one place they are computed — so an
    Admin, who has no Balance and never appears in the standings, shows identity alone. That absence is
    correct: an Admin is outside the economy (it keeps `Σ balances ≡ 0`)."""
    me = _api("GET", "/me", token=_load_token())
    mine = next((r for r in _api("GET", "/karma") if r["username"] == me["username"]), None)
    payload = {**me, **({k: mine[k] for k in ("balance", "escrow", "share", "nice")} if mine else {})}

    def render():
        pairs = [("user", me["username"] + (" (admin)" if me["is_admin"] else ""))]
        if mine:
            pairs += [("balance", f"{mine['balance']:.1f}"), ("escrow", f"{mine['escrow']:.1f}"),
                      ("share", f"{mine['share']:.2f}"), ("nice", mine["nice"])]
        ui.kv(pairs)

    ui.emit(payload, as_json, render)


# --- your Credentials (on the Head) -----------------------------------------
# The CLI-parity counterpart of the Dashboard's Account page: one secret per kind per User, held on the
# Head under that User's password, injected into every Spawn you submit from either client.
#
# **Write-only.** There is no reveal and no route that would serve one — the Head cannot decrypt a
# Credential without a live session's key, and even with one it will not hand the plaintext back. What
# you get afterwards is the `hint` (for a W&B key, its last 4 characters), which is enough to tell the
# key you meant from some other key and is all `--reveal` was ever really used for.
#
# Verbs stay per-tool (`cuttle wandb …`) even though the storage and the routes are generic: a
# scientist reaches for the name of the thing they have a key *for*, not for the abstraction the Head
# files it under.

def wandb_set(key: str) -> None:
    _api("POST", "/account/credentials/wandb", token=_load_token(), body={"value": key})
    # The last 4 only, and only as confirmation you pasted what you meant to. Same masking the
    # Dashboard shows, for the same reason: a terminal keeps scrollback.
    ui.ok(f"W&B key stored on the Head (…{key.strip()[-4:]})")


def wandb_show() -> None:
    """Masked, and that is the only view there is — see the section comment."""
    status = _api("GET", "/account/credentials/wandb", token=_load_token())
    if not status["set"]:
        ui.say("no W&B key stored — your Spawns run with W&B disabled")
        ui.hint("set one with `cuttle wandb set`")
        return
    ui.data(f"•••••••••• {status['hint']}")
    ui.hint("the key itself cannot be read back — replace it with `cuttle wandb set`")


def wandb_clear() -> None:
    _api("DELETE", "/account/credentials/wandb", token=_load_token())
    ui.ok("W&B key cleared — your Spawns will run with W&B disabled")


# --- your GitHub key --------------------------------------------------------
# The second Credential kind, and the first **minted** one: the Head generates the keypair and you
# never see the private half. So these verbs are the inverse of `wandb`'s in the one way that matters —
# `wandb show` masks its secret, and `github show` prints its key in FULL, because what it prints is
# the PUBLIC half and printing it is the entire point. You cannot use this key without pasting that
# line into github.com/settings/keys.

GITHUB_KEYS_URL = "https://github.com/settings/ssh/new"


def _github_registered(status: dict) -> None:
    """The pubkey, and what to do with it. Shared by mint and show so the instructions cannot drift
    apart from the thing they are instructions for.

    `ui.block`, not `ui.data`: a `section` captures stdout for the life of its box, so during
    `cuttle setup` the key printed here was held back and replayed minutes later — under a line
    telling the operator to copy "the key above", with nothing above.
    """
    ui.block(status["hint"])
    ui.hint(f"add it at {GITHUB_KEYS_URL} — any title you like")


def github_mint(force: bool = False) -> dict:
    """Mint (or replace) the keypair. The Head keeps the private half sealed under your password; you
    get the public one.

    Replacing is destructive in a way `wandb set` is not, so it asks first: the old public key stays
    on your GitHub account doing nothing, and every Spawn authenticates with the new one from here —
    which will fail until you have pasted it in.
    """
    status = _api("GET", "/account/credentials/github", token=_load_token())
    if status["set"] and not force:
        ui.warn("a GitHub key is already minted for you")
        ui.hint("replacing it means pasting a new public key into GitHub — until you do, your Spawns "
                "cannot reach your private repos")
        if not ui.ask_confirm("Replace it?", False):
            raise ui.abort("kept the existing key")
    fresh = _api("POST", "/account/credentials/github", token=_load_token(), body={})
    ui.ok("GitHub key minted — the Head holds the private half, under your password")
    _github_registered(fresh)
    # Returned so the caller can hand it to `github_await`, which offers to copy it while it waits.
    return fresh


def github_show(to_clipboard: bool = False) -> None:
    """The public key, in full — the deliberate inverse of `wandb show`.

    `to_clipboard` is the flag rather than a keystroke: this command's stdout is its deliverable
    (`cuttle github show | …`), so it must not stop and wait for anyone. The keystroke belongs to the
    wait in `github_await`, which is already stopping.
    """
    status = _api("GET", "/account/credentials/github", token=_load_token())
    if not status["set"]:
        ui.say("no GitHub key minted — your Spawns cannot reach your private repos")
        ui.hint("mint one with `cuttle github mint`")
        return
    _github_registered(status)
    if to_clipboard:
        if ui.copy(status["hint"]):
            ui.ok("copied to your clipboard")
        else:
            ui.warn("nowhere to copy to on this box — no clipboard tool, and no terminal to ask")


def github_clear() -> None:
    _api("DELETE", "/account/credentials/github", token=_load_token())
    ui.ok("GitHub key forgotten — your Spawns no longer carry one")
    ui.hint(f"the public half is still on your GitHub account and now opens nothing; remove it at "
            f"{GITHUB_KEYS_URL.rsplit('/', 1)[0]}")


def github_minted() -> bool:
    """Whether this account already has a GitHub key. `setup` asks before offering to mint one — an
    Arm's owner re-running setup should not be talked into replacing a key that already works."""
    return bool(_api("GET", "/account/credentials/github", token=_load_token())["set"])


def github_verify() -> dict:
    """Ask the Head to authenticate to GitHub with your stored key, and say what came back.

    Three answers, not two (ccc_head_api.github): registered, not-registered-yet, and nobody could
    tell. The last is why this is not a bool — a Head with no route to github.com must not read as a
    key you have not pasted in yet.
    """
    return _api("POST", "/account/credentials/github/verify", token=_load_token())


def github_await(pubkey: str = "", timeout: float = 300.0, interval: float = 3.0) -> bool:
    """Poll until GitHub accepts the key. True if it did.

    **Skippable, and loudly.** Ctrl-C leaves you with a key that is armed on the Head and registered
    nowhere, which surfaces much later as `Permission denied (publickey)` inside a Spawn, blamed on
    the job. Saying so here is the difference between that and a mystery.

    `c` copies the key while the wait runs. The wait is the moment it is wanted — you are about to
    walk to a browser — and the poll would otherwise hold the terminal for five minutes with nothing
    to do but stare at a key you have to select by hand. `ui.copy` reaches the clipboard of an SSH
    client too (OSC 52), which is where `setup --head` usually runs from.
    """
    hint = "press c to copy it, or Ctrl-C to skip" if pubkey else "or press Ctrl-C to skip"
    ui.say(f"waiting for GitHub to accept the key — {hint}")
    deadline = time.monotonic() + timeout
    try:
        with ui.hotkeys() as fd:
            while True:
                got = github_verify()
                if got["status"] == "ok":
                    ui.ok(f"GitHub accepts it — authenticated as {got['username']}")
                    return True
                if got["status"] == "error":
                    ui.warn(f"could not check with GitHub — {got['detail']}")
                    ui.hint("the key is stored either way; `cuttle github verify` re-checks")
                    return False
                if time.monotonic() >= deadline:
                    ui.warn("GitHub has not accepted the key yet")
                    break
                # The poll interval IS the keystroke wait — one timer, so pressing `c` is answered
                # at once rather than at the end of whatever sleep it landed in.
                if pubkey and ui.waited_key(fd, interval).lower() == "c":
                    if ui.copy(pubkey):
                        ui.ok("copied to your clipboard")
                    else:
                        ui.warn("nowhere to copy to on this box — select the key above instead")
    except KeyboardInterrupt:
        ui.warn("skipped — GitHub has not accepted the key")
    ui.hint(f"paste it at {GITHUB_KEYS_URL}, then run `cuttle github verify`. Until you do, a Spawn "
            f"reaching a private repo fails with `Permission denied (publickey)`")
    return False


def logout() -> None:
    if config.SESSION_FILE.exists():
        _api("POST", "/logout", token=_load_token())
        config.SESSION_FILE.unlink()
    ui.ok("logged out")
    # Auto-download re-reads this file on every pass and never keeps a copy of the token, so logging
    # out genuinely stops it — which makes this the moment to say so, rather than leaving a User to
    # find out days later that results they were promised never arrived.
    waiting = watch.pending()
    if waiting:
        ui.hint(f"{len(waiting)} result(s) were still waiting to download here "
                f"({', '.join(str(e['spawn_id']) for e in waiting)}) — they resume when you log "
                f"back in, as long as the Head still holds them")


# --- user-admin (on the Head, as ccc) ---------------------------------------

def _admin_argv(args: list[str], env: dict[str, str] | None = None) -> list[str]:
    """The auth store's admin CLI, as the `ccc` user through the API venv, with the db path set.

    **Returned without the leading `sudo`** — every caller passes `sudo=True` to `system.run`, which
    prepends it *and* primes the credential first (`prime_sudo`). A literal "sudo" here would skip that
    priming, and since these calls capture output, sudo's password prompt would go straight into the
    captured pipe with nothing visible to answer it. Anything that runs sudo must go through
    `system.run`, so one prompt is drawn once, as a Rich row, and fed over stdin.

    `env` is anything the *service* gets from its systemd unit that this subprocess also needs. Only
    `CCC_HEAD_DB` is unconditional: the other unit variables (`CCC_HEAD_STATE`, `CCC_SIF`) have
    defaults identical to what the unit sets, so a subprocess without them behaves the same. The one
    that does not is `CCC_MESH_IP`, which is dynamic and has no default — see `spawns._submit_pinned`.
    """
    return ["-u", config.HEAD_API_USER, "env", f"CCC_HEAD_DB={config.HEAD_DB}",
            *[f"{k}={v}" for k, v in (env or {}).items()],
            str(config.API_VENV / "bin" / "python"), "-m", "ccc_head_api.admin", *args]


def _admin(args: list[str], check: bool = True, echo: bool = True):
    """Run a Head-local admin subcommand **and show what it said**.

    These are the commands whose stdout is the whole point: `add` prints a one-time temp password
    that exists nowhere else once the process exits, `list` prints the users. `system.run` captures
    by default (so `setup`'s command stream stays out of the way), so this captures explicitly and
    re-emits rather than letting output depend on a global verbosity flag.

    stdout is re-emitted as data: `pw=$(cuttle user add bob)` has to keep working.

    `echo=False` for the ONE caller whose stdout is not for anyone to read: `seed_admin`, which spends
    the temp password itself and never shows it. Worth a flag rather than a second run() call, because
    the re-emit is not only a print — `ui.data` also writes to the run log, so `setup --head` was
    leaving the cluster's first Admin password in cleartext under ~/.ccc/logs, outliving the password
    itself by however long the operator keeps their logs.
    """
    r = run(_admin_argv(args), sudo=True, capture_output=True, text=True, check=check)
    if echo and (r.stdout or "").strip():
        ui.data(r.stdout.rstrip())
    if (r.stderr or "").strip():
        ui.say(r.stderr.rstrip())
    return r


def user_add(username: str, admin: bool = False) -> None:
    _admin(["add", *(["--admin"] if admin else []), username])
    ensure_guise_accounts()


# --- Guises -------------------------------------------------------------------
# The uid is allocated in head.db by the command above, which needs no privilege at all. The Unix
# account it names does need root, and gets it only from the operator standing at the Head running
# this command, at a tty, through the same sudo every other `cuttle` command uses. There is no helper,
# no NOPASSWD root grant, and no HTTP route that creates a User (nothing unattended needs this).

def guise_accounts() -> list[tuple[int, str]]:
    """[(uid, username), ...] for every allocated Guise. Empty on any failure — the caller is a
    provisioning step, and one that cannot read the list has nothing to create."""
    r = run(_admin_argv(["guises"]), sudo=True, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            out.append((int(parts[0]), parts[1]))
    return out


def ensure_guise_accounts() -> None:
    """Create the Head-local Unix account for every Guise that doesn't have one yet.

    Idempotent and backfilling by construction: it asks head.db for the whole list every time, so the
    same call provisions a User created seconds ago and any older User with no account yet. A
    `setup --head` re-run is the upgrade path.

    Head-only. On an Arm the accounts are created lazily by the Prolog, as root, at the moment a Spawn
    lands there — the only mechanism that can reach a donor's box that is powered off today and
    enrolled tomorrow.
    """
    for uid, _username in guise_accounts():
        provision_guise(uid)
    provision_guise(config.GUISE_CLUSTER_UID)  # the enrollment benchmark's — belongs to no User


def provision_guise(uid: int) -> None:
    """One Guise account, at exactly `uid`, in GUISE_GROUP. Non-fatal if it is already there (a re-run).

    The same account the Slurm prolog creates on a donor's Arm, by the same rules — this is just the
    path for the two boxes where an account is needed BEFORE any Spawn has landed: the Head (which
    submits, and so must be able to `sudo -u` into every Guise) and an Arm at `setup --arm` (whose
    enrollment benchmark runs as the cluster Guise before `Prolog=` may even be switched on).

    Home-less (`-M`) for the same reason the Mantle is: nothing on any node reads a Guise's home — the
    Pod makes its own inside the `--writable-tmpfs` overlay — so a donor's box is left with an empty
    account and no directory. `nologin` because nothing should ever log into a Guise on a host, and
    nothing can: no password, no authorized_keys, and slurmd execs a batch script by its shebang.

    That shell does NOT stay behind in the Pod, though — apptainer patches the Pod's /etc/passwd with
    this entry verbatim, shell field included, so a Tide Pool's sshd would exec nologin and refuse the
    session ("This account is currently not available") unless repaired; see PAYLOAD_TIDEPOOL in
    ccc_head_api.spawns. Its private group (gid == uid) keeps the shared group from ever owning a
    file — that group exists only to be the Runas list on the Head's sudoers rule.
    """
    name = config.guise_name(uid)
    run(["groupadd", "--gid", str(config.GUISE_GROUP_GID), config.GUISE_GROUP], sudo=True, check=False)
    run(["groupadd", "--gid", str(uid), name], sudo=True, check=False)
    run(["useradd", "--uid", str(uid), "--gid", str(uid), "--groups", config.GUISE_GROUP,
         "--no-create-home", "--shell", "/usr/sbin/nologin", "--comment", config.GUISE_GECOS, name],
        sudo=True, check=False)
    # The shared group, asserted separately and every run, because `--groups` above applies only when
    # useradd CREATES the account: on one that already exists useradd does nothing at all and exits
    # non-zero, which the check=False swallows. An account can survive a teardown (a Spawn still
    # running holds `userdel` off — see teardown.end_guise_processes), and the next setup then rebuilds
    # GUISE_GROUP without it.
    #
    # `check=True`, alone on this path: GUISE_GROUP is the Runas list on the Head's one sudoers rule
    # (headapi._SUDOERS_CCC_SLURM), so a Guise outside it cannot be sudo'd into at all and every Spawn
    # it owns dies at sbatch with "sudo: a password is required" — a failure that surfaces nowhere near
    # here, and only for the User unlucky enough to own that uid.
    run(["usermod", "--append", "--groups", config.GUISE_GROUP, name], sudo=True, check=True)


def user_list() -> None:
    _admin(["list"])


def user_remove(username: str) -> None:
    _admin(["remove", username])


def user_reset(username: str) -> None:
    _admin(["reset", username])


def user_names() -> list[str]:
    """Usernames, for a picker. Captured rather than printed — `_admin` exists to *show* output, and
    this is the one caller that wants the values instead. Empty on any failure: a picker that cannot
    read the list falls back to a free-text prompt, which still works."""
    r = run(_admin_argv(["list"]), sudo=True, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return []
    return [line.split()[0] for line in (r.stdout or "").splitlines()
            if line.strip() and not line.startswith("(")]


def user_exists(username: str) -> bool:
    """Does this User have an account? `cuttle arm authorize --user` checks before minting a token —
    an owner typo would otherwise strand the Arm's Karma income on a User that never existed."""
    return run(_admin_argv(["exists", username]), sudo=True, check=False).returncode == 0


def seed_admin(username: str) -> str | None:
    """First-admin seed at `setup --head`. Tolerant of re-runs — **None** then, meaning this account
    already exists and nothing here knows its password.

    **The temp password is returned, never printed.** It used to be lifted into a `ui.result` so it
    survived the box collapsing, which made it the one reusable secret CCC ever put on a screen: it sat
    in the operator's scrollback, and in their terminal's history, long after it had been spent. Setup
    now spends it immediately — `login_seeded` above, then the forced rotation — so it lives only in
    this process's memory and only for as long as those two calls take.

    `admin.py` is untouched by that and its stated invariant holds: it still prints the temp password to
    ITS stdout, which is captured here and goes nowhere else.
    """
    r = _admin(["add", "--admin", username], check=False, echo=False)
    ui.result("admin", username)
    for line in (r.stdout or "").splitlines():
        if line.startswith("temp password:"):
            return line.split(":", 1)[1].strip()
    return None
