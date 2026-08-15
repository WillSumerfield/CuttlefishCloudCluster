"""Auto-download (`cuttle.watch`) — a Spawn's results arrive without anyone asking for them.

The sweeper has no terminal, runs after `cuttle run` returns, and races a Head that deletes
results after 7 days (sooner if the submitter's 32GB **Allowance** fills). Every failure mode
here is silent by construction: results that never arrive, an obligation dropped for a
*different* still-running Spawn, a logout that permanently strands everything, or extraction
that writes outside its destination.

NOT tested: the detached fork itself (`_launch` is stubbed suite-wide — a child interpreter
inherits none of the monkeypatching keeping this suite off the real `~/.ccc` and Head), and the
15-second poll loop (`time.sleep` + `while`).
"""

import io
import pathlib
import tarfile

import pytest

from ccc_head_api import spawns as head_spawns
from cuttle import config, watch


def _outputs_on_head(job_id: int, name: str = "ckpt.pt", data: bytes = b"weights") -> None:
    """A finished Spawn as the Head holds one: results under `outputs/`, and the `exit` file whose
    arrival IS the end-of-life signal (stage_out sends it last — see ccc_head_api.siphons)."""
    d = head_spawns.SPAWNS_DIR / str(job_id)
    (d / "outputs").mkdir(parents=True, exist_ok=True)
    (d / "outputs" / name).write_bytes(data)
    (d / "exit").write_text("0\n")


@pytest.fixture
def logged_in(cli, monkeypatch):
    """A CLI holding a real session against the in-process Head, with squeue out of the picture —
    `active_state` shells out to a binary this suite does not have, and every Spawn here is finished."""
    monkeypatch.setattr(head_spawns, "active_state", lambda job_id: None)
    cli("login", input="alice\nhunter2\n")
    return cli


class TestTheWatchListOutlivesTheProcess:
    """The entry is the durable intent and the sweeper is only the current attempt at it. If that is
    ever the other way round, a reboot mid-Spawn loses results the Head then deletes on schedule."""

    def test_an_armed_entry_records_an_absolute_destination(self, ccc_home, tmp_path):
        """The sweeper wakes in a directory of its own choosing (`_launch` sets cwd=/), so a relative
        destination would extract into wherever that happened to be — root, on a real box."""
        watch.arm(7, tmp_path / "project")
        assert watch.read(7)["dest"] == str((tmp_path / "project").resolve())

    def test_pruning_never_drops_an_obligation(self, ccc_home, monkeypatch):
        """The cap exists to stop the bookkeeping growing forever. If it could evict a *waiting* entry
        it would become exactly the silent loss this module exists to prevent."""
        monkeypatch.setattr(watch, "MAX_ENTRIES", 5)
        watch.arm(1, "/tmp")
        for i in range(2, 40):
            watch._finish(i, watch.DONE, "", dest="/tmp")
        assert watch.read(1)["state"] == watch.WAITING
        assert len(watch.entries()) == 6          # the 5 newest settled, plus the obligation

    def test_a_corrupt_entry_is_ignored_rather_than_fatal(self, ccc_home):
        """A torn write must not make `cuttle status` — or the sweeper — fail for every other Spawn."""
        config.WATCH_DIR.mkdir(parents=True)
        (config.WATCH_DIR / "3.json").write_text("{not json")
        watch.arm(4, "/tmp")
        assert [e["spawn_id"] for e in watch.pending()] == [4]


