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

**Grouping is presentation, and is the one hand-written thing here.** `GROUPS` sorts existing commands
into folders — twenty-five flat rows on a Head is a list nobody reads. It does not contradict
"derived, never hand-written": that rule is about which commands EXIST, and it still holds, because a
command missing from every folder falls through to the top level. The failure mode of this table is a
command in the wrong place, never a command that vanished. The command tree itself is untouched —
`cuttle queue` is still `cuttle queue`; a folder is a place to look, never a prefix to type.
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
LOGIN_REQUIRED = {"queue", "history", "status", "logs", "cancel", "karma",
                  "tide", "account", "logout", "whoami", "passwd", "tidepool"}


@dataclass
class MenuState:
    """The four facts the menu filters on. Computed once per `choose`, then threaded down so the
    tests can fabricate a state without a network probe (and so the probe runs exactly once)."""
    logged_in: bool
    provisioned: bool
    pointed: bool
    on_head: bool

# Menu-only grouping: which folder each top-level command is filed under. A folder is a row that
# opens onto commands that are NOT its subcommands — see the header. Keyed by a path no argv can
# collide with, because these are the only menu paths that never become a command line.
FOLDER = "\0"
GROUPS: dict[str, list[str]] = {
    f"{FOLDER}spawns": ["queue", "history", "status", "logs", "cancel"],
    # `karma` whole, rather than its standings here and `bump`/`override` under the admin folder: one
    # command's subtree splits across two places exactly once and then nobody can find either half.
    # Both are Head-gated inside it anyway, so a donor's box still never sees them.
    f"{FOLDER}cluster": ["karma", "tide"],
    f"{FOLDER}account": ["account", "passwd", "whoami", "logout"],
    f"{FOLDER}admin": ["user", "arm", "munge", "backup", "build-installer"],
    f"{FOLDER}box": ["update", "setup", "feature", "teardown"],
}

# Short labels for the menu. The command's own help is written for `--help` and is a paragraph; a
# menu needs four words. Keyed by the path so subcommands can be labelled too.
#
# Two rules, so twenty-five rows read as one list rather than twenty-five decisions:
#
# * **A folder or a group is a noun phrase; a leaf is an imperative.** The row either names a subject
#   you are going to look inside, or tells you what picking it does.
# * **Inside a real group, the child drops the group's noun** — the box is already titled
#   `cuttle arm`, so "Remove an arm" says it twice. Folder children keep theirs, because a folder
#   holding one visible row collapses INTO the top level (see `_collapse`), and a collapsed
#   "Change the settings" out there names nothing.
LABELS = {
    f"{FOLDER}spawns": "Spawns",
    f"{FOLDER}cluster": "The cluster",
    f"{FOLDER}account": "My account",
    f"{FOLDER}admin": "Run the cluster",
    f"{FOLDER}box": "This box",

    "dashboard": "Open the dashboard",
    "login": "Log in",

    # Spawns. Cluster-wide, so no possessive: `queue` and `history` are everyone's, like the
    # Dashboard's landing table. "My" is kept only where ownership really is the filter.
    "queue": "See the queue",
    "history": "See past Spawns",
    "status": "Check a Spawn",
    "logs": "Read a Spawn's output",
    "cancel": "Cancel a Spawn",

    # The cluster.
    "tidepool": "My Tidepool",
    "tidepool enter": "Enter it (ssh)",
    "tidepool status": "See its state and disk use",
    "tidepool restart": "Restart it (files + packages kept)",
    "tidepool reset": "Reset it from the base (home kept)",
    "tidepool keys": "SSH keys",
    "tidepool keys add": "Allow another key",
    "tidepool keys remove": "Remove a key",

    "karma": "Karma",
    "karma ledger": "My ledger",
    "karma bump": "Bump a Spawn up the queue",
    "karma override": "Adjust someone's Balance",
    "tide": "See the Tide (demand)",

    # My account.
    "account": "Show my standing",
    "passwd": "Change my password",
    "whoami": "Show who I am logged in as",
    "logout": "Log out",

    # Run the cluster — Head-local operator work.
    "user": "Users",
    "user list": "List",
    "user add": "Create one",
    "user reset": "Reset a password",
    "user remove": "Delete one",
    "arm": "Arms",
    "arm list": "List",
    "arm authorize": "Authorize a new one",
    "arm admit": "Admit an enrolled one",
    "arm benchmark": "Re-benchmark",
    "arm chown": "Change the owner",
    "arm retract": "Pause (stop new work, end what's running)",
    "arm return": "Reopen after a pause",
    "arm sync-key": "Re-fetch the munge key from the Head",
    "arm remove": "Remove",
    "munge": "The cluster's munge key",
    "munge rotate": "Rotate it",
    "backup": "Backups",
    "backup create": "Snapshot the Head's state",
    "backup restore": "Restore the Head from a snapshot",
    "build-installer": "Build a client installer",

    # This box. Self-contained, because this folder is the one that collapses.
    "update": "Update this box (keep all settings)",
    "setup": "Set up this box",
    "teardown": "Tear down this box",
    "feature": "Arm Features",
    "feature list": "List the Features CCC supports",
    "feature show": "Print a starter environment.yml",

}


