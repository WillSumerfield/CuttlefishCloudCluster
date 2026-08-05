"""The ledger and the Balance it implies, and the meter that feeds it.

What's tested is the arithmetic nothing else can check: every failure here is a **remote
consequence** — a standing that's quietly wrong, a nice computed perfectly and pushed onto a job
it should never have touched, or a bill wrong by a factor nobody will ever notice.

- The ledger's Σ ≡ 0, Decay's shape and its NON-memorylessness (which is *why* no `karma_balance`
  table exists), Share's monotonicity and saturation, and which jobs the tick refuses to touch.
- Demand (the number every bill multiplies by), the hold and its ratchet, and Settlement: a
  Transfer that sums to zero, a Commons burn that doesn't, and self-use that costs nothing.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import typer

from ccc_head_api import db, karma, tick
from cuttle import config as cli_config
from cuttle import karma as cli_karma


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A real head.db, schema and all — the ledger is the thing under test, so it isn't faked."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    c = db.connect()
    db.init_schema(c)
    for name in ("alice", "bob", "carol"):
        db.add_user(c, name)
    return c


def uid(conn: sqlite3.Connection, name: str) -> int:
    return db.get_user(conn, name)["id"]


def at(conn: sqlite3.Connection, days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ts(seconds_from_now: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestLedger:
    def test_a_transfer_leaves_the_sum_at_zero(self, conn):
        """The invariant the whole economy rests on. Every ordinary flow is a Transfer — the submitter
        is debited and the Arm's owner credited the SAME Karma — so Σ balances ≡ 0, always. There is no
        supply to inflate and no pot to run dry, which is what lets a new User join at 0 (exactly
        average) diluting nobody."""
        db.record_karma(conn, "transfer", 250.0, uid(conn, "alice"), uid(conn, "bob"), job_id=7)
        db.record_karma(conn, "transfer", 40.0, uid(conn, "bob"), uid(conn, "carol"), job_id=8)
        assert sum(karma.balances(conn).values()) == pytest.approx(0.0)

    def test_a_commons_burn_destroys_karma_and_an_override_conjures_it(self, conn):
        """The two flows that are NOT transfers, and the only two that may move Σ off zero. A Spawn on
        an admin-owned Arm has no owner to pay, so its cost is burned (NULL `to`); an Override credit
        comes from nowhere (NULL `from`). Neither is a leak: Decay reabsorbs the drift, which is why
        Σ≡0 is an attractor here rather than something anyone has to police."""
        db.record_karma(conn, "burn", 100.0, uid(conn, "alice"), None, job_id=9)
        assert sum(karma.balances(conn).values()) == pytest.approx(-100.0)
        db.record_karma(conn, "override", 100.0, None, uid(conn, "alice"))
        assert sum(karma.balances(conn).values()) == pytest.approx(0.0)

    def test_decay_pulls_a_balance_back_toward_zero(self, conn):
        """Debt is forgiven and hoards evaporate, so standing reflects *recent* conduct. At τ a debt is
        worth ~5% of its face value — which is also what makes it safe to DELETE the row there
        (db.prune_ledger)."""
        conn.execute("INSERT INTO karma_ledger (at, kind, amount, from_user, to_user) VALUES (?,?,?,?,?)",
                     (at(conn, karma.KARMA_TAU_DAYS), "transfer", 1000.0, uid(conn, "alice"),
                      uid(conn, "bob")))
        conn.commit()
        assert karma.balances(conn)[uid(conn, "bob")] == pytest.approx(50.3, rel=0.02)

    def test_the_curve_is_a_plateau_then_a_cliff(self):
        """The shape is the point, and a plain exponential does not have it: recent conduct keeps
        nearly all its weight for a fortnight, then falls away fast enough that a month-old event is
        numerically dead. Under exp(-age/30) a 5-day-old event would already be down to 0.85 and a
        30-day-old one still worth 0.37."""
        assert karma.decay(0) == pytest.approx(1.0)
        assert karma.decay(5) == pytest.approx(0.977, rel=0.01)
        assert karma.decay(10) == pytest.approx(0.857, rel=0.01)
        assert karma.decay(20) == pytest.approx(0.368, rel=0.01)
        assert karma.decay(30) == pytest.approx(0.050, rel=0.02)
        ages = [0, 5, 10, 20, 30, 45]
        assert all(karma.decay(a) > karma.decay(b) for a, b in zip(ages, ages[1:]))

    def test_a_future_dated_row_weighs_one_rather_than_more(self):
        """Clock skew, a restored backup, a Head whose time jumps. The plain exponential this
        replaced weighted a year-ahead row by e^12; here it is simply worth its face value."""
        assert karma.decay(-1) == 1.0
        assert karma.decay(-365) == 1.0

    def test_a_balance_is_summed_from_absolute_ages_not_decayed_on_touch(self, conn):
        """Why there is no `karma_balance` table — and now a stronger reason than caching.

        `decay` is a stretched exponential, so `decay(a)·decay(b) ≠ decay(a+b)`: decaying a running
        total on every touch is not the same arithmetic as decaying each contribution by its own age.
        A stored balance would not merely drift from the ledger, it would be *wrong*, and a User's
        standing being quietly wrong forever is the worst failure this economy has.

        Three events at different ages: the real code must match the sum over absolute ages, and must
        NOT match the decay-on-touch recursion that a cache would have to use.
        """
        alice = uid(conn, "alice")
        events = [(30.0, 500.0), (10.0, -200.0), (2.0, 75.0)]  # (days ago, delta) — oldest first
        for days, delta in events:
            conn.execute(
                "INSERT INTO karma_ledger (at, kind, amount, from_user, to_user) VALUES (?,?,?,?,?)",
                (at(conn, days), "override", abs(delta),
                 None if delta > 0 else alice, alice if delta > 0 else None))
        conn.commit()

        summed = sum(delta * karma.decay(days) for days, delta in events)
        assert karma.balances(conn)[alice] == pytest.approx(summed, rel=1e-6)

        rolling, last = 0.0, events[0][0]
        for days, delta in events:                      # the recursion a cache would maintain
            rolling *= karma.decay(last - days)
            rolling += delta
            last = days
        rolling *= karma.decay(last)
        assert rolling != pytest.approx(summed, rel=1e-3), "the curve is memoryless after all"

    def test_events_past_the_curves_floor_are_deleted(self, conn):
        """The ledger is the one table here that is pruned. `spawns` and `users` are the permanent
        accounting record; a Karma row that no longer moves any Balance is not evidence of anything.
        Safe only because of the curve: at this age a row is worth ~5%, so what the prune discards is
        a rounding error rather than someone's standing."""
        alice = uid(conn, "alice")
        for days in (45.0, 31.0, 29.0, 1.0):
            conn.execute(
                "INSERT INTO karma_ledger (at, kind, amount, from_user, to_user) VALUES (?,?,?,?,?)",
                (at(conn, days), "override", 100.0, None, alice))
        conn.commit()
        assert db.prune_ledger(conn, karma.KARMA_LEDGER_RETENTION_DAYS) == 2
        remaining = conn.execute("SELECT COUNT(*) FROM karma_ledger").fetchone()[0]
        assert remaining == 2


