"""Phase 4 slice 1 — the ledger, the Balance derived from it, the Share, and the nice it pushes.

Small tests, as we go; the Fake Cluster and the across-simulated-weeks claims wait for slice 4. What is
worth testing *now* is the arithmetic nothing else can check: every one of these failures is a **remote
consequence** — a standing that is quietly wrong, or a nice that is computed perfectly and lands on a
job it should never have touched.
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
        """Debt is forgiven and hoards evaporate, so standing reflects *recent* conduct. One τ of debt
        is worth 1/e of what it was."""
        conn.execute("INSERT INTO karma_ledger (at, kind, amount, from_user, to_user) VALUES (?,?,?,?,?)",
                     (at(conn, karma.KARMA_TAU_DAYS), "transfer", 1000.0, uid(conn, "alice"),
                      uid(conn, "bob")))
        conn.commit()
        assert karma.balances(conn)[uid(conn, "bob")] == pytest.approx(1000.0 / 2.718281828, rel=1e-3)

    def test_decay_is_linear_so_the_ledger_IS_the_balance(self, conn):
        """Why there is no `karma_balance` table. Decay is linear, so a decayed sum over the rows and a
        decay-on-touch running total are EXACTLY equal — not approximately. A stored balance could
        therefore only ever be a cache that silently disagrees with the ledger that produced it, and a
        User's standing being quietly wrong forever is the worst failure this economy has.

        Here: three events at different ages, summed by the real code, against the decay-on-touch
        recursion (`B ← B·exp(-Δt/τ); B += delta`) walked forward by hand.
        """
        alice = uid(conn, "alice")
        events = [(30.0, 500.0), (10.0, -200.0), (2.0, 75.0)]  # (days ago, delta) — oldest first
        for days, delta in events:
            conn.execute(
                "INSERT INTO karma_ledger (at, kind, amount, from_user, to_user) VALUES (?,?,?,?,?)",
                (at(conn, days), "override", abs(delta),
                 None if delta > 0 else alice, alice if delta > 0 else None))
        conn.commit()

        rolling, last = 0.0, events[0][0]
        for days, delta in events:                      # walk the recursion forward through time
            rolling *= karma.decay(last - days)
            rolling += delta
            last = days
        rolling *= karma.decay(last)                    # ...and from the final event to now

        assert karma.balances(conn)[alice] == pytest.approx(rolling, rel=1e-6)


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
        monkeypatch.setattr(tick.slurm, "set_nice", lambda job, nice: pushed.__setitem__(job, nice))
        return pushed

    def queue(self, monkeypatch, jobs):
        monkeypatch.setattr(tick.slurm, "pending", lambda: jobs)

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
        assert tick.once() == 0
        assert cluster == {}

    def test_an_unchanged_nice_is_not_rewritten(self, conn, cluster, monkeypatch):
        """The tick runs every 30s forever. Pushing a value Slurm already holds would hammer slurmctld
        with writes that change nothing, for every queued Spawn in the cluster, for as long as it is
        queued."""
        db.record_spawn(conn, 104, uid(conn, "bob"), "train.py", "uuid-b")
        settled = karma.nice_by_user(conn)[uid(conn, "bob")]
        self.queue(monkeypatch, [{"id": 104, "nice": settled}])
        assert tick.once() == 0
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

    def test_bumping_a_running_spawn_is_refused(self, slurm):
        """The regression. `squeue` lists RUNNING jobs too, so the old guard — "did squeue return
        anything?" — only ever caught a Spawn that was *gone*, despite its error saying otherwise.

        A bump on a running Spawn therefore succeeded, did nothing, and said it had worked. nice feeds
        the priority that decides who runs *next*; a Spawn already holding a card is past that
        decision, and no value we write can reach back and change it. Reporting success for that is
        worse than failing — the operator moves on believing the queue was reordered.
        """
        squeue, pushed = slurm
        squeue("RUNNING 0\n")
        with pytest.raises(typer.BadParameter, match="running"):
            cli_karma.bump(42)
        assert pushed == []  # and, crucially, nothing was written to Slurm

    def test_bumping_a_finished_spawn_is_refused(self, slurm):
        squeue, pushed = slurm
        squeue("")
        with pytest.raises(typer.BadParameter, match="not in the queue"):
            cli_karma.bump(42)
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

    def test_clear_is_guarded_too(self, slurm):
        """--clear on a running Spawn is just as much of a no-op, and used to be just as silent."""
        squeue, pushed = slurm
        squeue("RUNNING -131072\n")
        with pytest.raises(typer.BadParameter, match="running"):
            cli_karma.bump(42, clear=True)
        assert pushed == []

        squeue("PENDING -131072\n")
        cli_karma.bump(42, clear=True)
        assert pushed == [["scontrol", "update", "JobId=42", "Nice=0"]]
