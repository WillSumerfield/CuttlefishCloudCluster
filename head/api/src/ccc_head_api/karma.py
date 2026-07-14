"""The Karma economy (Phase 4 slice 1) — Balance, Decay, Share, and the nice a Share implies.

**Karma ranks Users; it never gates them.** Nothing here can refuse, hold, or kill a Spawn. The entire
output of this module is one integer per queued Spawn — its Slurm `nice` — so a User in poor standing
loses *contention*, and loses nothing at all when nobody is competing.

The arithmetic, end to end:

    Balance(u) = Σ over the ledger of ±amount · exp(-Δt / τ)      τ = KARMA_TAU_DAYS
    Share(u)   = σ((Balance - Escrow) / KARMA_SHARE_SCALE)        σ = logistic, so Share ∈ (0, 1)
    nice(u)    = round((1 - Share) · NICE_MAX)                    nice ∈ [0, NICE_MAX]

**A Balance is derived, never stored.** Decay is *linear*, so the decayed sum over the ledger below and
a decay-on-touch running total are exactly equal — a `karma_balance` column could only ever be a cache
that silently disagrees with the ledger it came from. See db._SCHEMA_V3.

**Escrow suppresses Share immediately**, which is what makes queue-flooding self-punishing: 500 queued
Spawns move no Balance until they finish, so settle-at-end alone would let a burst hold the top of the
queue for hours. The hold lands the moment the Spawn is queued (slice 2 writes it).

**KARMA_SHARE_SCALE cannot reorder anyone.** σ is monotonic, so for *any* scale the User order by nice
is the User order by Balance. What it tunes is **resolution** — how hard Karma competes with age. Too
small and everyone pins to the rails and age alone decides; too large and nobody separates.
"""

import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from . import db

# Decay: a Balance drifts back to 0 with a 1-month time constant. Debt is forgiven, hoards evaporate,
# and standing reflects recent conduct rather than history. It is also what makes the economy
# self-healing — Karma conjured by an Override or destroyed on a commons Arm is simply reabsorbed, so
# Σ balances ≡ 0 is an attractor rather than an invariant anyone has to police.
KARMA_TAU_DAYS = 30.0

# The Karma a User must be up (or down) to sit at Share ≈ 0.73 (or 0.27). 1 Karma = 1 TFLOP-hour, so
# 10k ≈ 8 solid days of one 4090: a User taking a quarter of the cluster for a month and donating
# nothing lands near Share 0.19. Re-tune when the fleet grows or the real spread is visible — a wrong
# scale costs resolution, never correctness.
KARMA_SHARE_SCALE = 10_000.0

# The nice range Karma spends. MUST equal cuttle.config.NICE_MAX — the CLI writes PriorityWeightAge from
# it, and the two ranges being equal is what makes poor standing cost a Spawn at most one day of apparent
# queue age. This package never imports the CLI, so the two are pinned together by a test on the seam.
NICE_MAX = 2**16

TICK_SECONDS = 30


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def decay(age_days: float) -> float:
    return math.exp(-age_days / KARMA_TAU_DAYS)


def balances(conn: sqlite3.Connection, now: datetime | None = None) -> dict[int, float]:
    """Every User's Balance, decayed to `now`. Users with no ledger rows are simply absent — they are
    at 0, which is where everyone starts and is exactly average."""
    now = now or datetime.now(timezone.utc)
    out: dict[int, float] = defaultdict(float)
    for r in db.karma_rows(conn):
        amount = r["amount"] * decay((now - _parse(r["at"])).total_seconds() / 86400)
        if r["to_user"] is not None:
            out[r["to_user"]] += amount
        if r["from_user"] is not None:
            out[r["from_user"]] -= amount
    return dict(out)


def share(balance: float, escrow: float = 0.0) -> float:
    """The effect a Balance actually has, bounded in [0, 1]. Saturating on purpose: hoarding Karma has
    diminishing returns and debt has diminishing punishment, so neither end runs away.

    Written as the two-branch logistic, not `1/(1+exp(-x))`, because that form OVERFLOWS: at a Balance
    of -1e9 (one Override typo away) `exp(-x)` raises OverflowError rather than saturating — inside the
    tick, on every pass, forever. Both branches keep the exponent negative, so an extreme Balance
    underflows harmlessly to a Share of 0.0 or 1.0. Those are legal Shares: they mean nice NICE_MAX and
    nice 0, both of which are ordinary values Slurm is happy to take.
    """
    x = (balance - escrow) / KARMA_SHARE_SCALE
    if x < 0:
        e = math.exp(x)
        return e / (1.0 + e)
    return 1.0 / (1.0 + math.exp(-x))


def nice_for(share_value: float) -> int:
    """The one number this whole module exists to produce. Best standing → nice 0 (Slurm's default, so
    a Spawn we have never touched is treated as *good* standing, not bad); worst → NICE_MAX."""
    return round((1.0 - share_value) * NICE_MAX)


def standings(conn: sqlite3.Connection) -> list[dict]:
    """Every User's standing, best first — the payload of `GET /karma` and the tick's input alike.

    Public to any authed caller by design: a Balance is meaningless in isolation. Standing only exists
    against everyone else's, so a User who cannot see the others cannot see their own.
    """
    bal = balances(conn)
    esc = db.escrow_totals(conn)
    rows = conn.execute("SELECT id, username FROM users").fetchall()
    out = []
    for r in rows:
        b, e = bal.get(r["id"], 0.0), esc.get(r["id"], 0.0)
        s = share(b, e)
        out.append({"user_id": r["id"], "username": r["username"], "balance": round(b, 2),
                    "escrow": round(e, 2), "share": round(s, 4), "nice": nice_for(s)})
    return sorted(out, key=lambda u: -u["share"])


def nice_by_user(conn: sqlite3.Connection) -> dict[int, int]:
    return {u["user_id"]: u["nice"] for u in standings(conn)}


def override(conn: sqlite3.Connection, username: str, amount: float, note: str | None = None) -> float:
    """Admin add/remove of a User's Balance — the only flow that is not a Transfer. It conjures Karma
    (or destroys it), and Decay absorbs the imbalance, which is why it needs no counterparty to
    balance against. Returns the User's new Balance.

    Distinct from a **Queue bump**, which moves one Spawn once and touches no Balance. An Override
    bought to win one race would go on winning every *other* race for a month.
    """
    user = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if user is None:
        raise KeyError(username)
    to_user = user["id"] if amount > 0 else None      # NULL from = conjured
    from_user = user["id"] if amount < 0 else None    # NULL to   = destroyed
    db.record_karma(conn, "override", abs(amount), from_user, to_user, note=note)
    return balances(conn).get(user["id"], 0.0)
