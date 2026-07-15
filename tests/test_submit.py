"""Phase 4 slice 0 — what a Spawn must declare, and what only an Admin may.

Two things changed at submit: `--time` and `--vram` became required (nothing can price a Spawn whose
length nobody declared, and nothing can place one whose needs nobody declared), and `--node` became
admin-only.
"""

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ccc_head_api import db
from ccc_head_api import main as head_main
from ccc_head_api import slurm
from ccc_head_api import spawns as head_spawns
from cuttle.main import app as cli_app


class TestRenderedSbatch:
    """The #SBATCH header the Head writes for a Spawn."""

    def render(self, **kw):
        kw.setdefault("minutes", 60)
        kw.setdefault("constraint", "vram24|vram80")
        kw.setdefault("node", None)
        return head_spawns._render(
            uuid="u", staging="/tmp/u", gpu=1, script="train.py", args=[], files=[], **kw)

    def test_time_limit_is_declared(self):
        """A bare integer is minutes to Slurm. Required of every Spawn: the partition is
        MaxTime=INFINITE, so a runaway with no limit would squat on a donor's GPU forever."""
        assert "#SBATCH --time=60" in self.render(minutes=60)

    def test_constraint_carries_the_vram_or_expansion(self):
        assert "#SBATCH --constraint=vram24|vram80" in self.render()

    def test_no_constraint_line_when_empty(self):
        """An empty `#SBATCH --constraint=` would match no node at all, rather than any node."""
        assert "--constraint" not in self.render(constraint="")

    def test_nodelist_only_when_pinned(self):
        assert "#SBATCH --nodelist=box" in self.render(node="box")
        assert "--nodelist" not in self.render(node=None)


class FakeUser(dict):
    pass


def client(monkeypatch, is_admin: bool) -> TestClient:
    """A Head API with a logged-in caller and no Slurm, no disk — but a real (empty) database.

    Every submit quotes its Escrow through karma.quote_hold, which reads the **Tide** from head.db, so
    the store is no longer optional even for an Admin (whose hold is None — but the route still opens the
    connection, exactly as it does in production, where the Head always has head.db). Empty is the
    interesting state: with no Demand history every bucket forecasts 1.0, so the quote must come out
    exactly as it did before the Tide existed.
    """
    user = {"id": 1, "username": "alice", "is_admin": is_admin, "must_change_password": False}
    head_main.app.dependency_overrides[head_main.current_user] = lambda: user
    # A fresh empty db every time. Not optional: the route reads the Tide from head.db even for an Admin
    # (whose hold is None). Tests that need rows (the cancel tests) seed AFTER this, into this same path.
    # Always a temp file, never db.DB_PATH's default — on a live Head that default is a real ccc-owned
    # store this process cannot read.
    monkeypatch.setattr(db, "DB_PATH", Path(tempfile.mkdtemp()) / "head.db")
    db.ensure_schema()
    monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box"}])
    monkeypatch.setattr(slurm, "vram_constraint", lambda gb: "vram24")
    monkeypatch.setattr(head_spawns, "submit", lambda *a, **kw: 4242)
    return TestClient(head_main.app)


def post(c: TestClient, **meta):
    meta.setdefault("script", "train.py")
    return c.post("/submit", content=b"", headers={"X-CCC-Meta": json.dumps(meta)})


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


class TestSubmitRequiresDeclarations:
    def test_happy_path(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24)
        assert r.status_code == 200
        assert r.json() == {"id": 4242}

    @pytest.mark.parametrize("missing", ["minutes", "vram"])
    def test_missing_declaration_is_refused(self, monkeypatch, missing):
        meta = {"minutes": 60, "vram": 24}
        del meta[missing]
        r = post(client(monkeypatch, is_admin=False), **meta)
        assert r.status_code == 422
        assert missing in r.json()["detail"]


class TestNodeIsAdminOnly:
    """A User declares what they NEED and lets Slurm choose. Naming a machine sidesteps Eligibility —
    and with it, the Demand the Spawn should have added to every card that could have run it."""

    def test_non_admin_may_not_pin(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24, node="gpu-box")
        assert r.status_code == 403
        assert "--vram" in r.json()["detail"]  # the refusal points at what they should do instead

    def test_admin_may_pin(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=True), minutes=60, vram=24, node="gpu-box")
        assert r.status_code == 200

    def test_admin_pinning_an_unknown_arm_is_caught_here(self, monkeypatch):
        """We know the real node list; sbatch's own error for a bad --nodelist is opaque."""
        r = post(client(monkeypatch, is_admin=True), minutes=60, vram=24, node="typo")
        assert r.status_code == 422
        assert "gpu-box" in r.json()["detail"]  # ...and we say what the Arms actually are

    def test_gate_is_the_head_not_the_client(self, monkeypatch):
        """The CLI still HAS a --node flag (an admin needs it). Enforcement therefore has to live at
        the Head, since a client cannot be trusted to police itself."""
        c = client(monkeypatch, is_admin=False)
        assert post(c, minutes=60, vram=24, node="gpu-box").status_code == 403


class TestCliRequiresTimeAndVram:
    """No defaults, and no autofill. A `--vram` that defaulted to the fleet's biggest card would make
    every untouched field eligible for that one GPU alone — queued behind everything, at the highest
    Demand, for work that may need 2 GB."""

    def run(self, *args):
        return CliRunner().invoke(cli_app, ["run", *args])

    def test_bare_run_is_refused(self):
        assert self.run("train.py").exit_code != 0

    @pytest.mark.parametrize("args", [
        ["train.py", "--time", "60"],   # no --vram
        ["train.py", "--vram", "24"],   # no --time
    ])
    def test_half_a_declaration_is_refused(self, args):
        r = self.run(*args)
        assert r.exit_code != 0
        assert "Missing option" in r.output


class TestCancel:
    """DELETE /spawns/{id} — the escape that CCC never had. You may end your own; an Admin may end
    anyone's. Attribution is the ledger's (every Spawn runs as the Mantle), so authorization is a row
    lookup, not Slurm's owner field."""

    def _seed(self, owner_id: int):
        """Insert an owner and their Spawn into whatever db `client()` just stood up. Called AFTER the
        client, so it writes to the same DB_PATH the handler will read."""
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                "VALUES (?,?,?,?,0)", (owner_id, f"u{owner_id}", 0, "x"))
            db.record_spawn(conn, job_id=77, user_id=owner_id, script="train.py", staging_uuid="u")
            conn.commit()

    def test_owner_may_cancel(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)  # caller is id 1
        self._seed(owner_id=1)
        killed = []
        monkeypatch.setattr(slurm, "cancel", lambda jid: killed.append(jid))
        r = c.delete("/spawns/77")
        assert r.status_code == 204
        assert killed == [77]

    def test_stranger_may_not_cancel(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)
        self._seed(owner_id=2)  # someone else's Spawn
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("must not scancel"))
        r = c.delete("/spawns/77")
        assert r.status_code == 403

    def test_admin_may_cancel_anyones(self, monkeypatch):
        c = client(monkeypatch, is_admin=True)
        self._seed(owner_id=2)
        killed = []
        monkeypatch.setattr(slurm, "cancel", lambda jid: killed.append(jid))
        r = c.delete("/spawns/77")
        assert r.status_code == 204
        assert killed == [77]

    def test_unknown_spawn_is_404(self, monkeypatch):
        c = client(monkeypatch, is_admin=True)
        self._seed(owner_id=1)
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("must not scancel"))
        r = c.delete("/spawns/999")
        assert r.status_code == 404
