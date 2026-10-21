"""The CLI end-to-end, as an operator drives it.

These are the tests the Dashboard will never have (no browser robot, by decision), so they carry the
weight for both clients: the Dashboard rides the same API, and what matters is that the API is safe
and the operations work. See `conftest.py` for the rig.
"""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from ccc_head_api import db, karma, slurm
from cuttle import auth, config, menu, pickers, ui
from cuttle import spawns as spawns_mod

from conftest import USERS


def _spawn_id(output: str) -> int:
    """The id `cuttle run` reports back."""
    return int(re.search(r"submitted spawn (\d+)", output).group(1))


class TestSession:
    def test_login_stores_a_session_and_greets_the_user(self, cli):
        result = cli("login", input=f"alice\n{USERS['alice']}\n")
        assert result.exit_code == 0, result.output
        assert "logged in as alice" in result.output
        assert config.SESSION_FILE.read_text().strip()

    def test_login_refuses_a_wrong_password(self, cli):
        result = cli("login", input="alice\nnot-my-password\n")
        assert result.exit_code != 0
        assert not config.SESSION_FILE.exists()

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

    def test_an_expired_session_says_to_log_in_again(self, signed_in):
        """A credential file that exists but holds a bearer the Head rejects (expired / revoked) is a
        different failure from having no file at all: the Head answers 401, not the local not-logged-in
        guard. It must remap to an actionable line, never echo the Head's raw 'Unauthorized'."""
        config.SESSION_FILE.write_text("stale-token-the-head-never-issued")
        result = signed_in("whoami")
        assert result.exit_code != 0
        assert "session expired" in result.output
        assert "not logged in" not in result.output   # the wrong guard would fire on an absent file

    def test_logout_revokes_the_session_at_the_head(self, signed_in):
        assert signed_in("logout").exit_code == 0
        assert not config.SESSION_FILE.exists()
        # The point of a logout is the *server* forgetting, not just the client. A second whoami
        # would pass on a stale local token if the Head still honoured the row.
        assert signed_in("whoami").exit_code != 0

    def test_password_change_takes_effect(self, signed_in):
        result = signed_in("passwd", input=f"{USERS['alice']}\nnew-passphrase\nnew-passphrase\n")
        assert result.exit_code == 0, result.output
        config.SESSION_FILE.unlink()
        assert signed_in("login", input="alice\nnew-passphrase\n").exit_code == 0


class TestVersion:
    """Eager, so it answers before login/setup — the moment you ask a hand-carried client what
    it is."""

    def test_version_prints_and_exits(self, cli):
        for flag in ("--version", "-V"):
            r = cli(flag)
            assert r.exit_code == 0, r.output
            assert r.output.strip() == "cuttle 0.0.1"


class TestPasswordPolicy:
    """The Head is authoritative (the Dashboard rides the same route), so the policy is proven
    server-direct; the CLI adds an inline pre-check so a weak choice never round-trips.
    """

    def _token(self, head, user="alice"):
        return head.post("/login", json={"username": user, "password": USERS[user]}).json()["token"]

    def _passwd(self, head, token, old, new):
        return head.post("/passwd", json={"old_password": old, "new_password": new},
                         headers={"Authorization": f"Bearer {token}"})

    # --- server-authoritative ---
    def test_the_head_refuses_a_short_password(self, head):
        r = self._passwd(head, self._token(head), USERS["alice"], "Ab1$")
        assert r.status_code == 422 and "8 characters" in r.json()["detail"]

    def test_the_head_refuses_a_single_character_class(self, head):
        r = self._passwd(head, self._token(head), USERS["alice"], "alllowercase")
        assert r.status_code == 422 and "mix" in r.json()["detail"]

    def test_the_head_refuses_reusing_the_current_password(self, head):
        tok = self._token(head)
        assert self._passwd(head, tok, USERS["alice"], "Str0ngPass").status_code == 200
        r = self._passwd(head, tok, "Str0ngPass", "Str0ngPass")   # now the current one
        assert r.status_code == 422 and "differ" in r.json()["detail"]

    def test_the_head_accepts_a_compliant_password(self, head):
        assert self._passwd(head, self._token(head), USERS["alice"], "Str0ngPass").status_code == 200

    def test_a_wrong_old_password_is_403_before_policy_is_judged(self, head):
        """Old-password check first: a 422 on a wrong old password would tell an attacker which
        candidate new strings are well-formed without ever proving they hold the account."""
        r = self._passwd(head, self._token(head), "wrong-old", "weak")
        assert r.status_code == 403

    # --- CLI pre-check + forced-rotation framing ---
    def test_passwd_reprompts_a_weak_password_inline(self, signed_in):
        result = signed_in("passwd",
                           input=f"{USERS['alice']}\nshort\nshort\nStr0ngPass\nStr0ngPass\n")
        assert result.exit_code == 0, result.output
        assert "at least 8 characters" in result.output

    def test_forced_first_login_is_framed_refuses_the_temp_then_accepts(self, cli):
        with db.connect() as conn:
            temp = db.add_user(conn, "carol")            # must_change=1, random temp password
        # login (carol, temp) -> forced rotation: reuse temp (refused: new==current) -> a strong one.
        result = cli("login", input=f"carol\n{temp}\n{temp}\n{temp}\nStr0ngPass\nStr0ngPass\n")
        assert result.exit_code == 0, result.output
        assert "temporary password" in result.output      # the why-line frames the change
        assert "logged in as carol" in result.output
        # rotation took effect: the temp is dead, the new password logs in.
        assert cli("login", input="carol\nStr0ngPass\n").exit_code == 0
        assert cli("login", input=f"carol\n{temp}\n").exit_code != 0