class TestShare:
    def test_zero_balance_is_the_middle(self, conn):
        """Everyone starts at 0, and 0 must be neither an advantage nor a punishment."""
        assert karma.share(0.0) == pytest.approx(0.5)
        assert karma.nice_for(karma.share(0.0)) == karma.NICE_MAX // 2

    def test_share_is_monotonic_in_balance(self):
        """More Karma is never worse. This is also why KARMA_SHARE_SCALE cannot reorder anyone — σ is
        monotonic, so the User order by nice is the User order by Balance for ANY scale. The scale buys
        resolution (how hard Karma competes with age), never correctness."""
        for scale in (100.0, 10_000.0, 1e6):
            karma.KARMA_SHARE_SCALE = scale
            shares = [karma.share(b) for b in (-5e4, -1e3, 0.0, 1e3, 5e4)]
            assert shares == sorted(shares)
        karma.KARMA_SHARE_SCALE = 10_000.0

    def test_saturating_means_neither_end_runs_away(self):
        """Hoarding has diminishing returns; debt has diminishing punishment. A User a *million* Karma
        up is not meaningfully ahead of one 100k up — no Balance buys an unassailable position — and a
        User deep in debt is never pushed past the back of the queue."""
        assert karma.share(1e6) - karma.share(1e5) < 0.01
        assert karma.nice_for(karma.share(-1e6)) == karma.NICE_MAX  # the back of the queue, not past it

    def test_an_extreme_balance_saturates_instead_of_exploding(self):
        """The naive logistic (`1/(1+exp(-x))`) does not saturate at the bottom — it raises
        OverflowError. A single Override typo (-1e9) would then crash the tick on every pass, forever,
        and the whole queue would silently freeze at whatever nice it last held."""
        for balance in (-1e12, -1e9, 1e9, 1e12):
            assert 0.0 <= karma.share(balance) <= 1.0

    def test_escrow_suppresses_share_immediately(self, conn):
        """The anti-flooding property, and the reason Escrow exists at all. Settle-at-end alone is
        gameable: 500 queued Spawns move no Balance until they finish, so a burst would hold the top of
        the queue for hours. The hold lands when the Spawn QUEUES, so flooding instantly costs the
        flooder their own queue position."""
        assert karma.share(5000.0, escrow=20_000.0) < karma.share(5000.0, escrow=0.0)

    def test_nice_stays_inside_the_range_slurm_will_take(self):
        """nice < 0 is a Queue bump — operator-only, and the tick's flag for 'hands off'. The economy
        must never emit one by accident, at any Balance, however extreme."""
        for balance in (-1e9, -1e4, 0.0, 1e4, 1e9):
            assert 0 <= karma.nice_for(karma.share(balance)) <= karma.NICE_MAX


class TestSeamWithTheScheduler:
    def test_the_two_packages_agree_on_the_nice_range(self):
        """A **seam**: the CLI writes PriorityWeightAge into slurm.conf from *its* NICE_MAX, and the
        Head API pushes a nice computed from *its own*. Neither package imports the other (they are
        separate distributions with separate venvs), so nothing but this test stops them drifting —
        and if the API's range grew past the CLI's age weight, the worst-standing User would stop being
        able to age to the front of the queue. Starvation, from a constant edited on one side."""
        assert karma.NICE_MAX == cli_config.NICE_MAX == cli_config.PRIORITY_WEIGHT_AGE

    def test_best_standing_is_slurms_default_nice(self):
        """nice 0 must mean 'best standing', because 0 is what Slurm gives a job we have never touched.
        Were it the other way round, an unattributed Spawn would arrive at the WORST standing — and the
        enrollment benchmark, which the CLI sbatches outside the API, would sink to the back of the
        queue behind everyone's backlog."""
        assert karma.nice_for(karma.share(1e9)) == 0


class TestOverride:
    def test_override_moves_the_balance_both_ways(self, conn):
        assert karma.override(conn, "alice", 5000.0) == pytest.approx(5000.0, rel=1e-3)
        assert karma.override(conn, "alice", -8000.0) == pytest.approx(-3000.0, rel=1e-3)

    def test_override_is_felt_in_the_queue(self, conn):
        """An Override changes *standing*, so it is felt in every future contention — that is the whole
        difference from a Queue bump, which moves one Spawn once and touches no Balance."""
        before = karma.nice_by_user(conn)[uid(conn, "alice")]
        karma.override(conn, "alice", 50_000.0)
        assert karma.nice_by_user(conn)[uid(conn, "alice")] < before

    def test_unknown_user_is_refused(self, conn):
        with pytest.raises(KeyError):
            karma.override(conn, "nobody", 1.0)


