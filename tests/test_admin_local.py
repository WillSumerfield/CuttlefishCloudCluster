"""The Head-local admin commands, where the admin writes went.

`cuttle karma override` and `cuttle run --node` shell into `ccc_head_api.admin` as the `ccc`
account, on the Head, rather than travelling over HTTP. The `sudo` wrapper is untestable here,
but the module underneath is a plain Python entry point — the half that actually mutates
head.db, so the half worth testing.

Without these, moving a capability Head-local would mean replacing a tested API route with an
untested subprocess: the security invariant would hold and the feature would silently not work.
"""


import pytest

from ccc_head_api import admin, db, karma, spawns


@pytest.fixture
def head_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        db.add_user(conn, "alice")
        db.add_user(conn, "root", is_admin=True)
    return tmp_path


def balance(username: str) -> float:
    with db.connect() as conn:
        user = db.get_user(conn, username)
        return karma.balances(conn).get(user["id"], 0.0)


class TestOverride:
    def test_it_credits_a_balance(self, head_db, capsys):
        assert admin.main(["override", "alice", "500"]) == 0
        assert balance("alice") == pytest.approx(500, rel=1e-3)
        assert "alice balance" in capsys.readouterr().out

    def test_it_debits_too(self, head_db):
        admin.main(["override", "alice", "-250"])
        assert balance("alice") == pytest.approx(-250, rel=1e-3)

    def test_an_unknown_user_is_refused(self, head_db, capsys):
        assert admin.main(["override", "nobody", "500"]) == 1
        assert "no such user" in capsys.readouterr().err

    def test_an_admin_is_outside_the_economy(self, head_db, capsys):
        """An Admin has no standing to adjust — Overriding one is a category error, not a balance."""
        assert admin.main(["override", "root", "500"]) == 1
        assert capsys.readouterr().err.strip()


class TestReset:
    """`reset` handles a forgotten password without recreating the account — `remove` + `add`
    would wipe the User's Karma balance and orphan their Arms."""

    def test_it_prints_a_temp_password_that_works(self, head_db, capsys):
        assert admin.main(["reset", "alice"]) == 0
        out = capsys.readouterr().out
        assert "temp password:" in out
        temp_pw = out.splitlines()[1].split(": ", 1)[1]
        with db.connect() as conn:
            user = db.verify_password(conn, "alice", temp_pw)
            assert user is not None
            assert user["must_change_password"]

    def test_it_kills_existing_sessions(self, head_db):
        with db.connect() as conn:
            user = db.get_user(conn, "alice")
            token = db.create_session(conn, user["id"])
            assert db.lookup_session(conn, token) is not None
        admin.main(["reset", "alice"])
        with db.connect() as conn:
            assert db.lookup_session(conn, token) is None

    def test_an_unknown_user_is_refused(self, head_db, capsys):
        assert admin.main(["reset", "nobody"]) == 1
        assert "no such user" in capsys.readouterr().err

    def test_the_old_password_no_longer_works(self, head_db):
        admin.main(["reset", "alice"])
        with db.connect() as conn:
            assert db.get_user(conn, "alice") is not None  # still exists, unlike remove+add
            assert db.verify_password(conn, "alice", "not-the-temp-password") is None


class TestRetractAny:
    """`cuttle arm retract/return --any` — the operator's Head-local escape hatch, which writes head.db
    directly (retraction.retract needs no further privilege) rather than the owner-scoped network
    route. Mirrors TestReset's shape: the sudo wrapper is untestable here, this module underneath it
    is."""

    def test_it_flags_the_arm_retracted(self, head_db, fake_slurm):
        assert admin.main(["retract", "gpu-box"]) == 0
        with db.connect() as conn:
            assert db.retracted_nodes(conn) == {"gpu-box"}

    def test_return_clears_it(self, head_db, fake_slurm):
        admin.main(["retract", "gpu-box"])
        assert admin.main(["return", "gpu-box"]) == 0
        with db.connect() as conn:
            assert db.retracted_nodes(conn) == set()

    def test_an_unknown_arm_is_refused(self, head_db, fake_slurm, capsys):
        assert admin.main(["retract", "not-a-real-arm"]) == 1
        assert "no such Arm" in capsys.readouterr().err


class TestPinnedSubmit:
    """`cuttle run --node`, minus the sudo. The point of routing it through `submit_checked` rather
    than shelling out to sbatch directly is that the ledger still gets written — so that is what
    these check."""

    @pytest.fixture
    def rig(self, head_db, fake_slurm):
        pass

    def test_a_pinned_spawn_is_recorded_and_attributed(self, rig, capsys):
        rc = admin.main(["submit", "--user", "alice", "--script", "train.py",
                         "--minutes", "60", "--vram", "24", "--node", "gpu-box"])
        assert rc == 0
        job_id = int(capsys.readouterr().out.strip())
        with db.connect() as conn:
            alice = db.get_user(conn, "alice")
            spawn = db.get_spawn(conn, job_id)
        assert spawn is not None, "a pinned Spawn must still record its Attribution row"
        assert spawn["user_id"] == alice["id"]
        assert spawn["node"] == "gpu-box"

    def test_a_pinned_spawn_still_holds_escrow(self, rig, capsys):
        """The whole reason this path reuses submit_checked. A Spawn submitted around the pricing
        code would occupy a donated card that nobody is charged for."""
        admin.main(["submit", "--user", "alice", "--script", "train.py",
                    "--minutes", "60", "--vram", "24", "--node", "gpu-box"])
        job_id = int(capsys.readouterr().out.strip())
        with db.connect() as conn:
            assert job_id in {e["job_id"] for e in db.live_escrows(conn)}

    def test_an_unknown_arm_is_refused(self, rig, capsys):
        assert admin.main(["submit", "--user", "alice", "--script", "train.py",
                           "--minutes", "60", "--vram", "24", "--node", "typo"]) == 1
        assert "unknown Arm" in capsys.readouterr().err

    def test_an_unknown_user_is_refused(self, rig, capsys):
        assert admin.main(["submit", "--user", "nobody", "--script", "train.py",
                           "--minutes", "60", "--vram", "24", "--node", "gpu-box"]) == 1
        assert "no such user" in capsys.readouterr().err

    def test_a_pinned_submit_to_a_retracted_arm_is_refused(self, rig, capsys):
        with db.connect() as conn:
            db.mark_arm_retracted(conn, "gpu-box")
        assert admin.main(["submit", "--user", "alice", "--script", "train.py",
                           "--minutes", "60", "--vram", "24", "--node", "gpu-box"]) == 1
        assert "retracted" in capsys.readouterr().err