class _Folder:
    """A synthetic menu folder — a row you open, holding commands that are not its subcommands.

    It has no callback and never becomes argv: there is no `cuttle spawns`. `_menu_children` answers
    for it instead of a `.commands` dict, so nothing can mistake it for a real group and try to
    prefix its members' paths with it.
    """

    hidden = False
    invoke_without_command = False

    def __init__(self, key: str):
        self.key = key
        self.label = LABELS[key]
        self.members = GROUPS[key]


# A group that also RUNS on its own needs two labels, not one: the row you open is the subject
# ("Karma"), and the row inside it that fires the default action is a verb phrase like any other leaf
# ("Standings"). Labelling both from LABELS put "Karma" inside "Karma", which reads as a loop.
OWN_ACTION = {"karma": "Standings"}

# Sentinels for the navigation entries. Deliberately NOT None: `ui._ask` reads a None answer as
# "^C/^D inside the prompt" and raises KeyboardInterrupt, so a Choice whose value is None makes
# "← back" indistinguishable from an interrupt and kills the menu instead of navigating it.
BACK = "\0back"
QUIT = "\0quit"

# Never the pre-selected entry: destructive, and one arrow key from `setup`.
NEVER_DEFAULT = {"teardown", "user remove", "arm remove", "tidepool reset"}

# Top-level order — by how often you reach for it, not by declaration order in main.py. The first
# entry is pre-selected, so it should be the daily verb rather than the one that re-provisions the
# box. Anything unlisted keeps its declared position, after these.
# `login` trails the Spawn folders but leads everything unlisted, so on a freshly-installed box the
# pre-selected row is "Log in" — what install.sh printed as the next step — rather than `setup`. It
# only ever shows when logged out, so this costs the everyday menu nothing.
# `dashboard` sits *after* login for the same reason: it shows on any pointed box, including the
# logged-out one, and must not displace "Log in" as the pre-selected row there.
# Only top-level rows are ranked here. Inside a folder the member order in `GROUPS` governs, and
# inside a real group the declaration order does — both because the sort is stable and everything
# unlisted ties.
ORDER = ["tidepool", f"{FOLDER}spawns", f"{FOLDER}cluster", f"{FOLDER}account",
         "login", "dashboard", f"{FOLDER}admin", f"{FOLDER}box"]


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
    if not config.SESSION_FILE.exists():
        return False, None
    token = config.SESSION_FILE.read_text().strip()
    try:
        # TIMEOUT_BEST_EFFORT, not the JSON default: this runs every time bare `cuttle` opens a menu,
        # and a menu must never hang on a Head that is off or off-mesh — the offline safe-set below is
        # a perfectly good answer, and it is wanted promptly.
        api.for_head().json("GET", "/me", token=token, timeout=api.TIMEOUT_BEST_EFFORT)
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
    """Something the menu descends INTO — a real command group, or one of our folders.

    Duck-typed on purpose for the real ones. `TyperGroup` does NOT subclass `click.Group` — Typer
    vendors its own click, so its MRO is `typer._click.core.Command` and `isinstance(cmd,
    click.Group)` is **False** for `arm`, `user` and `karma`. A walker using isinstance renders those
    three as leaves and silently loses all eleven subcommands under them."""
    return isinstance(cmd, _Folder) or (hasattr(cmd, "commands") and bool(cmd.commands))


