"""`cuttle karma` — read standings, Override a Balance, Queue-bump a Spawn.

Three commands, and they deliberately do NOT all travel the same road:

- `karma` and `karma override` are **API clients** — they run from any box, against the Head API, with
  a session. An Override touches nothing but head.db, which the API owns, so `is_admin` is the entire
  privilege it needs. Both are Dashboard-able as they stand.
- `karma bump` is a **Head-box operator command**, like `arm authorize` / `arm remove`. It must be:
  a bump is a *negative* nice, and Slurm grants a negative nice only to an operator. Slurm has never
  heard of CCC's Admin — every Spawn, and every scontrol call the API makes, is the one `ccc` uid — so
  routing it through the API would mean handing operator rights to the whole LAN-facing web process.
  The stack's only operator is the CLI on the Head, via sudo. (The **privileged admin sidecar** in
  ROADMAP Phase 2 is what eventually makes this family reachable from the Dashboard.)
"""

import subprocess

import typer

from . import config
from .auth import _api
from .system import run


def standings() -> None:
    """Every User's standing, best first. Everyone's — a Balance means nothing on its own."""
    rows = _api("GET", "/karma")
    print(f"{'USER':<16}{'BALANCE':>12}{'ESCROW':>10}{'SHARE':>8}{'NICE':>8}")
    for r in rows:
        print(f"{r['username']:<16}{r['balance']:>12.1f}{r['escrow']:>10.1f}"
              f"{r['share']:>8.2f}{r['nice']:>8}")


def override(username: str, amount: float, note: str | None = None) -> None:
    """Add (or remove) Karma from a User's Balance. Admin-only, enforced at the API."""
    r = _api("POST", "/karma/override", body={"username": username, "amount": amount, "note": note})
    print(f"cuttle: {r['username']} balance {r['balance']:+.1f}")


def _current_nice(spawn_id: int) -> int:
    r = subprocess.run(["squeue", "-h", "-j", str(spawn_id), "-o", "%y"],
                       capture_output=True, text=True)
    nice = r.stdout.strip()
    if not nice:
        raise typer.BadParameter(f"spawn {spawn_id} is not in the queue (already running, or gone)")
    return int(nice)


def bump(spawn_id: int, clear: bool = False) -> None:
    """Shove one queued Spawn up the queue — or hand it back to the economy with --clear.

    A bump **bypasses** the economy rather than participating in it: no Karma moves, and it leaves no
    ledger row. That is deliberate. A bump bought with Karma would go on winning every *other* race for
    a month; this moves one Spawn, once.

    The negative nice IS the bookkeeping. The Karma tick skips any job with nice < 0 precisely because
    only an operator could have set one — so a bumped Spawn is left alone until it runs or is cleared,
    with no bump table and no coordination state anywhere. Bump again to shove it further.
    """
    if clear:
        run(["scontrol", "update", f"JobId={spawn_id}", "Nice=0"], sudo=True)
        print(f"cuttle: spawn {spawn_id} returned to the economy (next tick re-ranks it)")
        return
    nice = min(0, _current_nice(spawn_id)) - config.BUMP_STEP
    run(["scontrol", "update", f"JobId={spawn_id}", f"Nice={nice}"], sudo=True)
    print(f"cuttle: spawn {spawn_id} bumped (nice {nice})")
