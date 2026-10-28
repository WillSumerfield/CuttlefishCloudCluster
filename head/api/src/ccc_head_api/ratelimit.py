"""In-memory failed-password throttle (`/login`, `/passwd`). Dual-keyed: a caller is blocked if EITHER
its source IP or the username tried has racked up too many recent failures, so neither one IP spraying
usernames nor a guess against one account spread over many IPs slips through. The per-username key
means anyone can lock an account out for up to WINDOW — accepted.

The IP limit is far looser: campus wifi NATs many people behind one exit IP.

State lives only in this process's memory — no schema, no migration. It resets on a Head API
restart, which is a deliberate admin action, not something a remote attacker can trigger.
"""

import time

USER_LIMIT = 5   # failures allowed per username before it is blocked
IP_LIMIT = 50    # … per source IP
WINDOW = 1800    # seconds a failure counts, so a key stays blocked this long after its last failure

_failures: dict[str, list[float]] = {}


def ip(addr: str) -> str:
    return f"ip:{addr}"


def user(name: str) -> str:
    return f"user:{name}"


def _limit(key: str) -> int:
    return IP_LIMIT if key.startswith("ip:") else USER_LIMIT


def _recent(key: str, now: float) -> list[float]:
    kept = [t for t in _failures.get(key, []) if now - t < WINDOW]
    if kept:
        _failures[key] = kept
    else:
        _failures.pop(key, None)
    return kept


def check(*keys: str) -> float | None:
    """None if none of `keys` is currently blocked, else the seconds to wait (the longest of any
    blocked key)."""
    now = time.monotonic()
    wait = None
    for key in keys:
        recent = _recent(key, now)
        if len(recent) >= _limit(key):
            wait = max(wait or 0.0, WINDOW - (now - recent[-1]))
    return wait


def record_failure(*keys: str) -> None:
    """Also drops every expired key, so spraying fresh usernames can't grow this without bound."""
    now = time.monotonic()
    for k in list(_failures):
        _recent(k, now)
    for key in keys:
        _failures.setdefault(key, []).append(now)


def clear(*keys: str) -> None:
    """A successful login: the username's failures no longer mean anything. Never pass the IP key —
    an attacker's own account would then reset its IP's count."""
    for key in keys:
        _failures.pop(key, None)


def reset() -> None:
    """Test-only: wipe all state (there is no production caller — a real reset is a process restart)."""
    _failures.clear()