class TestTheTick:
    """The loop's *decisions*, with Slurm faked out. What it chooses to touch is the whole risk here:
    every wrong choice is silent, and lands on someone else's job."""

    @pytest.fixture
    def cluster(self, conn, monkeypatch):
        """A queue, and a record of every nice we push back into it."""
        pushed = {}
        monkeypatch.setattr(db, "connect", lambda: conn)
        monkeypatch.setattr(tick.slurm, "arms", lambda: [])
        monkeypatch.setattr(tick.slurm, "set_nice",
                            lambda job, nice, owner=None: pushed.__setitem__(job, nice))
        return pushed

    def queue(self, monkeypatch, jobs):
        monkeypatch.setattr(tick.slurm, "queue",
                            lambda: [{"state": "PENDING", "node": None, "gpus": 1,
                                      "start_time": None, **j} for j in jobs])

    def test_a_spawn_is_niced_by_its_submitters_standing(self, conn, cluster, monkeypatch):
        db.record_spawn(conn, 101, uid(conn, "alice"), "train.py", "uuid-a")
        karma.override(conn, "alice", -50_000.0)  # deep in debt: should sink
        self.queue(monkeypatch, [{"id": 101, "nice": 0}])
        tick.once()
        assert cluster[101] == karma.nice_by_user(conn)[uid(conn, "alice")]
        assert cluster[101] > karma.NICE_MAX // 2

    def test_a_bumped_spawn_is_left_alone(self, conn, cluster, monkeypatch):
        """The bump flag, and why it needs no table. Slurm refuses a negative nice from a job's owner,
        and every Spawn is owned by the Mantle — so a negative nice is a thing ONLY an operator could
        have set. Skipping it is the entire coordination protocol between the admin and the economy.

        Without this, the very next tick would overwrite the admin's bump with the submitter's ordinary
        standing — and the bump would appear to work, for up to 30 seconds."""
        db.record_spawn(conn, 102, uid(conn, "alice"), "train.py", "uuid-a")
        karma.override(conn, "alice", -50_000.0)
        self.queue(monkeypatch, [{"id": 102, "nice": -cli_config.BUMP_STEP}])
        tick.once()
        assert 102 not in cluster

    def test_an_unattributed_spawn_is_left_alone(self, conn, cluster, monkeypatch):
        """The enrollment benchmark: the CLI sbatches it as the Mantle, outside the API, so it has no
        Attribution row and no submitter to charge. It keeps nice 0 and therefore outranks queued work
        — which is what we want (admitting an Arm should not wait behind a poor-standing User's
        backlog), and needs no special case: it simply isn't in the ledger."""
        self.queue(monkeypatch, [{"id": 103, "nice": 0}])
        assert tick.once() == (0, 0)
        assert cluster == {}

    def test_an_admins_spawn_is_left_alone(self, conn, cluster, monkeypatch):
        """The Admin is outside the economy — a role for running the cluster, not using it. They hold
        no Balance, so there is no standing to rank their Spawns by, and `spawn_owners` omits them
        exactly as it omits the enrollment benchmark. Same skip, no new branch.

        The consequence is deliberate and worth seeing in a test: an Admin's Spawn sits at nice 0 and
        therefore outranks every User's, for free. Doing research from the admin account would exempt
        that work from the economy entirely."""
        db.add_user(conn, "root", is_admin=True)
        db.record_spawn(conn, 105, uid(conn, "root"), "train.py", "uuid-r")
        self.queue(monkeypatch, [{"id": 105, "nice": 0}])
        assert tick.once() == (0, 0)
        assert cluster == {}

    def test_an_unchanged_nice_is_not_rewritten(self, conn, cluster, monkeypatch):
        """The tick runs every 30s forever. Pushing a value Slurm already holds would hammer slurmctld
        with writes that change nothing, for every queued Spawn in the cluster, for as long as it is
        queued."""
        db.record_spawn(conn, 104, uid(conn, "bob"), "train.py", "uuid-b")
        settled = karma.nice_by_user(conn)[uid(conn, "bob")]
        self.queue(monkeypatch, [{"id": 104, "nice": settled}])
        assert tick.once() == (0, 0)
        assert cluster == {}


class TestQueueBump:
    """`cuttle karma bump` — the operator's one escape hatch, and the one command that lies if its
    guard is wrong."""

    @pytest.fixture
    def slurm(self, monkeypatch):
        """Fake `squeue -o "%T %y"` and record every `scontrol update` the bump tries to make."""
        pushed = []
        monkeypatch.setattr(cli_karma, "run", lambda cmd, **kw: pushed.append(cmd))

        def squeue(out):
            monkeypatch.setattr(cli_karma.subprocess, "run",
                                lambda *a, **kw: SimpleNamespace(stdout=out, returncode=0))
        return squeue, pushed

    def test_bumping_a_running_spawn_is_refused(self, slurm, capsys):
        """`squeue` lists RUNNING jobs too, so a naive "did squeue return anything?" guard only
        catches a Spawn that's *gone*, despite its error saying otherwise.

        A bump on a running Spawn would then succeed, do nothing, and claim to have worked. nice
        feeds the priority that decides who runs *next*; a Spawn already holding a card is past
        that decision, and no value written can reach back and change it. Reporting success
        there is worse than failing — the operator moves on believing the queue was reordered.
        """
        squeue, pushed = slurm
        squeue("RUNNING 0\n")
        # A bump refusal is runtime queue state, not a usage error — so ui.abort (typer.Exit, message
        # printed), not typer.BadParameter's "Try --help" box. The reason must still reach the user.
        with pytest.raises(typer.Exit):
            cli_karma.bump(42)
        assert "running" in capsys.readouterr().err
        assert pushed == []  # and, crucially, nothing was written to Slurm

    def test_bumping_a_finished_spawn_is_refused(self, slurm, capsys):
        squeue, pushed = slurm
        squeue("")
        with pytest.raises(typer.Exit):
            cli_karma.bump(42)
        assert "not in the queue" in capsys.readouterr().err
        assert pushed == []

    def test_a_pending_spawn_takes_one_negative_step(self, slurm):
        """A bump is one step BELOW the Spawn's current nice — not a reset to 0. Slurm's nice 0 is
        merely *best standing*; only a negative nice outranks a perfectly-standing rival, and only an
        operator can set one, which is exactly what makes it the bump flag the tick reads."""
        squeue, pushed = slurm
        squeue(f"PENDING {karma.NICE_MAX}\n")  # worst possible standing
        cli_karma.bump(42)
        assert pushed == [["scontrol", "update", "JobId=42", f"Nice={-cli_config.BUMP_STEP}"]]

    def test_bumping_twice_shoves_it_further(self, slurm):
        """`min(0, current)` is what makes a second bump compound instead of undoing the first."""
        squeue, pushed = slurm
        squeue(f"PENDING {-cli_config.BUMP_STEP}\n")
        cli_karma.bump(42)
        assert pushed == [["scontrol", "update", "JobId=42", f"Nice={-2 * cli_config.BUMP_STEP}"]]

    def test_clear_is_guarded_too(self, slurm, capsys):
        """--clear on a running Spawn is just as much of a no-op — silently, without this guard."""
        squeue, pushed = slurm
        squeue("RUNNING -131072\n")
        with pytest.raises(typer.Exit):
            cli_karma.bump(42, clear=True)
        assert "running" in capsys.readouterr().err
        assert pushed == []

        squeue("PENDING -131072\n")
        cli_karma.bump(42, clear=True)
        assert pushed == [["scontrol", "update", "JobId=42", "Nice=0"]]


