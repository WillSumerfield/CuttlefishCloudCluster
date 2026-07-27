"""Phase 4 slice 0 — what a Spawn must declare, and what only an Admin may.

Two things changed at submit: `--time` and `--vram` became required (nothing can price a Spawn whose
length nobody declared, and nothing can place one whose needs nobody declared), and `--node` became
admin-only.

*(Phase 6 went further: `--node` is now refused over the API for everyone — pinning is a Head-local
command. The tests below were updated to pin that invariant instead.)*
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
    monkeypatch.setattr(slurm, "queue", lambda: [])  # no live Spawns — submit_checked's cap check
    monkeypatch.setattr(head_spawns, "submit", lambda *a, **kw: 4242)
    return TestClient(head_main.app)


def post(c: TestClient, **meta):
    meta.setdefault("script", "train.py")
    return c.post("/api/submit", content=b"", headers={"X-CCC-Meta": json.dumps(meta)})


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


class TestNodeIsNotAvailableOverTheApi:
    """Pinning a Spawn to a named Arm was admin-only over this route until Phase 6 settled that the
    Head API exposes **no admin writes at all**. It is now `cuttle run --node`, on the Head.

    A User declares what they NEED and lets Slurm choose. Naming a machine sidesteps Eligibility — and
    with it, the Demand the Spawn should have added to every card that could have run it.
    """

    def test_non_admin_may_not_pin(self, monkeypatch):
        r = post(client(monkeypatch, is_admin=False), minutes=60, vram=24, node="gpu-box")
        assert r.status_code == 403

    def test_an_admin_may_not_pin_either(self, monkeypatch):
        """The point of the phase. An Admin has no branch here, so a stolen admin Session buys
        nothing a stolen User Session does not."""
        r = post(client(monkeypatch, is_admin=True), minutes=60, vram=24, node="gpu-box")
        assert r.status_code == 403
        assert "on the Head" in r.json()["detail"]  # and it says where the capability went

    def test_a_pin_is_refused_not_ignored(self, monkeypatch):
        """Silently dropping `node` would submit a Spawn that ran somewhere the caller did not ask
        for, and report success — worse than refusing."""
        r = post(client(monkeypatch, is_admin=True), minutes=60, vram=24, node="typo")
        assert r.status_code == 403

    def test_submitting_without_a_pin_still_works(self, monkeypatch):
        """The [U] needs-based path is untouched — only the admin branch was removed."""
        assert post(client(monkeypatch, is_admin=False), minutes=60, vram=24).status_code == 200


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
        """The refusal names the flag that is missing. Since Phase 6 these are promptable, so the
        message is ours rather than click's — but with no terminal to prompt on, half a declaration
        is still refused rather than defaulted."""
        r = self.run(*args)
        assert r.exit_code != 0
        missing = "--vram" if "--time" in args else "--time"
        assert missing in r.output


class TestConcurrentSpawnCap:
    """Escrow prices queue-flooding (Phase 8 loose end — docs/TODO.md's "No rate-limiting anywhere")
    but never hard-stops it; this does. 'Concurrent' is Slurm PENDING or RUNNING — queuing instead of
    running doesn't dodge the cap. Admins are exempt, matching db.spawn_owners' own exemption."""

    def _seed_owned_spawns(self, user_id: int, job_ids: list[int]):
        """Insert a user + one Spawn row per job_id, all attributed to `user_id`. Called AFTER
        `client()`, so it writes to the same DB_PATH the handler will read (see TestCancel._seed)."""
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO users (id, username, is_admin, pw_hash, must_change_password) "
                "VALUES (?,?,?,?,0)", (user_id, f"u{user_id}", 0, "x"))
            for jid in job_ids:
                db.record_spawn(conn, job_id=jid, user_id=user_id, script="train.py",
                                staging_uuid=f"u{jid}")
            conn.commit()

    def test_at_the_cap_is_refused(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)  # caller is id 1
        job_ids = list(range(1, head_spawns.MAX_CONCURRENT_SPAWNS + 1))
        self._seed_owned_spawns(1, job_ids)
        monkeypatch.setattr(slurm, "queue", lambda: [{"id": j} for j in job_ids])
        r = post(c, minutes=60, vram=24)
        assert r.status_code == 422
        assert "queued/running" in r.json()["detail"]

    def test_one_under_the_cap_still_works(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)
        job_ids = list(range(1, head_spawns.MAX_CONCURRENT_SPAWNS))  # one short of the cap
        self._seed_owned_spawns(1, job_ids)
        monkeypatch.setattr(slurm, "queue", lambda: [{"id": j} for j in job_ids])
        assert post(c, minutes=60, vram=24).status_code == 200

    def test_someone_elses_active_spawns_dont_count(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)  # caller is id 1
        job_ids = list(range(1, head_spawns.MAX_CONCURRENT_SPAWNS + 5))
        self._seed_owned_spawns(2, job_ids)  # all owned by a different user
        monkeypatch.setattr(slurm, "queue", lambda: [{"id": j} for j in job_ids])
        assert post(c, minutes=60, vram=24).status_code == 200

    def test_an_admin_is_exempt(self, monkeypatch):
        c = client(monkeypatch, is_admin=True)  # caller is id 1
        job_ids = list(range(1, head_spawns.MAX_CONCURRENT_SPAWNS + 5))
        self._seed_owned_spawns(1, job_ids)
        monkeypatch.setattr(slurm, "queue", lambda: [{"id": j} for j in job_ids])
        assert post(c, minutes=60, vram=24).status_code == 200


class TestCancel:
    """DELETE /spawns/{id} — the escape that CCC never had. You may end **your own**, and only your
    own; cancel-anyone moved to the Head in Phase 6. Attribution is the ledger's (every Spawn runs as
    the Mantle), so authorization is a row lookup, not Slurm's owner field."""

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
        r = c.delete("/api/spawns/77")
        assert r.status_code == 204
        assert killed == [77]

    def test_stranger_may_not_cancel(self, monkeypatch):
        c = client(monkeypatch, is_admin=False)
        self._seed(owner_id=2)  # someone else's Spawn
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("must not scancel"))
        r = c.delete("/api/spawns/77")
        assert r.status_code == 403

    def test_an_admin_may_not_cancel_anyones_over_the_api(self, monkeypatch):
        """Cancel-anyone was an admin branch on this route until Phase 6 removed it: it is now
        `cuttle cancel <id> --any`, on the Head. The ownership test is unconditional, so admin or
        not, a Session can only stop the Spawns it owns."""
        c = client(monkeypatch, is_admin=True)
        self._seed(owner_id=2)
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("must not scancel"))
        r = c.delete("/api/spawns/77")
        assert r.status_code == 403

    def test_unknown_spawn_is_404(self, monkeypatch):
        c = client(monkeypatch, is_admin=True)
        self._seed(owner_id=1)
        monkeypatch.setattr(slurm, "cancel", lambda jid: pytest.fail("must not scancel"))
        r = c.delete("/api/spawns/999")
        assert r.status_code == 404


