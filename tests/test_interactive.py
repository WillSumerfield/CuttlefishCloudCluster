"""Every command runs with no args and asks for the rest.

**Consequence tests only.** Nothing here checks how anything looks; see `TESTING.md`. Each test
below names something that outlives the screen — a script that blocks forever, a user deleted
without confirmation, a piped `$(cuttle queue --json)` corrupted, a one-time password lost, a terminal
left stranded.

Prompts go through the `ui` seam with a scripted human, because a real prompt blocks on a real
keyboard. One test at the bottom drives a real pty, because the seam stays green while the prompt
layer underneath it is broken.
"""

import io
import os
import pty
import subprocess
import sys
import termios
import time

import pytest
import typer
from rich.console import Console

from cuttle import auth, config, headapi, main, menu, settings, system, ui
from cuttle import setup as setup_mod


class _Probe:
    """A stand-in for the Api client, whose `.json()` does whatever the test wants (return a body, or
    raise an ApiError) — enough to drive `menu._session` without a Head."""

    def __init__(self, on_json):
        self._on_json = on_json

    def json(self, *a, **k):
        return self._on_json(*a, **k)


@pytest.fixture
def answers():
    """Install a scripted human, and guarantee it is uninstalled — a leaked one makes the next test
    answer prompts it never asked."""
    def install(*ordered, **keyed):
        a = ui.Answers(*ordered, **keyed)
        ui.install_answers(a)
        return a
    yield install
    ui.install_answers(None)


class TestPromptingFillsTheGaps:
    def test_a_supplied_argument_is_never_re_asked(self, answers, monkeypatch):
        """A flag always wins. This is what keeps the installer and every scripted provisioning run
        from stopping to ask a question they already answered."""
        monkeypatch.setattr(main.auth_mod, "user_add", lambda u, admin: None)
        a = answers()                       # nothing scripted: any prompt raises
        main.user_add("bob", True)
        assert a.asked == []

    def test_an_optional_flag_is_not_demanded_of_a_script(self, answers, monkeypatch):
        """`cuttle user add bob` must not start interrogating you about --admin. Optional flags are
        prompted only when the required argument was missing too, i.e. when we are already a wizard."""
        seen = {}
        monkeypatch.setattr(main.auth_mod, "user_add", lambda u, admin: seen.update(admin=admin))
        answers()                            # any prompt would raise
        main.user_add("bob", None)
        assert seen == {"admin": False}


class TestNothingBlocksWithoutATerminal:
    """The worst available failure is a job that hangs forever waiting for a keystroke nobody can
    send. A missing argument off-terminal must be an error, and must name the flag that fixes it."""

    def test_a_missing_argument_is_an_error_not_a_prompt(self, monkeypatch):
        monkeypatch.setattr(ui, "_ANSWERS", None)
        with pytest.raises(typer.Exit):
            ui.need(None, "--time", lambda: pytest.fail("prompted with no terminal"))

    def test_no_input_forces_it_off_even_on_a_terminal(self, answers, monkeypatch):
        """--no-input is for a script that happens to run under a terminal — a scripted human is
        still refused."""
        answers("bob")
        monkeypatch.setattr(ui, "NO_INPUT", True)
        try:
            assert ui.interactive() is False
        finally:
            monkeypatch.setattr(ui, "NO_INPUT", False)