# --- the meter ----------------------------------------------------------

def arm(name, power=50.0, vram_mb=24564, owner="carol", state="idle", admitted=True, powers=None):
    """`power=` is the single-card shorthand; `powers=` gives a multi-Device Arm its own list."""
    return {"name": name, "powers": powers if powers is not None else ([power] if power else None),
            "vram_mb": vram_mb, "owner": owner, "state": state, "admitted": admitted}


def need(gpus=1, vram_gb=8, node=None):
    return {"gpus": gpus, "vram_gb": vram_gb, "node": node}


def job(jid, state="PENDING", node=None, gpus=1, nice=0, start_time=None, devices=None):
    """`devices` = the GRES indices Slurm reports a RUNNING Spawn holds (slurm._gpu_indices). Defaults
    to the first `gpus` cards, the ordinary case on a one-card Arm."""
    return {"id": jid, "state": state, "node": node, "gpus": gpus, "nice": nice,
            "start_time": start_time,
            "devices": devices if devices is not None else
            (list(range(gpus)) if state == "RUNNING" else [])}


class TestDemand:
    """How badly each Arm is wanted. Every Spawn's price is this number times its card's Power, so an
    error here is an error in every bill the cluster ever sends."""

    def test_the_plans_worked_example(self):
        """'A GPU used 1h with someone queued behind it the whole hour = 2.0.' The running Spawn claims
        the card (1.0) and the queued one, eligible for nothing else, claims it too (1.0)."""
        arms, jobs = [arm("a")], [job(1, "RUNNING", node="a"), job(2)]
        assert karma.demand_by_arm(arms, jobs, {2: need()})["a"] == 2.0

    def test_an_arm_running_anything_is_never_below_one(self):
        """The floor, and the invariant that makes the whole meter safe: a running Spawn counts ITSELF,
        so Demand on a busy card is ≥ 1.0 and **a second is never sold below its Power**. Were the
        running Spawn not to count itself, a Spawn alone on an idle cluster would read Demand 0 and run
        for free — and the cluster's quietest hours would be its most exploitable."""
        assert karma.demand_by_arm([arm("a")], [job(1, "RUNNING", node="a")], {})["a"] == 1.0

    def test_an_idle_unwanted_arm_reads_zero(self):
        assert karma.demand_by_arm([arm("a")], [], {})["a"] == 0.0

    def test_a_queued_spawn_spreads_one_claim_across_what_it_could_run_on(self):
        """One Spawn is one claim on the fleet, wherever it lands — so it adds 1/3 to each of the three
        Arms it is eligible for, not 1.0 to each. Otherwise a single queued Spawn would triple the
        price of the whole cluster."""
        arms = [arm("a"), arm("b"), arm("c")]
        d = karma.demand_by_arm(arms, [job(1)], {1: need()})
        assert d == pytest.approx({"a": 1 / 3, "b": 1 / 3, "c": 1 / 3})
        assert sum(d.values()) == pytest.approx(1.0)

    def test_scarce_cards_price_highest(self):
        """Eligibility is what makes the economy price scarcity rather than size. A big-VRAM card is
        wanted by the big jobs AND the small ones; a small card is wanted only by the small ones. So
        Demand lands hardest on exactly the card the cluster can least spare — with no rule saying so."""
        arms = [arm("small", vram_mb=8192), arm("big", vram_mb=81920)]
        d = karma.demand_by_arm(arms, [job(1), job(2)], {1: need(vram_gb=8), 2: need(vram_gb=80)})
        assert d["big"] > d["small"]

    def test_a_flood_is_capped(self):
        """The griefing bound. Demand is set by whoever is *queuing* and paid by whoever is *running*,
        so 500 queued Spawns would bill every running Spawn 500x — and the flooder, who need only
        cancel, would pay nothing at all. Since Balance drives nice for tau=30 days, that would let one
        User bench another for a month: a gate, in a model that has none."""
        jobs = [job(1, "RUNNING", node="a")] + [job(i) for i in range(2, 502)]
        needs = {i: need() for i in range(2, 502)}
        assert karma.demand_by_arm([arm("a")], jobs, needs)["a"] == karma.DEMAND_MAX

    def test_a_drained_arm_takes_no_demand_and_makes_the_rest_scarcer(self):
        """Slurm will not place work on a drained Arm, so no queued Spawn is eligible for it — and the
        claim it would have taken lands on the cards that remain. A machine going away makes the others
        more expensive, which is what scarcity means."""
        healthy = karma.demand_by_arm([arm("a"), arm("b")], [job(1)], {1: need()})
        drained = karma.demand_by_arm([arm("a"), arm("b", state="drain")], [job(1)], {1: need()})
        assert healthy["a"] == pytest.approx(0.5)
        assert drained["a"] == pytest.approx(1.0)   # b is gone; a absorbs the whole claim
        assert drained["b"] == 0.0

    def test_a_cpu_only_spawn_claims_no_device(self):
        """--gpu 0 is free, and falls out of the arithmetic rather than being special-cased."""
        assert karma.demand_by_arm([arm("a")], [job(1)], {1: need(gpus=0)})["a"] == 0.0

    def test_an_admin_pinned_spawn_concentrates_its_demand(self):
        """--node names a machine instead of declaring a need, so all of its Demand lands on that one
        Arm. The honest price of sidestepping eligibility."""
        d = karma.demand_by_arm([arm("a"), arm("b")], [job(1)], {1: need(node="b")})
        assert d == {"a": 0.0, "b": 1.0}

    def test_demand_is_per_card_not_per_arm(self):
        """Demand is 'the device-claims on it over the devices it has' — a per-card figure. One held
        card out of four reads 0.25, not 1.0: a busy 4-card box must not quadruple every bill it
        charges. A fully-busy 4-card box (all four running) reads exactly 1.0, same as a fully-busy
        single-card Arm — cost() multiplies Demand by a Spawn's own Power sum once, so this figure has
        to mean the same thing regardless of how many cards share it."""
        one_of_four = karma.demand_by_arm(
            [arm("a", powers=[50.0] * 4)], [job(1, "RUNNING", node="a", gpus=1)], {})
        assert one_of_four["a"] == pytest.approx(0.25)

        all_four = karma.demand_by_arm(
            [arm("a", powers=[50.0] * 4)], [job(1, "RUNNING", node="a", gpus=4)], {})
        assert all_four["a"] == pytest.approx(1.0)


