"""Where a `cuttle` command is allowed to run.

Every command declares the set of box contexts it makes sense in; `@requires` enforces that before
the body runs, so a command on the wrong box gets one clear line instead of a confusing failure deep
in its guts (`cuttle user add` on an Arm used to die on the missing API venv). The menu reads the
same declarations off `__ccc_requires__` — one place says "where does this belong".

The checks are deliberately cheap and offline: role is read from disk, and USER means only "pointed,
with a session token on disk", not "the Head agrees the token is live". A stale token is the API
call's problem to report (a clean 401), not a reason to make every command pay a network round-trip
before it starts.
"""

from __future__ import annotations

import enum
import functools

from . import config, ui


class Ctx(enum.Enum):
    """A box situation a command can require. A command names a set; it runs if the box is in ANY of
    them."""
    ANY = "any"                  # runs anywhere, even a bare box (setup)
    HEAD = "head"                # on the Head box (local admin as ccc/sudo)
    ARM = "arm"                  # on an Arm box
    PROVISIONED = "provisioned"  # Head or Arm (acts on what setup laid down)
    POINTED = "pointed"          # the box knows a Head
    USER = "user"                # pointed + a session token on disk (the API-client surface)


def satisfied() -> set[Ctx]:
    """The contexts this box meets right now. Offline — see the module docstring."""
    from . import menu   # late: menu imports this module (P2), so don't import it at module load
    s = {Ctx.ANY}
    head, arm = menu.is_head(), menu.is_arm()
    if head:
        s.add(Ctx.HEAD)
    if arm:
        s.add(Ctx.ARM)
    if head or arm:
        s.add(Ctx.PROVISIONED)
    if menu.is_pointed():
        s.add(Ctx.POINTED)
    # USER = a usable session bearer, the same test `auth._load_token` applies: the stored token OR
    # the ambient one the Head drops inside a Tidepool/Dive (where there is no ~/.ccc/session and the
    # box isn't "pointed" at all). Checking only SESSION_FILE would refuse every submitter command run
    # from inside a Tidepool.
    if config.SESSION_FILE.exists() or config.ambient_session() is not None:
        s.add(Ctx.USER)
    return s


def _explain(allowed: frozenset[Ctx], have: set[Ctx]) -> tuple[str, str | None]:
    """(what's wrong, how to fix it) for a box that satisfies none of `allowed`, phrased for the
    thing actually missing — a wrong box, an un-set-up box, or no session — not a dump of the set.
    'not logged in' is kept verbatim: it's the line the rest of the CLI already uses."""
    # Pure role mismatch — nothing about pointing or a session is in play.
    if allowed <= {Ctx.HEAD, Ctx.ARM, Ctx.PROVISIONED}:
        where = " or ".join(w for c, w in ((Ctx.HEAD, "the Head"), (Ctx.ARM, "an Arm"),
                                           (Ctx.PROVISIONED, "the Head or an Arm")) if c in allowed)
        step = ("run this on the Head" if Ctx.HEAD in allowed else
                "run this on the Arm itself" if Ctx.ARM in allowed else
                "run this on the Head or an Arm")
        return f"this command runs on {where}", step
    # Lifecycle (update/teardown): they act on what setup laid down, so a bare box has nothing to do.
    if Ctx.PROVISIONED in allowed and Ctx.POINTED in allowed:
        return "this box isn't set up yet", "run `cuttle setup` first"
    # Submitter surface: needs a session (and so a Head to point at).
    if Ctx.USER in allowed:
        head_road = " — or run it on the Head" if Ctx.HEAD in allowed else ""
        if Ctx.POINTED in have:
            return "not logged in", "log in first: `cuttle login`" + head_road
        return ("this box isn't pointed at a cluster",
                "point it first: `cuttle setup --client`, then `cuttle login`" + head_road)
    # Pointing only (login, dashboard).
    if Ctx.POINTED in allowed:
        return "this box isn't pointed at a cluster", "point it first: `cuttle setup --client`"
    return "this command can't run here", None


def require_now(*allowed: Ctx) -> None:
    """Enforce a context imperatively — for a group callback's own action (e.g. `cuttle karma` →
    standings), which can't use `@requires` without also gating its differently-scoped subcommands."""
    allowed_set = frozenset(allowed)
    have = satisfied()
    if Ctx.ANY not in allowed_set and not (have & allowed_set):
        msg, step = _explain(allowed_set, have)
        raise ui.abort(msg, next_step=step)


def requires(*allowed: Ctx):
    """Declare the contexts a command is valid in, and refuse (one clear line) on any other box.

    Stacks *below* `@app.command()` so Typer still sees the real signature through `functools.wraps`
    (`inspect.signature` follows `__wrapped__`). The declared set is left on `__ccc_requires__` for
    the menu to read.
    """
    allowed_set = frozenset(allowed)

    def deco(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            require_now(*allowed_set)
            return func(*args, **kwargs)

        wrapper.__ccc_requires__ = allowed_set
        return wrapper

    return deco


def declares(*allowed: Ctx):
    """Stamp `__ccc_requires__` without wrapping or enforcing. For a group callback whose own action
    (`cuttle karma` → standings) guards itself with `require_now` inside its
    `invoke_without_command` branch: it can't use `@requires` (that would gate its differently-scoped
    subcommands too), but the menu still needs to read where its own-action row belongs."""
    allowed_set = frozenset(allowed)

    def deco(func):
        func.__ccc_requires__ = allowed_set
        return func

    return deco