class TestSubmit:
    """`cuttle run` — the operation the whole cluster exists for."""

    def test_a_spawn_is_submitted_and_attributed(self, signed_in, fake_slurm):
        result = signed_in("run", "train.py", "--name", "a spawn", "--time", "60", "--vram", "24")
        assert result.exit_code == 0, result.output
        assert "submitted spawn" in result.output
        # Attribution is the ledger's, not Slurm's — every Spawn runs as the Mantle, so Slurm's own
        # user field says `ccc` for all of them. If this row is missing the Spawn is unowned.
        with db.connect() as conn:
            alice = db.get_user(conn, "alice")
            spawn = db.get_spawn(conn, _spawn_id(result.output))
        assert spawn is not None, "no Attribution row was recorded"
        assert spawn["user_id"] == alice["id"]

    def test_the_escrow_is_held_at_submit(self, signed_in, fake_slurm):
        """A Spawn that existed without its hold, even briefly, is one its submitter's Share has not
        been charged for — precisely the window a queue-flooder wants."""
        result = signed_in("run", "train.py", "--name", "a spawn", "--time", "60", "--vram", "24")
        with db.connect() as conn:
            escrows = {e["job_id"]: e for e in db.live_escrows(conn)}
        assert _spawn_id(result.output) in escrows

    def test_time_and_vram_are_required(self, signed_in, fake_slurm):
        """Nothing can price a Spawn whose length nobody declared, nor place one whose needs
        nobody did — asserted via the missing flag, not just `exit_code != 0`, so a broken rig
        can't pass silently. Both are promptable, so the refusal is ours, not click's: no
        terminal to ask on, never a quietly-filled default.
        """
        no_time = signed_in("run", "train.py", "--name", "n", "--vram", "24")
        assert no_time.exit_code != 0 and "--time" in no_time.output
        no_vram = signed_in("run", "train.py", "--name", "n", "--time", "60")
        assert no_vram.exit_code != 0 and "--vram" in no_vram.output

    def test_submitting_without_a_session_is_refused(self, cli, fake_slurm):
        result = cli("run", "train.py", "--name", "a spawn", "--time", "60", "--vram", "24")
        assert result.exit_code != 0
        assert "not logged in" in result.output


