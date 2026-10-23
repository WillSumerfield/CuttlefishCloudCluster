"""The output and error layer.

The central promise is *negative*: a user never sees a traceback, and never sees nothing at all.
Both are invisible to a test that only checks exit codes — a bug here can print **nothing
whatsoever** while still exiting 1, the worst outcome available. So assertions here check what
reaches the terminal.
"""

import json
import subprocess
import sys

import pytest
import typer
from typer.testing import CliRunner

from cuttle import auth, system, ui



@pytest.fixture(autouse=True)
def _reset_flags(monkeypatch):
    """DEBUG/VERBOSE are module state; a test that flips one must not leak it into the next."""
    monkeypatch.setattr(ui, "DEBUG", False)
    monkeypatch.setattr(ui, "VERBOSE", False)


class TestErrorsReachTheUser:
    """`ui.abort` is printed, not raised for a framework to render.

    Typer vendors its own click, so a `click.ClickException` subclass sails past Typer's handler
    and is shown nowhere at all — exit code 1, no message, invisible to exit-code-only checks."""

    def app(self):
        app = typer.Typer(pretty_exceptions_enable=False)

        @app.command()
        def boom():
            raise ui.abort("the Head refused us")

        return app

    def test_the_message_is_printed(self):
        result = CliRunner().invoke(self.app(), [])
        assert result.exit_code == 1
        assert "the Head refused us" in result.output

    def test_it_is_not_rendered_as_a_usage_error(self):
        """An unreachable Head is not a typo. `typer.BadParameter` would print `Usage: ...` and
        'Try --help', which cannot possibly help."""
        result = CliRunner().invoke(self.app(), [])
        assert "Usage:" not in result.output
        assert "--help" not in result.output


class TestNoTracebacks:
    def test_an_unexpected_error_becomes_one_line(self, capsys):
        code = ui.report(RuntimeError("something broke"))
        captured = capsys.readouterr()
        assert code == 1
        assert "something broke" in captured.err
        assert "Traceback" not in captured.err

    def test_debug_puts_the_traceback_back(self, monkeypatch, capsys):
        monkeypatch.setattr(ui, "DEBUG", True)
        try:
            raise RuntimeError("something broke")
        except RuntimeError as e:
            ui.report(e)
        assert "Traceback" in capsys.readouterr().err

    def test_an_error_with_no_message_still_says_something(self, capsys):
        """`str(exc)` is empty for plenty of exceptions; printing "✗ " and nothing else would be
        another silent failure."""
        ui.report(KeyError())
        assert "KeyError" in capsys.readouterr().err


class TestJsonOutput:
    def test_json_is_parseable_and_alone(self, capsys):
        payload = [{"username": "alice", "balance": 12.5}]
        ui.emit(payload, True, lambda: pytest.fail("human renderer must not run"))
        assert json.loads(capsys.readouterr().out) == payload

    def test_without_the_flag_the_human_renderer_runs(self, capsys):
        ui.emit({"a": 1}, False, lambda: ui.data("pretty"))
        assert capsys.readouterr().out.strip() == "pretty"


class TestTables:
    def test_figures_are_right_aligned_and_words_are_not(self, capsys):
        """Alignment is inferred from the column's contents. Right-aligned prose against a ragged
        left edge is the one thing that makes a table look broken."""
        assert ui._numeric(["1200.0", "-300.0"])
        assert ui._numeric(["12", "-", ""])        # '-' is a blank, not a word
        assert not ui._numeric(["admitted", "head"])
        assert not ui._numeric(["24G", "16G"])     # a unit suffix is not a figure
        assert not ui._numeric(["-", ""])          # nothing to go on => leave it alone

    def test_a_table_prints_its_values(self, capsys):
        ui.table(["USER", "BALANCE"], [["alice", "12.5"]])
        printed = capsys.readouterr().out
        assert "alice" in printed and "12.5" in printed


class TestStreams:
    """stdout is data; stderr is commentary. A command whose output you would pipe must not have
    progress chatter mixed into it."""

    def test_status_lines_go_to_stderr(self, capsys):
        ui.ok("done")
        ui.warn("careful")
        ui.fail("nope")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert all(w in captured.err for w in ("done", "careful", "nope"))

    def test_data_goes_to_stdout(self, capsys):
        ui.data("4201")
        captured = capsys.readouterr()
        assert captured.out.strip() == "4201"
        assert captured.err == ""


class TestAStreamingCommandSkipsTheAlternateScreen:
    """`--follow` output must arrive live, so it must NOT take the alternate screen (which holds stdout
    back until the flow ends). Detected off `sys.argv` because the screen is claimed before any command
    function is entered.
    """

    def _streaming(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["cuttle", *argv])
        return ui._detect_streaming()

    def test_the_flag_driven_exemptions_still_hold(self, monkeypatch):
        assert self._streaming(monkeypatch, "logs", "58", "--follow")
        assert not self._streaming(monkeypatch, "logs", "58")