class TestDraining:
    def test_a_finished_spawns_results_land_extracted(self, logged_in, tmp_path):
        """The whole feature. If this stops working nothing announces it — the User simply finds no
        results, on a day the Head has already deleted its own copy."""
        dest = tmp_path / "proj"
        dest.mkdir()
        _outputs_on_head(1)
        watch.arm(1, dest)
        assert watch.drain_one(1) is True
        assert (dest / "spawn-1-outputs" / "ckpt.pt").read_bytes() == b"weights"
        assert watch.read(1)["state"] == watch.DONE

    def test_a_running_spawn_stays_owed(self, cli, monkeypatch, tmp_path):
        """Settling an entry early is unrecoverable: nothing ever looks at it again."""
        monkeypatch.setattr(head_spawns, "active_state", lambda job_id: "RUNNING")
        cli("login", input="alice\nhunter2\n")
        watch.arm(2, tmp_path)
        assert watch.drain_one(2) is False
        assert watch.read(2)["state"] == watch.WAITING

    def test_one_running_spawn_does_not_block_a_finished_one(self, cli, monkeypatch, tmp_path):
        """A pass that stopped at the first still-running Spawn would leave the finished one behind it
        waiting another 15s — or, behind a long enough job, until the sweeper died with the machine.
        Entries are polled in id order, so 'the one in front is still running' is the common case."""
        monkeypatch.setattr(head_spawns, "active_state",
                            lambda job_id: "RUNNING" if job_id == 3 else None)
        cli("login", input="alice\nhunter2\n")
        _outputs_on_head(4)
        watch.arm(3, tmp_path)
        watch.arm(4, tmp_path)
        assert watch.one_pass() == 1          # 3 is still owed
        assert watch.read(4)["state"] == watch.DONE
        assert watch.read(3)["state"] == watch.WAITING

    def test_a_failed_spawn_still_downloads(self, logged_in, tmp_path):
        """A run killed at its --time limit with six hours of checkpoints in a declared output is exactly when
        the results matter most. Gating on a clean exit would throw that away."""
        d = head_spawns.SPAWNS_DIR / "5"
        (d / "outputs").mkdir(parents=True)
        (d / "outputs" / "ckpt.pt").write_bytes(b"partial")
        (d / "exit").write_text("137\n")
        watch.arm(5, tmp_path)
        watch.drain_one(5)
        assert (tmp_path / "spawn-5-outputs" / "ckpt.pt").read_bytes() == b"partial"

    def test_nothing_to_fetch_records_the_heads_own_words(self, logged_in, tmp_path, head):
        """The Head is the only party that can tell "wrote nothing" from "held it and it is now gone"
        (7 days elapsed, or evicted for the Allowance). A guess made here would tell a User their Spawn
        produced nothing when it produced 3GB they came back too late for."""
        (head_spawns.SPAWNS_DIR / "6" / "outputs").mkdir(parents=True)
        (head_spawns.SPAWNS_DIR / "6" / "exit").write_text("0\n")
        watch.arm(6, tmp_path)
        assert watch.drain_one(6) is True
        entry = watch.read(6)
        assert entry["state"] == watch.EMPTY
        assert "wrote no outputs" in entry["note"]

    def test_an_unreachable_head_keeps_the_obligation(self, logged_in, tmp_path, monkeypatch):
        """A Head that is off, off-mesh or mid-restart is a delay, not an answer. Settling on a
        transport failure would discard results that are sitting there intact."""
        from cuttle import api
        monkeypatch.setattr(watch, "_client",
                            lambda token: (_ for _ in ()).throw(api.ApiError("/x", "unreachable")))
        watch.arm(8, tmp_path)
        assert watch.drain_one(8) is False
        assert watch.read(8)["state"] == watch.WAITING


class TestTheSessionIsNeverCopied:
    """`cuttle logout` has to mean something. The sweeper re-reads the credentials file every pass and
    keeps no copy, so logging out really does stop it — which is only safe if logging back in resumes."""

    def test_logged_out_parks_rather_than_drops(self, logged_in, tmp_path):
        watch.arm(9, tmp_path)
        config.CREDENTIALS_FILE.unlink()
        assert watch.drain_one(9) is True
        assert watch.read(9)["state"] == watch.PARKED

    def test_a_parked_entry_resumes_after_logging_back_in(self, logged_in, tmp_path):
        """Logging out for five minutes must not permanently strand every outstanding result."""
        _outputs_on_head(10)
        watch.arm(10, tmp_path)
        config.CREDENTIALS_FILE.unlink()
        watch.drain_one(10)
        logged_in("login", input="alice\nhunter2\n")
        watch.arm(10, tmp_path)          # what the re-arm hook's caller effectively restores
        watch.drain_one(10)
        assert (tmp_path / "spawn-10-outputs" / "ckpt.pt").exists()