class TestTheClientWizardPointsTheBoxItself:
    """A **generic** bundle leaves the box knowing no Head, and `cuttle setup --client` is what
    install.sh tells you to run next. Handing straight to `auth.login` would abort with "run
    `cuttle login --head …`" the moment it found no stored Head URL — the wizard refusing to ask
    the one question it exists to ask.

    What is ASKED for is an address; what is STORED is the **Cluster name**. The address becomes this
    box's `/etc/hosts` line and is never seen again — so every box in the cluster says `ccc.lab`,
    and the pin, the config and `cuttle dashboard` all agree on one spelling.
    """

    @pytest.fixture(autouse=True)
    def no_host_mutations(self, monkeypatch):
        """`point_at_head` writes /etc/hosts and resolves the answer. Both are real mutations of the
        test box; captured here so the tests can assert on WHAT would have been written."""
        self.hosts = []
        monkeypatch.setattr(setup_mod.system, "write_hosts_entry",
                            lambda addr, name=None: self.hosts.append(addr))
        monkeypatch.setattr(setup_mod.socket, "gethostbyname",
                            lambda host: "172.30.26.166" if host == "head.lan" else host)
        self.asked = []
        monkeypatch.setattr(setup_mod.tls, "fetch_cluster_name",
                            lambda url: self.asked.append(url) or "lab")

    def test_the_cluster_name_is_asked_of_the_head(self, answers, ccc_home, monkeypatch):
        answers("172.30.26.166")
        setup_mod.point_at_head()
        assert self.asked == ["https://172.30.26.166"] and settings.load()["cluster"] == "lab"

    def test_an_unpointed_box_is_asked_which_head(self, answers, ccc_home, monkeypatch):
        monkeypatch.setattr(setup_mod.auth, "login", lambda head: "alice")
        monkeypatch.setattr(setup_mod.tls, "ensure_ca", lambda url: None)
        answers("172.30.26.166")
        setup_mod.setup_client()
        assert settings.load()["head"] == headapi.head_url("ccc.lab")

    def test_the_address_typed_becomes_the_hosts_entry(self, answers, ccc_home):
        """The name is useless without it: nothing in CCC resolves `ccc.lab`, so a box pointed at
        the name with no hosts line is pointed nowhere."""
        answers("172.30.26.166")
        setup_mod.point_at_head()
        assert self.hosts == ["172.30.26.166"]

    def test_a_hostname_is_resolved_before_it_is_written(self, answers, ccc_home):
        """/etc/hosts maps an ADDRESS to a name. Writing `head.lan ccc.lab` would parse fine and
        never resolve anything."""
        answers("http://head.lan:8082")
        setup_mod.point_at_head()
        assert self.hosts == ["172.30.26.166"]

    def test_answering_with_the_name_writes_nothing(self, answers, ccc_home):
        """On a box that already has the entry — a re-run, or an Arm — the existing line is the only
        thing that could have resolved the answer. Rewriting it from itself would be circular."""
        answers(headapi.head_url("ccc.lab"))
        setup_mod.point_at_head()
        assert self.hosts == []

    def test_a_baked_box_offers_what_the_installer_wrote(self, answers, ccc_home, monkeypatch):
        """install.sh already wrote config.yaml, so the prompt arrives pre-filled and accepting it is
        one Enter — which must not corrupt the URL the bundle baked in. (`_prompt_line` pre-fills its
        buffer with the default, so the scripted human accepts it by supplying it.)"""
        baked = headapi.head_url("ccc.lab")
        settings.save({"head": baked})
        monkeypatch.setattr(setup_mod.auth, "login", lambda head: "alice")
        monkeypatch.setattr(setup_mod.tls, "ensure_ca", lambda url: None)
        a = answers(baked)
        setup_mod.setup_client()
        assert settings.load()["head"] == baked
        assert a.asked, "the box was never asked which Head it talks to"

    def test_a_cleared_field_is_re_asked_not_stored(self, answers, ccc_home):
        """The prompt is pre-filled, so "" means the field was cleared — and every normalising branch
        would turn it into a valid-looking `https://` that fails at the next login instead."""
        answers("", "172.30.26.166")
        assert setup_mod.point_at_head() == headapi.head_url("ccc.lab")

    @pytest.mark.parametrize("typed,url", [
        ("172.30.26.166", headapi.head_url("ccc.lab")),       # bare host -> scheme, no port
        ("172.30.26.166:9000", f"https://ccc.lab:9000"),   # a non-default port survives
        ("http://head.lan:8082", f"http://ccc.lab:8082"),  # a deliberate http:// survives
        ("https://head.lan:8082/", f"https://ccc.lab:8082"),  # trailing slash dropped
    ])
    def test_an_address_is_accepted_however_it_is_typed(self, answers, ccc_home, typed, url):
        """`--head` takes a URL because it is a flag. A person answering "which Head?" types an
        address, and a wizard that only accepts a full URL is a trap. The host they gave becomes the
        hosts entry; their scheme and port are theirs and survive into what is stored."""
        answers(typed)
        assert setup_mod.point_at_head() == url


class TestDangerIsProportional:
    def test_a_typed_confirmation_must_match_exactly(self, answers):
        answers("ccc-hea")                   # a near miss
        assert ui.ask_typed("Tear down?", "ccc-head") is False

    def test_removing_a_user_requires_typing_the_name(self, answers, monkeypatch):
        called = []
        monkeypatch.setattr(main.auth_mod, "user_remove", lambda u: called.append(u))
        answers("no")                        # answering the typed prompt with anything else
        with pytest.raises(typer.Exit):
            main.user_remove("alice", yes=False)
        assert called == [], "a user was deleted without the typed confirmation"


class TestTheMenuIsDerived:
    """Hand-maintaining a second copy of the command list is how it goes stale."""

    def _root(self):
        import typer
        return typer.main.get_command(main.app)

    def test_groups_are_recognised(self):
        """TyperGroup does NOT subclass click.Group — Typer vendors its own click. An isinstance
        check renders arm/user/karma as leaves and silently loses all their subcommands."""
        root = self._root()
        assert [n for n, c in root.commands.items() if menu._is_group(c)] == \
            ["arm", "user", "karma", "backup", "munge", "tidepool", "dive", "feature"]

    def test_hiding_is_menu_only(self, monkeypatch):
        """The command must stay reachable when typed. A detection bug that removed it outright is
        indistinguishable from 'this version does not have it'."""
        monkeypatch.setattr(menu, "is_head", lambda: False)
        assert "user" in self._root().commands

    def test_destructive_entries_are_never_the_default(self):
        st = menu.MenuState(logged_in=True, provisioned=True, pointed=True, on_head=True)
        rows = menu._entries(self._root(), st)
        ordered = [p for p, _, _ in rows if p not in menu.NEVER_DEFAULT]
        assert ordered and ordered[0] != "teardown"


