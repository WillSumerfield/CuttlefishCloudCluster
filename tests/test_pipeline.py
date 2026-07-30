"""Phase 4 slice 4 — the Fake Cluster: the real economy across simulated weeks.

These are the claims that only exist *over time*, and that no unit test can reach — they need a
scheduler, a clock, and hundreds of Spawns interacting. The economy here is the **real** code
(`tick.once`, `karma`, `db`, a real temp ledger); only Slurm is modelled, and only the behaviours we
verified on hardware (see tests/fake_cluster.py). Each test states a promise the whole design makes:

  * **Karma ranks, it never gates** — poor standing loses contention and NOTHING on an idle cluster.
  * **Σ balances ≡ 0** — every settlement is a transfer; the ledger conserves Karma.
  * A **queue-flooder tanks their own Share** — the Escrow lands at submit, so flooding is self-punishing.
  * A **cancel settles at zero** — retracting a Spawn that did no work costs and earns nothing.
  * The bound on hogging is **economic, not scheduler-side** — spending Karma is what makes you wait.
  * A **commons burn** is the one settlement that doesn't conserve — and it drifts Σ by exactly itself.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # tests/ is not a package
from fake_cluster import FakeCluster

from ccc_head_api import db, karma, tick


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A real head.db, and tick.once() pointed at it — the economy under test is the real one."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    c = db.connect()
    db.init_schema(c)
    monkeypatch.setattr(db, "connect", lambda: c)  # the tick opens its own connection; make it ours
    return c


def user(conn, name, is_admin=False):
    db.add_user(conn, name, is_admin=is_admin)
    r = db.get_user(conn, name)
    return {"id": r["id"], "username": name, "is_admin": bool(r["is_admin"])}


def arm(name, power, owner, vram_mb=24564):
    """One single-card Arm. `powers` is a list because a Device is what carries a Power — a one-card
    box is just the shortest such list."""
    return {"name": name, "powers": [power], "vram_mb": vram_mb, "owner": owner}


def total_balance(conn, now):
    return sum(karma.balances(conn, now).values())


class TestKarmaNeverGates:
    def test_worst_standing_still_runs_immediately_on_an_idle_cluster(self, conn, monkeypatch):
        """The model's first promise. A User with a ruinous Balance loses *contention* and nothing else:
        with the card free, their Spawn starts at the very next opportunity, nice notwithstanding."""
        bob = user(conn, "bob")
        pariah = user(conn, "pariah")
        karma.override(conn, "pariah", -500_000)  # about as poor as standing gets
        fc = FakeCluster(conn, [arm("box", 50.0, owner="bob")]).install(monkeypatch)

        jid = fc.submit(pariah, vram_gb=8, minutes=10)
        fc.advance(1)
        assert fc.state(jid) == "RUNNING"          # it ran, despite the worst standing in the cluster
        assert fc.waited_days(jid) < 0.01

    def test_standing_only_orders_under_contention(self, conn, monkeypatch):
        """With ONE card and two waiters, the better-standing User goes first. Same nice mechanism as
        above — but now it decides an order instead of gating a Spawn."""
        rich = user(conn, "rich")
        poor = user(conn, "poor")
        karma.override(conn, "poor", -400_000)
        fc = FakeCluster(conn, [arm("box", 50.0, owner="rich")]).install(monkeypatch)

        poor_job = fc.submit(poor, vram_gb=8, minutes=30)
        rich_job = fc.submit(rich, vram_gb=8, minutes=30)  # queued behind, but better standing
        fc.advance(1)
        assert fc.state(rich_job) == "RUNNING"     # the fresh, better-standing Spawn wins the card
        assert fc.state(poor_job) == "PENDING"


class TestTheLedgerConserves:
    def test_a_transfer_only_run_keeps_sum_at_zero(self, conn, monkeypatch):
        """Σ balances ≡ 0, the invariant the whole ledger is shaped around, held across a week of
        cross-submission onto each other's User-owned Arms."""
        a = user(conn, "a")
        b = user(conn, "b")
        fc = FakeCluster(conn, [arm("arm-a", 40.0, owner="a"),
                                arm("arm-b", 60.0, owner="b")]).install(monkeypatch)
        # Each submits repeatedly; work lands on whichever card is free — often the other's.
        submitters = [a, b, a, b, a, b]
        for k in range(60):                        # ~a simulated day at 24-min steps
            if k % 5 == 0:
                fc.submit(submitters[k // 10 % len(submitters)], vram_gb=8, minutes=20)
            fc.advance(24)
        assert total_balance(conn, fc.now) == pytest.approx(0.0, abs=1e-6)
        # ...and something actually happened — this is not zero because nothing moved.
        assert any(r["kind"] == "settle" for r in conn.execute("SELECT kind FROM karma_ledger"))

    def test_a_commons_burn_drifts_the_sum_by_exactly_the_burn(self, conn, monkeypatch):
        """The one settlement that does NOT conserve. An Admin-owned Arm is a Commons Arm: the Karma is
        destroyed, not credited, so Σ balances goes negative by exactly what was burned — and by nothing
        else. (Decay would reabsorb it over a month; here we read it before Decay bites.)"""
        admin = user(conn, "admin", is_admin=True)
        worker = user(conn, "worker")
        fc = FakeCluster(conn, [arm("commons", 50.0, owner="admin")]).install(monkeypatch)

        jid = fc.submit(worker, vram_gb=8, minutes=10)
        for _ in range(14):
            fc.advance(1)
        assert fc.state(jid) == "GONE"
        rows = [r for r in conn.execute("SELECT amount, from_user, to_user FROM karma_ledger")]
        assert len(rows) == 1 and rows[0]["to_user"] is None      # burned: no recipient
        burned = rows[0]["amount"]
        assert burned > 0
        # Σ has gone negative by the burn — bar a few minutes of Decay between the settle and now (the
        # ledger amount is undecayed; total_balance decays it to fc.now). Not conserved, by exactly this.
        assert total_balance(conn, fc.now) == pytest.approx(-burned, rel=1e-3)


class TestFloodingIsSelfPunishing:
    def test_a_flooder_tanks_their_own_share(self, conn, monkeypatch):
        """The Escrow lands the instant a Spawn is queued, and it suppresses Share immediately — so a
        User who floods the queue tanks their OWN standing before a single flooded Spawn has run. This
        is what makes settle-at-end ungameable: 500 queued Spawns cannot buy top-of-queue for hours."""
        flooder = user(conn, "flooder")
        quiet = user(conn, "quiet")
        # A single small Arm, so the flood mostly sits PENDING — holds outstanding, none settled yet.
        fc = FakeCluster(conn, [arm("box", 50.0, owner="quiet")]).install(monkeypatch)

        quiet_share = [s for s in karma.standings(conn, fc.now) if s["username"] == "quiet"][0]["share"]
        for _ in range(40):
            fc.submit(flooder, vram_gb=8, minutes=120)     # a burst of long holds
        fc.advance(1)
        st = {s["username"]: s for s in karma.standings(conn, fc.now)}
        assert st["flooder"]["escrow"] > 0                 # the holds are live immediately
        assert st["flooder"]["share"] < quiet_share        # ...and have already sunk the flooder
        assert st["flooder"]["nice"] > st["quiet"]["nice"]  # so the flooder's own queue ranks last


class TestCancel:
    def test_a_pending_cancel_settles_at_zero(self, conn, monkeypatch):
        """A Spawn cancelled before it runs did no work: it settles for zero, its hold is released, and
        its would-be Arm owner earns nothing. This closes the farm — run 55 min, retract, bank the
        Karma, victim pays for destroyed work — by making the retract worth exactly nothing."""
        a = user(conn, "a")
        b = user(conn, "b")
        # One card, kept busy by b, so a's Spawn never starts.
        fc = FakeCluster(conn, [arm("box", 50.0, owner="b")]).install(monkeypatch)
        fc.submit(b, vram_gb=8, minutes=600)               # b hogs the only card
        fc.advance(1)
        victim = fc.submit(a, vram_gb=8, minutes=30)
        fc.advance(1)
        assert fc.state(victim) == "PENDING"
        assert db.escrow_totals(conn).get(a["id"], 0.0) > 0  # a is holding while queued

        fc.cancel(victim)
        fc.advance(1)
        settle = [r for r in conn.execute(
            "SELECT amount, from_user, to_user FROM karma_ledger WHERE from_user=?", (a["id"],))]
        assert len(settle) == 1 and settle[0]["amount"] == 0.0   # settled at zero
        assert db.escrow_totals(conn).get(a["id"], 0.0) == 0.0   # hold released
        assert karma.balances(conn, fc.now).get(a["id"], 0.0) == pytest.approx(0.0)  # a paid nothing


class TestTheBoundIsEconomic:
    def test_a_spender_cannot_hog_forever(self, conn, monkeypatch):
        """The starvation bound is economic, not scheduler-side (temp/KARMA.md is explicit about this):
        age saturation alone gives no hard guarantee, but *spending Karma is what makes you poor*. A User
        who keeps a card busy pays for every second — so their Share falls, their nice climbs, and a
        rival they were out-ranking overtakes them. The hog converts into someone the waiter can beat."""
        hog = user(conn, "hog")
        waiter = user(conn, "waiter")
        fc = FakeCluster(conn, [arm("box", 50.0, owner="waiter")]).install(monkeypatch)

        hog_share_start = [s for s in karma.standings(conn, fc.now)
                           if s["username"] == "hog"][0]["share"]
        # The hog submits back-to-back long Spawns; the waiter has one modest Spawn queued throughout.
        fc.submit(waiter, vram_gb=8, minutes=30)
        completed_by_waiter = 0
        for k in range(120):                               # ~2.5 simulated days at 30-min steps
            if k % 4 == 0:
                fc.submit(hog, vram_gb=8, minutes=120)     # keep feeding the queue
            fc.advance(30)
            # count the waiter's completions as they settle
            if fc.jobs and fc.state(1) == "GONE":
                completed_by_waiter = 1

        hog_now = [s for s in karma.standings(conn, fc.now) if s["username"] == "hog"][0]
        waiter_now = [s for s in karma.standings(conn, fc.now) if s["username"] == "waiter"][0]
        assert hog_now["share"] < hog_share_start          # the hog paid, and it shows
        assert hog_now["share"] < waiter_now["share"]      # the waiter now out-ranks the hog
        assert completed_by_waiter == 1                    # and was not starved: their Spawn ran