class TestCancel:
    """`cuttle cancel` — your own over the API, anyone's only on the Head."""

    def _spawn(self, cli):
        return _spawn_id(cli("run", "train.py", "--name", "a spawn", "--time", "60", "--vram", "24").output)

    def test_you_may_cancel_your_own(self, signed_in, fake_slurm, monkeypatch):
        killed = []
        monkeypatch.setattr(slurm, "cancel", lambda jid: killed.append(jid))
        spawn = self._spawn(signed_in)
        result = signed_in("cancel", spawn, "--yes")
        assert result.exit_code == 0, result.output
        assert killed == [spawn]

    def test_the_confirmation_can_be_declined(self, signed_in, fake_slurm, monkeypatch):
        """The Dashboard asks before cancelling; so does this. Declining must not scancel."""
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("must not scancel"))
        spawn = self._spawn(signed_in)
        assert signed_in("cancel", spawn, input="n\n").exit_code != 0

    def test_you_may_not_cancel_someone_elses(self, cli, fake_slurm, monkeypatch):
        """alice submits; root tries to stop it over the API and is refused — an Admin has no
        branch on that route. Off the Head there is no second road either, so this is the end of it."""
        monkeypatch.setattr(menu, "is_head", lambda: False)
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("must not scancel"))
        cli("login", input=f"alice\n{USERS['alice']}\n")
        spawn = self._spawn(cli)
        config.SESSION_FILE.unlink()
        cli("login", input=f"root\n{USERS['root']}\n")
        result = cli("cancel", spawn, "--yes")
        assert result.exit_code != 0
        assert "your own Spawns" in result.output

    def _as_root_on_the_head(self, cli, monkeypatch):
        """Someone else's live Spawn, and an operator standing on the Head looking at it."""
        cli("login", input=f"alice\n{USERS['alice']}\n")
        spawn = self._spawn(cli)
        config.SESSION_FILE.unlink()
        cli("login", input=f"root\n{USERS['root']}\n")
        monkeypatch.setattr(menu, "is_head", lambda: True)
        ran = []
        monkeypatch.setattr(spawns_mod, "run", lambda cmd, **kw: ran.append(cmd))
        return spawn, ran

    def test_on_the_head_someone_elses_takes_the_operator_road(self, cli, fake_slurm,
                                                               monkeypatch):
        """The refusal is the ownership test, not the end. On the Head it selects `sudo scancel` —
        with no flag, because the menu can only ever run this command bare."""
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("the API route must not run"))
        spawn, ran = self._as_root_on_the_head(cli, monkeypatch)
        result = cli("cancel", spawn, "--yes")
        assert result.exit_code == 0, result.output
        assert ran[0] == ["scancel", str(spawn)]
        # The marker DELETE writes has to be written here too, or the Spawn reports itself as having
        # died on its own.
        assert ran[1][:2] == ["-u", config.HEAD_API_USER] and ran[1][2] == "touch"
        assert "Head operator" in result.output

    def test_on_the_head_the_picker_offers_the_whole_queue(self, signed_in, monkeypatch):
        """The bug's real shape: the menu runs `cancel` with no options, so a picker filtered to your
        own Spawns never showed the operator the one they meant to stop."""
        monkeypatch.setattr(menu, "is_head", lambda: True)
        # `ui.need` only reaches a picker when somebody can be asked — the menu's whole situation, and
        # the one CliRunner does not reproduce on its own.
        monkeypatch.setattr(ui, "interactive", lambda: True)
        seen = {}

        def fake_pick(message, mine_only=True):
            seen["mine_only"] = mine_only
            return 424242

        monkeypatch.setattr(pickers, "pick_spawn", fake_pick)
        signed_in("cancel", "--yes")           # the cancel itself 404s; the picker call is the subject
        assert seen["mine_only"] is False

    def test_an_id_the_ledger_never_recorded_is_not_escalated(self, signed_in, monkeypatch):
        """A 404 is not a 403. Escalating one would blind-scancel an arbitrary id — that is what
        `--any` is for, deliberately typed."""
        monkeypatch.setattr(menu, "is_head", lambda: True)
        monkeypatch.setattr(spawns_mod, "run", lambda *a, **kw: pytest.fail("must not scancel"))
        result = signed_in("cancel", "424242", "--yes")
        assert result.exit_code != 0

    def test_any_is_refused_off_the_head(self, signed_in, monkeypatch):
        """The only Head-local admin road that does not go through `auth._admin`, so it needs the
        guard the others get for free."""
        monkeypatch.setattr(menu, "is_head", lambda: False)
        monkeypatch.setattr(spawns_mod, "run", lambda *a, **kw: pytest.fail("must not sudo here"))
        result = signed_in("cancel", "424242", "--any", "--yes")
        assert result.exit_code != 0
        assert "on the Head" in result.output