def _reachable(st, node=None, prefix="", root=None) -> set[str]:
    """Walk every menu level and collect the command paths on offer."""
    root = root or typer.main.get_command(main.app)
    node = node if node is not None else root
    found = set()
    for path, _, cmd in menu._entries(node, st, prefix, root):
        found.add(path)
        if menu._is_group(cmd) and path != prefix.strip():
            found |= _reachable(st, cmd, "" if isinstance(cmd, menu._Folder) else f"{path} ", root)
    return found


def _labels(st, node=None, prefix="", root=None) -> list[tuple[str, str]]:
    """(path, label) for every row at every menu level, in the order they are drawn."""
    root = root or typer.main.get_command(main.app)
    node = node if node is not None else root
    out = []
    for path, label, cmd in menu._entries(node, st, prefix, root):
        out.append((path, label))
        if menu._is_group(cmd) and path != prefix.strip():
            out += _labels(st, cmd, "" if isinstance(cmd, menu._Folder) else f"{path} ", root)
    return out


class TestTheMenuGatesByState:
    """Bare `cuttle` offers only what the box can do *now*: no `run` before you can log in, no
    `setup` on a box that already carries a role, no admin off the Head. Menu-only — typed commands
    still run and fail with their own message (that invariant is tested above)."""

    def _paths(self, **state) -> set[str]:
        """Every command path reachable from *any* level of the menu in this state.

        Not just the top level any more: `GROUPS` files most commands into folders, so "offered" and
        "on the first screen" stopped being the same question. What the gating tests care about is
        whether a box can get to a command at all."""
        return _reachable(menu.MenuState(**state))

    def _top(self, **state) -> list[str]:
        """Just the first screen, in order — for the tests that are about what your hand lands on."""
        root = typer.main.get_command(main.app)
        return [p for p, _, _ in menu._entries(root, menu.MenuState(**state), "", root)]

    def test_logged_out_hides_the_submit_suite_and_offers_login(self):
        paths = self._paths(logged_in=False, provisioned=True, pointed=True, on_head=False)
        assert "login" in paths
        assert paths.isdisjoint({"dive", "karma"})
        assert "logout" not in paths and "whoami" not in paths

    def test_logged_in_shows_the_submit_suite_and_hides_login(self):
        paths = self._paths(logged_in=True, provisioned=True, pointed=True, on_head=False)
        assert {"dive", "karma"} <= paths
        assert {"logout", "whoami", "passwd"} <= paths
        assert "login" not in paths

    def test_every_command_that_needs_a_session_is_declared_as_needing_one(self):
        """`_visible` DEFAULTS to True, so a new submitter command that nobody adds to LOGIN_REQUIRED
        is not merely unsorted — it is offered to a logged-OUT user, whose first act is a 401. That is
        how one command shipped. This is the invariant, rather than a test naming one command: anything
        reaching the Head with a session bearer belongs in that set.

        Checked at every level, not just the first: filing a command into a folder must not be a way
        to smuggle it past its own gate."""
        logged_out = self._paths(logged_in=False, provisioned=True, pointed=True, on_head=False)
        assert logged_out.isdisjoint({"queue", "history", "dive", "karma", "tide", "account"})

    def test_the_menu_never_falls_back_to_a_truncated_help_string(self):
        """`_label` falls back to `get_short_help_str(limit=40)` — a command missing from LABELS gets
        its docstring's first paragraph chopped mid-word as its menu row. Every row a User can actually
        reach should be deliberately named, at every level."""
        reachable = self._paths(logged_in=True, provisioned=True, pointed=True, on_head=True)
        assert [p for p in reachable if p not in menu.LABELS] == []

    def test_setup_never_goes_away(self):
        """`setup` is where every answer is made or CHANGED, so a provisioned box still needs it —
        that is the only route to a new Feature or a different coordinator address. It also has to
        survive on a merely *pointed* box, which is the promote-a-client-to-an-Arm path (hiding it
        there was the installer bug)."""
        for provisioned, pointed in ((False, False), (False, True), (True, True)):
            assert "setup" in self._paths(logged_in=False, provisioned=provisioned, pointed=pointed,
                                          on_head=False)

    def test_setup_is_renamed_once_there_are_settings_to_change(self):
        """Next to `update`, a row reading "Set up this box" on a live Head is the one a hand reaches
        for by reflex — and it is the one that re-asks every question."""
        def label(**state):
            st = menu.MenuState(**state)
            return dict((p, lbl) for p, lbl in _labels(st))["setup"]
        assert label(logged_in=False, provisioned=False, pointed=False,
                     on_head=False) == "Set up this box"
        assert label(logged_in=True, provisioned=True, pointed=True,
                     on_head=True) == "Change this box's settings"

    def test_update_needs_a_box_that_was_already_set_up(self):
        """An **Update** lays recorded answers down again, so there must BE recorded answers. A bare
        box has none — and offering it there would promise a re-run of something that never ran."""
        assert "update" not in self._paths(logged_in=False, provisioned=False, pointed=False,
                                           on_head=False)
        assert "update" in self._paths(logged_in=False, provisioned=False, pointed=True,
                                       on_head=False)
        assert "update" in self._paths(logged_in=True, provisioned=True, pointed=True,
                                       on_head=True)

    def test_teardown_shows_once_there_is_anything_to_remove(self):
        """A role, or a client's own `~/.ccc` — but not a bare box that has merely been run once."""
        assert "teardown" not in self._paths(logged_in=False, provisioned=False, pointed=False,
                                             on_head=False)
        assert "teardown" in self._paths(logged_in=False, provisioned=False, pointed=True,
                                         on_head=False)
        assert "teardown" in self._paths(logged_in=True, provisioned=True, pointed=True,
                                         on_head=False)

    def test_a_freshly_installed_box_leads_with_login_then_setup(self):
        """What a baked bundle leaves behind: pointed, roleless, logged out. `install.sh` says to run
        `cuttle login`, so that must be the pre-selected row — with `setup` still reachable."""
        st = dict(logged_in=False, provisioned=False, pointed=True, on_head=False)
        assert self._top(**st)[0] == "login"
        assert "setup" in self._paths(**st)
        # And the folder it now lives in still sorts below `login`, so nothing about grouping
        # promoted "this box" work above the one thing install.sh told you to do.
        top = self._top(**st)
        assert top.index(f"{menu.FOLDER}box") > top.index("login")

    def test_the_dashboard_is_offered_wherever_a_head_is_known_but_never_leads(self):
        """It needs no session — the browser does its own login — so it shows on a pointed box that
        is logged out. It must still not displace `login` as the pre-selected row there, which is the
        one thing ORDER is protecting."""
        root = typer.main.get_command(main.app)
        st = menu.MenuState(logged_in=False, provisioned=False, pointed=True, on_head=False)
        rows = [p for p, _, _ in menu._entries(root, st)]
        assert "dashboard" in rows and rows[0] == "login"
        assert "dashboard" not in self._paths(logged_in=True, provisioned=True, pointed=False,
                                              on_head=False)

    def test_the_admin_family_is_the_heads_alone(self):
        admin = {"user", "arm", "build-installer"}
        assert admin.isdisjoint(self._paths(logged_in=True, provisioned=True, pointed=True,
                                            on_head=False))
        assert admin <= self._paths(logged_in=True, provisioned=True, pointed=True, on_head=True)

    def test_karma_descends_to_standings_plus_ledger_and_never_hides_standings(self):
        """karma has a client-visible read now (ledger), so it is a group everywhere — but its own
        action (standings) must not fall out of the menu behind its subcommands. Descending shows
        standings as an explicit leaf plus ledger on a client; the Head adds override/bump."""
        karma = typer.main.get_command(main.app).commands["karma"]
        client = menu.MenuState(logged_in=True, provisioned=True, pointed=True, on_head=False)
        head = menu.MenuState(logged_in=True, provisioned=True, pointed=True, on_head=True)
        root = typer.main.get_command(main.app)
        assert menu._visible_children("karma", karma, client, root) is True
        client_rows = [p for p, _, _ in menu._entries(karma, client, "karma ", root)]
        head_rows = [p for p, _, _ in menu._entries(karma, head, "karma ", root)]
        # "karma" (its own row) is standings; the whole point is that it survives the descent.
        assert client_rows == ["karma", "karma ledger"]
        assert "karma" in head_rows
        assert "karma override" in head_rows and "karma bump" in head_rows


