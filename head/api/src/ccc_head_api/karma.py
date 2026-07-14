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
from datetime import datetime, timedelta, timezone

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

# The ceiling on Demand, and the reason it needs one. Demand is set by whoever is *queuing* but paid by
# whoever is *running* — so a User can inflate everyone else's bill for free: queue 500 Spawns, let the
# cluster be billed at the spike, then cancel. Escrow released, nothing settled, the flooder pays ZERO.
# And because Balance drives `nice` for τ=30 days, an unbounded spike would let one User bench another
# for a month — which is a **gate**, and this model has none. Past ~8x the signal is saturated anyway
# (the card is plainly scarce; a larger number changes no scheduling decision) while the *billing*
# consequence kept growing without bound. Accrual already dilutes a short spike across a long Spawn's
# life; this bounds the rest.
DEMAND_MAX = 8.0

# The Tide's window (slice 3), and so how long a sample is worth keeping.
DEMAND_RETENTION_DAYS = 30


def tides(conn: sqlite3.Connection) -> dict[str, dict[int, float]]:
    """The **Tide**: each Arm's mean Demand by hour of the day, over the last DEMAND_RETENTION_DAYS.

    24 buckets per Arm, and no more. Hour-of-week (168) would be the "obvious" refinement and is the
    wrong one: the window is ~4 weeks, so each bucket would forecast off ~4 observations. 24 buckets get
    ~30 days × 120 samples/hour each.

    ⚠ Known and accepted: hour-of-day blends a dead Sunday into a busy Tuesday, so the forecast is a
    little wrong on both. The fix, if it ever bites, is `hour × is_weekend` (48 buckets, still ~960
    observations in the thinnest) — one `GROUP BY` away.
    """
    t: dict[str, dict[int, float]] = defaultdict(dict)
    for r in db.demand_by_hour(conn, DEMAND_RETENTION_DAYS):
        t[r["node"]][r["hour"]] = r["demand"]
    return t


def tide(tides: dict[str, dict[int, float]], node: str, hour: int) -> float:
    """One Arm's forecast Demand at one hour. An unseen Arm or an empty bucket forecasts **1.0**.

    1.0 is not a placeholder, it is the honest cold-start value: a cluster with no history has no basis
    to forecast anything but "ordinary", and 1.0 is what a card running exactly one Spawn reads. It is
    also what makes slice 2 a special case of this one rather than something to rewrite — with a flat
    Tide, `hold_for` reduces EXACTLY to `minutes/60 × gpus × max(Power over eligible)`.
    """
    return tides.get(node, {}).get(hour, 1.0)


def _up(arm: dict) -> bool:
    """Can Slurm actually place work here? A drained or downed Arm takes nothing, so no queued Spawn is
    eligible for it and none of their Demand lands on it — which correctly makes every *remaining* card
    scarcer the moment one goes away."""
    state = arm.get("state") or ""
    return bool(arm.get("admitted")) and "down" not in state and "drain" not in state


def eligible(arms: list[dict], need) -> list[dict]:
    """The Arms a Spawn could run on — recomputed from the LIVE registry every pass, never frozen at
    submit. An Arm admitted after this Spawn queued joins its eligible set and takes a share of its
    Demand; a drained one drops out.

    An Admin-pinned Spawn (`--node`) is eligible for exactly one Arm, so it concentrates all of its
    Demand there. That is the honest price of naming a machine instead of declaring a need.
    """
    if need["node"]:
        return [a for a in arms if a["name"] == need["node"]]
    return [a for a in arms if _up(a) and a["vram_mb"]
            and round(a["vram_mb"] / 1024) >= (need["vram_gb"] or 0)]


def demand_by_arm(arms: list[dict], jobs: list[dict], needs: dict) -> dict[str, float]:
    """How badly each Arm is wanted, right now: the device-claims on it over the devices it has.

    A **running** Spawn claims the card it holds — so any Arm actually running something reads ≥ 1.0,
    and **a second is therefore never sold below its Power**. A **queued** Spawn spreads one claim
    evenly across every Arm it is eligible for, so its total contribution to the fleet is 1.0 wherever
    it eventually lands, and the scarce big-VRAM cards — wanted by big jobs *and* small ones —
    accumulate the most. An idle Arm nobody wants reads 0, and is charged to nobody.

    A queued Spawn with no `spawns` row (the enrollment benchmark) adds no queued Demand: we cannot know
    what it is eligible for. It is pinned to one node and short-lived, so the undercount is negligible —
    and once it *runs* it claims its card like anything else.
    """
    d = {a["name"]: 0.0 for a in arms}
    for j in jobs:
        if j["state"] == "RUNNING" and j["node"] in d:
            d[j["node"]] += j["gpus"]
    for j in jobs:
        if j["state"] != "PENDING":
            continue
        need = needs.get(j["id"])
        if need is None or not need["gpus"]:  # unattributed, or CPU-only (claims no device)
            continue
        elig = eligible(arms, need)
        for a in elig:
            d[a["name"]] += need["gpus"] / len(elig)
    return {name: min(DEMAND_MAX, v) for name, v in d.items()}


