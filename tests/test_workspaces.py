"""Workspaces — the durable form of a Tide Pool, and the one Allowance its holder divides up.

What's tested here: that a Workspace reserves a **Storage** out of a 32GB pool at creation and that the
pool is a hard refusal rather than an eviction; that usage is read off the disk rather than a stored
figure (so a mirror the Head never watched still counts); that a Storage may be resized but never below
what the tree already holds; that a visit claim can be taken exactly once and is reconciled by the tick;
that deleting is refused while a visit is live; and that the download streams rather than buffers.

The visit itself — the Siphon, the job script, the mirror — is Phase 3 and is not tested here.
"""

import io
import json
import tarfile
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db, slurm, workspaces
from ccc_head_api import environments as env
from ccc_head_api import main as head_main
from ccc_head_api import spawns as head_spawns

GIB = 1024 ** 3


def _env_block(drop_lock=""):
    """The %%ENV_BUILD%% text a visit's job script carries, for the parts that depend on the lock."""
    m = env.Manifest(kind="uv-project", path="pyproject.toml")
    return head_spawns._render_env_build(m, Path("/tmp/ws"), [], [], [], [], drop_lock)

_META = {"name": "scratch", "storage_bytes": 4 * GIB, "gpus": 1, "vram_mb": 24576, "minutes": 120,
         # Both optional for a Workspace, exactly as they are for the Tide Pool a visit is — carried in
         # the default meta so the common tests exercise the shape a real client sends.
         "manifest": "requirements.txt", "outputs": ["checkpoints"], "features": []}