class TestExtractionIsUnattended:
    def test_a_traversing_member_cannot_escape_the_destination(self, ccc_home, tmp_path):
        """Nobody is watching the paths go by. A member naming `../` would write outside the
        destination silently — the one thing an unattended `extractall` can do that a human running
        `tar xzf` would at least see."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("../escaped.txt")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
        dest = tmp_path / "proj"
        dest.mkdir()
        with pytest.raises(Exception):
            watch._extract(buf.getvalue(), dest, 11)
        assert not (tmp_path / "escaped.txt").exists()

    def test_the_default_destination_is_created_rather_than_required(self, ccc_home, tmp_path):
        """Results land in ~/ccc/outputs, which a box that has never downloaded anything does not
        have yet. Nothing may be dropped for a reason the User never sees."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("ckpt.pt")
            info.size = 2
            tar.addfile(info, io.BytesIO(b"ok"))
        assert not config.OUTPUTS_DIR.exists()
        out = watch._extract(buf.getvalue(), config.OUTPUTS_DIR, 12)
        assert out == config.OUTPUTS_DIR / "spawn-12-outputs"
        assert (out / "ckpt.pt").read_bytes() == b"ok"

    def test_an_unreachable_dest_falls_back_rather_than_failing(self, ccc_home, tmp_path,
                                                                monkeypatch):
        """`--dest` can name a directory that is gone by the time a long Spawn ends (deleted, an
        unmounted share). Results still have to land somewhere."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("ckpt.pt")
            info.size = 2
            tar.addfile(info, io.BytesIO(b"ok"))

        bad = tmp_path / "gone"
        real_mkdir = pathlib.Path.mkdir

        def refuse(self, *a, **kw):
            if self == bad:
                raise OSError("read-only file system")
            return real_mkdir(self, *a, **kw)
        monkeypatch.setattr(pathlib.Path, "mkdir", refuse)
        out = watch._extract(buf.getvalue(), bad, 13)
        assert out == config.OUTPUTS_DIR / "spawn-13-outputs"


class TestTheSweeperIsSingular:
    def test_a_second_sweeper_is_not_forked_while_one_holds_the_lock(self, ccc_home,
                                                                    no_real_sweeper, tmp_path):
        """A 20-Spawn shell loop would otherwise leave 20 processes polling the Head independently —
        and two of them draining the same entry means downloading the same result twice."""
        watch.arm(13, tmp_path)
        held = watch._lock()
        try:
            watch.ensure_sweeper()
            assert no_real_sweeper == []
        finally:
            held.close()
        watch.ensure_sweeper()
        assert len(no_real_sweeper) == 1

    def test_nothing_pending_forks_nothing(self, ccc_home, no_real_sweeper):
        """The re-arm hook runs on EVERY `cuttle` command. If it forked unconditionally, `cuttle
        --help` on a box that has never submitted anything would start a background process."""
        watch.ensure_sweeper()
        assert no_real_sweeper == []


class TestSubmitArmsIt:
    def test_run_arms_a_watch_entry_by_default(self, cli, fake_slurm, tmp_path, monkeypatch):
        """The default is the feature. If `run` stops arming, nothing else in this file ever fires."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "train.py").write_text("print('hi')\n")
        (tmp_path / "requirements.txt").write_text("")
        cli("login", input="alice\nhunter2\n")
        cli("run", "train.py", "--name", "a spawn", "--time", "10", "--vram", "8", "-o", "ckpt",
            "--manifest", "requirements.txt")
        assert [e["dest"] for e in watch.pending()] == [str(config.OUTPUTS_DIR.resolve())]

    def test_dest_overrides_where_results_land(self, cli, fake_slurm, tmp_path, monkeypatch):
        """One predictable default, and an escape hatch for anyone who does want results beside the
        code that produced them."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "train.py").write_text("print('hi')\n")
        (tmp_path / "requirements.txt").write_text("")
        cli("login", input="alice\nhunter2\n")
        cli("run", "train.py", "--name", "a spawn", "--time", "10", "--vram", "8", "-o", "ckpt",
            "--manifest", "requirements.txt", "--dest", str(tmp_path))
        assert [e["dest"] for e in watch.pending()] == [str(tmp_path.resolve())]

    def test_no_download_arms_nothing(self, cli, fake_slurm, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "train.py").write_text("print('hi')\n")
        (tmp_path / "requirements.txt").write_text("")
        cli("login", input="alice\nhunter2\n")
        cli("run", "train.py", "--name", "a spawn", "--time", "10", "--vram", "8", "-o", "ckpt",
            "--manifest", "requirements.txt", "--no-download")
        assert watch.entries() == []