class TestTheMenuIsGroupedNotFlat:
    """Twenty-five rows on a Head is a list nobody reads — you scan it once, fail to find the thing,
    and type the command instead. Folders are presentation only: `cuttle queue` is still
    `cuttle queue`, and the argv a folder hands back must never contain the folder."""

    HEAD = dict(logged_in=True, provisioned=True, pointed=True, on_head=True)

    def _top(self, **state):
        root = typer.main.get_command(main.app)
        return menu._entries(root, menu.MenuState(**state), "", root)

    def test_the_head_sees_nine_rows_not_twenty_five(self):
        assert len(self._top(**self.HEAD)) == 9   # Tidepool, Dives, 6 folders, dashboard

    def test_a_folder_is_never_part_of_a_command_line(self):
        """The whole risk of a synthetic level: a path that reads fine in the menu and is not a
        command. `choose` returns `picked.split()` straight to click, so a folder key leaking into a
        member's path is an unparseable argv, not a wrong screen."""
        st = menu.MenuState(**self.HEAD)
        for path in _reachable(st):
            if path in menu.GROUPS:
                continue
            assert menu.FOLDER not in path, f"{path!r} carries a folder key into its argv"

    def test_every_command_still_reaches_the_menu_in_some_state(self):
        """A folder can misfile a command; it must not be able to lose one. `GROUPS` is hand-written,
        so the failure it invites is a new command nobody adds to it — which has to fall through to
        the top level rather than vanish.

        Across states, not one: `login` is *supposed* to be gone once you hold a session. What must
        not exist is a command no box in any state is ever offered."""
        root = typer.main.get_command(main.app)
        reachable = set()
        for state in (self.HEAD,
                      dict(logged_in=False, provisioned=False, pointed=False, on_head=False),
                      dict(logged_in=False, provisioned=False, pointed=True, on_head=False),
                      dict(logged_in=True, provisioned=True, pointed=True, on_head=False)):
            reachable |= _reachable(menu.MenuState(**state))
        for name in root.commands:
            assert name in reachable, f"`cuttle {name}` is in the tree but not in the menu"

    def test_a_filed_command_leaves_the_top_level(self):
        """Otherwise grouping adds rows instead of removing them."""
        top = {p for p, _, _ in self._top(**self.HEAD)}
        assert top.isdisjoint({"queue", "history", "user", "arm", "teardown", "whoami"})

    def test_a_folder_holding_one_row_collapses_into_it(self, monkeypatch):
        """A folder earns its row by holding a choice. On a bare box almost everything is gated off,
        and making someone open "This box" to find the single thing it can do is a keystroke charging
        rent."""
        bare = dict(logged_in=False, provisioned=False, pointed=False, on_head=False)
        # `feature` is the other thing a bare box can do, so the folder holds two and stays a folder.
        assert f"{menu.FOLDER}box" in {p for p, _, _ in self._top(**bare)}
        # Take it away and the folder must disappear in favour of `setup` itself.
        monkeypatch.setitem(menu.GROUPS, f"{menu.FOLDER}box", ["update", "setup", "teardown"])
        rows = {p for p, _, _ in self._top(**bare)}
        assert "setup" in rows and f"{menu.FOLDER}box" not in rows
        # And `feature`, filed nowhere now, falls through to the top level rather than vanishing.
        assert "feature" in rows

    def test_a_collapsed_folder_keeps_the_folders_slot(self):
        """Ranked before collapsing. Otherwise the lone row jumps to wherever that command would have
        sorted on its own, and a menu that reorders itself by state is one you cannot learn."""
        bare = dict(logged_in=False, provisioned=False, pointed=False, on_head=False)
        rows = [p for p, _, _ in self._top(**bare)]
        assert rows[-1] == f"{menu.FOLDER}box"


