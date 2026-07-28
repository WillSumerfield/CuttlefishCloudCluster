"""Everything `cuttle` shows a human goes through here.

One module, so the CLI reads as one tool rather than a pile of commands that each invented their own
formatting. Three jobs:

**Say things consistently.** `ok` / `warn` / `fail` / `say` / `hint`, with a single accent colour and
status glyphs. Restrained on purpose: it has to stay readable on a light terminal, a dark one, and in
a log file with the colour stripped out.

**Keep provisioning legible.** `setup` runs dozens of shell commands, and echoing each one buries the
one line that matters. `step()` shows what is happening now and hides the command stream — unless
`--verbose`, or unless the step fails, in which case the captured output is exactly what you need and
is printed in full.

**Never show a traceback to a user.** A stack trace is a bug report, not an error message. `report()`
turns an exception into one line; `--debug` (or `CUTTLE_DEBUG=1`) puts the traceback back for the
person who actually wants it.

Scripting is not an afterthought: `emit()` gives every read command a `--json` form, so the pretty
output can stay pretty without anyone having to parse it.
"""

import contextlib
import io
import json
import os
import select
import sys
import termios
import threading
from contextlib import contextmanager

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# One accent, plus the three states. Named rather than inlined so a future re-theme is one edit.
THEME = Theme({
    "accent": "cyan",
    "ok": "bold green",
    "warn": "yellow",
    "err": "bold red",
    "head": "bold",
    "muted": "dim",
})

out = Console(theme=THEME, highlight=False)
# Progress, status and diagnostics go to stderr so that a command whose stdout is DATA stays pipeable.
err = Console(theme=THEME, highlight=False, stderr=True)

DEBUG = os.environ.get("CUTTLE_DEBUG", "") not in ("", "0")
VERBOSE = False
NO_INPUT = False


def configure(debug: bool = False, verbose: bool = False, no_input: bool = False) -> None:
    """Called once from the root callback. The env var is a floor, not a default: CUTTLE_DEBUG=1 in a
    shell stays on for every command in it without repeating the flag."""
    global DEBUG, VERBOSE, NO_INPUT
    DEBUG = DEBUG or debug
    VERBOSE = VERBOSE or verbose
    NO_INPUT = NO_INPUT or no_input


def _tty() -> bool:
    """A real terminal on both ends. Separate from `interactive()` because *drawing* (boxes, cursor
    escapes) needs a genuine terminal, while *asking* is also satisfied by a scripted test human."""
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):   # a closed or replaced stream
        return False


def interactive() -> bool:
    """May we stop and ask a human?

    **Only when there is one.** A missing argument in CI, a pipe or a cron job stays a usage error;
    prompting there hangs the job forever, which is the worst failure available — worse than any
    error message, because nothing is printed and nothing ever returns.

    Both streams are checked: stdin because that is where an answer would come from, stderr because
    that is where the question is drawn. `cuttle run < /dev/null` and `cuttle run 2>log` are both
    non-interactive, and neither should block.
    """
    if NO_INPUT:
        return False
    return _ANSWERS is not None or _tty()


# --- the run log ------------------------------------------------------------
#
# The frame is live: rows scroll up and out as work proceeds, and the whole box is erased when it
# closes. That is deliberate — progress is not a transcript — but it means the screen can no longer
# be the record. Every line the CLI produces is also written here, plain, and the path is offered at
# the end of any run that had something to say.

_LOG = None                     # the open log file, if a run has started one
_LOG_PATH = None
_TROUBLE: list[str] = []        # warnings and failures, in order — surfaced at the end


def _open_log():
    """One file per run, opened lazily so a command that says nothing leaves nothing behind."""
    global _LOG, _LOG_PATH
    if _LOG is None:
        import datetime
        from . import config
        try:
            config.LOG_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            _LOG_PATH = config.LOG_DIR / f"cuttle-{stamp}.log"
            _LOG = _LOG_PATH.open("a")
            _LOG.write(f"$ {' '.join(sys.argv)}\n")
        except OSError:
            # An unwritable home is not a reason to refuse to run. The screen still works.
            _LOG, _LOG_PATH = False, None
    return _LOG


def _log(markup: str) -> None:
    f = _open_log()
    if f:
        f.write(Text.from_markup(markup).plain + "\n")
        f.flush()   # a run that dies mid-step must still have its log


def log_path():
    return _LOG_PATH


# --- saying things ----------------------------------------------------------

def say(message: str) -> None:
    _log(message)
    _emit(message)


def ok(message: str) -> None:
    _log(f"OK   {message}")
    _emit(f"[ok]✓[/ok] {message}")


def warn(message: str) -> None:
    _log(f"WARN {message}")
    _TROUBLE.append(f"[warn]![/warn] {message}")
    _emit(f"[warn]![/warn] {message}")


def fail(message: str) -> None:
    _log(f"FAIL {message}")
    _TROUBLE.append(f"[err]✗[/err] {message}")
    _emit(f"[err]✗[/err] {message}")


def hint(message: str) -> None:
    """The 'what to do next' line under an error or a result. Dim: it is guidance, not the outcome."""
    _emit(f"  [muted]{message}[/muted]")


def data(text: str = "") -> None:
    """Command *output* — the thing a caller might pipe. stdout, unstyled, no glyph.

    Inside a box this lands in the section's stdout capture rather than on screen — see `section`.
    """
    _log(text)
    out.print(text, markup=False, soft_wrap=True)


def result(key: str, value: object) -> None:
    """A fact worth keeping from a long operation — shown in a panel after the box collapses.

    Progress is transient and gets erased; results are the reason the command was run. Kept apart so
    the one line you needed is not the one that scrolled past.
    """
    _FACTS.append((key, str(value)))


# --- structured output ------------------------------------------------------

