"""In-memory failed-login throttle. Dual-keyed: a caller is blocked if EITHER its source IP or its
attempted username has racked up too many recent failures, so neither a single attacker IP spraying
many usernames nor a distributed guess against one account from many IPs slips through.

State lives only in this process's memory — no schema, no migration. It resets on a Head API
restart, which is a deliberate admin action, not something a remote attacker can trigger.
"""

import time

THRESHOLD = 5    # failures allowed (per key) before it is blocked
COOLDOWN = 300   # seconds a key stays blocked, counted from its most recent failure

_failures: dict[str, list[float]] = {}


def _recent(key: str, now: float) -> list[float]:
    kept = [t for t in _failures.get(key, []) if now - t < COOLDOWN]
    _failures[key] = kept
    return kept


def check(*keys: str) -> float | None:
    """None if none of `keys` is currently blocked, else the seconds to wait (the longest of any
    blocked key)."""
    now = time.monotonic()
    wait = None
    for key in keys:
        recent = _recent(key, now)
        if len(recent) >= THRESHOLD:
            remaining = COOLDOWN - (now - recent[-1])
            wait = max(wait or 0.0, remaining)
    return wait


def record_failure(*keys: str) -> None:
    now = time.monotonic()
    for key in keys:
        _failures.setdefault(key, []).append(now)


def clear(*keys: str) -> None:
    """A successful login for these keys — their failure history no longer means anything."""
    for key in keys:
        _failures.pop(key, None)


def reset() -> None:
    """Test-only: wipe all state (there is no production caller — a real reset is a process restart)."""
    _failures.clear()