class TestHeadLocalOutputSurvivesQuietMode:
    """`system.run` captures output by default so `setup` stays legible. That default silently ate
    the one-time temp password out of `cuttle user add` — the user was created, and the operator was
    shown nothing to hand them. Any Head-local command whose stdout IS the deliverable must re-emit
    it, and must not depend on `--verbose` to do so."""

    def _fake_admin(self, monkeypatch, script: str):
        """The admin CLI stubbed to a plain `python -c`, and sudo stubbed out around it.

        `_admin` runs through `system.run(sudo=True)`, which primes the credential so the
        password prompt draws instead of being swallowed by capture. Here the escalation is
        neutralised (prime is a no-op, a leading "sudo" dropped at the subprocess boundary) so
        the test can focus on output plumbing — `run`'s capture default, `_admin`'s re-emit —
        which still runs for real.
        """
        monkeypatch.setattr(auth, "_admin_argv", lambda args: [sys.executable, "-c", script])
        monkeypatch.setattr(system, "prime_sudo", lambda: None)
        # `user_add` provisions the new User's Guise afterwards. With sudo neutralised below that
        # would run real groupadd/useradd/usermod against the test machine — accidental, and harmless
        # only while every one of them was check=False. The account plumbing has its own tests
        # (test_guises.TestAGuiseIsAlwaysInTheSharedGroup); this class is about stdout.
        monkeypatch.setattr(auth, "ensure_guise_accounts", lambda: None)
        real = subprocess.run
        monkeypatch.setattr(subprocess, "run",
                            lambda cmd, **kw: real(cmd[1:] if cmd[:1] == ["sudo"] else cmd, **kw))

    def test_the_temp_password_reaches_stdout(self, monkeypatch, capsys):
        self._fake_admin(monkeypatch, "print('created user'); print('temp password: s3cret')")
        auth.user_add("bob")
        captured = capsys.readouterr()
        assert "temp password: s3cret" in captured.out
        assert "created user" in captured.out

    def test_it_does_not_require_verbose(self, monkeypatch, capsys):
        """The regression: output was inherited, so it appeared only when ui.VERBOSE was set."""
        assert ui.VERBOSE is False          # the autouse fixture guarantees quiet mode
        self._fake_admin(monkeypatch, "print('temp password: s3cret')")
        auth.user_add("bob")
        assert "s3cret" in capsys.readouterr().out

    def test_stdout_is_capturable_by_a_script(self, monkeypatch, capsys):
        """`pw=$(cuttle user add bob)` — the password is data, not commentary."""
        self._fake_admin(monkeypatch, "print('temp password: s3cret')")
        auth.user_add("bob")
        assert capsys.readouterr().err == ""


class TestTheRunLogKeepsNoSecrets:
    """The one place a CCC secret was landing in cleartext. Everything else under ~/.ccc that holds
    one is explicitly 0600 and holds it on purpose; this file was simply never thought of as
    secret-bearing, and it records every command line verbatim."""

    def test_a_token_is_redacted(self):
        argv = ["cuttle", "setup", "--arm", "--token", "s3cr3t-blob", "--verbose"]
        assert ui._redact_argv(argv) == [
            "cuttle", "setup", "--arm", "--token", "<redacted>", "--verbose"]

    def test_an_ordinary_command_is_untouched(self):
        argv = ["cuttle", "queue", "--limit", "5"]
        assert ui._redact_argv(argv) == argv

    def test_the_logs_are_capped(self, tmp_path, monkeypatch):
        """One file per invocation, on a CLI built to be scripted — anything polling `cuttle status`
        in a loop accumulates one forever."""
        from cuttle import config
        monkeypatch.setattr(config, "MAX_LOGS", 3)
        for i in range(6):
            (tmp_path / f"cuttle-2026080{i}-000000.log").write_text("x")
        ui._prune_logs(tmp_path)
        kept = sorted(p.name for p in tmp_path.iterdir())
        assert kept == ["cuttle-20260803-000000.log", "cuttle-20260804-000000.log",
                        "cuttle-20260805-000000.log"]

    def test_the_minted_token_never_reaches_the_log(self, monkeypatch):
        logged = []
        monkeypatch.setattr(ui, "_log", lambda m: logged.append(m))
        monkeypatch.setattr(ui, "_emit", lambda m: None)
        ui.secret("  tok-abc123")
        ui.data("tok-abc123", log=False)
        assert logged == []