class TestTheHold:
    def test_quoted_from_the_priciest_arm_it_could_land_on(self):
        """Max, not mean — and not merely to be safe. Node Weight ∝ 1/Power, so Slurm actively PREFERS
        the fastest free eligible card: a --vram 8 Spawn is eligible for the 4090 and will often land
        there. Quoting from the cheapest card would under-hold the common case, and under-holding is
        the direction a queue-flooder exploits."""
        elig = [arm("slow", power=10.0), arm("fast", power=50.0)]
        assert karma.hold_for(60, 1, elig) == pytest.approx(50.0)  # 1h x 50 TFLOP/s = 50 Karma

    def test_an_hour_on_a_4090_is_about_fifty_karma(self):
        """1 Karma = 1 TFLOP-hour. Nothing arbitrary: Power is already TFLOP/s, so the unit falls out of
        the measurement and no reference constant has to be chosen."""
        assert karma.hold_for(120, 1, [arm("a", power=50.0)]) == pytest.approx(100.0)

    def test_an_unpriced_fleet_holds_nothing(self):
        assert karma.hold_for(60, 1, [arm("a", power=None)]) == 0.0


class TestTheMeter:
    """Accrual and Settlement, with Slurm faked. The tick is the only thing that ever moves Karma."""

    @pytest.fixture
    def cluster(self, conn, monkeypatch):
        state = {"jobs": [], "arms": [arm("donor", owner="carol")]}
        monkeypatch.setattr(db, "connect", lambda: conn)
        monkeypatch.setattr(tick.slurm, "queue", lambda: state["jobs"])
        monkeypatch.setattr(tick.slurm, "arms", lambda: state["arms"])
        monkeypatch.setattr(tick.slurm, "set_nice", lambda job, nice, owner=None: None)
        return state

    def spawn(self, conn, jid, user, hold=100.0, **kw):
        db.record_spawn(conn, jid, uid(conn, user), "train.py", f"u{jid}",
                        minutes=60, gpus=1, vram_gb=8, hold=hold, **kw)

    def test_the_hold_suppresses_share_the_moment_a_spawn_queues(self, conn):
        """Why Escrow exists at all. A settle-at-end economy is trivially gamed: 500 queued Spawns move
        no Balance until they finish, so a burst holds the top of the queue for hours. The hold lands
        the instant the Spawn is queued, so flooding the queue instantly costs you your own position."""
        before = karma.nice_by_user(conn)[uid(conn, "alice")]
        self.spawn(conn, 201, "alice", hold=20_000.0)
        assert karma.nice_by_user(conn)[uid(conn, "alice")] > before  # worse standing, immediately

    def test_the_meter_ratchets_past_the_hold_but_never_below_it(self, conn):
        """MAX(hold, accrued) is the ratchet. The forecast is a FLOOR — so an under-quoted hold cannot
        be exploited — and the meter raises it if the real cost overtakes it, so a Demand spike is felt
        in the submitter's Share within 30s rather than as a surprise at Settlement. The claim can only
        ever go up, and a Spawn can never quietly cheapen its own submitter's standing while it runs."""
        u = uid(conn, "alice")
        self.spawn(conn, 202, "alice", hold=100.0)
        assert db.escrow_totals(conn)[u] == 100.0        # accrued 0 -> the hold is the floor

        db.accrue(conn, 202, "donor", 40.0, at(conn, 0))
        assert db.escrow_totals(conn)[u] == 100.0        # still under the hold: claim does not drop

        db.accrue(conn, 202, "donor", 250.0, at(conn, 0))
        assert db.escrow_totals(conn)[u] == 250.0        # overtook it: the claim follows the truth

    def test_accrual_bills_elapsed_time_not_ticks(self, conn, cluster):
        """If the karma service was down for an hour, the User still HELD the card for that hour and
        still owes for it. Billing per-tick instead of per-second would mean an outage silently makes
        the cluster free — and would make the tick interval a price."""
        self.spawn(conn, 203, "alice")
        started = datetime.now(timezone.utc) - timedelta(hours=2)
        cluster["jobs"] = [job(203, "RUNNING", node="donor", start_time=started.timestamp())]
        tick.once()
        # 2h held, 1 GPU, 50 TFLOP/s, Demand 1.0 (it is alone) => ~100 Karma
        accrued = db.live_escrows(conn)[0]["accrued"]
        assert accrued == pytest.approx(100.0, rel=0.01)

    def test_settling_transfers_to_the_arms_owner_and_sums_to_zero(self, conn, cluster):
        """The core flow, and the invariant it preserves. The submitter is debited and the Arm's owner
        credited the SAME Karma, so Σ balances ≡ 0 — there is no supply, no pot, and nothing to inflate."""
        self.spawn(conn, 204, "alice")
        db.accrue(conn, 204, "donor", 75.0, at(conn, 0))
        cluster["jobs"] = []                     # gone from squeue == it ended
        assert tick.once() == (0, 1)

        bal = karma.balances(conn)
        assert bal[uid(conn, "alice")] == pytest.approx(-75.0, rel=1e-3)
        assert bal[uid(conn, "carol")] == pytest.approx(+75.0, rel=1e-3)
        assert sum(bal.values()) == pytest.approx(0.0, abs=1e-9)
        assert db.live_escrows(conn) == []       # the claim is released

    def test_a_commons_arm_burns_the_karma(self, conn, cluster):
        """An Admin-owned Arm is the cluster's own hardware, which nobody donated. The Karma is
        DESTROYED rather than credited — the Admin earns nothing, ever — and Decay reabsorbs the drift,
        which is why Σ ≡ 0 is an attractor rather than an invariant anyone has to police."""
        db.add_user(conn, "root", is_admin=True)
        cluster["arms"] = [arm("donor", owner="root")]
        self.spawn(conn, 205, "alice")
        db.accrue(conn, 205, "donor", 60.0, at(conn, 0))
        cluster["jobs"] = []
        tick.once()

        bal = karma.balances(conn)
        assert bal[uid(conn, "alice")] == pytest.approx(-60.0, rel=1e-3)
        assert uid(conn, "root") not in bal          # the Admin never enters the ledger
        assert sum(bal.values()) < 0                 # destroyed, not transferred

    def test_running_on_your_own_arm_is_free(self, conn, cluster):
        """You pay yourself: the transfer nets to zero with no special case anywhere in the code."""
        cluster["arms"] = [arm("donor", owner="alice")]
        self.spawn(conn, 206, "alice")
        db.accrue(conn, 206, "donor", 90.0, at(conn, 0))
        cluster["jobs"] = []
        tick.once()
        assert karma.balances(conn).get(uid(conn, "alice"), 0.0) == pytest.approx(0.0, abs=1e-9)

    def test_a_spawn_cancelled_while_queued_settles_at_zero(self, conn, cluster):
        """It never held a card, so it owes nothing — and its hold is released in full. This is also
        what makes over-declaring --time cost a User nothing but queue position."""
        self.spawn(conn, 207, "alice", hold=500.0)
        cluster["jobs"] = []
        tick.once()
        assert karma.balances(conn).get(uid(conn, "alice"), 0.0) == pytest.approx(0.0, abs=1e-9)
        assert db.live_escrows(conn) == []

    def test_an_unpriced_arm_settles_at_zero_without_crashing(self, conn, cluster):
        """A benchmark that flaked leaves an Arm with no Power. It is a free GPU until someone reads the
        log — but it must not take the tick down with it, because a dead tick freezes the whole queue's
        nice at whatever it last held, silently, forever."""
        cluster["arms"] = [arm("donor", power=None)]
        self.spawn(conn, 208, "alice")
        started = datetime.now(timezone.utc) - timedelta(hours=2)
        cluster["jobs"] = [job(208, "RUNNING", node="donor", start_time=started.timestamp())]
        tick.once()
        assert db.live_escrows(conn)[0]["accrued"] == 0.0

    def test_a_spawn_is_billed_for_the_card_it_actually_held(self, conn, cluster):
        """The claim per-Device Power exists to make true. Two Spawns, same box, same second, same
        Demand — the one on the throttled card pays less, because a second on that card really does buy
        less. A node average would charge them identically and nothing in the system would ever notice.

        `devices` are the GRES indices Slurm reports for a running job (`gres_detail`, IDX:n), and they
        index straight into the Arm's `powers` because gres.conf's File lines were written in that
        order."""
        cluster["arms"] = [arm("donor", powers=[80.0, 20.0])]
        started = datetime.now(timezone.utc) - timedelta(hours=1)
        self.spawn(conn, 301, "alice")
        self.spawn(conn, 302, "alice")
        cluster["jobs"] = [
            job(301, "RUNNING", node="donor", devices=[0], start_time=started.timestamp()),
            job(302, "RUNNING", node="donor", devices=[1], start_time=started.timestamp()),
        ]
        tick.once()
        accrued = {e["job_id"]: e["accrued"] for e in db.live_escrows(conn)}
        # Same node, same hour, same Demand (1.0 — Demand is per-card: two claims over two devices).
        # Only the Power differs.
        assert accrued[301] == pytest.approx(80.0, rel=0.01)   # 1h x 80 TFLOP/s x 1.0
        assert accrued[302] == pytest.approx(20.0, rel=0.01)   # 1h x 20 TFLOP/s x 1.0

    def test_two_cards_are_two_separate_purchases(self, conn, cluster):
        """'Several Devices are several separate purchases.' A Spawn holding both cards pays each one's
        own Power added together — not twice the faster, and not twice the average."""
        cluster["arms"] = [arm("donor", powers=[80.0, 20.0])]
        self.spawn(conn, 303, "alice")
        started = datetime.now(timezone.utc) - timedelta(hours=1)
        cluster["jobs"] = [job(303, "RUNNING", node="donor", gpus=2, devices=[0, 1],
                               start_time=started.timestamp())]
        tick.once()
        # 1h x (80 + 20) TFLOP/s x Demand 1.0 (a 2-GPU claim over the node's 2 devices) => 100 Karma
        assert db.live_escrows(conn)[0]["accrued"] == pytest.approx(100.0, rel=0.01)

    def test_a_spawn_slurm_reports_no_device_for_runs_free_and_loudly(self, conn, cluster):
        """The failure this design refuses to paper over. If the held card cannot be identified, the
        Spawn is NOT billed at the node's average — a bill nobody could ever check is worse than an
        obvious zero and a log line, which is exactly how an unpriced Arm already behaves."""
        cluster["arms"] = [arm("donor", powers=[80.0, 20.0])]
        self.spawn(conn, 304, "alice")
        started = datetime.now(timezone.utc) - timedelta(hours=1)
        cluster["jobs"] = [job(304, "RUNNING", node="donor", devices=[],
                               start_time=started.timestamp())]
        tick.once()
        assert db.live_escrows(conn)[0]["accrued"] == 0.0

    def test_a_cpu_only_spawn_holds_no_device_and_pays_nothing(self, conn, cluster):
        """The same empty device list, and NOT the same case: a Spawn that asked for no GPU is
        correctly free, and must not be confused with one whose card we failed to identify."""
        cluster["arms"] = [arm("donor", powers=[80.0])]
        self.spawn(conn, 305, "alice")
        started = datetime.now(timezone.utc) - timedelta(hours=1)
        cluster["jobs"] = [job(305, "RUNNING", node="donor", gpus=0, devices=[],
                               start_time=started.timestamp())]
        tick.once()
        assert db.live_escrows(conn)[0]["accrued"] == 0.0

    def test_a_card_the_registry_never_priced_is_not_guessed_at(self, conn, cluster):
        """A donor added a fourth GPU and slurmd re-registered with it; the registry still holds three
        Powers. Billing index 3 at index 0's rate would be silent and wrong, so it bills nothing."""
        cluster["arms"] = [arm("donor", powers=[80.0, 80.0, 80.0])]
        self.spawn(conn, 306, "alice")
        started = datetime.now(timezone.utc) - timedelta(hours=1)
        cluster["jobs"] = [job(306, "RUNNING", node="donor", devices=[3],
                               start_time=started.timestamp())]
        tick.once()
        assert db.live_escrows(conn)[0]["accrued"] == 0.0

    def test_an_admins_spawn_is_never_escrowed_or_settled(self, conn, cluster):
        """The Admin is outside the economy: no hold, no meter, no ledger row. `hold=None` at submit is
        the whole of it."""
        db.add_user(conn, "root", is_admin=True)
        self.spawn(conn, 209, "root", hold=None)
        assert db.live_escrows(conn) == []
        cluster["jobs"] = []
        assert tick.once() == (0, 0)
        assert karma.balances(conn) == {}

    def test_demand_history_is_recorded_for_the_tide(self, conn, cluster):
        """The Tide forecasts from these samples, so the meter must already be collecting them —
        otherwise the Tide lands with an empty history and forecasts nothing for its first 30 days."""
        cluster["jobs"] = [job(210, "RUNNING", node="donor", start_time=0)]
        tick.once()
        rows = conn.execute("SELECT node, demand FROM karma_demand").fetchall()
        assert [(r["node"], r["demand"]) for r in rows] == [("donor", 1.0)]


