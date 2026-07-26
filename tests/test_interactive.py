"""Phase 6 — every command runs with no args and asks for the rest.

**Consequence tests only.** Nothing here checks how anything looks; see `TESTING.md`. Each test
below names something that outlives the screen — a script that blocks forever, a user deleted
without confirmation, a piped `id=$(cuttle run …)` corrupted, a one-time password lost, a terminal
left stranded. This file once held 40 tests, most of them asserting cursor arithmetic and box
borders; the drawing is verified by running `cuttle` and looking at it.

Prompts go through the `ui` seam with a scripted human, because a real prompt blocks on a real
keyboard. One test at the bottom drives a real pty, because the seam stays green while the prompt
layer underneath it is broken.
"""

import io
import os
import pty
import sys
import termios
import time

import pytest
import typer
from rich.console import Console

from cuttle import main, menu, ui


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
        assert [n for n, c in root.commands.items() if menu._is_group(c)] == ["arm", "user", "karma"]

    def test_hiding_is_menu_only(self, monkeypatch):
        """The command must stay reachable when typed. A detection bug that removed it outright is
        indistinguishable from 'this version does not have it'."""
        monkeypatch.setattr(menu, "is_head", lambda: False)
        assert "user" in self._root().commands

    def test_destructive_entries_are_never_the_default(self):
        st = menu.MenuState(logged_in=True, set_up=True, on_head=True)
        rows = menu._entries(self._root(), st)
        ordered = [p for p, _, _ in rows if p not in menu.NEVER_DEFAULT]
        assert ordered and ordered[0] != "teardown"


class TestTheMenuGatesByState:
    """Bare `cuttle` offers only what the box can do *now*: no `run` before you can log in, no
    `setup` on a box already set up, no admin off the Head. Menu-only — typed commands still run and
    fail with their own message (that invariant is tested above)."""

    def _paths(self, **state) -> set[str]:
        root = typer.main.get_command(main.app)
        return {p for p, _, _ in menu._entries(root, menu.MenuState(**state))}

    def test_logged_out_hides_the_submit_suite_and_offers_login(self):
        paths = self._paths(logged_in=False, set_up=True, on_head=False)
        assert "login" in paths
        assert paths.isdisjoint({"run", "status", "logs", "outputs", "cancel", "karma"})
        assert "logout" not in paths and "whoami" not in paths

    def test_logged_in_shows_the_submit_suite_and_hides_login(self):
        paths = self._paths(logged_in=True, set_up=True, on_head=False)
        assert {"run", "status", "logs", "outputs", "cancel", "karma"} <= paths
        assert {"logout", "whoami", "passwd"} <= paths
        assert "login" not in paths

    def test_setup_shows_only_before_the_box_is_set_up(self):
        assert "setup" in self._paths(logged_in=False, set_up=False, on_head=False)
        assert "setup" not in self._paths(logged_in=False, set_up=True, on_head=False)

    def test_teardown_shows_only_once_the_box_is_set_up(self):
        assert "teardown" not in self._paths(logged_in=False, set_up=False, on_head=False)
        assert "teardown" in self._paths(logged_in=True, set_up=True, on_head=False)

    def test_the_admin_family_is_the_heads_alone(self):
        admin = {"user", "arm", "build-installer"}
        assert admin.isdisjoint(self._paths(logged_in=True, set_up=True, on_head=False))
        assert admin <= self._paths(logged_in=True, set_up=True, on_head=True)

    def test_karma_descends_to_standings_plus_ledger_and_never_hides_standings(self):
        """karma has a client-visible read now (ledger), so it is a group everywhere — but its own
        action (standings) must not fall out of the menu behind its subcommands. Descending shows
        standings as an explicit leaf plus ledger on a client; the Head adds override/bump."""
        karma = typer.main.get_command(main.app).commands["karma"]
        client = menu.MenuState(logged_in=True, set_up=True, on_head=False)
        head = menu.MenuState(logged_in=True, set_up=True, on_head=True)
        assert menu._visible_children("karma", karma, client) is True
        client_rows = [p for p, _, _ in menu._entries(karma, client, "karma ")]
        head_rows = [p for p, _, _ in menu._entries(karma, head, "karma ")]
        # "karma" (its own row) is standings; the whole point is that it survives the descent.
        assert client_rows == ["karma", "karma ledger"]
        assert "karma" in head_rows
        assert "karma override" in head_rows and "karma bump" in head_rows


class TestTheSessionProbe:
    """'Logged in' for the menu means a live session, not just a file on disk. A dead token must read
    as logged out; a Head that is merely unreachable must NOT — that would nag a logged-in user to
    re-login every time their Head hiccups — and must say why it is showing less."""

    def _creds(self, monkeypatch, tmp_path):
        f = tmp_path / "credentials"
        f.write_text("tok")
        monkeypatch.setattr(menu.config, "CREDENTIALS_FILE", f)

    def test_no_token_is_logged_out_with_no_network_call(self, monkeypatch, tmp_path):
        monkeypatch.setattr(menu.config, "CREDENTIALS_FILE", tmp_path / "absent")
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
        """The one-time admin password is drawn on a buffer that is about to be thrown away."""
        buf = io.StringIO()
        monkeypatch.setattr(ui, "err", Console(file=buf, force_terminal=True, theme=ui.THEME))
        with ui.fullscreen():
            with ui.section("Setting up the Head", lambda: "done"):
                ui.result("temp password", "k7Qm")
        errs = buf.getvalue()
        assert "k7Qm" in errs
        assert errs.index("k7Qm") > errs.index("\x1b[?1049l"), "result died with the buffer"

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
        `id=$(cuttle run …)` is capturing."""
        answers("alice")
        ui.ask_text("Which user?")
        ui.data("4201")
        captured = capsys.readouterr()
        assert captured.out.strip() == "4201"


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