class TestNavigatingAFolderStillProducesACommandLine:
    """`choose` hands `picked.split()` straight to click. A folder is the one menu path that is not a
    command, so the thing to prove is that walking through one still comes back as argv click can
    parse — and that backing out of a real group reached through a folder lands in the folder rather
    than teleporting to the top."""

    @pytest.fixture(autouse=True)
    def head_box(self, monkeypatch):
        monkeypatch.setattr(menu, "_detect_state", lambda: (
            menu.MenuState(logged_in=True, provisioned=True, pointed=True, on_head=True), None))

    def test_a_command_inside_a_folder_comes_back_as_its_own_argv(self, answers):
        answers(f"{menu.FOLDER}spawns", "queue")
        assert menu.choose(main.app) == ["queue"]

    def test_a_subcommand_two_levels_down_keeps_its_real_path(self, answers):
        """`arm` sits inside "Run the cluster", so this walks folder → real group → leaf."""
        answers(f"{menu.FOLDER}admin", "arm", "arm list")
        assert menu.choose(main.app) == ["arm", "list"]

    def test_backing_out_of_a_group_returns_to_the_folder_it_was_opened_from(self, answers):
        """Lopping a name off the prefix would land at the top level instead: there is nothing in the
        string "arm " that remembers you came in through a folder."""
        answers(f"{menu.FOLDER}admin", "arm", menu.BACK, "munge", "munge rotate")
        assert menu.choose(main.app) == ["munge", "rotate"]

    def test_backing_out_of_a_folder_returns_to_the_top(self, answers):
        answers(f"{menu.FOLDER}spawns", menu.BACK, "dashboard")
        assert menu.choose(main.app) == ["dashboard"]


