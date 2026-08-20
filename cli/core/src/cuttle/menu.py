"""Bare `cuttle` — an arrow-key menu over the command tree.

**Derived, never hand-written.** The menu is walked out of the live Typer app, so a command added
tomorrow appears in it without anyone remembering to. A hand-maintained list is a second copy of the
command surface, and the second copy is always the stale one.

**Navigation is not dispatch.** Typing a partial command prompts for what is *missing*; the menu
descends the tree. They differ on a group that already does something: `cuttle karma` prints
standings and exits (a node with a default action is a leaf), while the menu can still walk into
karma's subcommands.

**Menu-only filtering.** Commands are hidden on a box that cannot run them *right now* — but only
here. `--help` still lists everything, and an explicitly typed command still runs and fails with a
real message. If a detection bug ever hides something wrongly, the command must still be reachable;
silently vanishing is indistinguishable from "this version doesn't have it", and costs hours.

Four axes decide what shows (see `MenuState`): whether you are **logged in** (a live session on the
Head — validated, not just a file on disk), whether the box is **provisioned** (it carries a role —
Head or Arm), whether it is **pointed** (cuttle here knows which Head to talk to), and whether you are
**on the Head** (the operator, who does admin with no session at all). "Admin" is not an axis: the
admin commands are Head-local operator commands, gated by being on the Head, never by a session's
`is_admin`.

Provisioned and pointed are deliberately separate (glossary: `cli/CONTEXT.md`): a **baked** bundle's
`install.sh` points a box (config + cert pin) without provisioning it, so conflating the two would
hide `setup` on a freshly-installed box and block turning it into an Arm from the menu.
"""

from dataclasses import dataclass

import typer

from . import api, config, settings, ui

# Commands that only work on the Head, because they write head.db or reconfigure Slurm directly (or,
# for build-installer, bake in this Head's address + cert). The Head API exposes zero admin-write
# endpoints, so admin work happens on the Head as the operator, never over a session. `teardown` is
# NOT here — it is gated by "is this box set up", because a client tears down its own `~/.ccc` too
# (`teardown --client`).
HEAD_ONLY = {"user", "arm", "karma override", "karma bump", "build-installer", "backup", "munge"}

# Commands that reach the Head API with a session bearer, so they are useless without one. The whole
# submitter surface plus the session's own housekeeping. `login` mints the token and so is pointedly
# absent; `karma` (standings) is a read behind the session and belongs here.
LOGIN_REQUIRED = {"run", "queue", "history", "status", "logs", "outputs", "recover", "cancel", "karma",
                  "tide", "account", "logout", "whoami", "passwd", "wandb", "recipe", "workspace"}

# How long the session probe (GET /me) may take before we give up and show the offline safe-set.
# Short on purpose: this runs every time bare `cuttle` opens a menu, and a menu must never hang.
PROBE_TIMEOUT = 2.0


@dataclass
class MenuState:
    """The four facts the menu filters on. Computed once per `choose`, then threaded down so the
    tests can fabricate a state without a network probe (and so the probe runs exactly once)."""
    logged_in: bool
    provisioned: bool
    pointed: bool
    on_head: bool