def _targz(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _seed(payload_bytes: int = 64) -> bytes:
    return _targz({"train.py": b"print('hi')\n", "requirements.txt": b"",
                   "data.bin": b"x" * payload_bytes})


def _spool_body(body: bytes) -> Path:
    with tempfile.NamedTemporaryFile("wb", suffix=".tar.gz", delete=False) as f:
        f.write(body)
        return Path(f.name)


class _Base:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
        db.ensure_schema()
        monkeypatch.setattr(head_spawns, "HEAD_STATE_DIR", tmp_path)
        with db.connect() as conn:
            db.add_user(conn, "alice")
            db.add_user(conn, "bob")

    def _create(self, user_id=1, tar=None, **over):
        meta = {**_META, **over}
        path = _spool_body(tar if tar is not None else _seed())
        with db.connect() as conn:
            return workspaces.create(conn, user_id, meta, path)


class TestCreating(_Base):
    def test_a_workspace_holds_the_tree_and_the_declaration(self):
        made = self._create()
        assert made["name"] == "scratch"
        assert made["storage_bytes"] == 4 * GIB
        assert (made["gpus"], made["vram_mb"], made["minutes"]) == (1, 24576, 120)
        assert made["manifest"] == "requirements.txt"
        assert made["outputs"] == ["checkpoints"]
        assert (workspaces.dir_of(1, "scratch") / "train.py").read_bytes() == b"print('hi')\n"

    def test_a_new_workspace_is_idle(self):
        assert self._create()["live"] is None

    def test_creating_runs_nothing(self):
        """Holding a Workspace costs no Karma — storage never does. Visiting it will, because a visit
        IS a Tide Pool."""
        self._create()
        with db.connect() as conn:
            assert db.live_escrows(conn) == []

    def test_the_seed_is_the_only_upload(self):
        """There is no update-the-code sibling of `create`: everything after the seed is written from
        inside the Workspace's own sessions. Asserted structurally so adding one is a deliberate act."""
        assert not hasattr(workspaces, "save")

    def test_a_second_workspace_of_the_same_name_is_refused(self):
        self._create()
        with pytest.raises(ValueError, match="already have a workspace"):
            self._create()

    def test_two_users_may_both_hold_one_name(self):
        self._create(user_id=1)
        self._create(user_id=2)
        with db.connect() as conn:
            assert db.get_workspace(conn, 1, "scratch") is not None
            assert db.get_workspace(conn, 2, "scratch") is not None

    @pytest.mark.parametrize("name", ["../escape", "a/b", ".hidden", "", "x" * 65])
    def test_a_name_that_is_not_a_directory_component_is_refused(self, name):
        with pytest.raises(ValueError, match="invalid workspace name"):
            self._create(name=name)

    def test_a_workspace_with_no_storage_is_refused(self):
        """The Storage is the whole point: it is what its owner divides out of their Allowance, and there
        is no default that could be right for them."""
        with pytest.raises(ValueError, match="storage_bytes"):
            self._create(storage_bytes=0)

    def test_time_and_vram_are_still_required(self):
        for field in ("minutes", "vram_mb"):
            with pytest.raises(ValueError, match=field):
                self._create(**{field: 0})

    def test_a_manifest_and_outputs_are_optional(self):
        """Optional exactly as they are for a Tide Pool — a Workspace opened to poke at a box has no
        environment to declare and nowhere it wants results sent."""
        made = self._create(manifest="", outputs=[])
        assert made["manifest"] == "" and made["outputs"] == []

    def test_a_manifest_not_in_the_seed_is_refused_before_anything_is_written(self):
        with pytest.raises(ValueError, match="manifest"):
            self._create(manifest="pyproject.toml")
        assert not workspaces.dir_of(1, "scratch").exists()

    def test_an_output_path_a_spawn_would_reject_is_refused_here_too(self):
        with pytest.raises(ValueError, match="invalid output path"):
            self._create(outputs=["../etc"])

    def test_a_nested_output_path_is_fine(self):
        """An output is a path in the tree, so a folder inside a folder is an ordinary thing to name."""
        self._create(outputs=["results/ckpt"])


class TestThePool(_Base):
    def test_the_pool_is_the_sum_of_the_storage_reserved(self, monkeypatch):
        """Reserved, not used: an empty 4GB Workspace costs its owner the whole 4GB."""
        monkeypatch.setattr(workspaces, "MAX_USER_BYTES", 10 * GIB)
        self._create(name="a", storage_bytes=4 * GIB)
        self._create(name="b", storage_bytes=4 * GIB)
        with db.connect() as conn:
            assert db.user_workspace_storage(conn, 1) == 8 * GIB

    def test_a_workspace_past_the_pool_is_refused(self, monkeypatch):
        monkeypatch.setattr(workspaces, "MAX_USER_BYTES", 10 * GIB)
        self._create(name="a", storage_bytes=8 * GIB)
        with pytest.raises(workspaces.PoolExceeded):
            self._create(name="b", storage_bytes=4 * GIB)

    def test_a_refused_creation_evicts_nothing_and_writes_nothing(self, monkeypatch):
        """A hard refusal, never the silent eviction `retention.py` does — the User divides this
        Allowance up themselves, so making room is theirs to do."""
        monkeypatch.setattr(workspaces, "MAX_USER_BYTES", 10 * GIB)
        self._create(name="a", storage_bytes=8 * GIB)
        with pytest.raises(workspaces.PoolExceeded):
            self._create(name="b", storage_bytes=4 * GIB)
        assert workspaces.dir_of(1, "a").exists()
        assert not workspaces.dir_of(1, "b").exists()

    def test_the_pool_is_per_user(self, monkeypatch):
        monkeypatch.setattr(workspaces, "MAX_USER_BYTES", 10 * GIB)
        self._create(user_id=1, storage_bytes=8 * GIB)
        self._create(user_id=2, storage_bytes=8 * GIB)   # bob's own pool, untouched by alice's

    def test_deleting_frees_the_storage_back(self, monkeypatch):
        monkeypatch.setattr(workspaces, "MAX_USER_BYTES", 10 * GIB)
        self._create(name="a", storage_bytes=8 * GIB)
        with db.connect() as conn:
            workspaces.delete(conn, 1, "a")
        self._create(name="b", storage_bytes=8 * GIB)

    def test_a_seed_larger_than_the_storage_is_refused_separately(self):
        """Distinct from the pool because the remedy is: ask for a bigger Storage, not delete another
        Workspace."""
        with pytest.raises(workspaces.SeedTooBig):
            self._create(storage_bytes=1000, tar=_seed(payload_bytes=10_000))
        assert not workspaces.dir_of(1, "scratch").exists()


class TestUsage(_Base):
    def test_usage_is_read_off_the_disk(self):
        """Never a stored figure: a Workspace's tree changes every 60 seconds from inside a live
        session, over rsyncd, with nothing on the Head watching it happen."""
        self._create()
        before = workspaces.usage(1, "scratch")
        (workspaces.dir_of(1, "scratch") / "grown.bin").write_bytes(b"y" * 5_000)
        assert workspaces.usage(1, "scratch") == before + 5_000

    def test_a_deletion_inside_the_tree_is_seen_too(self):
        self._create(tar=_seed(payload_bytes=10_000))
        (workspaces.dir_of(1, "scratch") / "data.bin").unlink()
        assert workspaces.usage(1, "scratch") < 1_000

    def test_a_symlink_out_of_the_tree_counts_as_a_link_not_its_target(self, tmp_path):
        """A Workspace is the one tree in CCC its owner can put arbitrary symlinks in. Following one
        would let a link to a huge file report a Storage nobody is holding."""
        self._create()
        big = tmp_path / "big.bin"
        big.write_bytes(b"z" * 100_000)
        (workspaces.dir_of(1, "scratch") / "link").symlink_to(big)
        assert workspaces.usage(1, "scratch") < 10_000

    def test_a_missing_tree_is_zero_rather_than_an_error(self):
        assert workspaces.usage(1, "never-made") == 0


class TestResizing(_Base):
    def _update(self, name="scratch", user_id=1, **over):
        with db.connect() as conn:
            return workspaces.update_meta(conn, user_id, name, {**_META, **over})

    def test_a_storage_can_be_grown(self, monkeypatch):
        monkeypatch.setattr(workspaces, "MAX_USER_BYTES", 32 * GIB)
        self._create()
        assert self._update(storage_bytes=8 * GIB)["storage_bytes"] == 8 * GIB

    def test_growing_past_the_pool_is_refused(self, monkeypatch):
        monkeypatch.setattr(workspaces, "MAX_USER_BYTES", 10 * GIB)
        self._create(name="a", storage_bytes=4 * GIB)
        self._create(name="b", storage_bytes=4 * GIB)
        with pytest.raises(workspaces.PoolExceeded):
            self._update(name="a", storage_bytes=8 * GIB)

    def test_a_resize_is_weighed_on_what_it_grows_by(self, monkeypatch):
        """This Workspace's own reservation comes out of the pool before the new one is weighed — the
        same delta arithmetic an updated Recipe gets."""
        monkeypatch.setattr(workspaces, "MAX_USER_BYTES", 10 * GIB)
        self._create(name="a", storage_bytes=4 * GIB)
        self._create(name="b", storage_bytes=4 * GIB)
        assert self._update(name="a", storage_bytes=6 * GIB)["storage_bytes"] == 6 * GIB

    def test_shrinking_below_what_the_tree_holds_is_refused(self):
        self._create(tar=_seed(payload_bytes=10_000))
        with pytest.raises(workspaces.StorageTooSmall):
            self._update(storage_bytes=1_000)

    def test_a_refused_shrink_changes_nothing(self):
        self._create(tar=_seed(payload_bytes=10_000))
        with pytest.raises(workspaces.StorageTooSmall):
            self._update(storage_bytes=1_000)
        with db.connect() as conn:
            assert db.get_workspace(conn, 1, "scratch")["storage_bytes"] == 4 * GIB

    def test_shrinking_to_exactly_what_is_held_is_allowed(self):
        self._create(tar=_seed(payload_bytes=10_000))
        held = workspaces.usage(1, "scratch")
        assert self._update(storage_bytes=held)["storage_bytes"] == held

    def test_a_manifest_not_in_the_tree_is_refused(self):
        self._create()
        with pytest.raises(ValueError, match="manifest"):
            self._update(manifest="pyproject.toml")

    def test_the_manifest_may_be_dropped_entirely(self):
        self._create()
        assert self._update(manifest="")["manifest"] == ""

    def test_updating_one_that_does_not_exist_is_a_miss(self):
        with pytest.raises(ValueError, match="no workspace named"):
            self._update(name="ghost")

    def test_a_resize_during_a_live_visit_is_accepted(self):
        """It takes effect on the NEXT visit — the running session's gate is a constant baked into its
        job script, and moving it under a session mid-write is the one thing a resize must not do."""
        self._create()
        with db.connect() as conn:
            db.claim_workspace(conn, 1, 4242)
        assert self._update(storage_bytes=8 * GIB)["storage_bytes"] == 8 * GIB


class TestTheVisitClaim(_Base):
    def test_a_claim_is_taken_exactly_once(self):
        """Two simultaneous opens must not both submit and then both --delete-mirror one tree from
        different Arms. The loser attaches to the winner's session instead."""
        self._create()
        with db.connect() as conn:
            assert db.claim_workspace(conn, 1, 100) is True
            assert db.claim_workspace(conn, 1, 200) is False
            assert db.get_workspace(conn, 1, "scratch")["live_job_id"] == 100

    def test_releasing_names_the_spawn_that_holds_it(self):
        """A stale release — a submit that failed after claiming, racing a tick that already cleared it
        and an open that re-claimed — must not free somebody else's live visit."""
        self._create()
        with db.connect() as conn:
            db.claim_workspace(conn, 1, 100)
            db.release_workspace(conn, 1, 999)
            assert db.get_workspace(conn, 1, "scratch")["live_job_id"] == 100
            db.release_workspace(conn, 1, 100)
            assert db.get_workspace(conn, 1, "scratch")["live_job_id"] is None

    def test_the_tick_clears_a_claim_whose_spawn_has_left_the_queue(self):
        self._create()
        with db.connect() as conn:
            db.claim_workspace(conn, 1, 100)
            assert workspaces.reconcile(conn, [100]) == 0        # still running
            assert workspaces.reconcile(conn, [55]) == 1         # gone
            assert db.get_workspace(conn, 1, "scratch")["live_job_id"] is None

    def test_an_empty_queue_clears_every_claim(self):
        """A Head restart, or a Slurm that lost the lot: an empty queue means no visit is live, and a
        claim nobody cleared is a Workspace its owner can never open again."""
        self._create()
        with db.connect() as conn:
            db.claim_workspace(conn, 1, 100)
            assert workspaces.reconcile(conn, []) == 1


class TestDeleting(_Base):
    def test_delete_takes_the_row_and_the_tree(self):
        self._create()
        with db.connect() as conn:
            assert workspaces.delete(conn, 1, "scratch") is True
            assert db.get_workspace(conn, 1, "scratch") is None
        assert not workspaces.dir_of(1, "scratch").exists()

    def test_deleting_is_refused_while_a_visit_is_live(self):
        """The tree is what that session is mirroring into — deleting it would race a writer that
        recreates parts of it seconds later."""
        self._create()
        with db.connect() as conn:
            db.claim_workspace(conn, 1, 4242)
            with pytest.raises(workspaces.Live):
                workspaces.delete(conn, 1, "scratch")
        assert workspaces.dir_of(1, "scratch").exists()

    def test_deleting_one_that_is_not_there_is_false(self):
        with db.connect() as conn:
            assert workspaces.delete(conn, 1, "ghost") is False


class TestDeletedUsers(_Base):
    def test_the_rows_cascade_and_the_sweep_takes_the_files(self):
        self._create(user_id=2)
        with db.connect() as conn:
            conn.execute("DELETE FROM users WHERE id=2")
            conn.commit()
            assert workspaces.sweep_orphans(conn) == 1
        assert not workspaces.dir_of(2, "scratch").exists()

    def test_the_sweep_leaves_living_users_alone(self):
        self._create(user_id=1)
        with db.connect() as conn:
            assert workspaces.sweep_orphans(conn) == 0
        assert workspaces.dir_of(1, "scratch").exists()

    def test_the_sweep_is_quiet_when_nobody_has_ever_made_one(self):
        with db.connect() as conn:
            assert workspaces.sweep_orphans(conn) == 0


class TestTheRenderedVisit:
    """What a visit's job script says — the Siphon it draws from, how its Outputs go home, and the mirror.

    Rendered directly rather than submitted: placement, pricing and sbatch are `submit_checked`'s own
    tests, and what belongs here is that a visit's script differs from an ordinary Spawn's in exactly
    the ways it is supposed to.
    """

    def _render(self, tmp_path, **over):
        kwargs = dict(staging=tmp_path / "projects" / "abc123", gpu=1, script="", args=[], files=[],
                      outputs=[], node=None, minutes=60, constraint="vram24", token="TOK",
                      secret="SEC", tidepool_port=2200, tidepool_pubkey="ssh-ed25519 AAA",
                      workspace=tmp_path / "ws" / "scratch", workspace_storage=4 * GIB)
        return head_spawns._render(**{**kwargs, **over})

    def test_the_staging_dir_renders_empty_so_the_remote_branch_is_forced(self, tmp_path):
        """`[ -d "" ]` is false everywhere, including on the Head's own co-located Arm — so there is one
        mirror implementation rather than two, and durable writes land as the Mantle."""
        assert 'staging_dir=""' in self._render(tmp_path)

    def test_an_ordinary_spawn_still_names_its_staging_dir(self, tmp_path):
        body = self._render(tmp_path, workspace=None, workspace_storage=0)
        assert 'staging_dir=""' not in body
        assert "abc123" in body

    def test_the_stage_in_is_a_whole_module_delete_mirror(self, tmp_path):
        body = self._render(tmp_path)
        assert 'rsync -a --delete "rsync://TOK@' in body
        assert "/ws-TOK/" in body
        assert "--files-from" not in body.split("ws_staged=1")[0].split("if ! ")[-1]

    def test_an_ordinary_spawn_still_stages_in_from_its_proj_module(self, tmp_path):
        body = self._render(tmp_path, workspace=None, workspace_storage=0)
        assert "/proj-TOK/" in body and "--files-from" in body

    def test_a_visit_binds_nothing_and_pushes_the_declared_path_itself(self, tmp_path):
        """$proj here IS the durable tree, so unlike every other Spawn nothing is bound over it. A bind
        would keep the bytes in a directory of CCC's own and leave an empty mountpoint behind in what the
        owner gets back — waiting on every future visit and in `workspace download`. The declared folder
        goes to [out-…] as a second DESTINATION rather than a second copy, so it costs no Storage."""
        body = self._render(tmp_path, outputs=["results"])
        assert 'ccc_out="$proj/outputs"' not in body
        assert 'outbind="$outbind' not in body
        assert 'ws_outputs="$proj/./results"' in body
        assert "rsync -aR $ws_outputs" in body

    def test_a_visit_that_declared_no_output_pushes_nothing(self, tmp_path):
        """Declaring is optional here (CONTEXT.md) — the tree is kept either way."""
        assert 'ws_outputs=""' in self._render(tmp_path)

    def test_declared_outputs_go_home_even_when_the_storage_is_full(self, tmp_path):
        """The two ceilings are different Allowances: results are charged to the output Allowance, not to
        this Workspace's Storage, so a tree too big to mirror is no reason to strand what the session was
        run to produce."""
        mirror = self._render(tmp_path, outputs=["results"]).rsplit(
            "ws_mirror() {", 1)[1].split("\n    }", 1)[0]
        assert mirror.index("ws_outputs") < mirror.index('[ "$over" = 0 ] || return 0')

    def test_the_storage_is_baked_in_as_a_constant(self, tmp_path):
        """No cross-Workspace interaction to get wrong, and a resize mid-visit cannot move it under a
        session that is already writing."""
        assert f"ws_storage={4 * GIB}" in self._render(tmp_path)

    def test_the_mirror_is_gated_on_stage_in_alone(self, tmp_path):
        """Deliberately NOT also on non-empty: `rm -rf *` inside a session has to reach the stored tree,
        or emptying a Workspace silently does nothing and the files return on the next visit."""
        body = self._render(tmp_path)
        # rsplit: the no-op stub near the top of the script is also spelled `ws_mirror() {`.
        mirror = body.rsplit("ws_mirror() {", 1)[1].split("\n    }", 1)[0]
        assert '[ "$ws_staged" = 1 ] || return 0' in mirror
        assert "ls -A" not in mirror

    def test_the_mirror_runs_from_the_exit_trap_and_a_loop(self, tmp_path):
        body = self._render(tmp_path)
        assert "ws_mirror; recover_scratch" in body       # the trap's final sync
        assert "sleep 60; ws_mirror" in body              # and the loop behind it

    def test_there_is_no_recovery_window_for_a_visit(self, tmp_path):
        """$proj here IS the durable tree, already mirrored home — a day-long copy of it would be a
        second copy of the thing that was already saved."""
        assert "recover_scratch() { :; }" in self._render(tmp_path)

    def test_an_ordinary_spawn_defines_a_real_recover_scratch(self, tmp_path):
        assert "recover_scratch() { :; }" not in self._render(tmp_path, workspace=None)

    def test_an_ordinary_spawn_gets_no_workspace_block_at_all(self, tmp_path):
        body = self._render(tmp_path, workspace=None, workspace_storage=0)
        assert "ws_staged" not in body and "ws_loop() {" not in body
        # The no-op stub and the empty pid DO stay: an UNDEFINED ws_mirror would write bash's own
        # "command not found" into $sink, which is what leaves a file in /tmp for every Spawn ever run.
        assert "ws_mirror() { :; }" in body and 'ws_loop_pid=""' in body

    def test_over_its_storage_the_sync_is_skipped_and_nothing_is_evicted(self, tmp_path):
        """Nothing is deleted to make room — the session keeps working, it just stops being saved. The
        marker is what tells the Head (and so the User, mid-session) that this is happening."""
        mirror = self._render(tmp_path).rsplit("ws_mirror() {", 1)[1].split("\n    }", 1)[0]
        assert 'echo "$over" > "$scratch/workspace.full"' in mirror
        assert '[ "$over" = 0 ] || return 0' in mirror
        # The refusal is a skipped rsync and nothing else: no rm, no eviction, no truncation.
        assert "rm -" not in mirror

    def test_the_marker_is_written_every_pass_so_full_can_be_recovered_from(self, tmp_path):
        """0 when under. Without that the Head would keep reading a stale 'full' back after the User
        had already freed the space."""
        mirror = self._render(tmp_path).rsplit("ws_mirror() {", 1)[1].split("\n    }", 1)[0]
        assert "over=0" in mirror

    def test_the_pusher_is_never_restarted_because_ccc_out_never_moves(self, tmp_path):
        """It used to be: WS_BLOCK reassigned $ccc_out into the tree, and `push_periodic &` had already
        forked with the old value. A visit binds nothing now and $ccc_out is never reassigned, so the
        original loop stays correct and the restart it needed is gone."""
        block = self._render(tmp_path).split("ws_staged=1", 1)[1]
        assert "push_periodic &" not in block


class TestTheSiphon:
    """`[ws-<tok>]` — writable, rooted at the tree, serving both directions."""

    @pytest.fixture(autouse=True)
    def _rsyncd(self, monkeypatch, tmp_path):
        from ccc_head_api import siphons
        monkeypatch.setattr(siphons, "RSYNCD_DIR", tmp_path / "rsyncd.d")
        monkeypatch.setattr(head_spawns, "HEAD_STATE_DIR", tmp_path)
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")

    def test_the_module_is_writable_and_rooted_at_the_tree(self, tmp_path):
        from ccc_head_api import siphons
        tok, _ = siphons.open_ws(tmp_path / "ws" / "scratch")
        conf = (tmp_path / "rsyncd.d" / f"{tok}.conf").read_text()
        assert f"[ws-{tok}]" in conf
        assert "read only = false" in conf
        assert str(tmp_path / "ws" / "scratch") in conf

    def test_bind_keeps_the_ws_module_writable_and_adds_the_out_half(self, tmp_path):
        """`bind` re-renders from the template the source module names, so a writable module can never
        come back read-only."""
        from ccc_head_api import siphons
        tok, _ = siphons.open_ws(tmp_path / "ws" / "scratch")
        siphons.bind(tok, 77)
        conf = (tmp_path / "rsyncd.d" / "77.conf").read_text()
        assert f"[ws-{tok}]" in conf and f"[out-{tok}]" in conf
        assert conf.count("read only = false") == 2

    def test_binding_a_proj_siphon_is_unchanged(self, tmp_path):
        from ccc_head_api import siphons
        tok, _ = siphons.open("uuid-1")
        siphons.bind(tok, 78)
        conf = (tmp_path / "rsyncd.d" / "78.conf").read_text()
        assert f"[proj-{tok}]" in conf and f"[out-{tok}]" in conf
        assert conf.count("read only = false") == 1     # the out half only

    def test_stage_in_close_leaves_the_ws_module_standing(self, tmp_path):
        """`close_proj` revokes a Spawn's read of its own upload at stage-in. A visit has no upload, and
        its module is what the session mirrors into for its whole life."""
        from ccc_head_api import siphons
        tok, _ = siphons.open_ws(tmp_path / "ws" / "scratch")
        siphons.bind(tok, 79)
        siphons.close_proj(79)
        assert f"[ws-{tok}]" in (tmp_path / "rsyncd.d" / "79.conf").read_text()


class TestOpening(_Base):
    """Submit-or-attach, and the claim that decides which."""

    @pytest.fixture(autouse=True)
    def _submitted(self, monkeypatch):
        self.calls = []

        def fake(*a, **kw):
            self.calls.append(kw)
            return 4242
        monkeypatch.setattr(head_spawns, "submit_checked", fake)

    def _open(self, name="scratch", user_id=1, **over):
        with db.connect() as conn:
            return workspaces.visit(conn, user_id, name, False, over)

    def test_opening_submits_a_tide_pool_bound_to_the_tree(self):
        self._create()
        job_id, attached = self._open()
        assert (job_id, attached) == (4242, False)
        kw = self.calls[0]
        assert kw["tidepool"] is True
        assert kw["workspace"] == workspaces.dir_of(1, "scratch")
        assert kw["workspace_storage"] == 4 * GIB
        # INHERITED from the Workspace, like a Recipe run's. A visit carries no script either, so
        # without this it was the one Spawn the archive had nothing at all to title its row with.
        assert kw["name"] == "scratch"

    def test_the_declaration_is_what_a_visit_asks_for(self):
        self._create()
        self._open()
        kw = self.calls[0]
        assert kw["manifest"] == "requirements.txt"
        assert kw["features"] == []

    def test_overrides_change_this_visit_only(self):
        self._create()
        self._open(minutes=15)
        with db.connect() as conn:
            assert db.get_workspace(conn, 1, "scratch")["minutes"] == 120

    def test_a_second_open_attaches_rather_than_submitting(self):
        """Two sessions --delete-mirroring one tree from different Arms is the failure the claim
        exists to prevent."""
        self._create()
        self._open()
        assert self._open() == (4242, True)
        assert len(self.calls) == 1

    def test_the_claim_is_taken_before_sbatch(self):
        """sbatch is what mints an id, so claiming afterwards would let two opens both submit."""
        self._create()
        seen = []

        def fake(*a, **kw):
            with db.connect() as conn:
                seen.append(db.get_workspace(conn, 1, "scratch")["live_job_id"])
            return 4242
        head_spawns.submit_checked = fake
        self._open()
        assert seen == [db.SUBMITTING]

    def test_a_failed_submit_releases_the_claim(self):
        self._create()

        def boom(*a, **kw):
            raise ValueError("no Arm has that much VRAM")
        head_spawns.submit_checked = boom
        with pytest.raises(ValueError):
            self._open()
        with db.connect() as conn:
            assert db.get_workspace(conn, 1, "scratch")["live_job_id"] is None

    def test_an_in_flight_claim_survives_a_tick(self):
        """A tick landing between the claim and sbatch must not free a Workspace out from under a
        submission that is about to succeed."""
        self._create()
        with db.connect() as conn:
            db.claim_workspace(conn, 1)
            assert workspaces.reconcile(conn, []) == 0
            assert db.get_workspace(conn, 1, "scratch")["live_job_id"] == db.SUBMITTING

    def test_a_claim_left_behind_by_a_dead_submit_is_collected(self):
        self._create()
        with db.connect() as conn:
            db.claim_workspace(conn, 1)
            conn.execute("UPDATE workspaces SET last_visited_at='2020-01-01T00:00:00Z' WHERE id=1")
            conn.commit()
            assert workspaces.reconcile(conn, []) == 1

    def test_a_starting_visit_is_not_offered_as_a_spawn_id(self):
        self._create()
        with db.connect() as conn:
            db.claim_workspace(conn, 1)
            row = workspaces.to_json(db.get_workspace(conn, 1, "scratch"), 0)
        assert row["live"] is None and row["starting"] is True

    def test_opening_one_that_does_not_exist_is_a_miss(self):
        with pytest.raises(ValueError, match="no workspace named"):
            self._open(name="ghost")


# --- the API ------------------------------------------------------------------
# Same in-process rig test_recipes.py uses.

def _client(monkeypatch, tmp_path, user_id=1) -> TestClient:
    user = {"id": user_id, "username": "alice", "is_admin": False, "must_change_password": False, "dek": None}
    head_main.app.dependency_overrides[head_main.current_user] = lambda: user
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        db.add_user(conn, "alice")
        db.add_user(conn, "bob")
    monkeypatch.setattr(head_spawns, "HEAD_STATE_DIR", tmp_path)
    monkeypatch.setattr(slurm, "arms", lambda: [{"name": "gpu-box"}])
    monkeypatch.setattr(slurm, "queue", lambda: [])
    return TestClient(head_main.app)


def _post(c: TestClient, body: bytes, **meta):
    return c.post("/api/workspaces", content=body,
                  headers={"X-CCC-Meta": json.dumps({**_META, **meta}),
                           "Content-Type": "application/gzip"})


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


class TestApi:
    def test_create_then_list(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert _post(c, _seed()).status_code == 201
        rows = c.get("/api/workspaces").json()
        assert [w["name"] for w in rows] == ["scratch"]
        assert rows[0]["storage_bytes"] == 4 * GIB and rows[0]["used_bytes"] > 0

    def test_the_pool_answers_413_and_says_what_to_do(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        monkeypatch.setattr(workspaces, "MAX_USER_BYTES", 1 * GIB)
        r = _post(c, _seed(), storage_bytes=4 * GIB)
        assert r.status_code == 413
        assert "delete a workspace" in r.json()["detail"]

    def test_a_seed_too_big_for_its_storage_says_so_instead(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        r = _post(c, _seed(payload_bytes=10_000), storage_bytes=1_000)
        assert r.status_code == 413
        assert "--storage" in r.json()["detail"]

    def test_a_shrink_below_usage_is_422_with_the_real_number(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, _seed(payload_bytes=10_000))
        r = c.patch("/api/workspaces/scratch", json={**_META, "storage_bytes": 1_000})
        assert r.status_code == 422
        assert "already holds" in r.json()["detail"]

    def test_workspaces_are_private(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        with db.connect() as conn:
            workspaces.create(conn, 2, {**_META, "name": "bobs"}, _spool_body(_seed()))
        assert c.get("/api/workspaces").json() == []
        assert c.delete("/api/workspaces/bobs").status_code == 404

    def test_delete_removes_it(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, _seed())
        assert c.delete("/api/workspaces/scratch").status_code == 204
        assert c.get("/api/workspaces").json() == []

    def test_deleting_a_live_one_is_409(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, _seed())
        with db.connect() as conn:
            db.claim_workspace(conn, 1, 4242)
        r = c.delete("/api/workspaces/scratch")
        assert r.status_code == 409
        assert "4242" in r.json()["detail"]

    def test_download_streams_the_whole_tree(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, _seed())
        r = c.get("/api/workspaces/scratch/download")
        assert r.status_code == 200
        names = tarfile.open(fileobj=io.BytesIO(r.content)).getnames()
        assert "workspace-scratch/train.py" in names

    def test_download_takes_a_subpath(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, _seed())
        (workspaces.dir_of(1, "scratch") / "sub").mkdir()
        (workspaces.dir_of(1, "scratch") / "sub" / "note.txt").write_text("hi")
        r = c.get("/api/workspaces/scratch/download", params={"path": "sub"})
        assert r.status_code == 200
        names = tarfile.open(fileobj=io.BytesIO(r.content)).getnames()
        assert names == ["workspace-scratch-sub", "workspace-scratch-sub/note.txt"]

    @pytest.mark.parametrize("path", ["../../etc", "/etc/passwd", "nope"])
    def test_a_subpath_outside_the_tree_is_404(self, monkeypatch, tmp_path, path):
        c = _client(monkeypatch, tmp_path)
        _post(c, _seed())
        assert c.get("/api/workspaces/scratch/download", params={"path": path}).status_code == 404

    def test_a_symlink_out_of_the_tree_cannot_be_downloaded_through(self, monkeypatch, tmp_path):
        """The resolved answer is what is checked, not the string — a Workspace's owner can put any
        symlink they like inside their own tree."""
        c = _client(monkeypatch, tmp_path)
        _post(c, _seed())
        (workspaces.dir_of(1, "scratch") / "out").symlink_to(tmp_path)
        assert c.get("/api/workspaces/scratch/download",
                     params={"path": "out"}).status_code == 404

    def test_downloading_one_that_is_not_yours_404s(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        with db.connect() as conn:
            workspaces.create(conn, 2, {**_META, "name": "bobs"}, _spool_body(_seed()))
        assert c.get("/api/workspaces/bobs/download").status_code == 404

    def test_open_submits_and_reports_the_spawn(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, _seed())
        monkeypatch.setattr(head_spawns, "submit_checked", lambda *a, **kw: 4242)
        r = c.post("/api/workspaces/scratch/open")
        assert r.status_code == 200
        assert r.json() == {"id": 4242, "attached": False}
        assert c.get("/api/workspaces").json()[0]["live"] == 4242

    def test_opening_a_live_one_attaches(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, _seed())
        monkeypatch.setattr(head_spawns, "submit_checked", lambda *a, **kw: 4242)
        c.post("/api/workspaces/scratch/open")
        r = c.post("/api/workspaces/scratch/open")
        assert r.json() == {"id": 4242, "attached": True}

    def test_opening_one_that_is_not_yours_404s(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        with db.connect() as conn:
            workspaces.create(conn, 2, {**_META, "name": "bobs"}, _spool_body(_seed()))
        assert c.post("/api/workspaces/bobs/open").status_code == 404

    def test_the_full_marker_is_readable_during_a_visit(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, _seed())
        monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
        (tmp_path / "spawns" / "4242").mkdir(parents=True)
        with db.connect() as conn:
            db.claim_workspace(conn, 1, 4242)
        assert c.get("/api/workspaces/scratch/full").json()["full"] is False
        (tmp_path / "spawns" / "4242" / "workspace.full").write_text("9999\n")
        st = c.get("/api/workspaces/scratch/full").json()
        assert st["full"] is True and st["bytes"] == 9999 and st["live"] == 4242

    def test_an_idle_workspace_is_never_full(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        _post(c, _seed())
        assert c.get("/api/workspaces/scratch/full").json() == {
            "live": None, "full": False, "bytes": 0, "storage_bytes": 4 * GIB}

    def test_the_routes_need_a_session(self, tmp_path):
        head_main.app.dependency_overrides.clear()
        assert TestClient(head_main.app).get("/api/workspaces").status_code == 401


class TestAVisitsFeatureSuppliedWheel:
    """The one that did not work. A **Workspace** is seeded from a single directory, so a project
    pointing `[tool.uv.sources]` at `../vlearn-main/…whl` — the layout that makes it resolve on its
    owner's own machine — points at a file the cluster has never had a copy of. The placeholder used to
    be skipped for a visit outright, to keep the Head from rewriting a durable tree, and the build died
    looking in the Spawn's scratch for a wheel nothing had put there:

        error: Distribution not found at: file:///tmp/ccc-3/vlearn-main/vlearn-0.3.9-…whl

    Nothing is written any more (`environments.substitute_supplied`), so the tree needs no protecting
    from it and a visit is served by the same mechanism as every other Spawn.
    """

    M = env.Manifest(kind="uv-project", path="pyproject.toml")

    def _tree(self, tmp_path, path="../vlearn-main/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl",
              lock=None):
        """A Workspace's durable tree — nested the way `workspaces/<uid>/<name>` really is."""
        ws = tmp_path / "workspaces" / "3" / "transformerrl"
        ws.mkdir(parents=True)
        (ws / "pyproject.toml").write_text(
            '[project]\nname = "transformerrl"\nversion = "0.1.0"\n'
            'requires-python = "==3.11.14"\ndependencies = ["vlearn"]\n\n'
            f'[tool.uv.sources]\nvlearn = {{ path = "{path}" }}\n')
        if lock is not None:
            (ws / "uv.lock").write_text(lock)
        return ws

    def test_the_wheel_that_was_never_uploaded_gets_one_anyway(self, tmp_path):
        """Bound beside the project, in the Spawn's own scratch, at exactly the path the declaration
        resolves to from `$proj`."""
        ws = self._tree(tmp_path)
        files, binds, _ = env.substitute_supplied(ws, self.M)
        assert [(b.src, b.dest) for b in binds] == [("src0", "../vlearn-main")]
        assert [f.rel for f in files] == ["src0/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl"]

    def test_the_durable_tree_is_left_exactly_as_its_owner_left_it(self, tmp_path):
        """The reason the carve-out existed. It is now a property of the mechanism rather than of a
        branch that had to remember to skip — there is no write to skip."""
        ws = self._tree(tmp_path)
        before = {p.name: p.read_bytes() for p in ws.rglob("*") if p.is_file()}
        env.substitute_supplied(ws, self.M)
        assert {p.name: p.read_bytes() for p in ws.rglob("*") if p.is_file()} == before

    def test_a_lock_its_owner_keeps_is_bound_over_and_never_edited(self, tmp_path):
        """Their lock records the wheel they actually hold. A Head that rewrote it in place would hand
        back a tree whose lock matches nothing on their machine — and the mirror carries that home."""
        ws = self._tree(tmp_path, lock=(
            'version = 1\n\n[[package]]\nname = "vlearn"\nversion = "0.3.9"\n'
            'source = { path = "../vlearn-main/vlearn-0.3.9-cp311-cp311-linux_x86_64.whl" }\n'
            'wheels = [{ filename = "vlearn-0.3.9-cp311-cp311-linux_x86_64.whl", '
            'hash = "sha256:' + "e" * 64 + '" }]\n'))
        before = (ws / "uv.lock").read_text()
        files, binds, _ = env.substitute_supplied(ws, self.M)
        assert env.StubBind("uv.lock", "uv.lock", over_project=True) in binds
        assert "sha256:" + "e" * 64 not in next(f for f in files if f.rel == "uv.lock").data.decode()
        assert (ws / "uv.lock").read_text() == before

    def test_the_lock_uv_invents_does_not_outlive_the_visit(self, tmp_path):
        """`uv sync` writes one whether or not the owner keeps one, and for a visit that lands in the
        durable tree. The one it would leave records the placeholder's hash rather than the wheel its
        owner holds, so it is removed as soon as the build is done."""
        ws = self._tree(tmp_path)
        assert not (ws / "uv.lock").exists()
        block = _env_block(drop_lock=env.lock_rel(ws, self.M))
        assert 'rm -f "$proj/uv.lock"' in block
        assert block.index("ccc_build_or_die") < block.index('rm -f "$proj/uv.lock"')

    def test_a_lock_the_owner_keeps_is_never_removed(self, tmp_path):
        """The other half of the same decision — that one is bound over, and deleting it would destroy
        something its owner deliberately keeps."""
        assert 'rm -f' not in _env_block(drop_lock="")