class TestLogStreams:
    """`/logs` serves stdout, `/stderr` serves stderr — separate files on disk (spawns.py writes both).
    A Spawn that dies on a traceback leaves `out` empty and the cause in `err`; before /stderr existed
    that cause was unreachable from every client. A stream with no file yet reads as empty, not 404."""

    def _spawn_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path)
        d = tmp_path / "77"
        d.mkdir()
        return d

    def test_logs_serves_stdout_stderr_serves_stderr(self, monkeypatch, tmp_path):
        c = client(monkeypatch, is_admin=False)
        d = self._spawn_dir(monkeypatch, tmp_path)
        (d / "out").write_text("epoch 1 loss=2.3\n")
        (d / "err").write_text("Traceback (most recent call last)\n")
        assert c.get("/api/spawns/77/logs").text == "epoch 1 loss=2.3\n"
        assert c.get("/api/spawns/77/stderr").text == "Traceback (most recent call last)\n"

    def test_a_crash_leaves_the_cause_only_in_stderr(self, monkeypatch, tmp_path):
        """The reported bug: out empty + FAILED, cause on disk. /stderr is the reach that was missing."""
        c = client(monkeypatch, is_admin=False)
        d = self._spawn_dir(monkeypatch, tmp_path)
        (d / "out").write_text("")
        (d / "err").write_text("RuntimeError: CUDA out of memory\n")
        assert c.get("/api/spawns/77/logs").text == ""
        assert "CUDA out of memory" in c.get("/api/spawns/77/stderr").text

    def test_a_missing_stream_is_empty_not_404(self, monkeypatch, tmp_path):
        c = client(monkeypatch, is_admin=False)
        self._spawn_dir(monkeypatch, tmp_path)   # dir exists, neither file written yet
        assert c.get("/api/spawns/77/logs").status_code == 200
        assert c.get("/api/spawns/77/stderr").status_code == 200
        assert c.get("/api/spawns/77/stderr").text == ""
