"""Phase 6 slice 1 — the CLI end-to-end, as an operator drives it.

These are the tests the Dashboard will never have (no browser robot, by decision), so they carry the
weight for both clients: the Dashboard rides the same API, and what matters is that the API is safe
and the operations work. See `conftest.py` for the rig.
"""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from ccc_head_api import db, karma, slurm
from cuttle import config

from conftest import USERS


def _spawn_id(output: str) -> int:
    """The id `cuttle run` reports back."""
    return int(re.search(r"submitted spawn (\d+)", output).group(1))


class TestSession:
    def test_login_stores_a_session_and_greets_the_user(self, cli):
        result = cli("login", input=f"alice\n{USERS['alice']}\n")
        assert result.exit_code == 0, result.output
        assert "logged in as alice" in result.output
        assert config.CREDENTIALS_FILE.read_text().strip()

    def test_login_refuses_a_wrong_password(self, cli):
        result = cli("login", input="alice\nnot-my-password\n")
        assert result.exit_code != 0
        assert not config.CREDENTIALS_FILE.exists()

    def test_admin_login_says_so(self, cli):
        result = cli("login", input=f"root\n{USERS['root']}\n")
        assert "(admin)" in result.output

    def test_whoami_reads_the_session_back(self, signed_in):
        result = signed_in("whoami")
        assert result.exit_code == 0, result.output
        assert result.output.strip() == "alice"

    def test_whoami_without_a_session_is_refused(self, cli):
        """The bearer is the whole authorisation — no credential file, no reads."""
        result = cli("whoami")
        assert result.exit_code != 0
        assert "not logged in" in result.output

    def test_logout_revokes_the_session_at_the_head(self, signed_in):
        assert signed_in("logout").exit_code == 0
        assert not config.CREDENTIALS_FILE.exists()
        # The point of a logout is the *server* forgetting, not just the client. A second whoami
        # would pass on a stale local token if the Head still honoured the row.
        assert signed_in("whoami").exit_code != 0

    def test_password_change_takes_effect(self, signed_in):
        result = signed_in("passwd", input=f"{USERS['alice']}\nnew-passphrase\nnew-passphrase\n")
        assert result.exit_code == 0, result.output
        config.CREDENTIALS_FILE.unlink()
        assert signed_in("login", input="alice\nnew-passphrase\n").exit_code == 0


class TestSubmit:
    """`cuttle run` — the operation the whole cluster exists for."""

    def test_a_spawn_is_submitted_and_attributed(self, signed_in, fake_slurm, project):
        result = signed_in("run", "train.py", "--time", "60", "--vram", "24")
        assert result.exit_code == 0, result.output
        assert "submitted spawn" in result.output
        # Attribution is the ledger's, not Slurm's — every Spawn runs as the Mantle, so Slurm's own
        # user field says `ccc` for all of them. If this row is missing the Spawn is unowned.
        with db.connect() as conn:
            alice = db.get_user(conn, "alice")
            spawn = db.get_spawn(conn, _spawn_id(result.output))
        assert spawn is not None, "no Attribution row was recorded"
        assert spawn["user_id"] == alice["id"]

    def test_the_escrow_is_held_at_submit(self, signed_in, fake_slurm, project):
        """A Spawn that existed without its hold, even briefly, is one its submitter's Share has not
        been charged for — precisely the window a queue-flooder wants."""
        result = signed_in("run", "train.py", "--time", "60", "--vram", "24")
        with db.connect() as conn:
            escrows = {e["job_id"]: e for e in db.live_escrows(conn)}
        assert _spawn_id(result.output) in escrows

    def test_the_uploaded_project_reaches_the_head(self, signed_in, fake_slurm, project):
        signed_in("run", "train.py", "--time", "60", "--vram", "24")
        staged = list((fake_slurm / "projects").glob("*/train.py"))
        assert staged, "the project tree was not unpacked on the Head"
        assert "hello from a Spawn" in staged[0].read_text()

    def test_time_and_vram_are_required(self, signed_in, fake_slurm, project):
        """Nothing can price a Spawn whose length nobody declared, nor place one whose needs nobody
        did. Naming the missing option in the assertion matters: `exit_code != 0` alone would pass
        just as happily if the rig itself were broken.

        Since these became promptable the refusal is ours, not click's — a Spawn is refused because
        there is no terminal to ask on, never because a default quietly filled it in.
        """
        no_time = signed_in("run", "train.py", "--vram", "24")
        assert no_time.exit_code != 0 and "--time" in no_time.output
        no_vram = signed_in("run", "train.py", "--time", "60")
        assert no_vram.exit_code != 0 and "--vram" in no_vram.output

    def test_submitting_without_a_session_is_refused(self, cli, fake_slurm, project):
        result = cli("run", "train.py", "--time", "60", "--vram", "24")
        assert result.exit_code != 0
        assert "not logged in" in result.output


