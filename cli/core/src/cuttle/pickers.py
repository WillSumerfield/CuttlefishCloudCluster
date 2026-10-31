"""Pickers — prompts that offer real choices instead of asking you to remember a name.

Each degrades to a free-text prompt if its list can't be fetched (Head unreachable, not on Head,
empty registry), so a picker never hard-fails a command that needs it most.
"""

from . import auth, backup, enroll, ui


def _pick(message: str, rows: list[tuple[str, str]], noun: str) -> str:
    """`rows` is [(value, description)]. Falls back to free text when there is nothing to show."""
    if not rows:
        return ui.ask_text(f"{message} ({noun} name)")
    width = max(len(v) for v, _ in rows)
    choices = [ui.choice(f"{v.ljust(width)}  {d}" if d else v, v) for v, d in rows]
    return ui.ask_select(message, choices)


def pick_user(message: str = "Which user?") -> str:
    try:
        names = auth.user_names()
    except Exception:
        names = []
    return _pick(message, [(n, "") for n in names], "user")


def pick_arm(message: str = "Which Arm?") -> str:
    try:
        reg = enroll.load_registry()
    except Exception:
        reg = {}
    rows = [(name, f"{e.get('owner') or '-'}, {enroll._arm_state(name, e).split(',')[0]}")
            for name, e in sorted(reg.items())]
    return _pick(message, rows, "Arm")


def pick_backup(message: str = "Which backup?") -> str:
    try:
        paths = backup.list_backups()
    except Exception:
        paths = []
    return _pick(message, [(str(p), p.name) for p in paths], "backup")


def pick_dive(message: str = "Which Dive?", mine_only: bool = True) -> int:
    """The live queue — nobody remembers job ids."""
    try:
        rows = auth._api("GET", "/dives") or []
    except Exception:
        rows = []
    me = None
    if mine_only:
        try:
            me = (auth._api("GET", "/me") or {}).get("username")
        except Exception:
            me = None
    entries = []
    for s in rows:
        if me and s.get("started_by") not in (None, me):
            continue
        if s.get("id"):
            main = f"~/{s['main']}" if s.get("main") else ""
            entries.append((str(s["id"]), f"{s.get('state', '?')}  {s.get('started_by') or '-'}  {main}".strip()))
    picked = _pick(message, entries, "Dive")
    try:
        return int(str(picked).strip())
    except ValueError:
        raise ui.abort(f"{picked!r} is not a Dive id")
