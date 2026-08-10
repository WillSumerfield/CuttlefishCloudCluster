"""The Karma economy — Balance, Decay, Share, and the nice a Share implies.

**Karma ranks Users; it never gates them.** Nothing here can refuse, hold, or kill a Spawn. The entire
output of this module is one integer per queued Spawn — its Slurm `nice` — so a User in poor standing
loses *contention*, and loses nothing at all when nobody is competing.

The arithmetic, end to end:

    Balance(u) = Σ over the ledger of ±amount · decay(Δt)         see KARMA_TAU_DAYS for the curve
    Share(u)   = σ((Balance - Escrow) / KARMA_SHARE_SCALE)        σ = logistic, so Share ∈ (0, 1)
    nice(u)    = round((1 - Share) · NICE_MAX)                    nice ∈ [0, NICE_MAX]

**A Balance is derived, never stored.** It is summed from each row's own age every time it is asked
for. A `karma_balance` column decayed on touch would not merely be a cache that could drift — it would
be *wrong*: `decay` is a stretched exponential and is not memoryless, so decaying a running total is
not the same arithmetic as decaying each contribution. See db._SCHEMA_V3.

**Escrow suppresses Share immediately**, which is what makes queue-flooding self-punishing: 500 queued
Spawns move no Balance until they finish, so settle-at-end alone would let a burst hold the top of the
queue for hours. The hold lands the moment the Spawn is queued.

**KARMA_SHARE_SCALE cannot reorder anyone.** σ is monotonic, so for *any* scale the User order by nice
is the User order by Balance. What it tunes is **resolution** — how hard Karma competes with age. Too
small and everyone pins to the rails and age alone decides; too large and nobody separates.
"""

import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import db, slurm

# Decay: a Balance drifts back to 0 within a month. Debt forgiven, hoards evaporate, standing reflects
# recent conduct not history. Also makes the economy self-healing — Karma conjured by an Override or
# destroyed on a commons Arm is simply reabsorbed, so Σ balances ≡ 0 is an attractor.
#
# A stretched exponential, not a plain one: `exp(-(age/scale)**SHARPNESS)`, scale = TAU/ONSET = 20 days.
# The shape is a plateau then a cliff — 0.98 at 5d, 0.86 at 10d, 0.37 at 20d, 0.05 at 30d — where a
# plain exp(-age/30) is already down to 0.85 by day 5 and still worth 0.37 at day 30. Two things follow.
# Recent conduct keeps close to its full weight instead of bleeding from the first day, which is what
# "recent" ought to mean; and by KARMA_TAU_DAYS a row is worth ~5%, which is what makes it safe to
# DELETE at that age (db.prune_ledger). A row pruned under the old curve was still worth 37%, so the
# table could not be bounded without visibly moving every Balance.
KARMA_TAU_DAYS = 30.0
DECAY_ONSET = 1.5       # how much earlier than τ the cliff begins: scale = τ/onset
DECAY_SHARPNESS = 2.7   # how abrupt it is. 1.0 would be the plain exponential
# Rows past this are deleted, not just decayed. Equal to τ deliberately — that is where the curve above
# has flattened to ~5%, so the truncation is a rounding error rather than a policy. Must stay ≥ the
# Dashboard's 30-day `ledger`/`usage` windows, which read these rows to draw their graphs.
KARMA_LEDGER_RETENTION_DAYS = 30

# Karma a User must be up/down to sit at Share ≈0.73/0.27. 1 Karma = 1 TFLOP-hour, so 10k ≈ 8 days of
# one 4090: taking a quarter of the cluster for a month while donating nothing lands near Share 0.19.
# Re-tune as the fleet/spread changes — a wrong scale costs resolution, never correctness.
KARMA_SHARE_SCALE = 10_000.0

# The nice range Karma spends. MUST equal cuttle.config.NICE_MAX — the CLI writes PriorityWeightAge from
# it, and the two ranges being equal is what makes poor standing cost a Spawn at most one day of apparent
# queue age. This package never imports the CLI, so the two are pinned together by a test on the seam.
NICE_MAX = 2**16

TICK_SECONDS = 30

# Ceiling on Demand. Demand is set by whoever is *queuing* but paid by whoever is *running* — without a
# cap, queuing 500 Spawns then cancelling inflates everyone's bill for free (Escrow released, nothing
# settled). Also, Balance drives nice until the decay curve reaches the floor at ~30 days, so an
# unbounded spike could bench a User for a month — a **gate**, which this model must never have. Past ~8x the signal is saturated anyway (the
# card is plainly scarce); this just bounds the runaway billing consequence.
DEMAND_MAX = 8.0

# The Tide's window, and how long a sample is worth keeping.
DEMAND_RETENTION_DAYS = 30