class TestRetraction:
    """`ccc_head_api.retraction` starts the TERM + the 3-minute grace; this is the tick's half —
    force-killing whatever is still alive past the deadline, and making sure a caught Spawn
    settles at ZERO no matter what it accrued. Retract must never be a way to farm Karma by
    donating a card and yanking it back mid-run."""

    @pytest.fixture
    def cluster(self, conn, monkeypatch):
        state = {"jobs": [], "arms": [arm("donor", owner="carol")]}
        monkeypatch.setattr(db, "connect", lambda: conn)
        monkeypatch.setattr(tick.slurm, "queue", lambda: state["jobs"])
        monkeypatch.setattr(tick.slurm, "arms", lambda: state["arms"])
        monkeypatch.setattr(tick.slurm, "set_nice", lambda job, nice, owner=None: None)
        signalled = []
        monkeypatch.setattr(tick.slurm, "signal", lambda jid, sig: signalled.append((jid, sig)))
        return state, signalled

    def _spawn(self, conn, jid, user="alice", hold=100.0):
        db.record_spawn(conn, jid, uid(conn, user), "train.py", f"u{jid}",
                        minutes=60, gpus=1, vram_gb=8, hold=hold)

    def test_a_caught_job_past_its_deadline_is_force_killed(self, conn, cluster):
        state, signalled = cluster
        self._spawn(conn, 601)
        db.catch_retracting_job(conn, 601, "donor", ts(-1))
        state["jobs"] = [job(601, "RUNNING", node="donor", start_time=0)]
        tick.once()
        assert signalled == [(601, "KILL")]

    def test_a_caught_job_still_inside_its_grace_is_left_alone(self, conn, cluster):
        state, signalled = cluster
        self._spawn(conn, 602)
        db.catch_retracting_job(conn, 602, "donor", ts(180))
        state["jobs"] = [job(602, "RUNNING", node="donor", start_time=0)]
        tick.once()
        assert signalled == []

    def test_a_job_already_gone_is_not_force_killed_again(self, conn, cluster):
        """It ended on its own inside the grace window — `_settle` is what finishes the job off, not
        another scancel for a process that no longer exists."""
        state, signalled = cluster
        self._spawn(conn, 603)
        db.catch_retracting_job(conn, 603, "donor", ts(-1))
        state["jobs"] = []
        tick.once()
        assert signalled == []

    def test_a_caught_job_settles_at_zero_no_matter_what_it_accrued(self, conn, cluster):
        self._spawn(conn, 604)
        db.accrue(conn, 604, "donor", 999.0, ts(-1))
        db.catch_retracting_job(conn, 604, "donor", ts(-1))
        cluster[0]["jobs"] = []  # it has left the queue
        tick.once()
        assert karma.balances(conn).get(uid(conn, "alice"), 0.0) == pytest.approx(0.0, abs=1e-9)
        assert karma.balances(conn).get(uid(conn, "carol"), 0.0) == pytest.approx(0.0, abs=1e-9)
        assert db.retracting_jobs(conn) == []   # cleared once it settles