def _numeric(values: list) -> bool:
    """Should this column be right-aligned? Only if every value in it reads as a figure.

    Inferred from the data rather than declared by each caller: alignment is a property of what is in
    the column, and asking every caller to say so is how one of them ends up disagreeing. Text
    right-aligned against a ragged left edge is the single thing that makes a table look broken.
    """
    seen = False
    for v in values:
        if v is None or v == "" or v == "-":
            continue
        seen = True
        try:
            float(str(v).rstrip("%").replace(",", ""))
        except ValueError:
            return False
    return seen


def table(columns: list[str], rows: list[list], title: str | None = None) -> None:
    t = Table(title=title, header_style="head", box=None, pad_edge=False, title_justify="left")
    for i, c in enumerate(columns):
        column = [r[i] for r in rows if i < len(r)]
        t.add_column(c, justify="right" if _numeric(column) else "left",
                     style="accent" if i == 0 else None)
    for row in rows:
        t.add_row(*("" if v is None else str(v) for v in row))
    out.print(t)


def kv(pairs: list[tuple[str, object]], title: str | None = None) -> None:
    """A detail block — one subject, several facts. `cuttle account`, a Spawn's detail."""
    if title:
        out.print(f"[head]{title}[/head]")
    width = max((len(k) for k, _ in pairs), default=0)
    for key, value in pairs:
        out.print(f"  [muted]{key.ljust(width)}[/muted]  {value}")


_SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list) -> str:
    """A one-line glyph plot — the *shape* of a series in a single table cell. `cuttle tide` draws one
    per Arm so a User can read the quiet hours off the curve.

    Scaled to the series' own min..max, so what reads is the shape rather than the absolute level (the
    level is printed alongside — the shape alone cannot be compared across rows, and is not meant to
    be). A flat or single-value series is all baseline; a `None` is a gap."""
    nums = [v for v in values if v is not None]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    span = hi - lo
    if span == 0:
        return _SPARK[0] * len(values)
    return "".join(" " if v is None else _SPARK[round((v - lo) / span * (len(_SPARK) - 1))]
                   for v in values)


def emit(payload, as_json: bool, render) -> None:
    """Either the machine form or the human one — never both, and never a half-styled hybrid.

    `--json` prints exactly `payload` and nothing else, so a script gets a stable contract while the
    human rendering stays free to change.
    """
    if as_json:
        out.print_json(json.dumps(payload, default=str))
    else:
        render()


# --- progress ---------------------------------------------------------------

@contextmanager
def step(label: str):
    """One phase of a long operation. Quiet while it works, loud only if it breaks.

    Under `--verbose` the spinner is skipped entirely: a live spinner and a command's own output
    fight over the same lines, and the output is the reason you asked for verbose.
    """
    if VERBOSE:
        err.print(f"[accent]→[/accent] {label}")
        yield
        ok(label)
        return
    if _FRAME is None:
        if _SCREEN is not None:
            # On the canvas but outside a box: a loose spinner row, animated by the tick. Not
            # err.status — its own Live would draw under the canvas and the tick would paint over it.
            _LOOSE.append(Spinner("dots", text=Text(f"{label}…")))
            i = len(_LOOSE) - 1
            _paint()
            try:
                yield
            except BaseException:
                _log(f"FAIL {label}")
                _TROUBLE.append(f"[err]✗[/err] {label}")
                _LOOSE[i] = Text.from_markup(f"[err]✗[/err] {label}")
                _paint()
                raise
            _log(f"OK   {label}")
            _LOOSE[i] = Text.from_markup(f"[ok]✓[/ok] {label}")
            _paint()
            return
        # No box and no canvas (a streaming command): Rich's own transient status line, on its own.
        with err.status(f"{label}…", spinner="dots"):
            try:
                yield
            except BaseException:
                fail(label)
                raise
        ok(label)
        return
    # Inside a box the spinner is a *row of the frame*, not a line Rich owns beside it. The Spinner
    # renderable animates off the frame's own refresh clock, so it stays within the borders — the
    # previous version could only fake a left bar and drew its glyph outside the box.
    i = _FRAME.add(Spinner("dots", text=Text(f"{label}…")))
    try:
        yield
    except BaseException:
        _log(f"FAIL {label}")
        _TROUBLE.append(f"[err]✗[/err] {label}")
        _FRAME.replace(i, Text.from_markup(f"[err]✗[/err] {label}"))
        raise
    _log(f"OK   {label}")
    _FRAME.replace(i, Text.from_markup(f"[ok]✓[/ok] {label}"))


# --- asking -----------------------------------------------------------------
#
# Every prompt in `cuttle` goes through `_ask`. Three reasons, and the last is the important one:
#
# **Prompts are commentary, so they must draw on stderr.** A prompt on stdout would land in the
# middle of the data stream `id=$(cuttle run …)` is capturing.
#
# **It is the test seam.** Prompts block on a real keyboard, so tests install a scripted `Answers`
# here and never reach the terminal at all. One pty smoke test covers the wiring the seam bypasses.
#
# **The prompts are ours.** They were questionary/prompt_toolkit, which owns its own region of the
# screen and repaints it — so a prompt could never be drawn *inside* a box, only beside one. That is
# what left every box open at the bottom with the live question spilling past its walls. Rendering
# the prompt as rows of the frame (see `_Frame`) is the only arrangement where the border closes,
# and it costs us a key reader and four prompt types. There is no widget tree and no event loop
# here: a prompt is a function that reads keys and returns a value, exactly as before.

_ANSWERS = None   # set by tests, via install_answers()