class TestTheMenuReadsAsOneList:
    """Two rules, so twenty-five rows are one list rather than twenty-five decisions: a folder or a
    group is a noun phrase, a leaf is an imperative, and inside a real group the child drops the
    group's noun (the box is already titled `cuttle arm`)."""

    HEAD = dict(logged_in=True, provisioned=True, pointed=True, on_head=True)

    def test_no_row_asks_you_a_question(self):
        """"Who am I?" was a menu entry. A row says what picking it does; it does not interview you."""
        labels = [lbl for _, lbl in _labels(menu.MenuState(**self.HEAD))]
        assert [l for l in labels if l.endswith("?")] == []

    def test_no_child_repeats_its_own_groups_noun(self):
        """"Remove an arm" inside a box titled `cuttle arm` says it twice."""
        st = menu.MenuState(**self.HEAD)
        root = typer.main.get_command(main.app)
        for name, cmd in root.commands.items():
            if not menu._is_group(cmd) or name in menu.GROUPS:
                continue
            for path, label, _ in menu._entries(cmd, st, f"{name} ", root):
                if path == name:
                    continue      # the group's own-action row (karma → Standings)
                assert name.rstrip("s") not in label.lower().split(), \
                    f"{path!r} labelled {label!r} repeats its group's noun"

    def test_the_possessive_is_kept_only_where_ownership_is_the_filter(self):
        """`queue` and `history` are everyone's, like the Dashboard's landing table; the account is
        yours. "My Spawns" would have promised a filter there is no code for."""
        labels = dict(_labels(menu.MenuState(**self.HEAD)))
        assert labels[f"{menu.FOLDER}spawns"] == "The queue"
        assert menu.LABELS[f"{menu.FOLDER}account"].startswith("My")

    def test_a_group_that_also_runs_is_named_twice(self):
        """`cuttle karma` is both a subject you open and an action. One label for both put "Karma"
        inside "Karma", which reads as a loop."""
        st = menu.MenuState(**self.HEAD)
        root = typer.main.get_command(main.app)
        karma = root.commands["karma"]
        inside = dict((p, lbl) for p, lbl, _ in menu._entries(karma, st, "karma ", root))
        assert menu.LABELS["karma"] == "Karma"
        assert inside["karma"] == "Standings"


class TestTheSessionProbe:
    """'Logged in' for the menu means a live session, not just a file on disk. A dead token must read
    as logged out; a Head that is merely unreachable must NOT — that would nag a logged-in user to
    re-login every time their Head hiccups — and must say why it is showing less."""

    def _creds(self, monkeypatch, tmp_path):
        f = tmp_path / "credentials"
        f.write_text("tok")
        monkeypatch.setattr(menu.config, "SESSION_FILE", f)

    def test_no_token_is_logged_out_with_no_network_call(self, monkeypatch, tmp_path):
        monkeypatch.setattr(menu.config, "SESSION_FILE", tmp_path / "absent")
        monkeypatch.setattr(menu.api, "for_head",
                            lambda *a, **k: pytest.fail("probed the Head with no token to check"))
        assert menu._session() == (False, None)

    def test_a_live_session_is_logged_in(self, monkeypatch, tmp_path):
        self._creds(monkeypatch, tmp_path)
        monkeypatch.setattr(menu.api, "for_head",
                            lambda *a, **k: _Probe(lambda *a, **k: {"username": "alice"}))
        assert menu._session() == (True, None)

    def test_a_401_reads_as_logged_out(self, monkeypatch, tmp_path):
        self._creds(monkeypatch, tmp_path)
        def dead(*a, **k):
            raise menu.api.ApiError("/me", "invalid token", status=401)
        monkeypatch.setattr(menu.api, "for_head", lambda *a, **k: _Probe(dead))
        assert menu._session() == (False, None)

    def test_an_unreachable_head_is_not_a_logout(self, monkeypatch, tmp_path):
        self._creds(monkeypatch, tmp_path)
        def unreachable(*a, **k):
            raise menu.api.ApiError("/me", "cannot reach the Head", status=None)
        monkeypatch.setattr(menu.api, "for_head", lambda *a, **k: _Probe(unreachable))
        logged_in, warning = menu._session()
        assert logged_in is False
        assert warning and "offline" in warning


class TestTheBox:
    """Only what the box does to *stdout*. How it looks, and the row arithmetic that erases it, are
    verified by running `cuttle` — see TESTING.md."""

    @pytest.fixture
    def on_a_tty(self, monkeypatch):
        monkeypatch.setattr(ui, "_tty", lambda: True)

    def test_stdout_is_held_until_the_box_closes(self, on_a_tty, capsys):
        """stdout is the pipeable stream, so it can never carry borders — and a raw write inside
        the box would be an uncounted row. It is captured and replayed instead."""
        with ui.section("T", lambda: ""):
            print("raw print")
            ui.data("via data")
            # The point is the *timing*: nothing may reach stdout while the box owns the screen,
            # because an uncounted row breaks the erase. Asserting only on the final contents
            # passes just as happily when the writes went straight through.
            assert capsys.readouterr().out == ""
        cap = capsys.readouterr()
        assert cap.out == "raw print\nvia data\n"
        assert "raw print" not in cap.err     # never drawn inside the box

    def test_the_box_gives_stdout_back(self, on_a_tty, monkeypatch):
        """The box borrows stdout; it has to give it back, or everything printed *after* it goes
        into a released buffer nobody reads — which is where `cuttle user add`'s one-time password
        and `arm authorize`'s enrolment token went.

        Rich's Live installs a stdout redirect of its own, so this only reproduces against a console
        that believes it is a terminal — under capsys the Live never redirects and nothing breaks.
        """
        monkeypatch.setattr(ui, "err", Console(file=io.StringIO(), force_terminal=True,
                                               theme=ui.THEME))
        before = sys.stdout
        with ui.section("T", lambda: ""):
            pass
        assert sys.stdout is before

    def test_held_stdout_survives_a_failure(self, on_a_tty, capsys):
        """A failed command's output is the entire reason to have captured it."""
        with pytest.raises(RuntimeError):
            with ui.section("T", lambda: ""):
                print("what the command said")
                raise RuntimeError("boom")
        assert "what the command said" in capsys.readouterr().out