def _menu_children(node, root, prefix: str) -> list[tuple[str, object]]:
    """(path, command) for everything one level under `node`, before any filtering.

    Three shapes, because the menu tree is not the command tree. At the **root** the folders stand in
    for every command they claim, so the top level is seven rows instead of twenty-five. Inside a
    **folder** the children are those commands at their REAL paths — a folder is a place to look, not
    a prefix to type, and `cuttle queue` must still come back as `["queue"]`. Inside a **real group**
    it is the ordinary prefixed walk.
    """
    if isinstance(node, _Folder):
        return [(path, _descend(root, path)) for path in node.members]
    if not prefix:
        # Derived here rather than cached beside GROUPS, so the table stays the single place this is
        # said — and a command filed nowhere still surfaces at the top level rather than vanishing.
        filed = {path for members in GROUPS.values() for path in members}
        return ([(name, cmd) for name, cmd in node.commands.items() if name not in filed]
                + [(key, _Folder(key)) for key in GROUPS])
    return [(f"{prefix}{name}", cmd) for name, cmd in node.commands.items()]


def _visible(path: str, st: MenuState) -> bool:
    """Is this *leaf* command offered in state `st`? Menu-only — a typed command still runs (see the
    module header). The order matters: a path's most specific gate wins (`karma override` is
    Head-only even though `karma` itself is login-gated)."""
    top = path.split()[0]
    if path in HEAD_ONLY or top in HEAD_ONLY:
        return st.on_head
    # `setup` is where an answer is MADE or CHANGED, so it never goes away: a provisioned box still
    # has settings to change, and a merely *pointed* one is the promote-a-client-to-an-Arm path.
    # (`setup --arm --re-enroll` on a live Arm is a typed recovery command; mesh.py prints it.)
    if top == "setup":
        return True
    # `update` lays the recorded answers down again, so it needs there to BE recorded answers — a role,
    # or a client's own pointing. Same gate as `teardown`, and for the same reason: both act on what
    # setup left behind, and neither has anything to act on before it has run.
    if top == "update":
        return st.provisioned or st.pointed
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


def _shown_children(path: str, group, st: MenuState, root) -> list[tuple[str, object]]:
    """The children of `group` that state `st` actually offers."""
    return [(p, c) for p, c in _menu_children(group, root, f"{path} ")
            if not getattr(c, "hidden", False) and _node_visible(p, c, st, root)]


def _visible_children(path: str, group, st: MenuState, root) -> bool:
    """Does `group` have at least one child worth descending into, in state `st`?"""
    return bool(_shown_children(path, group, st, root))


def _collapse(path: str, cmd, st: MenuState, root):
    """A folder holding exactly one visible row IS that row.

    Otherwise a fresh box — where almost everything is gated off — would make you open "This box" to
    find the single thing it can do. A folder earns its row by holding a choice; with nothing to
    choose it is a keystroke charging rent."""
    if not isinstance(cmd, _Folder):
        return path, cmd
    shown = _shown_children(path, cmd, st, root)
    return shown[0] if len(shown) == 1 else (path, cmd)


def _node_visible(path: str, cmd, st: MenuState, root=None) -> bool:
    """Whether to show a menu row for `cmd` — a leaf, or a group with something inside it.

    A group with no visible children normally vanishes. The exception is a group that *also runs on
    its own* (karma → standings, via invoke_without_command): with every subcommand filtered out it
    is offered as a leaf, if its own action is permitted. That is what lets a client see one `karma`
    entry (standings) while the Head sees a group it descends into (override/bump)."""
    if _is_group(cmd):
        return _visible_children(path, cmd, st, root) or (
            getattr(cmd, "invoke_without_command", False) and _visible(path, st))
    return _visible(path, st)


