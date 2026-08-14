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


class TestTheAlternateScreenLetsGoForALiveShell:
    """A command that hands the terminal to a program of its own must NOT take the alternate screen.

    `fullscreen()` claims a canvas and `_tick_start()` re-asserts it ~12×/s, absolutely. Against a real
    `ssh` drawing its own remote pty that is not a cosmetic clash: the repaint wins, and the session is
    a blank screen with the shell alive and unreadable underneath it.

    Detected off `sys.argv` because the screen is claimed before any command function is entered — so
    the detector has to recognise a command by its spelling rather than by a parsed flag.
    """

    def _streaming(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["cuttle", *argv])
        return ui._detect_streaming()

    def test_a_workspace_visit_hands_the_terminal_over(self, monkeypatch):
        """A visit IS a Tide Pool and execs the same `ssh` (`tidepool._wait_and_attach`) — but carries
        no flag saying so, which is exactly how it was missed."""
        assert self._streaming(monkeypatch, "workspace", "open", "scratch")

    def test_global_flags_do_not_hide_the_command(self, monkeypatch):
        """Every global option is a boolean switch (main._root), so none consumes a value and shifts
        the command name out of position. If one ever takes an argument, this is what breaks."""
        assert self._streaming(monkeypatch, "--debug", "workspace", "open", "scratch")
        assert self._streaming(monkeypatch, "-v", "workspace", "open", "scratch", "--time", "30")

    def test_the_name_is_prompted_for_when_absent(self, monkeypatch):
        assert self._streaming(monkeypatch, "workspace", "open")

    @pytest.mark.parametrize("argv", [
        ("workspace", "list"),
        ("workspace", "download", "scratch"),
        # 'open' as a workspace NAME, not the command — a substring match would call these streaming
        # and cost them the canvas for nothing.
        ("workspace", "new", "open"),
        ("workspace", "delete", "open"),
    ])
    def test_the_rest_of_the_workspace_commands_keep_the_canvas(self, monkeypatch, argv):
        assert not self._streaming(monkeypatch, *argv)

    def test_the_flag_driven_exemptions_still_hold(self, monkeypatch):
        assert self._streaming(monkeypatch, "run", "--tidepool")
        assert self._streaming(monkeypatch, "run", "train.py", "--follow")
        assert not self._streaming(monkeypatch, "run", "train.py", "--name", "x")


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


class TestRunPrintsTheIdToStdout:
    """`id=$(cuttle run ...)` has to capture the id and nothing else, while a human still sees the
    confirmation."""

    def test_stdout_is_just_the_spawn_id(self, signed_in, fake_slurm, project):
        result = signed_in("run", "train.py", "--name", "a spawn", "--time", "60", "--vram", "24", "--output", "out",
                          "--manifest", "requirements.txt")
        assert result.exit_code == 0, result.output
        assert result.stdout.strip().isdigit()


class TestTheRunLogKeepsNoSecrets:
    """The one place a CCC secret was landing in cleartext. Everything else under ~/.ccc that holds
    one is explicitly 0600 and holds it on purpose; this file was simply never thought of as
    secret-bearing, and it records every command line verbatim."""

    def test_a_token_is_redacted(self):
        argv = ["cuttle", "setup", "--arm", "--token", "s3cr3t-blob", "--verbose"]
        assert ui._redact_argv(argv) == [
            "cuttle", "setup", "--arm", "--token", "<redacted>", "--verbose"]

    def test_a_wandb_key_is_redacted(self):
        """A positional, not a flag — and the durable one: an enrollment token expires in an hour,
        a W&B key never does."""
        assert ui._redact_argv(["cuttle", "wandb", "set", "deadbeef"])[-1] == "<redacted>"

    def test_the_wandb_prompt_form_has_nothing_to_redact(self):
        assert ui._redact_argv(["cuttle", "wandb", "set"]) == ["cuttle", "wandb", "set"]
        assert ui._redact_argv(["cuttle", "wandb", "show"]) == ["cuttle", "wandb", "show"]

    def test_an_ordinary_command_is_untouched(self):
        argv = ["cuttle", "run", "train.py", "--vram", "24"]
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