class TestArmRetraction:
    """`cuttle arm retract`/`return` — the owner's own self-service pause, over the API (not Head-local
    like the rest of `cuttle arm ...`). Mirrors TestCancel's shape: an unconditional ownership check,
    no Admin branch."""

    def _own_gpu_box(self, monkeypatch, owner="alice"):
        monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box", "vram_mb": 24564,
                                                      "admitted": True, "state": "idle",
                                                      "powers": [60.0], "gpu": 1, "owner": owner}])

    def test_you_may_retract_and_return_your_own(self, signed_in, fake_slurm, monkeypatch):
        self._own_gpu_box(monkeypatch)
        result = signed_in("arm", "retract", "gpu-box")
        assert result.exit_code == 0, result.output
        with db.connect() as conn:
            assert db.retracted_nodes(conn) == {"gpu-box"}

        result = signed_in("arm", "return", "gpu-box")
        assert result.exit_code == 0, result.output
        with db.connect() as conn:
            assert db.retracted_nodes(conn) == set()

    def test_you_may_not_retract_someone_elses(self, cli, fake_slurm, monkeypatch):
        """gpu-box is alice's; root tries to retract it over the API and is refused — mirrors
        cancel's unconditional ownership test. Off the Head there is no second road."""
        monkeypatch.setattr(menu, "is_head", lambda: False)
        self._own_gpu_box(monkeypatch, owner="alice")
        cli("login", input=f"root\n{USERS['root']}\n")
        result = cli("arm", "retract", "gpu-box")
        assert result.exit_code != 0
        assert "your own Arm" in result.output

    def test_on_the_head_someone_elses_takes_the_operator_road(self, cli, fake_slurm, monkeypatch):
        """Same shape as cancel's: `pick_arm` has always listed every Arm, so the menu offered the
        operator an Arm and then refused it. On the Head the 403 selects the road instead."""
        self._own_gpu_box(monkeypatch, owner="alice")
        cli("login", input=f"root\n{USERS['root']}\n")
        monkeypatch.setattr(menu, "is_head", lambda: True)
        called = []
        monkeypatch.setattr(auth, "_admin", lambda args, **kw: called.append(list(args)))
        result = cli("arm", "retract", "gpu-box")
        assert result.exit_code == 0, result.output
        assert called == [["retract", "gpu-box"]]
        assert "Head operator" in result.output

        called.clear()
        assert cli("arm", "return", "gpu-box").exit_code == 0
        assert called == [["return", "gpu-box"]]

    def test_any_is_refused_off_the_head(self, cli, fake_slurm, monkeypatch):
        self._own_gpu_box(monkeypatch, owner="alice")
        cli("login", input=f"root\n{USERS['root']}\n")
        monkeypatch.setattr(menu, "is_head", lambda: False)
        monkeypatch.setattr(auth, "_admin", lambda *a, **kw: pytest.fail("must not sudo here"))
        result = cli("arm", "retract", "gpu-box", "--any")
        assert result.exit_code != 0
        assert "on the Head" in result.output
        with db.connect() as conn:
            assert db.retracted_nodes(conn) == set()

    def test_retracting_an_unknown_arm_is_refused(self, signed_in, fake_slurm):
        result = signed_in("arm", "retract", "not-a-real-arm")
        assert result.exit_code != 0
        assert "unknown Arm" in result.output


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
        # `name` here is the SLURM job name, which is the constant `ccc-spawn` in production and is
        # deliberately not what the queue shows — set to something conspicuous so the assertion below
        # can prove the row is titled from the ledger instead.
        monkeypatch.setattr(slurm, "spawns", lambda: [
            {"id": 4207, "name": "ccc-spawn", "state": "running", "node": "gpu-box",
             "gpus": 1, "user": "ccc", "time_limit": 60, "submit_time": 0, "start_time": 0}])
        with db.connect() as conn:
            alice = db.get_user(conn, "alice")
            db.record_spawn(conn, 4207, alice["id"], "train.py", 60, 1, 24, None,
                            name="nightly eval")
        r = signed_in("queue")
        assert r.exit_code == 0, r.output
        # Two things are the point, and both come from head.db rather than from Slurm: the **Name** a
        # person gave the Spawn, and its **Attribution** — `ccc` (Slurm's user) must never be shown.
        assert "4207" in r.output and "nightly eval" in r.output and "alice" in r.output
        assert "ccc-spawn" not in r.output

    def test_queue_json_is_the_raw_rows(self, signed_in, monkeypatch):
        monkeypatch.setattr(slurm, "spawns", lambda: [
            {"id": 4207, "name": "train.py", "state": "running", "node": "gpu-box",
             "gpus": 1, "user": "ccc", "time_limit": 60, "submit_time": 0, "start_time": 0}])
        r = signed_in("queue", "--json")
        assert json.loads(r.output)[0]["id"] == 4207

    def test_queue_without_a_session_is_refused(self, cli):
        r = cli("queue")
        assert r.exit_code != 0 and "not logged in" in r.output


