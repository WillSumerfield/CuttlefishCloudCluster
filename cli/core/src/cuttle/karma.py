"""`cuttle karma` — read standings, Override a Balance, Queue-bump a Spawn.

Three commands, and they deliberately do NOT all travel the same road:

- `karma` (standings) is an **API client** — it runs from any box, against the Head API, with a
  session. A read, so it stays remote.
- `karma override` **was** an API client and became a Head-box command in **Phase 6**: the Head API
  now exposes no admin writes at all, so that a stolen Session cannot mint Karma. It needs no
  privilege beyond writing head.db — the gate is *where you must be*, not what you may do.
- `karma bump` is a **Head-box operator command**, like `arm authorize` / `arm remove`. It must be:
  a bump is a *negative* nice, and Slurm grants a negative nice only to an operator. Slurm has never
  heard of CCC's Admin — every Spawn, and every scontrol call the API makes, is the one `ccc` uid — so
  routing it through the API would mean handing operator rights to the whole LAN-facing web process.
  The stack's only operator is the CLI on the Head, via sudo. (The **privileged admin sidecar** in
  ROADMAP Phase 2 is what eventually makes this family reachable from the Dashboard.)
"""

import subprocess

from . import auth, config, ui
from .auth import _api
from .system import run


def standings(as_json: bool = False) -> None:
    """Every User's standing, best first. Everyone's — a Balance means nothing on its own."""
    rows = _api("GET", "/karma")
    ui.emit(rows, as_json, lambda: ui.table(
        ["USER", "BALANCE", "ESCROW", "SHARE", "NICE"],
        [[r["username"], f"{r['balance']:.1f}", f"{r['escrow']:.1f}",
          f"{r['share']:.2f}", r["nice"]] for r in rows]))


def ledger(days: int = 30, as_json: bool = False) -> None:
    """**Your own** economic history: the real events, and the Balance they leave you at.

    Yours alone — unlike standings (everyone's, so you can see where you rank), a transaction history
    needs nobody else's rows. There is no Decay row and there never can be: Decay is the sag of the
    Balance *between* events, not an event, so a Balance that shrank with no rows to explain it is Decay
    working. The current Balance is the curve's last point."""
    data = _api("GET", f"/karma/ledger?days={days}")
    rows, curve = data["rows"], data["curve"]

    def render():
        if curve:
            ui.say(f"balance {curve[-1]['balance']:.1f}")
        if not rows:
            ui.say(f"no ledger events in the last {days} days")
            return
        ui.table(
            ["WHEN", "KIND", "KARMA", "WITH", "SPAWN", "NOTE"],
            [[r["at"][:16].replace("T", " "), r["kind"], f"{r['amount']:+.1f}",
              r.get("counterparty") or "-", r.get("job_id") or "-", r.get("note") or "-"]
             for r in rows])

    ui.emit(data, as_json, render)


def tide(days: int = 7, as_json: bool = False) -> None:
    """The Tide — how badly each Arm's card has been wanted, hour by hour. Read the shape and pick your
    quiet hour; nothing here quotes a price or warns you off (Karma ranks, it never gates).

    One sparkline per Arm, each scaled to its *own* range so a lightly-used card still shows a shape to
    read — the absolute level is the `NOW` column. Demand is bucketed to the hour by the Head."""
    series = _api("GET", f"/karma/demand?days={days}")

    def render():
        if not series:
            ui.say("no demand recorded yet — the Tide fills in as Spawns contend for cards")
            return
        by_node: dict[str, list] = {}
        for r in series:
            by_node.setdefault(r["node"], []).append(r["demand"])
        ui.table(
            ["ARM", "TIDE", "NOW"],
            [[node, ui.sparkline(vals), f"{vals[-1]:.1f}"] for node, vals in sorted(by_node.items())])

    ui.emit(series, as_json, render)


def override(username: str, amount: float, note: str | None = None) -> None:
    """Add (or remove) Karma from a User's Balance. **Head-local** — run it on the Head.

    It was an API route until Phase 6, and on its own terms it could have stayed: an Override touches
    nothing but head.db, which the API owns, so `is_admin` was all the privilege it needed. What it
    could not survive is the phase's invariant — the Head API exposes no admin writes at all. Minting
    Karma is the most valuable thing a stolen Session could do, and no authorization check makes a
    remote write as safe as one that cannot be requested remotely.
    """
    auth._admin(["override", username, str(amount), *(["--note", note] if note else [])])


def _pending_nice(spawn_id: int) -> int:
    """The Spawn's current nice — and a refusal if a bump could not possibly do anything to it.

    A bump only means something to a **PENDING** Spawn: nice feeds the priority that decides who runs
    *next*, and a Spawn already holding a card is past that decision. So "is it in the queue" is the
    wrong question — `squeue` lists RUNNING jobs too, and asking only that would let a bump on a
    running Spawn succeed, change nothing, and report success. "Is it still waiting" is the question.
    """
    r = subprocess.run(["squeue", "-h", "-j", str(spawn_id), "-o", "%T %y"],
                       capture_output=True, text=True)
    fields = r.stdout.split()
    # A bump refusal is runtime queue state, not a bad argument — ui.abort, not BadParameter's
    # "Try --help" box (which would tell the operator to check syntax that was never wrong).
    if not fields:
        raise ui.abort(
            f"Spawn {spawn_id} is not in the queue (finished, cancelled, or never existed)")
    state, nice = fields[0], int(fields[1])
    if state != "PENDING":
        raise ui.abort(f"Spawn {spawn_id} is {state.lower()}, not waiting — a bump only "
                       f"reorders the queue, and this Spawn is already past that")
    return nice


def bump(spawn_id: int, clear: bool = False) -> None:
    """Shove one queued Spawn up the queue — or hand it back to the economy with --clear.

    A bump **bypasses** the economy rather than participating in it: no Karma moves, and it leaves no
    ledger row. That is deliberate. A bump bought with Karma would go on winning every *other* race for
    a month; this moves one Spawn, once.

    The negative nice IS the bookkeeping. The Karma tick skips any job with nice < 0 precisely because
    only an operator could have set one — so a bumped Spawn is left alone until it runs or is cleared,
    with no bump table and no coordination state anywhere. Bump again to shove it further.
    """
    current = _pending_nice(spawn_id)  # both paths are meaningless on a Spawn that is not waiting
    if clear:
        run(["scontrol", "update", f"JobId={spawn_id}", "Nice=0"], sudo=True)
        ui.ok(f"spawn {spawn_id} returned to the economy (next tick re-ranks it)")
        return
    nice = min(0, current) - config.BUMP_STEP
    run(["scontrol", "update", f"JobId={spawn_id}", f"Nice={nice}"], sudo=True)
    ui.ok(f"spawn {spawn_id} bumped (nice {nice})")