def cost(seconds: float, gpus: int, power: float, demand: float) -> float:
    """What a slice of held device-time is worth. **1 Karma = 1 TFLOP-hour**, and Power is already
    TFLOP/s, so there is no reference constant to choose — the unit falls out of the measurement.

    Seconds **held**, not seconds busy: a Spawn buys exclusive access to a card, so squatting on one
    idle is expensive and finishing early is cheap.
    """
    return seconds * gpus * power * demand / 3600.0


def hold_for(minutes: int, gpus: int, elig: list[dict], hours: list[int] | None = None,
             tides: dict[str, dict[int, float]] | None = None) -> float:
    """The Escrow quoted at submit: what this Spawn would cost on the priciest Arm it could land on,
    at the Demand its hours usually see.

    **Max**, not mean, over the eligible set — and that is not merely the conservative choice, it is the
    *modal* one: node `Weight ∝ 1/Power`, so Slurm actively prefers the fastest free eligible card
    (confirmed on hardware 2026-07-14: with both cards idle, a `--vram 8` Spawn took the 4090). Quoting
    from the cheapest card would under-hold the common case, and under-holding is the gameable direction.

    **Mean**, not max, over the *hours* the Spawn would span — otherwise a 24h Spawn touches every hour
    and would be quoted at the daily peak purely for being long.

    Over-holding costs a User nothing but queue position — it is refunded in full at Settlement, since
    what actually moves is `accrued`, never this number.
    """
    hours = hours or [0]
    tides = tides or {}
    forecasts = [a["power"] * sum(tide(tides, a["name"], h) for h in hours) / len(hours)
                 for a in elig if a["power"]]
    return (minutes / 60.0) * gpus * max(forecasts, default=0.0)


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


def ledger(conn: sqlite3.Connection, user_id: int, days: int = 30, points: int = 120,
           now: datetime | None = None) -> dict:
    """One User's economic history: the **real rows**, and the Balance curve they imply.

    The rows are events that actually happened — a Settlement, an Override. **There is no Decay row, and
    there never can be**: Decay is not a transaction, it is `exp(-Δt/τ)` applied continuously between the
    rows. It is visible as the *sag of the curve*, and that is the only honest way to draw it. (A
    `kind: "decay"` row would also imply a stored Balance to decay, which is exactly the table that does
    not exist — see the module docstring.)

    The curve costs no storage: `B(t) = Σ over rows with tᵢ ≤ t of ±amountᵢ · exp(-(t - tᵢ)/τ)` is a pure
    function of the ledger, sampled at `points` instants.
    """
    now = now or datetime.now(timezone.utc)
    rows = db.ledger_rows(conn, user_id)

    # Signed from THIS User's point of view: money in is +, money out is -. A self-transfer (an owner on
    # their own Arm) is both, and nets to zero — free, as designed.
    events = []
    out = []
    for r in rows:
        signed = (r["amount"] if r["to_user"] == user_id else 0.0) \
               - (r["amount"] if r["from_user"] == user_id else 0.0)
        events.append((_parse(r["at"]), signed))
        out.append({
            "id": r["id"], "at": r["at"], "kind": r["kind"], "amount": signed,
            "job_id": r["job_id"], "note": r["note"],
            # The counterparty, if there is one. A NULL is not a missing name — it is the *point*:
            # Karma with no source was conjured (an Override), with no destination was destroyed (a
            # commons burn, on an Arm the Admin owns).
            "counterparty": (r["from_name"] if r["to_user"] == user_id else r["to_name"]),
        })

    start = now - timedelta(days=days)
    curve = []
    for k in range(points):
        t = start + (now - start) * (k / (points - 1))
        curve.append({
            "at": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "balance": sum(a * decay((t - ts).total_seconds() / 86400) for ts, a in events if ts <= t),
        })
    return {"rows": out, "curve": curve}


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
    # Admins are not economic actors: a role for RUNNING the cluster, not using it. They earn no Karma
    # (an Arm they own is a Commons Arm and its Karma is destroyed), pay none, and hold no Balance — so
    # they have no standing to appear in. This is also what keeps `Σ balances ≡ 0` true: an Admin never
    # reaches the ledger at all, which is why an Override aimed at one is refused below.
    rows = conn.execute("SELECT id, username FROM users WHERE is_admin = 0").fetchall()
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
    user = conn.execute("SELECT id, is_admin FROM users WHERE username=?", (username,)).fetchone()
    if user is None:
        raise KeyError(username)
    if user["is_admin"]:
        # Not tidiness — the invariant. An Admin is excluded from `standings()`, so Karma conjured into
        # one would still count in `balances()` while contributing to no standing, and Σ balances ≡ 0
        # would quietly stop holding. Admins never appear in the ledger, and that is what keeps it true.
        raise ValueError(f"{username} is an Admin — the Admin is outside the economy and holds no "
                         f"Balance to override")
    to_user = user["id"] if amount > 0 else None      # NULL from = conjured
    from_user = user["id"] if amount < 0 else None    # NULL to   = destroyed
    db.record_karma(conn, "override", abs(amount), from_user, to_user, note=note)
    return balances(conn).get(user["id"], 0.0)