class TestHistory:
    """`cuttle history` — the durable archive, disjoint from the live queue, with cost + outcome. The
    finished rows are seeded straight into head.db (the tick's stamping is covered in test_history.py);
    the live squeue is faked so the exclusion is what's exercised."""

    def _finished(self, job_id, user_id, exit_code, cost=None, owner_id=None, name="nightly eval"):
        with db.connect() as conn:
            db.record_spawn(conn, job_id, user_id, "train.py", 60, 1, 24, "gpu-box",
                            hold=5.0, name=name)
            db.mark_finished(conn, job_id, exit_code, "2026-07-26T12:00:00Z")
            if cost is not None:
                db.record_karma(conn, "settle", cost, user_id, owner_id, job_id=job_id)

    def _alice(self):
        with db.connect() as conn:
            return db.get_user(conn, "alice")["id"]

    def test_empty_history_says_so(self, signed_in, monkeypatch):
        monkeypatch.setattr(slurm, "spawns", lambda: [])
        r = signed_in("history")
        assert r.exit_code == 0, r.output
        assert "no Spawns in the last 30 days" in r.output

    def test_history_shows_cost_and_a_done_outcome(self, signed_in, monkeypatch):
        monkeypatch.setattr(slurm, "spawns", lambda: [])
        self._finished(4207, self._alice(), exit_code=0, cost=3.4, owner_id=self._alice())
        r = signed_in("history")
        assert r.exit_code == 0, r.output
        assert "4207" in r.output and "alice" in r.output
        assert "done" in r.output and "3.4" in r.output

    def test_the_archive_is_titled_by_the_name_not_the_script(self, signed_in, monkeypatch):
        """The SPAWN column carries the **Name** a person gave it, matching `queue`'s — so one Spawn
        reads identically on the live board and in the archive. It used to show the entry script."""
        monkeypatch.setattr(slurm, "spawns", lambda: [])
        # Short enough to survive the SPAWN column's width — the point here is which FIELD
        # the column reads, not how `ui.table` wraps a long one.
        self._finished(4209, self._alice(), exit_code=0, name="sweep3")
        r = signed_in("history")
        assert "sweep3" in r.output
        assert "train.py" not in r.output

    def test_a_nonzero_exit_reads_as_failed(self, signed_in, monkeypatch):
        monkeypatch.setattr(slurm, "spawns", lambda: [])
        self._finished(4208, self._alice(), exit_code=1, cost=1.0, owner_id=self._alice())
        assert "failed (1)" in signed_in("history").output

    def test_history_excludes_the_currently_queued(self, signed_in, monkeypatch):
        """A Spawn still in squeue belongs to `queue`, not `history` — the two views are disjoint."""
        with db.connect() as conn:
            db.record_spawn(conn, 4207, self._alice(), "train.py", 60, 1, 24, "gpu-box",
                            hold=5.0)
        monkeypatch.setattr(slurm, "spawns", lambda: [
            {"id": 4207, "name": "train.py", "state": "running", "node": "gpu-box",
             "gpus": 1, "user": "ccc", "time_limit": 60, "submit_time": 0, "start_time": 0}])
        assert "4207" not in signed_in("history").output

    def test_history_json_carries_cost_and_outcome(self, signed_in, monkeypatch):
        monkeypatch.setattr(slurm, "spawns", lambda: [])
        self._finished(4207, self._alice(), exit_code=0, cost=3.4, owner_id=self._alice())
        row = json.loads(signed_in("history", "--json").output)[0]
        assert row["job_id"] == 4207 and row["cost"] == 3.4 and row["exit_code"] == 0

    def test_history_without_a_session_is_refused(self, cli):
        r = cli("history")
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
    """The binding invariant, checked from the client side: an Admin's Session is worth no more
    over HTTP than any User's. These are the tests that fail loudly if someone re-adds a
    convenience route later."""

    def test_override_is_not_reachable_over_the_api(self, head, signed_in):
        """Head-local only (`cuttle karma override`), not reachable over HTTP. 405 rather than
        404 because the SPA catch-all claims unmatched GETs, so an unknown API path rejects the
        *method* either way.

        What matters more than the status code: no Karma moved. A route that answered 500 while
        still crediting the balance would pass a status-code assertion and be a catastrophe."""
        token = config.SESSION_FILE.read_text().strip()
        r = head.post("/karma/override", json={"username": "alice", "amount": 500},
                      headers={"Authorization": f"Bearer {token}"})
        assert r.status_code in (404, 405)
        with db.connect() as conn:
            alice = db.get_user(conn, "alice")
            # Absent from `balances` == 0, which is where everyone starts.
            assert karma.balances(conn).get(alice["id"], 0.0) == 0.0

    def test_the_endpoint_itself_refuses_a_pin(self, head, signed_in, fake_slurm):
        """Driven at the API, not the CLI: a client that simply stopped sending `node` would make a
        client-side test pass while the endpoint stayed wide open."""
        token = config.SESSION_FILE.read_text().strip()
        r = head.post("/submit",
                      headers={"Authorization": f"Bearer {token}"},
                      # Named and otherwise complete, so the 403 below is unambiguously about the pin
                      # rather than about some earlier gate.
                      json={"script": "t.py", "minutes": 60, "vram": 24, "node": "gpu-box",
                            "name": "a spawn"})
        assert r.status_code == 403
        assert "on the Head" in r.json()["detail"]