class TestTheAlternateScreen:
    """`cuttle` takes the terminal and gives it back. Anything worth keeping has to outlive the
    buffer, because the buffer is discarded on exit."""

    @pytest.fixture
    def on_a_tty(self, monkeypatch):
        monkeypatch.setattr(ui, "_tty", lambda: True)

    def test_the_terminal_is_restored_even_on_failure(self, on_a_tty, monkeypatch):
        """Leaving a user stranded on the alternate screen means a shell that looks wiped. The alt
        exit now comes from the screen canvas, which emits it only on a real terminal — so force
        one into a buffer, the same way TestTheBox does."""
        buf = io.StringIO()
        monkeypatch.setattr(ui, "err", Console(file=buf, force_terminal=True, theme=ui.THEME))
        with pytest.raises(RuntimeError):
            with ui.fullscreen():
                raise RuntimeError("boom")
        assert "\x1b[?1049l" in buf.getvalue()

    def test_results_survive_the_restore(self, on_a_tty, monkeypatch):
        """A result is drawn on a buffer that is about to be thrown away — the Dashboard's address is
        the whole reason anyone ran `setup --head`, and it is written eleven phases before the end.

        It used to be the one-time admin password. That is no longer a result, or anything else on a
        screen: setup spends it itself now (auth.seed_admin)."""
        buf = io.StringIO()
        monkeypatch.setattr(ui, "err", Console(file=buf, force_terminal=True, theme=ui.THEME))
        with ui.fullscreen():
            with ui.section("Setting up the Head", lambda: "done"):
                ui.result("dashboard", "https://ccc.lab")
        errs = buf.getvalue()
        assert "ccc.lab" in errs
        assert errs.index("ccc.lab") > errs.index("\x1b[?1049l"), "result died with the buffer"

    def test_stdout_survives_the_restore(self, on_a_tty, capsys):
        with ui.fullscreen():
            print("the spawn id")
        assert capsys.readouterr().out == "the spawn id\n"

class TestTheTerminalModeIsHandedBack:
    """Prompts put the terminal into a mode where nothing is echoed and Enter does not end a line.
    Leaving it that way means a shell that types nothing back — the user has to run `reset` blind."""

    def test_the_mode_is_restored_even_on_failure(self, monkeypatch):
        _, secondary = pty.openpty()
        monkeypatch.setattr(sys, "stdin", os.fdopen(secondary, "rb", buffering=0))
        before = termios.tcgetattr(secondary)
        with pytest.raises(RuntimeError):
            with ui._raw():
                assert termios.tcgetattr(secondary) != before, "_raw changed nothing to restore"
                raise RuntimeError("boom")
        assert termios.tcgetattr(secondary) == before


class TestTheHeadPredicate:
    def test_it_does_not_depend_on_head_db(self, monkeypatch):
        """head.db lives under /var/lib/ccc/head, which is drwx------ ccc ccc — so `HEAD_DB.exists()`
        is False for every user except ccc and root, *including the operator on the Head*. Using it
        would hide user management on the one box where it works."""
        from cuttle import config
        monkeypatch.setattr(config, "HEAD_DB", config.HEAD_DB.parent / "does-not-exist.db")
        monkeypatch.setattr(config, "API_VENV", config.CCC_HOME if hasattr(config, "CCC_HOME")
                            else config.API_VENV)
        # The predicate reads API_VENV, never HEAD_DB: moving head.db out of the way changes nothing.
        assert menu.is_head() == (config.API_VENV / "bin" / "python").exists()


class TestStreamsSurviveTheWizard:
    def test_prompts_do_not_pollute_stdout(self, answers, capsys):
        """Prompts are commentary. A prompt on stdout would put a menu in the middle of what
        `$(cuttle queue --json)` is capturing."""
        answers("alice")
        ui.ask_text("Which user?")
        ui.data("4201")
        captured = capsys.readouterr()
        assert captured.out.strip() == "4201"