class TestASecretCanBeTakenOffARemoteHead:
    """`arm authorize` prints a token onto the alternate screen and then blocks for five minutes
    waiting for the donor to redeem it. The screen repaints ~12×/s (unselectable), is discarded on
    exit (no scrollback), and the token is kept out of the run log on purpose — so `c` is the only
    way off the box. It used to be offered only where a local clipboard tool existed, which a Head
    reached over ssh never has.
    """

    def test_a_box_with_no_clipboard_tool_asks_the_terminal(self, monkeypatch, capsys):
        monkeypatch.setattr(ui, "_clipboard_cmd", lambda: None)
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")

        assert ui._copy_to_clipboard("tok-123") == "terminal"
        written = capsys.readouterr().err
        assert written == "\033]52;c;dG9rLTEyMw==\a", "OSC 52 carries base64, to the terminal's clip"

    def test_a_local_tool_is_preferred_because_it_answers(self, monkeypatch, capsys):
        """Its exit code is evidence; OSC 52 is fire-and-forget."""
        monkeypatch.setattr(ui, "_clipboard_cmd", lambda: ["wl-copy"])
        monkeypatch.setattr(ui.subprocess, "run",
                            lambda *a, **kw: type("R", (), {"returncode": 0})())
        assert ui._copy_to_clipboard("tok-123") == "clipboard"
        assert capsys.readouterr().err == "", "no escape sequence when a tool took it"

    def test_a_tool_that_fails_still_falls_back_to_the_terminal(self, monkeypatch, capsys):
        """An X11 tool with no display is exactly the remote case that broke."""
        monkeypatch.setattr(ui, "_clipboard_cmd", lambda: ["xclip", "-selection", "clipboard"])
        monkeypatch.setattr(ui.subprocess, "run",
                            lambda *a, **kw: type("R", (), {"returncode": 1})())
        monkeypatch.delenv("TMUX", raising=False)
        assert ui._copy_to_clipboard("tok-123") == "terminal"
        assert "\033]52;c;" in capsys.readouterr().err

    def test_tmux_is_asked_to_pass_the_sequence_through(self, monkeypatch, capsys):
        """A multiplexer eats sequences it does not know, and this is one of them."""
        monkeypatch.setattr(ui, "_clipboard_cmd", lambda: None)
        monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
        ui._copy_to_clipboard("tok-123")
        written = capsys.readouterr().err
        assert written.startswith("\033Ptmux;") and written.endswith("\033\\")
        assert "\033\033]52;c;" in written, "ESC is doubled inside a DCS string"


class TestTheCanvasWritesOnlyWhatMoved:
    """The alternate-screen canvas is repainted ~12x/s (ui._tick_start). Rich has no incremental
    screen diff, so that used to mean rewriting every cell of the terminal — padding included —
    twelve times a second, unbuffered, into a terminal that draws bytes as they arrive. gnome
    terminal answers the DECRQM query for synchronized output (mode 2026) with status 4,
    "permanently reset", so nothing there can make such a frame atomic. The only fix is to write
    less.
    """

    @pytest.fixture
    def canvas(self, monkeypatch):
        """A real Rich console on the alternate screen, writing to a buffer we can measure."""
        import io
        from rich.console import Console
        buf = io.StringIO()
        console = Console(file=buf, width=200, height=50, force_terminal=True, highlight=False)
        console.set_alt_screen(True)
        monkeypatch.setattr(ui, "err", console)
        monkeypatch.setattr(ui, "_SCREEN", object())   # only a flag now; nothing is called on it
        monkeypatch.setattr(ui, "_PREV", None)
        monkeypatch.setattr(ui, "_REASSERT_AT", 0.0)
        monkeypatch.setattr(ui, "_FRAME", None)
        monkeypatch.setattr(ui, "_LOOSE", [])
        buf.truncate(0), buf.seek(0)

        def paint(**kw):
            buf.truncate(0), buf.seek(0)
            ui._paint(**kw)
            return buf.getvalue()
        return paint

    def _row(self, text):
        from rich.text import Text
        ui._LOOSE.append(Text(text))

    def test_the_first_paint_writes_the_whole_screen(self, canvas):
        self._row("hello")
        assert len(canvas()) > 5000     # 200x50 cells, padding and all

    def test_an_unchanged_canvas_writes_nothing_at_all(self, canvas):
        self._row("hello")
        canvas()
        assert canvas() == ""

    def test_one_changed_row_costs_one_row(self, canvas):
        """The spinner case, and the whole point: a step turning must not redraw the terminal."""
        self._row("hello")
        canvas()
        from rich.text import Text
        ui._LOOSE[-1] = Text("hellp")     # one cell, as a spinner turning is
        out = canvas()
        assert out != ""
        assert len(out) < 400           # was ~10,000

    def test_a_changed_row_is_addressed_absolutely(self, canvas):
        """Cursor-addressed per run, so a partial write can never land on the wrong line."""
        self._row("hello")
        canvas()
        self._row("second")
        assert "\x1b[" in canvas()

    def test_a_resize_rewrites_everything(self, canvas):
        """Every row moved, so there is nothing a diff could usefully say."""
        self._row("hello")
        canvas()
        ui.err.size = (120, 40)
        assert len(canvas()) > 3000

    def test_the_screen_is_re_asserted_even_when_idle(self, canvas, monkeypatch):
        """Re-assertion is the repair for a canvas something ELSE wiped — exactly the case where
        nothing changed on our side. Rarer now, never gone."""
        self._row("hello")
        canvas()
        assert canvas() == ""
        monkeypatch.setattr(ui, "_REASSERT_AT", 0.0)   # as if _REASSERT_S had elapsed
        assert len(canvas()) > 5000

    def test_a_frame_is_wrapped_in_synchronized_output(self, canvas):
        """Inert on gnome-terminal, and kept for the terminals it does reach."""
        self._row("hello")
        out = canvas()
        assert out.startswith(ui._BSU) and out.endswith(ui._ESU)

    def test_there_is_no_canvas_to_paint_without_one(self, monkeypatch):
        monkeypatch.setattr(ui, "_SCREEN", None)
        ui._paint()   # must not raise, and must not touch the terminal

    def test_a_console_that_is_not_a_terminal_still_paints(self, monkeypatch):
        """Rich refuses cursor-addressed updates unless it believes it is on the alternate screen,
        and a console writing to a buffer never is. The frame still has to be drawn — there is just
        nothing there to tear."""
        import io
        from rich.console import Console
        from rich.text import Text
        buf = io.StringIO()
        console = Console(file=buf, width=80, height=24, highlight=False)   # NOT a terminal
        monkeypatch.setattr(ui, "err", console)
        monkeypatch.setattr(ui, "_SCREEN", console.screen())
        monkeypatch.setattr(ui, "_FRAME", None)
        monkeypatch.setattr(ui, "_LOOSE", [Text("hello")])
        ui._paint()
        assert "hello" in buf.getvalue()