def tides(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, dict[int, float]]:
    """The **Tide**: each Arm's mean Demand by hour of day, over the last DEMAND_RETENTION_DAYS.

    24 buckets per Arm, not hour-of-week (168) — the ~4wk window would leave each hour-of-week bucket
    only ~4 observations; 24 buckets get ~30 days × 120 samples/hour.

    Known tradeoff: hour-of-day blends a dead Sunday into a busy Tuesday. Fix if it bites: `hour ×
    is_weekend` (48 buckets, still ~960 obs in the thinnest) — one `GROUP BY` away.
    """
    t: dict[str, dict[int, float]] = defaultdict(dict)
    for r in db.demand_by_hour(conn, DEMAND_RETENTION_DAYS, now):
        t[r["node"]][r["hour"]] = r["demand"]
    return t


def tide(tides: dict[str, dict[int, float]], node: str, hour: int) -> float:
    """One Arm's forecast Demand at one hour. An unseen Arm or empty bucket forecasts **1.0** — the
    honest cold-start value (no history means no basis but "ordinary", and 1.0 is what a card running
    exactly one Spawn reads). With a flat Tide, `hold_for` reduces exactly to `minutes/60 × gpus ×
    max(Power over eligible)`.
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
    submit, so an Arm admitted after this Spawn queued joins its eligible set (and a drained one drops
    out).

    An Admin-pinned Spawn (`--node`) is eligible for exactly one Arm, concentrating all of its Demand
    there — the honest price of naming a machine instead of declaring a need.

    Both declared needs are applied **together**, exactly as `slurm.vram_constraint` applies them at
    submit: enough Devices on the one Arm, each holding the per-card share of the total `--vram`. Were
    the count left out, Demand — and therefore price — would land on Arms too small to ever host the
    Spawn.
    """
    if need["node"]:
        return [a for a in arms if a["name"] == need["node"]]
    gpus = max(1, need["gpus"] or 1)
    per_card = -(-(need["vram_gb"] or 0) // gpus)  # ceil; --vram is the total across the Devices
    # `slurm.card_gb`, not a rounding of its own: this decides which Arms a queued Spawn adds Demand
    # to, and Slurm decides which it may actually land on. Two different answers to "is this card big
    # enough" would price a Spawn against Arms it can never reach, and vice versa.
    return [a for a in arms if _up(a) and a["vram_mb"] and (a["gpus"] or 0) >= gpus
            and slurm.card_gb(a["vram_mb"]) >= per_card]


def demand_by_arm(arms: list[dict], jobs: list[dict], needs: dict) -> dict[str, float]:
    """How badly each Arm is wanted, right now: device-claims on it over the devices it has — a
    **per-card** figure, comparable across Arms with different card counts (`cost` relies on this:
    Demand multiplies a Spawn's Power sum once, never per Device held).

    A **running** Spawn claims the card it holds, so a single-card Arm running anything reads ≥1.0 — a
    second Spawn is never sold below its Power. On a multi-card Arm the same claim only pushes the
    *average* toward 1.0. A **queued** Spawn spreads one claim evenly across every Arm it's eligible
    for, so its total contribution to the fleet is 1.0 wherever it lands — scarce big-VRAM cards
    accumulate the most. An idle Arm nobody wants reads 0, charged to nobody.

    A queued Spawn with no `spawns` row (the enrollment benchmark) adds no queued Demand — we can't know
    what it's eligible for. Pinned to one node and short-lived, so the undercount is negligible.
    """
    d = {a["name"]: 0.0 for a in arms}
    devices = {a["name"]: len(a["powers"]) if a["powers"] else 1 for a in arms}
    for j in jobs:
        if j["state"] == "RUNNING" and j["node"] in d:
            d[j["node"]] += j["gpus"]
    for j in jobs:
        if j["state"] != "PENDING":
            continue
        need = needs.get(j["id"])
        if need is None:  # unattributed (the enrollment benchmark) — no declared need to spread
            continue
        elig = eligible(arms, need)
        for a in elig:
            d[a["name"]] += need["gpus"] / len(elig)
    return {name: min(DEMAND_MAX, v / devices[name]) for name, v in d.items()}


def cost(seconds: float, powers: list[float], demand: float) -> float:
    """What a slice of held device-time is worth. **1 Karma = 1 TFLOP-hour**; Power is already TFLOP/s,
    so no reference constant is needed — the unit falls out of the measurement.

    Seconds **held**, not busy: a Spawn buys exclusive access to a card, so idling is expensive and
    finishing early is cheap.

    `powers` is one entry per Device **held**, summed (not averaged) — several Devices are several
    separate purchases, so a Spawn on a slow card costs less than one on a fast card on the same node.
    Empty only for a Spawn Slurm hasn't placed yet; every Spawn holds at least one Device.

    Demand is per-card and shared by the whole node, so it multiplies the sum once rather than per
    Device — how a node is divided can never move its scarcity.
    """
    return seconds * sum(powers) * demand / 3600.0


def hold_for(minutes: int, gpus: int, elig: list[dict], hours: list[int] | None = None,
             tides: dict[str, dict[int, float]] | None = None) -> float:
    """The Escrow quoted at submit: what this Spawn would cost on the priciest Arm it could land on, at
    the Demand its hours usually see.

    **Max**, not mean, over the eligible set: node `Weight ∝ 1/Power`, so Slurm actively prefers the
    fastest free eligible card — quoting from the cheapest card would under-hold the common case, and
    under-holding is the gameable direction.

    **Mean**, not max, over the *hours* the Spawn would span — otherwise a 24h Spawn touches every hour
    and gets quoted at the daily peak purely for being long. Over-holding costs a User nothing but queue
    position; it's refunded in full at Settlement, since what actually moves is `accrued`, never this
    number.

    **Max over an Arm's own Devices too**, same reasoning: on a two-card box the Spawn is more likely to
    land on the faster card, so the hold is a floor that only ratchets up — the one reduction of a
    node's Powers that cannot underquote.
    """
    hours = hours or [0]
    tides = tides or {}
    forecasts = [max(a["powers"]) * sum(tide(tides, a["name"], h) for h in hours) / len(hours)
                 for a in elig if a["powers"]]
    return (minutes / 60.0) * gpus * max(forecasts, default=0.0)


def span_hours(minutes: int, start_hour: int) -> list[int]:
    """The hours-of-day a Spawn starting at `start_hour` and running `minutes` would touch, capped at 24
    (a Spawn longer than a day already spans every bucket, so the Tide's mean is the whole day)."""
    return [(start_hour + h) % 24 for h in range(min(24, minutes // 60 + 1))]


def quote_hold(conn: sqlite3.Connection, arms: list[dict], need: dict, minutes: int, gpus: int,
               is_admin: bool, now: datetime | None = None) -> float | None:
    """The Escrow to place on a Spawn at submit — the pricing decision, in one place.

    Extracted so `/submit` and the Fake Cluster quote a Spawn through the SAME code — a quote re-derived
    independently in the simulation harness could hide a pricing bug that runs in production.

    Returns **None** for an Admin: outside the economy, never held. Otherwise the hold over the eligible
    set, at the Tide of the hours the Spawn would span.
    """
    if is_admin:
        return None
    now = now or datetime.now(timezone.utc)
    elig = eligible(arms, need)
    return hold_for(minutes, gpus, elig, hours=span_hours(minutes, now.hour), tides=tides(conn, now))


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def decay(age_days: float) -> float:
    """Weight of a ledger row `age_days` old. See KARMA_TAU_DAYS for the shape and why.

    NOT memoryless: `decay(a)·decay(b) ≠ decay(a+b)`, which only a plain exponential satisfies. So a
    Balance must be summed from each row's absolute age — as `balances` and `ledger` both do — and can
    never be maintained as a running total decayed on touch.

    A future-dated row weighs 1.0, not more. Clamped rather than left to the arithmetic: a fractional
    power of a negative base is complex and would raise, and the plain exponential this replaced did
    something quieter and worse — a row a week ahead of the clock was weighted e^(7/30)·… and one a
    year ahead by e^12, so clock skew, a restored backup or a Head whose time jumps could inflate a
    Balance by orders of magnitude with nothing to show for it."""
    return math.exp(-((max(0.0, age_days) / (KARMA_TAU_DAYS / DECAY_ONSET)) ** DECAY_SHARPNESS))


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
    """One User's economic history: the real rows, and the Balance curve they imply.

    The rows are events that happened — a Settlement, an Override. **There is no Decay row, and never
    can be**: Decay isn't a transaction, it's `exp(-Δt/τ)` applied continuously between rows, visible
    only as the *sag of the curve*. (A `kind: "decay"` row would imply a stored Balance to decay —
    exactly the table that doesn't exist.)

    The curve costs no storage: `B(t) = Σ over rows with tᵢ ≤ t of ±amountᵢ · exp(-(t-tᵢ)/τ)`, sampled
    at `points` instants.
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


def usage(conn: sqlite3.Connection, days: int = 30, now: datetime | None = None) -> list[dict]:
    """Who's been consuming the cluster: one daily spend series per User, heaviest first.

    Counterpart to `demand_series` — that one is about *cards*, this one about *people*, over the same
    window: a month where one User's band swallows the plot explains a month of high Demand better than
    the Demand chart alone.

    Everyone's, like standings and the queue, for the same reason: usage is only legible in proportion.

    A User who spent nothing in the window is omitted, not sent as a row of zeroes — absent, not a thin
    band at the bottom. `series` is `days` UTC-day totals, oldest first, same length/index for every
    User.
    """
    now = now or datetime.now(timezone.utc)
    # Day keys, oldest first — the x-axis, built here so a day nobody spent on is a 0 and not a gap.
    start = (now - timedelta(days=days - 1)).date()
    keys = [(start + timedelta(days=k)).strftime("%Y-%m-%d") for k in range(days)]
    index = {d: k for k, d in enumerate(keys)}

    spend: dict[int, list[float]] = defaultdict(lambda: [0.0] * days)
    for r in db.usage_series(conn, keys[0]):
        k = index.get(r["day"])
        if k is not None:
            spend[r["user_id"]][k] = round(r["spent"], 2)

    bal = balances(conn, now)
    names = {u["id"]: u["username"] for u in conn.execute("SELECT id, username FROM users").fetchall()}
    out = [{"user_id": uid, "username": names.get(uid), "balance": round(bal.get(uid, 0.0), 2),
            "total": round(sum(series), 2), "series": series}
           for uid, series in spend.items() if names.get(uid) is not None]
    return sorted(out, key=lambda u: -u["total"])


def share(balance: float, escrow: float = 0.0) -> float:
    """The effect a Balance actually has, bounded in [0, 1]. Saturates on purpose: hoarding has
    diminishing returns, debt has diminishing punishment, neither end runs away.

    Two-branch logistic, not `1/(1+exp(-x))` — that form OVERFLOWS at extreme Balance (e.g. -1e9, one
    Override typo away): `exp(-x)` raises OverflowError instead of saturating, inside the tick, every
    pass. Both branches keep the exponent negative, so extremes underflow harmlessly to Share 0.0 or
    1.0 — legal values meaning nice NICE_MAX and nice 0, both ordinary to Slurm.
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


def standings(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Every User's standing, best first — the payload of `GET /karma` and the tick's input alike.

    Public to any authed caller: a Balance is meaningless in isolation, only legible against everyone
    else's.

    `now` decays every Balance to that instant; None is the wall clock. The Fake Cluster passes
    simulated time so the queue it ranks is the queue the economy would rank at that moment.
    """
    bal = balances(conn, now)
    esc = db.escrow_totals(conn)
    # The claims behind each total, so a User reading "−140 escrow" sees which Spawns hold it — public
    # on the same terms as the queue: what's running and what it costs is not private here.
    esc_rows = db.escrow_breakdown(conn)
    # Admins are not economic actors — a role for RUNNING the cluster, not using it. They earn no Karma
    # (an Arm they own is a Commons Arm whose Karma is destroyed), pay none, hold no Balance, and never
    # appear in standings. Also what keeps Σ balances ≡ 0 true — an Admin never reaches the ledger,
    # which is why an Override aimed at one is refused below.
    rows = conn.execute("SELECT id, username FROM users WHERE is_admin = 0").fetchall()
    out = []
    for r in rows:
        b, e = bal.get(r["id"], 0.0), esc.get(r["id"], 0.0)
        s = share(b, e)
        out.append({"user_id": r["id"], "username": r["username"], "balance": round(b, 2),
                    "escrow": round(e, 2), "share": round(s, 4), "nice": nice_for(s),
                    "escrow_spawns": [
                        {"job_id": x["job_id"], "script": x["script"], "node": x["node"],
                         "amount": round(x["amount"], 2)}
                        for x in esc_rows.get(r["id"], [])
                    ]})
    return sorted(out, key=lambda u: -u["share"])


def nice_by_user(conn: sqlite3.Connection, now: datetime | None = None) -> dict[int, int]:
    return {u["user_id"]: u["nice"] for u in standings(conn, now)}


def override(conn: sqlite3.Connection, username: str, amount: float, note: str | None = None) -> float:
    """Admin add/remove of a User's Balance — the only flow that is not a Transfer. Conjures Karma (or
    destroys it); Decay absorbs the imbalance, so it needs no counterparty. Returns the User's new
    Balance.

    Distinct from a **Queue bump**, which moves one Spawn once and touches no Balance — an Override
    bought to win one race would go on winning every *other* race for a month.
    """
    user = conn.execute("SELECT id, is_admin FROM users WHERE username=?", (username,)).fetchone()
    if user is None:
        raise KeyError(username)
    if user["is_admin"]:
        # Not tidiness — the invariant. An Admin excluded from standings() means Karma conjured into one
        # would still count in balances() while contributing to no standing, breaking Σ balances ≡ 0.
        # Admins never appear in the ledger, and that's what keeps it true.
        raise ValueError(f"{username} is an Admin — the Admin is outside the economy and holds no "
                         f"Balance to override")
    to_user = user["id"] if amount > 0 else None      # NULL from = conjured
    from_user = user["id"] if amount < 0 else None    # NULL to   = destroyed
    db.record_karma(conn, "override", abs(amount), from_user, to_user, note=note)
    return balances(conn).get(user["id"], 0.0)