class TestCancel:
    """`cuttle cancel` — your own over the API, anyone's only on the Head."""

    def _spawn(self, cli):
        return _spawn_id(cli("run", "train.py", "--time", "60", "--vram", "24").output)

    def test_you_may_cancel_your_own(self, signed_in, fake_slurm, project, monkeypatch):
        killed = []
        monkeypatch.setattr(slurm, "cancel", lambda jid: killed.append(jid))
        spawn = self._spawn(signed_in)
        result = signed_in("cancel", spawn, "--yes")
        assert result.exit_code == 0, result.output
        assert killed == [spawn]

    def test_the_confirmation_can_be_declined(self, signed_in, fake_slurm, project, monkeypatch):
        """The Dashboard asks before cancelling; so does this. Declining must not scancel."""
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("must not scancel"))
        spawn = self._spawn(signed_in)
        assert signed_in("cancel", spawn, input="n\n").exit_code != 0

    def test_you_may_not_cancel_someone_elses(self, cli, fake_slurm, project, monkeypatch):
        """alice submits; root tries to stop it over the API and is refused — an Admin has no branch
        on that route since Phase 6."""
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("must not scancel"))
        cli("login", input=f"alice\n{USERS['alice']}\n")
        spawn = self._spawn(cli)
        config.CREDENTIALS_FILE.unlink()
        cli("login", input=f"root\n{USERS['root']}\n")
        result = cli("cancel", spawn, "--yes")
        assert result.exit_code != 0
        assert "not your spawn" in result.output


class TestQueue:
    """`cuttle queue` — the shared live queue, with the ledger's attribution (Slurm's own user is
    `ccc` for every Spawn). The queue rows are faked at `slurm.spawns` so the route's real
    attribution enrichment (head.db) is what's under test, not squeue's JSON."""

    def test_the_empty_queue_says_so(self, signed_in, monkeypatch):
        monkeypatch.setattr(slurm, "spawns", lambda: [])
        r = signed_in("queue")
        assert r.exit_code == 0, r.output
        assert "queue is empty" in r.output

    def test_the_queue_lists_a_spawn_and_who_submitted_it(self, signed_in, monkeypatch):
        monkeypatch.setattr(slurm, "spawns", lambda: [
            {"id": 4207, "name": "train.py", "state": "running", "node": "gpu-box",
             "gpus": 1, "user": "ccc", "time_limit": 60, "submit_time": 0, "start_time": 0}])
        with db.connect() as conn:
            alice = db.get_user(conn, "alice")
            db.record_spawn(conn, 4207, alice["id"], "train.py", "stage-uuid", 60, 1, 24, None)
        r = signed_in("queue")
        assert r.exit_code == 0, r.output
        # The attribution is the point: `ccc` (Slurm's user) must NOT be what a reader sees.
        assert "4207" in r.output and "train.py" in r.output and "alice" in r.output

    def test_queue_json_is_the_raw_rows(self, signed_in, monkeypatch):
        monkeypatch.setattr(slurm, "spawns", lambda: [
            {"id": 4207, "name": "train.py", "state": "running", "node": "gpu-box",
             "gpus": 1, "user": "ccc", "time_limit": 60, "submit_time": 0, "start_time": 0}])
        r = signed_in("queue", "--json")
        assert json.loads(r.output)[0]["id"] == 4207

    def test_queue_without_a_session_is_refused(self, cli):
        r = cli("queue")
        assert r.exit_code != 0 and "not logged in" in r.output