class TestOverrideAndTheAdmin:
    def test_an_override_aimed_at_an_admin_is_refused(self, conn):
        """Not tidiness — the invariant. An Admin is excluded from standings(), so Karma conjured into
        one would still count in balances() while contributing to no standing, and Σ balances ≡ 0 would
        quietly stop holding. Admins never appear in the ledger, and that is what keeps it true."""
        db.add_user(conn, "root", is_admin=True)
        with pytest.raises(ValueError, match="outside the economy"):
            karma.override(conn, "root", 1000.0)
        assert db.karma_rows(conn) == []  # and nothing reached the ledger

    def test_an_admin_has_no_standing(self, conn):
        db.add_user(conn, "root", is_admin=True)
        assert "root" not in [s["username"] for s in karma.standings(conn)]


# --- the Tide, and the ledger a User actually reads ----------------------

def put_row(conn, kind, amount, from_user, to_user, at):
    """A ledger row at an explicit past time — record_karma always stamps now, but the curve
    tests need history to sag through."""
    conn.execute("INSERT INTO karma_ledger (at, kind, amount, from_user, to_user) VALUES (?,?,?,?,?)",
                 (at, kind, amount, from_user, to_user))
    conn.commit()


def sample(conn, node, days_ago, hour, demand):
    """One Demand sample, placed at a specific hour of a specific day."""
    t = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(hour=hour, minute=0, second=0)
    db.record_demand(conn, t.strftime("%Y-%m-%dT%H:%M:%SZ"), {node: demand})