# One path whose menu label depends on the box, because the command's job genuinely differs there.
# On a box with no role and no Head, `setup` is how you get one; on a box that has both, it is the
# only way to change an answer — and next to `update`, which is the one you almost always want,
# "Set up this box" would read like the ordinary re-run and get picked by mistake.
def _label(path: str, cmd, st: MenuState | None = None) -> str:
    if path == "setup" and st and (st.provisioned or st.pointed):
        return "Change this box's settings"
    return LABELS.get(path) or (cmd.get_short_help_str(limit=40) if cmd else path)


def _entries(group, st: MenuState, prefix: str = "", root=None) -> list[tuple[str, str, object]]:
    """(path, label, command) for each child of `group` visible in state `st`.

    When `group` is one we have descended INTO and it *also runs on its own* (karma → standings, via
    invoke_without_command), its own action leads the list as an explicit row. Otherwise descending
    into karma would hide the standings behind its subcommands — the default action, reachable only by
    backing out and… not descending. `choose` runs this row as a leaf rather than re-descending."""
    root = root if root is not None else group
    rows = []
    own = prefix.strip()
    if own and getattr(group, "invoke_without_command", False) and _visible(own, st):
        rows.append((own, group))
    rows += [(path, cmd) for path, cmd in _menu_children(group, root, prefix)
             if not getattr(cmd, "hidden", False) and _node_visible(path, cmd, st, root)]
    rank = {name: i for i, name in enumerate(ORDER)}
    # Ranked BEFORE collapsing, so a folder that turns out to hold one row keeps the folder's slot
    # rather than jumping to wherever that lone command would have sorted on its own.
    rows.sort(key=lambda r: (r[0] in NEVER_DEFAULT, rank.get(r[0], len(ORDER))))
    return [(path, OWN_ACTION[path] if path == own and path in OWN_ACTION
             else _label(path, cmd, st), cmd)
            for path, cmd in (_collapse(p, c, st, root) for p, c in rows)]


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
    # Where we came from, innermost last. An explicit stack rather than lopping a name off `prefix`,
    # because a folder is not a prefix: backing out of `cuttle arm` reached through "Run the cluster"
    # has to land in that folder, and there is nothing in the string "arm " that says so.
    stack: list[tuple[object, str, str]] = []
    node, prefix, title = root, "", "cuttle"
    while True:
        rows = _entries(node, st, prefix, root)
        if not rows:
            return None
        # `_entries` already ordered these: common verbs first, destructive ones last so that the
        # pre-selected entry is never one that destroys something.
        choices = [ui.choice(label, path) for path, label, _ in rows]
        choices.append(ui.choice("← back", BACK) if stack else ui.choice("Quit", QUIT))
        # One box per level, titled with where you are — so descending into `arm` erases the
        # top-level box and draws `cuttle arm` in its place, rather than stacking menus down the
        # screen. Each closes to a one-line breadcrumb of what was picked.
        chosen = {}
        with ui.section(title, lambda: chosen.get("label", "")):
            picked = ui.ask_select("What do you want to do?", choices)
            chosen["label"] = dict(((p, lbl) for p, lbl, _ in rows)).get(
                picked, "back" if picked == BACK else "cancelled")
        if picked == QUIT:
            return None
        if picked == BACK:
            node, prefix, title = stack.pop()
            continue
        cmd = dict((p, c) for p, _, c in rows)[picked]
        # A group with children to show is navigated into; `cuttle karma` printing standings and the
        # menu offering karma's subcommands are the two mechanisms, not a conflict. But a group whose
        # children are all filtered out in this state (karma on a client — override/bump are
        # Head-only) is run as a leaf instead, firing its own default action (standings).
        # `picked == prefix.strip()` is the group's own-action row (added by `_entries` when we are
        # already inside it) — run it as a leaf rather than descending into the group we are in.
        if _is_group(cmd) and _visible_children(picked, cmd, st, root) and picked != prefix.strip():
            stack.append((node, prefix, title))
            node = cmd
            # A folder contributes no prefix: its members keep their real paths, so `cuttle queue`
            # comes back as ["queue"] and not as a command line nothing could parse.
            prefix = "" if isinstance(cmd, _Folder) else f"{picked} "
            title = cmd.label if isinstance(cmd, _Folder) else f"cuttle {picked}"
            continue
        return picked.split()