class TestAccount:
    """`cuttle account` — identity from /me, standing from the standings. An Admin is outside the
    economy (absent from standings), so its account is identity alone."""

    def test_account_shows_identity_and_standing(self, signed_in):
        r = signed_in("account")
        assert r.exit_code == 0, r.output
        assert "alice" in r.output
        assert "balance" in r.output          # a User has a Balance, even at 0

    def test_admin_account_omits_the_balance(self, cli):
        cli("login", input=f"root\n{USERS['root']}\n")
        r = cli("account")
        assert r.exit_code == 0, r.output
        assert "root" in r.output
        # root is the Admin: no standing, so no Balance line — the absence is correct.
        assert "balance" not in r.output

    def test_account_json_carries_the_standing(self, signed_in):
        data = json.loads(signed_in("account", "--json").output)
        assert data["username"] == "alice"
        assert "balance" in data

    def test_account_without_a_session_is_refused(self, cli):
        r = cli("account")
        assert r.exit_code != 0 and "not logged in" in r.output


class TestKarmaLedger:
    """`cuttle karma ledger` — your own real events, and the Balance they leave you at."""

    def test_an_empty_ledger_says_so(self, signed_in):
        r = signed_in("karma", "ledger")
        assert r.exit_code == 0, r.output
        assert "no ledger events" in r.output

    def test_the_ledger_shows_an_override_event(self, signed_in):
        with db.connect() as conn:
            karma.override(conn, "alice", 500, note="welcome")
        r = signed_in("karma", "ledger")
        assert r.exit_code == 0, r.output
        assert "override" in r.output
        assert "+500.0" in r.output           # signed from alice's view: money in is +
        assert "balance" in r.output          # the curve's last point

    def test_ledger_without_a_session_is_refused(self, cli):
        r = cli("karma", "ledger")
        assert r.exit_code != 0 and "not logged in" in r.output


class TestTide:
    """`cuttle tide` — a demand sparkline per Arm. The series is seeded straight into karma_demand."""

    def test_no_demand_yet_says_so(self, signed_in):
        r = signed_in("tide")
        assert r.exit_code == 0, r.output
        assert "no demand recorded" in r.output

    def test_the_tide_draws_a_sparkline_and_the_now_value(self, signed_in):
        now = datetime.now(timezone.utc)
        with db.connect() as conn:
            for h in range(6):                # six hourly samples, demand 0..5
                at = (now - timedelta(hours=5 - h)).strftime("%Y-%m-%dT%H:00:00Z")
                db.record_demand(conn, at, {"gpu-box": float(h)})
        r = signed_in("tide")
        assert r.exit_code == 0, r.output
        assert "gpu-box" in r.output
        assert "5.0" in r.output              # the latest hour is the NOW column
        assert any(g in r.output for g in "▁▂▃▄▅▆▇█")

    def test_tide_without_a_session_is_refused(self, cli):
        r = cli("tide")
        assert r.exit_code != 0 and "not logged in" in r.output


class TestNoAdminWritesOverTheApi:
    """Phase 6's binding invariant, checked from the client side: an Admin's Session is worth no more
    over HTTP than any User's. These are the tests that fail loudly if someone re-adds a convenience
    route later."""

    def test_override_is_not_reachable_over_the_api(self, head, signed_in):
        """It moved to the Head (`cuttle karma override`). 405 rather than 404 because the SPA
        catch-all claims unmatched GETs, so an unknown API path rejects the *method* — either way
        there is no POST here.

        The status code is the weaker half of this test. What matters is that no Karma moved: a route
        that answered 500 while still crediting the balance would pass a status-code assertion and
        be a catastrophe."""
        token = config.CREDENTIALS_FILE.read_text().strip()
        r = head.post("/karma/override", json={"username": "alice", "amount": 500},
                      headers={"Authorization": f"Bearer {token}"})
        assert r.status_code in (404, 405)
        with db.connect() as conn:
            alice = db.get_user(conn, "alice")
            # Absent from `balances` == 0, which is where everyone starts.
            assert karma.balances(conn).get(alice["id"], 0.0) == 0.0

    def test_the_endpoint_itself_refuses_a_pin(self, head, signed_in, fake_slurm, project):
        """Driven at the API, not the CLI: a client that simply stopped sending `node` would make a
        client-side test pass while the endpoint stayed wide open."""
        token = config.CREDENTIALS_FILE.read_text().strip()
        r = head.post("/submit", content=b"",
                      headers={"Authorization": f"Bearer {token}",
                               "X-CCC-Meta": '{"script":"t.py","minutes":60,"vram":24,"node":"gpu-box"}'})
        assert r.status_code == 403
        assert "on the Head" in r.json()["detail"]