# Short labels for the menu. The command's own help is written for `--help` and is a paragraph; a
# menu needs four words. Keyed by the path so subcommands can be labelled too.
LABELS = {
    "run": "Submit a Spawn",
    "recipe": "My recipes",
    "recipe list": "List my recipes",
    "recipe save": "Freeze this folder into a recipe",
    "recipe run": "Run a recipe",
    "recipe update": "Re-capture a recipe's code",
    "recipe delete": "Delete a recipe",
    "workspace": "My workspaces",
    "workspace list": "List my workspaces",
    "workspace new": "Make this folder into a workspace",
    "workspace open": "Open a workspace",
    "workspace update": "Change a workspace's settings",
    "workspace download": "Download a workspace",
    "workspace delete": "Delete a workspace",
    "queue": "See the queue",
    "history": "Spawn history",
    "status": "Check a Spawn",
    "logs": "Read a Spawn's output",
    "outputs": "Download a Spawn's outputs",
    "recover": "Recover a Spawn's undeclared files",
    "cancel": "Cancel a Spawn",
    "karma": "Karma standings",
    "karma ledger": "My Karma ledger",
    "karma bump": "Bump a Spawn up the queue",
    "karma override": "Adjust someone's Balance",
    "tide": "The Tide (demand)",
    "account": "My account",
    "dashboard": "Open the dashboard",
    "login": "Log in",
    "logout": "Log out",
    "whoami": "Who am I?",
    "passwd": "Change my password",
    "setup": "Set up this box",
    "teardown": "Tear down this box",
    "build-installer": "Build a client installer",
    "user": "Manage users",
    "user add": "Create a user",
    "user list": "List users",
    "user remove": "Delete a user",
    "user reset": "Reset a user's password",
    "arm": "Manage Arms",
    "arm list": "List Arms",
    "arm authorize": "Authorize a new Arm",
    "arm admit": "Admit an enrolled Arm",
    "arm benchmark": "Re-benchmark an Arm",
    "arm activate": "Re-activate an Arm's Features",
    "arm chown": "Change an Arm's owner",
    "arm retract": "Pause your Arm (stop new work, end what's running)",
    "arm return": "Reopen a retracted Arm",
    "arm sync-key": "Re-fetch the munge key from the Head",
    "arm remove": "Remove an Arm",
    "wandb": "My W&B key",
    "wandb set": "Store my W&B key",
    "wandb show": "Show my W&B key",
    "wandb clear": "Forget my W&B key",
    "munge": "The cluster's munge key",
    "munge rotate": "Rotate the cluster's munge key",
    "backup": "Back up the Head",
    "backup create": "Snapshot the Head's state",
    "backup restore": "Restore the Head from a snapshot",
    "feature": "Arm Features",
    "feature list": "List the Features CCC supports",
    "feature show": "Print a starter environment.yml for a Feature",
}

# Sentinels for the navigation entries. Deliberately NOT None: `ui._ask` reads a None answer as
# "^C/^D inside the prompt" and raises KeyboardInterrupt, so a Choice whose value is None makes
# "← back" indistinguishable from an interrupt and kills the menu instead of navigating it.
BACK = "\0back"
QUIT = "\0quit"

# Never the pre-selected entry: destructive, and one arrow key from `setup`.
NEVER_DEFAULT = {"teardown", "user remove", "arm remove"}

# Top-level order — by how often you reach for it, not by declaration order in main.py. The first
# entry is pre-selected, so it should be the daily verb rather than the one that re-provisions the
# box. Anything unlisted keeps its declared position, after these.
# `login` trails the submit verbs but leads everything unlisted, so on a freshly-installed box the
# pre-selected row is "Log in" — what install.sh printed as the next step — rather than `setup`. It
# only ever shows when logged out, so this costs the everyday menu nothing.
# `dashboard` sits *after* login for the same reason: it shows on any pointed box, including the
# logged-out one, and must not displace "Log in" as the pre-selected row there.
ORDER = ["run", "workspace", "recipe", "queue", "history", "status", "logs", "outputs", "recover",
         "cancel", "karma", "tide", "account", "login", "dashboard"]


def is_head() -> bool:
    """Is this box the Head?

    The predicate is the API venv's interpreter, **not head.db**. head.db lives under
    `/var/lib/ccc/head`, which is `drwx------ ccc ccc` — so for every user except `ccc` and root,
    *including the operator standing on the Head*, `HEAD_DB.exists()` doesn't answer "no", it RAISES
    PermissionError (pathlib swallows only not-found-ish errors, never EACCES). Using it would crash
    the menu on the one machine where user management works.

    `/opt/ccc/api` is world-readable, and this interpreter is exactly what `auth._admin_argv` execs,
    so "offered in the menu" and "actually runnable" cannot drift apart.
    """
    return (config.API_VENV / "bin" / "python").exists()


def is_arm() -> bool:
    """Is this box enrolled as an Arm? Marked by its saved settings or its slurmd drop-in — the same
    pair `teardown.detect_role` uses to tell an Arm from a plain client."""
    return bool(settings.load().get("arm")) or config.SLURMD_DROPIN.exists()