class TestTheTide:
    """The Tide = each Arm's mean Demand by hour of the day, over the last 30 days. It is a *pricing*
    input and nothing else — it never reaches a screen as a forecast."""

    def test_an_empty_history_forecasts_one(self, conn):
        """The cold start, and the load-bearing default. 1.0 is what a card running exactly one Spawn
        reads, and it is what makes the flat-hold formula a special case of the Tide rather than
        something to rewrite: with a flat Tide the hold reduces EXACTLY to minutes/60 x gpus x
        max(Power)."""
        assert karma.tide(karma.tides(conn), "never-seen", 3) == 1.0

    def test_it_is_the_mean_of_that_hour_across_days(self, conn):
        """Same hour, different days -> one bucket. This is what makes the forecast a forecast rather
        than an echo of yesterday."""
        sample(conn, "gpu", days_ago=1, hour=14, demand=2.0)
        sample(conn, "gpu", days_ago=2, hour=14, demand=4.0)
        assert karma.tide(karma.tides(conn), "gpu", 14) == pytest.approx(3.0)

    def test_hours_do_not_bleed_into_each_other(self, conn):
        sample(conn, "gpu", days_ago=1, hour=2, demand=8.0)
        tides = karma.tides(conn)
        assert karma.tide(tides, "gpu", 2) == pytest.approx(8.0)
        assert karma.tide(tides, "gpu", 3) == 1.0  # an empty bucket forecasts 1.0, not 0.0

    def test_arms_do_not_bleed_into_each_other(self, conn):
        """Demand is per-Arm because scarcity is: the fleet's one big card is wanted by the jobs that
        need it AND by the jobs that don't."""
        sample(conn, "big", days_ago=1, hour=9, demand=6.0)
        sample(conn, "small", days_ago=1, hour=9, demand=1.0)
        tides = karma.tides(conn)
        assert karma.tide(tides, "big", 9) == pytest.approx(6.0)
        assert karma.tide(tides, "small", 9) == pytest.approx(1.0)

    def test_samples_beyond_the_window_are_not_in_the_forecast(self, conn):
        """DEMAND_RETENTION_DAYS is one window serving three jobs (Decay's tau, the Tide, the charts).
        A sample older than it prices nothing — and prune_demand deletes it anyway."""
        sample(conn, "gpu", days_ago=90, hour=5, demand=8.0)
        assert karma.tide(karma.tides(conn), "gpu", 5) == 1.0


class TestTheHoldRidesTheTide:
    def test_a_busy_hour_costs_more(self, conn):
        """The Tide's whole purpose. Same Spawn, same card, twice the usual Demand at that hour ->
        twice the hold. Scarcity is priced at submit, not just at settle."""
        sample(conn, "gpu", days_ago=1, hour=13, demand=2.0)
        elig = [arm("gpu", power=50.0)]
        quiet = karma.hold_for(60, 1, elig, hours=[3], tides=karma.tides(conn))
        busy = karma.hold_for(60, 1, elig, hours=[13], tides=karma.tides(conn))
        assert quiet == pytest.approx(50.0)   # untouched hour -> Tide 1.0 -> the slice-2 quote
        assert busy == pytest.approx(100.0)   # Tide 2.0

    def test_a_long_spawn_is_quoted_at_the_MEAN_of_its_hours_not_the_peak(self, conn):
        """Mean, not max, over the span. Otherwise a 24h Spawn touches every hour and would be quoted
        at the daily peak purely for being long — which would price honesty about duration."""
        sample(conn, "gpu", days_ago=1, hour=13, demand=3.0)  # one spike...
        elig = [arm("gpu", power=50.0)]
        hold = karma.hold_for(60, 1, elig, hours=[12, 13], tides=karma.tides(conn))
        assert hold == pytest.approx(50.0 * (1.0 + 3.0) / 2)  # ...diluted across the span

    def test_slice_two_is_the_special_case_of_a_flat_tide(self, conn):
        """The compatibility claim, stated as a test: an empty history must reproduce the
        flat-hold formula EXACTLY, or every hold quoted without Tide data was wrong."""
        elig = [arm("slow", power=10.0), arm("fast", power=50.0)]
        assert karma.hold_for(60, 1, elig, hours=[0, 1, 2], tides=karma.tides(conn)) \
            == karma.hold_for(60, 1, elig)


class TestTheLedgerAUserReads:
    def test_rows_are_signed_from_your_side(self, conn):
        """The same Settlement is income to one party and a bill to the other. One row, two readings."""
        a, b = uid(conn, "alice"), uid(conn, "bob")
        db.record_karma(conn, "settle", 10.0, b, a, job_id=1)  # bob -> alice
        assert karma.ledger(conn, a)["rows"][0]["amount"] == pytest.approx(10.0)
        assert karma.ledger(conn, b)["rows"][0]["amount"] == pytest.approx(-10.0)

    def test_the_counterparty_of_a_burn_is_nobody(self, conn):
        """A commons burn has no other side, and the page must be able to say so. NULL is not a missing
        name — it IS the fact: this Karma was destroyed, not paid to anyone."""
        b = uid(conn, "bob")
        db.record_karma(conn, "settle", 5.0, b, None, job_id=1)
        row = karma.ledger(conn, b)["rows"][0]
        assert row["amount"] == pytest.approx(-5.0) and row["counterparty"] is None

    def test_self_use_nets_to_zero_in_one_row(self, conn):
        """An owner on their own Arm is both parties. It costs nothing, and it does so without a special
        case anywhere — the signs simply cancel."""
        a = uid(conn, "alice")
        db.record_karma(conn, "settle", 40.0, a, a, job_id=1)
        assert karma.ledger(conn, a)["rows"][0]["amount"] == 0.0

    def test_there_are_no_decay_rows_and_the_curve_carries_it(self, conn):
        """The claim the whole no-karma_balance design rests on. Decay is not an event: nothing is
        written, ever. It is the SAG of the curve between two real rows — so a ledger with one row and
        no further activity must still show a Balance that falls."""
        a = uid(conn, "alice")
        put_row(conn, "override", 1000.0, None, a, at(conn, 20))

        led = karma.ledger(conn, a, days=30)
        assert [r["kind"] for r in led["rows"]] == ["override"]  # ONE row. No 'decay' row exists.

        curve = led["curve"]
        before = [p["balance"] for p in curve if p["at"] < at(conn, 20)]
        assert max(before) == 0.0                      # nothing before the event
        assert curve[-1]["balance"] < 1000.0           # ...and it has sagged since
        assert curve[-1]["balance"] == pytest.approx(1000.0 * karma.decay(20), rel=0.02)

    def test_the_curve_is_the_balance(self, conn):
        """The curve is not a second source of truth — it is balances() sampled. If these two ever
        disagree, one of them is lying to a User about where they stand."""
        a = uid(conn, "alice")
        put_row(conn, "override", 500.0, None, a, at(conn, 5))
        put_row(conn, "settle", 120.0, a, uid(conn, "bob"), at(conn, 2))
        led = karma.ledger(conn, a)
        assert led["curve"][-1]["balance"] == pytest.approx(karma.balances(conn)[a], rel=1e-6)

    def test_you_see_only_your_own_rows(self, conn):
        """Standings are everyone's (a Balance is meaningless alone); a transaction history is not."""
        a, b, c = uid(conn, "alice"), uid(conn, "bob"), uid(conn, "carol")
        db.record_karma(conn, "settle", 7.0, b, c, job_id=1)  # bob -> carol; alice is not a party
        assert karma.ledger(conn, a)["rows"] == []