class TestTheHeadAdminShellOutNeverBlocksOnAnInvisiblePrompt:
    """`sudo` writes its password prompt to stderr and reads from the tty. These calls capture
    output, so unless the credential is primed first the prompt goes into the pipe and the command
    waits on a question nobody can see — no error, no spinner, nothing to answer. `cuttle user add`
    went blank at exactly that point, and `arm authorize` stopped after the name (its `--user`
    picker calls `user_names`). Priming happens in `system.run`, so the rule is that every sudo
    command goes through it rather than carrying a literal "sudo" of its own.
    """

    @pytest.fixture
    def spy(self, monkeypatch):
        seen = {"primed": False, "argv": None}
        monkeypatch.setattr(system, "prime_sudo", lambda: seen.__setitem__("primed", True))
        monkeypatch.setattr(subprocess, "run",
                            lambda cmd, **kw: (seen.__setitem__("argv", cmd),
                                               subprocess.CompletedProcess(cmd, 0, "", ""))[1])
        return seen

    @pytest.mark.parametrize("call", [
        lambda: auth.user_names(),
        lambda: auth.user_exists("bob"),
        lambda: auth.user_add("bob", admin=False),
    ], ids=["user_names", "user_exists", "user_add"])
    def test_the_credential_is_primed_before_it_shells_out(self, spy, call):
        call()
        assert spy["primed"], "nothing asked for a password; sudo's own prompt would be swallowed"

    def test_the_command_still_runs_as_the_service_account(self, spy):
        """The argv must not drift while moving the `sudo` out of it."""
        auth.user_names()
        assert spy["argv"][:3] == ["sudo", "-u", "ccc"]


@pytest.fixture
def framed():
    """An open box on a canvas, at a chosen terminal height. Restores the module globals — a leaked
    `_FRAME` makes every later `ui.say` draw into a box that no longer exists."""
    class _NoScreen:
        def update(self, renderable):
            pass

    saved = (ui._FRAME, ui._SCREEN, ui._ALT, ui.err._width, ui.err._height)

    def open_box(height: int):
        ui.err._width, ui.err._height = 100, height
        ui._ALT, ui._SCREEN = True, _NoScreen()
        ui._FRAME = ui._Frame("cuttle")
        return ui._FRAME

    yield open_box
    ui._FRAME, ui._SCREEN, ui._ALT, ui.err._width, ui.err._height = saved


class TestALongMenuIsStillAMenu:
    """A box that renders empty offers nothing and explains nothing — the flow is simply over, with
    no error and no way forward. `cuttle` did exactly that once logged in: 19 entries sized against
    the terminal, drawn into a box shorter than they were, trimmed away as one over-tall row."""

    def _entries_drawn(self, frame) -> int:
        with ui.err.capture() as cap:
            ui.err.print(frame._render())
        return cap.get().count("entry ")

    @pytest.mark.parametrize("height", [24, 30, 40, 60, 100])
    def test_every_terminal_height_still_shows_the_list(self, framed, height):
        """The failure was height-dependent: it needed a terminal tall enough that the prompt asked
        for more rows than the box has, which is every ordinary window."""
        frame = framed(height)
        surface = ui._Surface()
        window = ui._select_window(surface.room(), 19)
        frame.add(ui.Group(*[ui.Text("?")] + [ui.Text(f"  entry {i}") for i in range(window)]),
                  window + 1)
        assert self._entries_drawn(frame) >= 3

    def test_a_prompt_is_never_sized_past_the_box_that_holds_it(self, framed):
        frame = framed(60)
        assert ui._Surface().room() <= frame.budget()

    def test_an_over_tall_row_crops_instead_of_vanishing(self, framed):
        """The safety net, independent of the sizing above: whatever mis-measures a row next, the
        newest one is still drawn."""
        frame = framed(40)
        frame.add(ui.Group(*[ui.Text(f"  entry {i}") for i in range(50)]), 50)
        assert self._entries_drawn(frame) >= 3


@pytest.mark.slow
def test_the_menu_actually_renders_on_a_real_terminal():
    """The one test that drives the prompt layer for real.

    Everything above replaces that layer, so all of it stays green while the real thing is broken —
    a terminal mode that mangles Rich's output, a key that never decodes, a frame that never paints.
    This catches that, and nothing else does.
    """
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["CCC_HOME"] = "/tmp/ccc-pty-test"
        os.execv(sys.executable, [sys.executable, "-m", "cuttle.main"])
    # A real window size: the screen canvas renders nothing at a pty's default 0x0.
    import fcntl
    import struct
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    time.sleep(3.0)
    out = b""
    try:
        os.write(fd, b"\x03")               # ^C — we only need the first paint
        deadline = time.time() + 3
        while time.time() < deadline:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            out += chunk
    except OSError:
        pass
    finally:
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    text = out.decode("utf8", "replace")
    assert "What do you want to do?" in text, text[-500:]
    # A throwaway CCC_HOME with no session: the menu shows the logged-out set, so `Log in` is the
    # stable entry to look for (the submit suite is gated behind a session now).
    assert "Log in" in text, text[-500:]