def is_provisioned() -> bool:
    """Does this box carry a role — Head or Arm? The state with cluster machinery installed on it, and
    the state `teardown` reverses. A client is never provisioned: `setup --client` installs nothing on
    the box, it only points the tool."""
    return is_head() or is_arm()


def is_pointed() -> bool:
    """Does cuttle here know which Head to talk to? A recorded Head URL is the whole test — that is
    what "pointed" means, and it is what a **baked** bundle's `install.sh` writes (config.yaml + the
    matching cert pin) and what `setup --client` writes.

    Deliberately NOT `CCC_HOME.exists()`: `ui._open_log` mkdirs `~/.ccc/logs` on any command that
    prints, so the directory says "this tool has been run here", not "this box is configured".
    """
    return bool(settings.load().get("head"))


def _session() -> tuple[bool, str | None]:
    """(logged_in, warning). Validate the stored token against the Head — but only when one exists, so
    a box that never logged in is logged-out with no network call at all.

    A 401/403 is a real logout. A timeout or an unreachable Head is neither a yes nor a no, so we do
    NOT guess 'logged in': we fall back to the offline safe-set (logged_in False, which shows `login`
    and hides the submit suite that could not work anyway) and return a line saying why.
    """
    if not config.CREDENTIALS_FILE.exists():
        return False, None
    token = config.CREDENTIALS_FILE.read_text().strip()
    try:
        api.for_head().json("GET", "/me", token=token, timeout=PROBE_TIMEOUT)
        return True, None
    except api.ApiError as e:
        if e.status in (401, 403):
            return False, None
        where = settings.load().get("head") or "the Head"
        return False, f"couldn't reach {where} — showing only what works offline"


def _detect_state() -> tuple[MenuState, str | None]:
    logged_in, warning = _session()
    return MenuState(logged_in=logged_in, provisioned=is_provisioned(), pointed=is_pointed(),
                     on_head=is_head()), warning


def _is_group(cmd) -> bool:
    """Duck-typed on purpose. `TyperGroup` does NOT subclass `click.Group` — Typer vendors its own
    click, so its MRO is `typer._click.core.Command` and `isinstance(cmd, click.Group)` is **False**
    for `arm`, `user` and `karma`. A walker using isinstance renders those three as leaves and
    silently loses all eleven subcommands under them."""
    return hasattr(cmd, "commands") and bool(cmd.commands)


def _visible(path: str, st: MenuState) -> bool:
    """Is this *leaf* command offered in state `st`? Menu-only — a typed command still runs (see the
    module header). The order matters: a path's most specific gate wins (`karma override` is
    Head-only even though `karma` itself is login-gated)."""
    top = path.split()[0]
    if path in HEAD_ONLY or top in HEAD_ONLY:
        return st.on_head
    # `setup` goes when there is nothing left to provision. A merely *pointed* box still shows it —
    # that is the whole promote-a-client-to-an-Arm path, and hiding it is what the installer broke.
    # (`setup --arm --re-enroll` on a live Arm is a typed recovery command; mesh.py prints it.)
    if top == "setup":
        return not st.provisioned
    # `teardown` goes when there is anything to remove — a role, or a client's own `~/.ccc`. A stray
    # logs dir alone is not worth a menu entry.
    if top == "teardown":
        return st.provisioned or st.pointed
    if top == "login":
        return not st.logged_in
    # Pointed, not logged in: the Dashboard does its own login, and a box that knows a Head can always
    # open one. Deliberately not LOGIN_REQUIRED — that set is "needs a session bearer from here", and
    # this command sends nothing anywhere; it hands an address to a browser.
    if top == "dashboard":
        return st.pointed
    if path in LOGIN_REQUIRED or top in LOGIN_REQUIRED:
        return st.logged_in
    return True


def _visible_children(path: str, group, st: MenuState) -> bool:
    """Does `group` have at least one child worth descending into, in state `st`?"""
    return any(_node_visible(f"{path} {name}", child, st)
               for name, child in group.commands.items()
               if not getattr(child, "hidden", False))