class Answers:
    """A scripted queue standing in for a human. Ordered by default; `keyed` matches on a substring
    of the prompt, for flows where the order is what is under test."""

    def __init__(self, *ordered, **keyed):
        self.ordered = list(ordered)
        self.keyed = keyed
        self.asked: list[str] = []

    def take(self, message: str):
        self.asked.append(message)
        for key, value in self.keyed.items():
            if key.replace("_", " ").lower() in message.lower():
                return self.keyed.pop(key)
        if not self.ordered:
            raise AssertionError(f"prompt with no scripted answer: {message!r} "
                                 f"(already asked: {self.asked})")
        return self.ordered.pop(0)

    @property
    def unused(self) -> list:
        return self.ordered + list(self.keyed.values())


def install_answers(answers: "Answers | None") -> None:
    global _ANSWERS
    _ANSWERS = answers


# One entry per escape sequence we act on. Anything unlisted decodes to "" and is ignored, which is
# the right default: an unknown sequence must never be mistaken for text and typed into the answer.
_SEQS = {
    b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
    b"\x1b[H": "home", b"\x1b[F": "end", b"\x1b[3~": "delete",
    b"\x1bOA": "up", b"\x1bOB": "down", b"\x1bOC": "right", b"\x1bOD": "left",
}


@contextmanager
def _raw():
    """stdin unbuffered and unechoed for the life of one prompt.

    Set once around the whole prompt rather than per keystroke: toggling the terminal discipline
    between keys is how a held-down arrow key ends up echoing raw escape bytes into the box.

    **Deliberately not `tty.setraw`.** Raw mode also clears `OPOST` on the output side, and on a
    terminal stdin and stderr are the same device — so it turns off `ONLCR` for everything Rich
    writes. Every `\\n` then feeds the line without returning the carriage, and the frame repaints
    as a staircase marching off the right edge. Only the input-side flags belong here.

    Clearing `ISIG` means ^C arrives as a byte rather than a signal; `_key`'s callers turn it back
    into KeyboardInterrupt, so a prompt is interruptible in the one way the rest of the CLI handles.
    """
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    mode = termios.tcgetattr(fd)     # a second read: mutating `saved` would lose the restore
    mode[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
    mode[6][termios.VMIN] = 1        # one byte is enough to return
    mode[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSADRAIN, mode)
    try:
        yield fd
    finally:
        # TCSADRAIN, not TCSANOW: restore only once our own output has been written, or the frame's
        # last repaint races the restore and lands on a terminal in the wrong mode.
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _key(fd) -> str:
    """One keypress: a character, a name from `_SEQS`, or "" for something we do not handle.

    ESC is ambiguous — it is both a key and the first byte of every arrow — so it is resolved by
    waiting a few milliseconds for more bytes. That is the standard ambiguity, and the short timeout
    is why a lone Escape is not confused with an arrow that arrived slowly.
    """
    data = os.read(fd, 1)
    if not data:
        raise KeyboardInterrupt          # stdin closed under us
    if data == b"\x1b":
        while select.select([fd], [], [], 0.03)[0]:
            data += os.read(fd, 1)
            if data[-1:].isalpha() or data[-1:] == b"~":
                break
        return _SEQS.get(data, "")
    # A multi-byte character arrives one byte at a time; assemble it before decoding, or a pasted
    # non-ASCII path turns into replacement characters.
    need_more = 4 if data[0] >= 0xF0 else 3 if data[0] >= 0xE0 else 2 if data[0] >= 0xC0 else 1
    while len(data) < need_more:
        data += os.read(fd, 1)
    return data.decode("utf8", "replace")


class _Surface:
    """Where a prompt draws. Either the open frame's rows, or — with no box — a transient Live of
    its own, so a bare prompt still repaints in place instead of scrolling a copy per keystroke."""

    def __init__(self):
        # Three places a prompt can draw: a row of the open box; a loose row on the canvas (a prompt
        # with no box open, but under fullscreen); or its own transient Live (no canvas at all — a
        # streaming command, or a test). `.slot` indexes the frame's rows or `_LOOSE`.
        self.live = None
        self.canvas = False
        self.slot = None
        if _FRAME is None:
            if _SCREEN is not None:
                self.canvas = True
            else:
                self.live = Live(Text(""), console=err, transient=True, refresh_per_second=12)

    def __enter__(self):
        if self.live:
            self.live.start()
        return self

    def show(self, renderable, height: int = 1) -> None:
        if self.live:
            self.live.update(renderable, refresh=True)
        elif self.canvas:
            if self.slot is None:
                _LOOSE.append(renderable)
                self.slot = len(_LOOSE) - 1
            else:
                _LOOSE[self.slot] = renderable
            _paint()
        elif self.slot is None:
            self.slot = _FRAME.add(renderable, height)
        else:
            _FRAME.replace(self.slot, renderable, height)

    def room(self) -> int:
        """How many lines this prompt may draw, at the place it actually lands.

        Inside a box that is the frame's budget — NOT the terminal height. The box is capped at
        BOX_ROWS however tall the window is, so on any terminal taller than ~26 rows a prompt sized
        to the terminal draws a row taller than the box can hold. Loose on the canvas or in a
        transient Live, the terminal is the limit, less a margin for what sits around it.
        """
        if _FRAME is not None and not self.canvas and self.live is None:
            return _FRAME.budget()
        return max(4, err.height - 7)

    def settle(self, line: Text) -> None:
        """Replace the live prompt with the one-line record of what was answered."""
        if self.live:
            self.live.stop()
            self.live = None
            err.print(line)
        elif self.canvas:
            _LOOSE[self.slot] = line
            _paint()
        else:
            _FRAME.replace(self.slot, line, 1)

    def __exit__(self, *exc):
        if self.live:
            self.live.stop()


def _answered(message: str, shown: str) -> Text:
    return Text.assemble(("? ", "accent"), (message, ""), ("  ", ""), (shown, "bold"))


def _select_window(room: int, count: int) -> int:
    """How many entries a select shows at once. One line of `room` goes to the question itself; the
    rest is a scrolling window that pages around the cursor rather than growing with the list — a
    row taller than the surface it draws on is a row that gets trimmed away entirely."""
    return max(3, min(count, room - 1))


def _prompt_select(surface, message: str, choices: list, default=None):
    idx = next((i for i, c in enumerate(choices) if c.value == default), 0)
    window = _select_window(surface.room(), len(choices))
    with _raw() as fd:
        while True:
            top = max(0, min(idx - window // 2, len(choices) - window))
            rows = [Text.assemble(("? ", "accent"), (message, "bold"))]
            for i in range(top, min(top + window, len(choices))):
                rows.append(Text.assemble(("❯ " if i == idx else "  ", "accent"),
                                          (str(choices[i].title),
                                           "accent" if i == idx else "")))
            surface.show(Group(*rows), len(rows))
            key = _key(fd)
            if key == "up":
                idx = (idx - 1) % len(choices)
            elif key == "down":
                idx = (idx + 1) % len(choices)
            elif key in ("\r", "\n"):
                return choices[idx].value
            elif key in ("\x03", "\x04"):
                raise KeyboardInterrupt


def _prompt_line(surface, message: str, default: str = "", secret: bool = False) -> str:
    buf = list(default)
    pos = len(buf)
    with _raw() as fd:
        while True:
            shown = "•" * len(buf) if secret else "".join(buf)
            line = Text.assemble(("? ", "accent"), (message, "bold"), ("  ", ""))
            line.append(shown[:pos])
            line.append("▏", style="accent")     # the cursor: Live owns the real one
            line.append(shown[pos:])
            surface.show(line)
            key = _key(fd)
            if key in ("\r", "\n"):
                return "".join(buf)
            elif key in ("\x03", "\x04"):
                raise KeyboardInterrupt
            elif key == "\x7f":                  # backspace
                if pos:
                    del buf[pos - 1]
                    pos -= 1
            elif key == "delete":
                if pos < len(buf):
                    del buf[pos]
            elif key == "left":
                pos = max(0, pos - 1)
            elif key == "right":
                pos = min(len(buf), pos + 1)
            elif key == "home":
                pos = 0
            elif key == "end":
                pos = len(buf)
            elif key == "\x15":                  # ^U — clear, the one editing shortcut worth having
                buf, pos = [], 0
            elif len(key) == 1 and key.isprintable():
                buf.insert(pos, key)
                pos += 1


def _prompt_confirm(surface, message: str, default: bool = True) -> bool:
    hint_text = "(Y/n)" if default else "(y/N)"
    surface.show(Text.assemble(("? ", "accent"), (message, "bold"), (f" {hint_text}", "dim")))
    with _raw() as fd:
        while True:
            key = _key(fd)
            if key in ("\r", "\n"):
                return default
            if key in ("\x03", "\x04"):
                raise KeyboardInterrupt
            if key.lower() == "y":
                return True
            if key.lower() == "n":
                return False


def _ask(kind: str, message: str, **kw):
    """One prompt, through the seam. The scripted human and the real one must agree on *every*
    rule, including that a None answer means ^C — a fake that is merely permissive lets a caller
    ship a None-valued menu entry that dies the moment a human touches it (which is exactly how
    "← back" shipped broken while the suite stayed green)."""
    if _ANSWERS is not None:
        answer = _ANSWERS.take(message)
        if answer is None:        # ^C / ^D inside a prompt
            raise KeyboardInterrupt
        if kind == "confirm" and isinstance(answer, str):
            # The scripted human answers in keystrokes, so "n" has to mean *no* — as a bare string
            # it is truthy, and a declined confirmation would read as an accepted one. The seam is
            # only worth having while it agrees with the real prompt on every rule.
            return answer.strip().lower() in ("y", "yes") if answer.strip() \
                else kw.get("default", True)
        return answer
    if not _tty():
        raise abort(f"cannot ask {message!r} — no terminal to prompt on")
    with _Surface() as surface:
        if kind == "select":
            choices = [c if isinstance(c, Choice) else Choice(str(c)) for c in kw["choices"]]
            answer = _prompt_select(surface, message, choices, kw.get("default"))
            kw = {"choices": choices}
        elif kind == "confirm":
            answer = _prompt_confirm(surface, message, kw.get("default", True))
        else:
            answer = _prompt_line(surface, message, kw.get("default", "") or "",
                                  secret=kind == "password")
        surface.settle(_answered(message, _shown(kind, answer, kw)))
    return answer


def _shown(kind: str, answer, kw: dict) -> str:
    """How an answer reads once the prompt is gone.

    A select yields its *value* (`"head"`), but the human chose a *label* ("The Head — control
    plane…"); echoing the value back would show them something they never picked. Passwords are
    never echoed at all.
    """
    if kind == "password":
        return "••••••••"
    if kind == "confirm":
        return "yes" if answer else "no"
    for choice in kw.get("choices", []):
        if getattr(choice, "value", object()) == answer:
            return str(choice.title)
    return str(answer)


def ask_text(message: str, default: str = "") -> str:
    return _ask("text", message, default=default)


def ask_int(message: str, default: str = "") -> int:
    """Prompt for a whole number, re-asking on anything that isn't one. The wizard used to wrap a raw
    `ask_text` in `int()`, so a typo ('sixty') or a bare Enter raised `invalid literal for int()` — a
    Python error — straight at the user. Re-asking is what they'd do anyway."""
    while True:
        raw = ask_text(message, default=default).strip()
        try:
            return int(raw)
        except ValueError:
            warn(f"{raw!r} isn't a whole number — enter digits only (e.g. 60)")


def ask_float(message: str, default: str = "") -> float:
    """Like `ask_int`, for a value that may be fractional or negative (a Karma adjustment)."""
    while True:
        raw = ask_text(message, default=default).strip()
        try:
            return float(raw)
        except ValueError:
            warn(f"{raw!r} isn't a number — enter a number (e.g. -50)")


def ask_password(message: str) -> str:
    return _ask("password", message)


def ask_confirm(message: str, default: bool = False) -> bool:
    return _ask("confirm", message, default=default)


class Choice:
    """A menu entry whose display text differs from the value it yields."""

    __slots__ = ("title", "value")

    def __init__(self, title: str, value=None):
        self.title = title
        self.value = title if value is None else value


def choice(label: str, value):
    """Built here so no command module renders its own menu entries — and so the scripted human in
    tests sees the same pair the real prompt does."""
    return Choice(label, value)


def ask_select(message: str, choices: list, default=None):
    """`choices` are `Choice` objects or plain strings."""
    return _ask("select", message, choices=choices, default=default)


def ask_typed(message: str, expected: str) -> bool:
    """Confirmation for something irreversible: the operator types the target's name.

    A y/N cannot protect `teardown` — it sits one arrow key from `setup` in the menu, and the same
    keystroke that confirms a Spawn cancellation would wipe the cluster. Typing a hostname is not
    something a hand does by reflex, which is the entire point.
    """
    return ask_text(f"{message} (type {expected!r} to confirm)").strip() == expected


def need(value, flag: str, ask):
    """Return `value`, or ask for it — the one place the interactive/unattended split is decided.

    A supplied flag always wins, so scripts and the installer never see a prompt. When nothing was
    supplied and nobody can be asked, the error names the flag that would have fixed it rather than
    complaining that a terminal is missing.
    """
    if value is not None:
        return value
    if not interactive():
        raise abort(f"missing {flag} — pass it explicitly (no terminal to prompt on)")
    return ask()


# --- the alternate screen ---------------------------------------------------

_ALT = False
_DEFERRED: list[tuple[str | None, list[tuple[str, str]]]] = []

# The single alternate-screen canvas (a `Console.screen()`), redrawn absolutely — home + full screen
# — on every paint. That absoluteness is the point: there is no cursor-relative repaint to fall out
# of sync, so scrolling the viewport or typing can never leave a stale frame stacked behind the live
# one. `_LOOSE` is what the canvas shows when no section box is open; `_LOCK` serialises paints
# between the main thread and the tick that animates the spinner.
_SCREEN = None
_LOCK = threading.Lock()
_LOOSE: list = []
_TICK = None
_TICK_STOP = None

# `--follow` tails a running Spawn: its whole purpose is output arriving *as it happens*. The
# alternate screen holds stdout back until the flow ends, which for a follow is when the Spawn ends —
# so the one command whose output must be live would show nothing at all until it was over. These
# runs stay inline. Read off argv rather than the parsed flag because the screen is claimed before
# any command function is entered.
STREAMING = False


def _detect_streaming() -> bool:
    return any(a in ("--follow", "-f") for a in sys.argv[1:])


@contextmanager
def fullscreen():
    """Take the whole terminal for an interactive flow, and hand it back exactly as it was.

    The alternate screen buffer (`?1049h`), which is what a pager or an editor uses: on exit the
    shell's scrollback is restored untouched, with none of the flow's own drawing left behind.

    **This is not the reverted full-screen app.** There are no widgets, no navigable screens and no
    event loop — it is the same boxes and prompts, on a scratch buffer. What it buys is that "remove
    the old box" becomes a screen clear rather than anything we have to compute.

    **Anything worth keeping must outlive the buffer.** On exit the screen is discarded, so results
    are deferred and printed to the real terminal afterwards, and stdout is captured and replayed —
    otherwise `cuttle setup --head` would restore the terminal and take the one-time admin password
    with it.
    """
    global _ALT, _SCREEN
    if not _tty() or _ALT or STREAMING:
        # Already on it (the outer frame owns the screen), or there is no terminal to take, or this
        # command streams — see STREAMING.
        yield
        return
    held = io.StringIO()
    capture = contextlib.redirect_stdout(held)
    # One canvas for the whole command, redrawn absolutely on every paint (see the `_SCREEN` note).
    _SCREEN = err.screen()
    _SCREEN.__enter__()
    _ALT = True
    _LOOSE.clear()
    _paint()
    _tick_start()                # animates the step spinner, and re-asserts the screen ~12×/s
    capture.__enter__()
    broke = False
    try:
        yield
    except BaseException:
        broke = True
        raise
    finally:
        _tick_stop()             # stop painting before we tear the canvas down
        capture.__exit__(None, None, None)
        _ALT = False
        _SCREEN.__exit__(None, None, None)
        _SCREEN = None
        if held.getvalue():
            sys.stdout.write(held.getvalue())
            sys.stdout.flush()
        for title, facts in _DEFERRED:
            _panel(title, facts)
        _DEFERRED.clear()
        _closing(broke)


def _closing(broke: bool) -> None:
    """The last thing on screen once the terminal is back.

    Progress scrolled past inside a box that no longer exists, so this is the only chance to say
    that something went wrong and where to read about it. A clean run says nothing — a CLI that
    reports its own log file on every success has trained you to ignore the line that matters.
    """
    # A failure (✗) is shown in full even when nothing propagated: `raise ui.abort(...)` becomes a
    # `typer.Exit` that Click *returns* as an exit code under standalone_mode=False, so `broke` stays
    # False — but "sudo authentication failed" is exactly the line the user needs, not a count of it.
    failed = broke or any("✗" in line for line in _TROUBLE)
    if failed and _TROUBLE:
        # A run that stopped: the warnings and errors are the reason, so they are shown in full
        # rather than pointed at.
        body = Text()
        for i, line in enumerate(_TROUBLE):
            body.append("\n" if i else "")
            body.append_text(Text.from_markup(line))
        if _LOG_PATH:
            body.append("\n\n")
            body.append(f"full log  {_LOG_PATH}", style="muted")
        err.print(Panel(body, title="[head]Warnings[/head]", title_align="left",
                        border_style="err", padding=(0, 2), width=_width()))
    elif _TROUBLE and _LOG_PATH:
        n = len(_TROUBLE)
        _emit(f"[warn]![/warn] {n} warning{'s' if n > 1 else ''} — full log: {_LOG_PATH}")


def _canvas_render():
    """The whole screen right now: completed sections (and any loose commentary) as collapsed rows,
    then the open section's box beneath them.

    Sections do NOT clear each other. The point of the box is to show the flow, so what is finished
    stays on screen above what is happening now — moving from the `Set up this box` wizard into
    `Setting up the Head` leaves the wizard's ✓ line in place rather than wiping it. Trimmed to the
    last screenful, the box included, so nothing renders past the top edge."""
    box = _FRAME._render() if _FRAME is not None else None
    room = err.height or BOX_ROWS
    if box is not None:
        room -= _height() or BOX_ROWS      # the rows the box itself will occupy
    loose = _LOOSE[-room:] if room > 0 else []
    parts = [*loose, *([box] if box is not None else [])]
    return Group(*parts) if parts else Text("")


# DEC private mode 2026, "synchronized output". A paint rewrites every cell of the screen (Rich has
# no incremental screen diff — see `_paint`), and a terminal that renders those bytes as they arrive
# shows the frame half-drawn. On WSL/Windows Terminal, where the write crosses ConPTY, that is
# *visible*: the box blinks and rows appear to jump. Between BSU and ESU the terminal buffers the
# whole frame and presents it atomically. An unknown private mode is ignored, so this is inert on the
# terminals that were already smooth (gnome-terminal et al).
_BSU, _ESU = "\x1b[?2026h", "\x1b[?2026l"


def _paint() -> None:
    """One absolute, full-screen redraw of the canvas — locked, because the tick thread paints too.

    A no-op when there is no canvas: a streaming command (fullscreen is skipped), or a section opened
    in a test. In those cases a frame draws itself through its own transient `Live` instead."""
    if _SCREEN is None:
        return
    with _LOCK:
        sync = err.is_terminal
        if sync:
            err.file.write(_BSU)
        _SCREEN.update(_canvas_render())   # writes and flushes the frame itself
        if sync:
            err.file.write(_ESU)
            err.file.flush()




def _tick_start() -> None:
    """A background repaint ~12×/s. Two jobs: animate the step spinner (the main thread is busy inside
    the step and cannot), and re-assert the absolute screen so a scroll or resize self-corrects.

    **Unconditional, deliberately.** Painting only on a visible change (a spinner turning, a resize)
    is the obvious optimisation and it breaks the second job: whatever wipes the canvas — ConPTY
    racing the alt-buffer switch, a stray write, a resize we did not notice — is exactly the case
    where nothing has changed on OUR side, so the repaint that would repair it never fires and the
    box sits there empty. Re-assertion IS the mechanism. The cost of the redraws is paid down by
    synchronized output instead (see `_paint`), which is what made them flicker.
    """
    global _TICK, _TICK_STOP
    _TICK_STOP = threading.Event()
    stop = _TICK_STOP
    def _loop():
        while not stop.wait(1 / 12):
            _paint()
    _TICK = threading.Thread(target=_loop, daemon=True)
    _TICK.start()


def _tick_stop() -> None:
    global _TICK
    if _TICK_STOP is not None:
        _TICK_STOP.set()
    if _TICK is not None:
        _TICK.join(timeout=1)
        _TICK = None


# --- sections ---------------------------------------------------------------

BOX_MAX = 96   # a box wider than this is harder to read, not more informative
BOX_ROWS = 22  # and one taller than this is mostly empty: steps scroll, they do not accumulate


_FRAME = None                          # the open _Frame, if any
_FACTS: list[tuple[str, str]] = []     # what `result()` collected for the current section


def _width() -> int:
    """Capped everywhere. Taking the terminal and filling it are separate things: the alternate
    screen is what stops the flow scrolling away, and a box stretched to 200 columns to match is
    harder to read, not more informative."""
    return min(err.width, BOX_MAX)


def _height() -> int | None:
    """A fixed, capped height on the alternate screen; natural height inline.

    Fixed rather than content-sized, because the box owns the screen there — a frame that grew and
    shrank with each step would jitter. Inline, None lets the Panel size to its content, which is
    what a box printed into scrollback should do."""
    return min(err.height, BOX_ROWS) if _ALT else None


class _Frame:
    """A titled box that *owns* its region of the screen for as long as it is open.

    The box is a single Rich `Live` rendering a `Panel`, and every line of it — commentary, spinner,
    the live prompt — is a row inside that Panel. Rich repaints the whole thing in place and knows
    exactly how tall it drew, so there is no counting of rows and no cursor arithmetic anywhere.

    This is what closes the bottom border. The previous design printed the box line by line, which
    meant the closing border could only be written once the content was finished, and anything that
    painted its own region (the prompt library, Rich's own spinner) necessarily drew *outside* the
    walls. A frame has no such gap: content and border are rendered together, every refresh.
    """

    def __init__(self, title: str):
        self.title = title
        self.rows: list[tuple[object, int]] = []
        # Normally the frame is painted onto the shared canvas (absolute redraw, no per-frame Live).
        # Without a canvas — a streaming command like `run --follow`, where `fullscreen` is a no-op,
        # or a section opened in a test — fall back to a private transient `Live`, the old in-place
        # box that erases itself on close.
        # redirect_stdout/stderr=False: Live otherwise swaps `sys.stdout` for a proxy of its own and
        # restores it on stop. `section` already holds stdout aside, and the two redirects do not
        # nest — Live restored the *capture buffer* as stdout after the capture had been released,
        # so every `ui.data` after any box (a temp password, an enrolment token) went to a dead
        # StringIO and was never seen. One owner of stdout, and it is `section`.
        self.live = None if _SCREEN is not None else Live(
            self._render(), console=err, transient=True, refresh_per_second=12,
            auto_refresh=True, redirect_stdout=False, redirect_stderr=False)

    def budget(self) -> int:
        """Rows available inside the borders. A prompt asks for this before it sizes itself — the box
        is capped at BOX_ROWS however tall the terminal is, so a prompt that measured the *terminal*
        would draw a row this trim then throws away (see `_render`)."""
        return max(3, (_height() or err.height) - 2)

    def _render(self):
        rows = self.rows
        # Finished work scrolls up and out of the box as new rows arrive — the frame shows what is
        # happening now, not a transcript. `setup` runs far more steps than a screen has rows, and
        # every one of them is in the log (see `_log`), which is where detail belongs.
        budget = self.budget()
        if sum(h for _, h in rows) > budget:
            kept, used = [], 0
            for renderable, height in reversed(rows):
                # The newest row is kept even when it alone overflows: the Panel has a fixed height
                # and crops it, which shows the top of an oversized prompt. Dropping it instead
                # renders an *empty box* — no rows, no reason, nothing to act on. That was the bug
                # a logged-in menu hit (19 entries, 18 rows of budget), so the trim now degrades
                # rather than blanks, whatever mis-sizes a row next.
                if used + height > budget and kept:
                    break
                kept.append((renderable, height))
                used += height
            rows = list(reversed(kept))
        body = Group(*(r for r, _ in rows)) if rows else Text("")
        return Panel(body, title=f"[head]{self.title}[/head]", title_align="left",
                     border_style="accent", padding=(0, 1),
                     width=_width(), height=_height())

    def _refresh(self) -> None:
        if self.live is not None:
            self.live.update(self._render(), refresh=True)
        else:
            _paint()

    def add(self, renderable, height: int = 1) -> int:
        self.rows.append((renderable, height))
        self._refresh()
        return len(self.rows) - 1

    def replace(self, i: int, renderable, height: int = 1) -> None:
        self.rows[i] = (renderable, height)
        self._refresh()


def _emit(markup: str) -> None:
    """One line of commentary — a row of the open box, a loose row on the canvas, or a plain line."""
    if _FRAME is not None:
        body = Text.from_markup(markup)
        # Rich wraps inside the Panel, so a long line stays within the borders on its own. We only need
        # the height so an overlong box trims by what was actually drawn.
        inner = _width() - 4
        _FRAME.add(body, max(1, len(body.wrap(err, inner))))
        return
    if _SCREEN is not None:
        # On the canvas, but between or outside boxes: a loose row, redrawn with the screen. NOT
        # err.print — that writes under the canvas and is wiped by the next paint.
        _LOOSE.append(Text.from_markup(markup))
        _paint()
        return
    err.print(markup)


@contextmanager
def section(title: str, summary=None):
    """A titled box for one action or phase. Collapses to a single line when it closes.

    `summary` (a callable, evaluated at exit) is what survives: enough to reconstruct what you
    answered when a later step fails. Nesting keeps the outermost box only — two frames repainting
    the same region would fight, and Rich permits one Live at a time regardless.
    """
    global _FRAME, _FACTS
    # Boxes are interactive furniture. Piped or redirected there is nothing to repaint and no
    # prompts to frame, so the box is skipped entirely and the output stays append-only — which also
    # keeps a non-interactive *failure* from being wrapped in an empty box.
    nested = _FRAME is not None
    if not (_tty() and not nested and not VERBOSE):
        try:
            yield
        finally:
            if not nested:
                line = summary() if callable(summary) else summary
                ok(f"{title}{'  ' + str(line) if line else ''}")
                _release(title)
        return

    _FACTS = []
    # NB: `_LOOSE` is NOT cleared here. A new section does not wipe the screen — the sections before
    # it stay on as collapsed ✓ rows above this box (see `_canvas_render`). Cleared only per command,
    # at `fullscreen` entry.
    frame = _Frame(title)
    _FRAME = frame
    # **Capture stdout for the life of the box.** stdout is the pipeable stream, so it can never
    # carry borders — `cuttle setup > file` must get plain bytes — and a raw write into the middle
    # of a repainting region would be overdrawn on the next refresh anyway. Rather than migrate ~50
    # `print()` calls across setup/enroll/mesh/teardown and rely on nobody ever adding a 51st, the
    # box takes stdout away for the duration: rich resolves `sys.stdout` on every write, so this
    # catches `ui.data`, `ui.table`, `ui.kv` and bare `print` alike. Released verbatim afterwards.
    held = io.StringIO()
    capture = contextlib.redirect_stdout(held)
    capture.__enter__()
    if frame.live is not None:
        frame.live.start()
    else:
        _paint()            # draw the (empty) box onto the canvas
    try:
        yield
    except BaseException:
        # Stop the frame *before* releasing stdout, so the two unwind in the order they were set up.
        # The error speaks for itself; the half-finished box does not. Captured output is released
        # either way — a failed command's output is the whole reason to have kept it.
        if frame.live is not None:
            frame.live.stop()
        capture.__exit__(*sys.exc_info())
        _FRAME = None
        _paint()
        _release(None, held.getvalue())
        raise
    if frame.live is not None:
        frame.live.stop()
    capture.__exit__(None, None, None)
    _FRAME = None
    line = summary() if callable(summary) else summary
    ok(f"{title}{'  ' + str(line) if line else ''}")   # a loose ✓ on the canvas, or a plain line
    _release(title, held.getvalue())


def _release(title: str | None = None, held: str = "") -> None:
    """Everything the box held back: captured stdout first, then the results panel.

    Order matters. stdout is the pipeable stream and is replayed verbatim, so a redirected run gets
    exactly the bytes it would have got without a box.
    """
    global _FACTS
    if held:
        sys.stdout.write(held)
        sys.stdout.flush()
    if _FACTS:
        if _ALT:
            # The alternate screen is discarded on exit; a result drawn on it would be destroyed
            # along with the box. Hold it for the real terminal.
            _DEFERRED.append((title, list(_FACTS)))
        else:
            _panel(title, _FACTS)
    _FACTS = []


def _panel(title: str | None, facts: list[tuple[str, str]]) -> None:
    """The results block — what was worth keeping from a box that is now gone."""
    width = max(len(k) for k, _ in facts)
    body = Text()
    for i, (key, value) in enumerate(facts):
        body.append("\n" if i else "")
        body.append(f"{key.ljust(width)}  ", style="muted")
        body.append(value, style="head")
    err.print(Panel(body, title=f"[head]{title}[/head]" if title else None, title_align="left",
                    border_style="ok", padding=(0, 2), width=_width()))


# --- errors -----------------------------------------------------------------

def abort(message: str, code: int = 1, next_step: str | None = None):
    """Report an *expected* failure and stop: the Head refused us, the Head is unreachable, the Spawn
    isn't yours. Returns the exception so callers can `raise ui.abort(...)` and keep control flow
    obvious to a reader (and to a type checker).

    `next_step`, when given, is printed as the dim guidance line under the error — the "how to fix it"
    to the message's "what broke". Kept on `abort` so the two always render in the right order (the
    ✗ line, then the hint), which a caller emitting `fail` + `hint` by hand can get wrong.

    Printed here and exited via `typer.Exit`, rather than raised as a `ClickException` for the
    framework to render. Two reasons, both learned the hard way:

    * **Typer vendors its own click.** `typer._click.exceptions.ClickException` is a different class
      from `click.exceptions.ClickException`, so a subclass of the latter sails straight through
      Typer's handler and is never shown — an error that vanishes entirely.
    * **`typer.BadParameter` is the wrong shape**, which is what these used to be. It is a *usage*
      error, so an unreachable Head printed `Usage: cuttle status ...` and "Try --help" — advice that
      could not possibly help.

    Printing before raising also means the message survives `CliRunner`, which invokes commands
    directly and never reaches `run_app`. The error layer is only worth trusting if the tests see
    exactly what the user sees.
    """
    fail(message)
    if next_step:
        hint(next_step)
    return typer.Exit(code)


def report(exc: BaseException) -> int:
    """Turn an exception into something worth reading, and return the exit code.

    Under `--debug` the traceback is printed instead: the person debugging the Head wants the stack,
    and making them edit code to get it is how a "clean" CLI becomes an obstructive one.
    """
    if DEBUG:
        import traceback
        traceback.print_exception(exc)
        return 1
    # A messageless exception (a bare KeyError/IndexError from a malformed response) still names its
    # type — a breadcrumb worth keeping — but framed in a sentence, not dumped as bare `KeyError`.
    fail(str(exc) or f"something went wrong (unexpected {exc.__class__.__name__}, no details)")
    hint("re-run with --debug for the full traceback")
    return 1


def run_app(app) -> None:
    """The CLI's outermost frame: whatever escapes a command becomes a message, not a stack trace.

    Typer's pretty-traceback rendering is turned off where the app is built, and we drive the parser
    with `standalone_mode=False` so *usage* errors come back here too. That last part is not
    cosmetic: `app()` in standalone mode lets Click print a bad-flag/unknown-command error itself and
    call `sys.exit` — but it prints to stderr, and `fullscreen` is holding the alternate screen, so
    that buffer (error and all) is torn down on exit and the user sees a bare non-zero exit with no
    message. Catching the error and rendering it *after* `fullscreen` restores the real screen is the
    fix. This also catches the rest — mostly `ApiError` (an unreachable Head, a refused call) plus
    real bugs.
    """
    global STREAMING
    STREAMING = _detect_streaming()
    # Typer vendors its own click, so its exceptions are `typer._click.*`, not `click.*` — catching
    # the stdlib classes would miss them entirely (see `abort`'s note). rich_utils.rich_format_error
    # is the same renderer standalone mode would have used, so the panel is unchanged.
    from typer._click.exceptions import Abort, ClickException
    from typer import rich_utils
    try:
        # Every command takes the terminal and gives it back — not just the bare-`cuttle` menu.
        # `fullscreen` is a no-op off a terminal, so pipes, cron and `--json` are untouched.
        with fullscreen():
            # standalone_mode=False: usage errors raise (caught below), `--help`/`typer.Exit` come
            # back as the return code instead of an internal sys.exit we can't frame.
            code = typer.main.get_command(app).main(standalone_mode=False)
        if isinstance(code, int) and code:
            raise SystemExit(code)
    except SystemExit:
        raise
    except ClickException as exc:
        # A bad flag, unknown command, or missing argument. fullscreen has handed the real screen
        # back, so this lands where it can be read.
        rich_utils.rich_format_error(exc)
        raise SystemExit(exc.exit_code)
    except (KeyboardInterrupt, Abort):
        err.print()
        fail("interrupted")
        raise SystemExit(130)
    except BaseException as exc:  # noqa: BLE001 — the whole point is that nothing gets through raw
        raise SystemExit(report(exc))