class TestTheRunDiff:
    """`ui._changed_runs` — contiguous stretches, so a redrawn box is one `move_to` and not forty."""

    def test_nothing_changed_is_no_runs(self):
        assert ui._changed_runs(["a", "b"], ["a", "b"]) == []

    def test_adjacent_rows_are_one_run(self):
        assert ui._changed_runs(["x", "y", "c"], ["a", "b", "c"]) == [(0, ["x", "y"])]

    def test_a_gap_splits_them(self):
        assert ui._changed_runs(["x", "b", "z"], ["a", "b", "c"]) == [(0, ["x"]), (2, ["z"])]

    def test_a_run_reaching_the_last_row_is_closed(self):
        assert ui._changed_runs(["a", "y"], ["a", "b"]) == [(1, ["y"])]


class TestACleanRunNeverReportsWarnings:
    """`_TROUBLE` is replayed by `_closing` as *the reason a run went wrong* — so anything recorded
    there is a claim that something broke. Two things put lines in it that had not.

    Together they produced the worst available outcome for `teardown`: a full sweep that removed
    everything correctly, printed `✓ Removing everything CCC  removed`, and then signed off under a
    red "Warnings" panel reporting an unreachable Head and re-serving the pre-flight caution about
    head.db as its cause.
    """

    def test_a_caution_before_a_confirmation_is_not_trouble(self, monkeypatch):
        """What a destructive command is about to do is the contract of the thing you are being asked
        to authorize, not a fault. Recorded, it made even a clean `teardown` end with "1 warning"."""
        monkeypatch.setattr(ui, "_TROUBLE", [])
        emitted = []
        monkeypatch.setattr(ui, "_emit", lambda m: emitted.append(m))
        ui.caution("this removes head.db")
        assert ui._TROUBLE == []
        assert "this removes head.db" in emitted[0]   # said to the user, just not counted against them

    def test_a_swallowed_revocation_failure_says_nothing(self, monkeypatch, tmp_path):
        """`teardown` revokes this box's Session best-effort and swallows the failure — but going
        through `auth._api` meant `ui.abort` had already printed and recorded a ✗ before raising, so
        the `except` hid the exception and nothing else. The Head here is unreachable, which on a
        teardown is the *normal* case: the box may be the Head it is trying to call."""
        from cuttle import api, config, teardown
        monkeypatch.setattr(ui, "_TROUBLE", [])
        monkeypatch.setattr(config, "SESSION_FILE", tmp_path / "session")
        config.SESSION_FILE.write_text("tok-abc123")

        def unreachable(*a, **kw):
            raise api.ApiError("/logout", api.UNREACHABLE.format(where="https://ccc.lab"))

        monkeypatch.setattr(api, "for_head", unreachable)
        emitted = []
        monkeypatch.setattr(ui, "_emit", lambda m: emitted.append(m))
        teardown.revoke_session()          # must not raise: the box is being wiped either way
        assert ui._TROUBLE == []
        assert emitted == []