def _node_visible(path: str, cmd, st: MenuState) -> bool:
    """Whether to show a menu row for `cmd` — a leaf, or a group with something inside it.

    A group with no visible children normally vanishes. The exception is a group that *also runs on
    its own* (karma → standings, via invoke_without_command): with every subcommand filtered out it
    is offered as a leaf, if its own action is permitted. That is what lets a client see one `karma`
    entry (standings) while the Head sees a group it descends into (override/bump)."""
    if _is_group(cmd):
        return _visible_children(path, cmd, st) or (
            getattr(cmd, "invoke_without_command", False) and _visible(path, st))
    return _visible(path, st)


def _label(path: str, cmd) -> str:
    return LABELS.get(path) or (cmd.get_short_help_str(limit=40) if cmd else path)


def _entries(group, st: MenuState, prefix: str = "") -> list[tuple[str, str, object]]:
    """(path, label, command) for each child of `group` visible in state `st`.

    When `group` is one we have descended INTO and it *also runs on its own* (karma → standings, via
    invoke_without_command), its own action leads the list as an explicit row. Otherwise descending
    into karma would hide the standings behind its subcommands — the default action, reachable only by
    backing out and… not descending. `choose` runs this row as a leaf rather than re-descending."""
    rows = []
    own = prefix.strip()
    if own and getattr(group, "invoke_without_command", False) and _visible(own, st):
        rows.append((own, _label(own, group), group))
    for name, cmd in group.commands.items():
        if getattr(cmd, "hidden", False):
            continue
        path = f"{prefix}{name}"
        if not _node_visible(path, cmd, st):
            continue
        rows.append((path, _label(path, cmd), cmd))
    rank = {name: i for i, name in enumerate(ORDER)}
    rows.sort(key=lambda r: (r[0] in NEVER_DEFAULT, rank.get(r[0], len(ORDER))))
    return rows


def _descend(root, path: str):
    """Walk `root` down a space-separated path to the group it names."""
    node = root
    for name in path.split():
        node = node.commands[name]
    return node


def choose(app: typer.Typer) -> list[str] | None:
    """Walk the tree until the user settles on a command. Returns its argv path, or None to quit."""
    root = typer.main.get_command(app)
    st, warning = _detect_state()
    # Said once, before the first box: the Head is unreachable, so the submit suite is hidden and only
    # offline-valid commands are shown. The menu still works — you just cannot see what needs the Head.
    if warning:
        ui.warn(warning)
    node, prefix = root, ""
    while True:
        rows = _entries(node, st, prefix)
        if not rows:
            return None
        # `_entries` already ordered these: common verbs first, destructive ones last so that the
        # pre-selected entry is never one that destroys something.
        choices = [ui.choice(label, path) for path, label, _ in rows]
        choices.append(ui.choice("← back", BACK) if prefix else ui.choice("Quit", QUIT))
        # One box per level, titled with where you are — so descending into `arm` erases the
        # top-level box and draws `cuttle arm` in its place, rather than stacking menus down the
        # screen. Each closes to a one-line breadcrumb of what was picked.
        chosen = {}
        with ui.section(f"cuttle {prefix}".strip(), lambda: chosen.get("label", "")):
            picked = ui.ask_select("What do you want to do?", choices)
            chosen["label"] = dict(((p, lbl) for p, lbl, _ in rows)).get(
                picked, "back" if picked == BACK else "cancelled")
        if picked == QUIT:
            return None
        if picked == BACK:
            # One level up. The tree is two deep today, so that is the root; written as a pop so a
            # third level does not silently teleport the user to the top.
            parent = " ".join(prefix.split()[:-1])
            node = root if not parent else _descend(root, parent)
            prefix = f"{parent} " if parent else ""
            continue
        cmd = dict((p, c) for p, _, c in rows)[picked]
        # A group with children to show is navigated into; `cuttle karma` printing standings and the
        # menu offering karma's subcommands are the two mechanisms, not a conflict. But a group whose
        # children are all filtered out in this state (karma on a client — override/bump are
        # Head-only) is run as a leaf instead, firing its own default action (standings).
        # `picked == prefix.strip()` is the group's own-action row (added by `_entries` when we are
        # already inside it) — run it as a leaf rather than descending into the group we are in.
        if _is_group(cmd) and _visible_children(picked, cmd, st) and picked != prefix.strip():
            node, prefix = cmd, f"{picked} "
            continue
        return picked.split()
