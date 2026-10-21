"""What a download says about itself while it is happening.

A 3GB result is minutes of transfer, and minutes of silence read as a hang — that is the whole reason
a progress line exists here at all. This file covers the half that was missing: the *denominator*.

What's tested: `tarstream.weigh` (the walk that decides both the 404 and the size), `X-CCC-Bytes` on
all three tar routes, the client reading it off a live stream, `api.download` counting decompressed
bytes so its numerator matches, and `ui.progress` latching a total that arrives after the line is
already drawn. NOT tested: what the bar looks like on a real terminal — `_Surface` renders to a Rich
console and the assertions here are on the Text it builds.
"""

import io
import tarfile

import httpx
import pytest
from fastapi.testclient import TestClient

from ccc_head_api import db, tarstream, workspaces
from ccc_head_api import main as head_main
from ccc_head_api import spawns as head_spawns
from cuttle import api, ui


# --- the walk -----------------------------------------------------------------

class TestWeigh:
    """One walk answers two questions, and the None is the more important one: it is the 404, and it
    has to be decidable before a single chunk commits the 200."""

    def test_a_missing_directory_weighs_nothing_at_all(self, tmp_path):
        assert tarstream.weigh(tmp_path / "never-made") is None

    def test_an_empty_directory_is_none_not_zero(self, tmp_path):
        """`submit` pre-creates every Spawn's outputs dir, so presence says nothing. Zero would be a
        200 with an empty tar; None is the 404 that tells a User their Spawn wrote nothing."""
        (tmp_path / "outputs").mkdir()
        assert tarstream.weigh(tmp_path / "outputs") is None

    def test_a_directory_of_only_directories_is_still_none(self, tmp_path):
        (tmp_path / "outputs" / "runs" / "ckpts").mkdir(parents=True)
        assert tarstream.weigh(tmp_path / "outputs") is None

    def test_it_sums_file_sizes_through_the_whole_tree(self, tmp_path):
        d = tmp_path / "outputs"
        (d / "runs").mkdir(parents=True)
        (d / "a.bin").write_bytes(b"x" * 700)
        (d / "runs" / "b.bin").write_bytes(b"y" * 300)
        assert tarstream.weigh(d) == 1000

    def test_an_empty_file_weighs_zero_but_is_still_something(self, tmp_path):
        """Zero bytes and nothing-at-all are different answers: a Spawn that wrote an empty file
        produced a result, and asking for it must not 404."""
        d = tmp_path / "outputs"
        d.mkdir()
        (d / "empty.log").touch()
        assert tarstream.weigh(d) == 0

    def test_a_symlink_counts_as_a_link_and_never_its_target(self, tmp_path):
        """Matches what the tar carries — a symlink member has size 0 — and what `workspaces.usage`
        already reports, so the number stays the same one a listing quotes."""
        big = tmp_path / "elsewhere.bin"
        big.write_bytes(b"z" * 5000)
        d = tmp_path / "outputs"
        d.mkdir()
        (d / "link").symlink_to(big)
        assert tarstream.weigh(d) == 0

    def test_the_sum_matches_what_a_client_will_count_out_of_the_tar(self, tmp_path):
        """The numerator `api.download_extract` accumulates is the sum of tar member sizes. If this
        walk counted anything else the bar would be drawn against a different quantity than it fills
        with, which is worse than no bar."""
        d = tmp_path / "outputs"
        (d / "sub").mkdir(parents=True)
        (d / "a.bin").write_bytes(b"x" * 137)
        (d / "sub" / "b.bin").write_bytes(b"y" * 42)
        blob = b"".join(tarstream.chunks(d, "outputs"))
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            assert sum(m.size for m in tf) == tarstream.weigh(d)


# --- the header ---------------------------------------------------------------

def _client(monkeypatch, tmp_path) -> TestClient:
    user = {"id": 1, "username": "alice", "is_admin": False, "must_change_password": False}
    head_main.app.dependency_overrides[head_main.current_user] = lambda: user
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "head.db")
    db.ensure_schema()
    with db.connect() as conn:
        db.add_user(conn, "alice")
    monkeypatch.setattr(head_spawns, "SPAWNS_DIR", tmp_path / "spawns")
    monkeypatch.setattr(head_spawns, "HEAD_STATE_DIR", tmp_path / "state")
    return TestClient(head_main.app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    head_main.app.dependency_overrides.clear()


def _workspace(conn, name: str) -> None:
    """A Workspace row for alice. Everything but the name is scenery here — what is under test is
    the tree on disk and the number the route reports about it."""
    db.add_workspace(conn, 1, name, 1 << 30, gpus=1, vram_mb=8192, minutes=60,
                     manifest="pyproject.toml", outputs=[], features=[])


class TestTheRoutesDeclareTheirSize:
    """A streamed body carries no `Content-Length` — that is the point of streaming it — so the size
    has to be declared out of band or the client has nothing to divide by.

    It rides on the response rather than coming from a metadata route the client asks first. The
    version that asked first read `/spawns/<id>/retention`, a ledger row written by a 30s tick, so
    collecting results the moment a Spawn ended found nothing and drew no bar. That is the common
    case, and it is exactly the one this is for."""

    def _outputs(self, tmp_path, job_id: int, size: int) -> None:
        d = tmp_path / "spawns" / str(job_id) / "outputs"
        d.mkdir(parents=True)
        (d / "result.bin").write_bytes(b"x" * size)

    def test_outputs_declares_what_the_tree_weighs(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        self._outputs(tmp_path, 1, 4096)
        r = c.get("/api/spawns/1/outputs")
        assert r.status_code == 200
        assert r.headers["X-CCC-Bytes"] == "4096"

    def test_outputs_declares_it_before_the_ledger_has_heard_of_the_spawn(self, monkeypatch,
                                                                          tmp_path):
        """The bug this replaced: no `add_output` row yet, because the tick has not run."""
        c = _client(monkeypatch, tmp_path)
        self._outputs(tmp_path, 2, 2048)
        with db.connect() as conn:
            assert db.get_spawn(conn, 2) is None
        assert c.get("/api/spawns/2/outputs").headers["X-CCC-Bytes"] == "2048"

    def test_recovery_declares_what_is_on_the_disk_now(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        d = tmp_path / "spawns" / "3" / "recovery"
        d.mkdir(parents=True)
        (d / "scratch.bin").write_bytes(b"y" * 900)
        r = c.get("/api/spawns/3/recovery/download")
        assert r.status_code == 200
        assert r.headers["X-CCC-Bytes"] == "900"

    def test_a_workspace_declares_the_subtree_it_was_asked_for(self, monkeypatch, tmp_path):
        """Not the whole Workspace. `?path=` narrows what is sent, so it must narrow the number
        too — this is why `workspaces.usage`, which only knows whole trees, could not be reused."""
        c = _client(monkeypatch, tmp_path)
        with db.connect() as conn:
            _workspace(conn, "scratch")
        root = workspaces.dir_of(1, "scratch")
        (root / "sub").mkdir(parents=True)
        (root / "big.bin").write_bytes(b"a" * 5000)
        (root / "sub" / "small.bin").write_bytes(b"b" * 11)
        assert c.get("/api/workspaces/scratch/download").headers["X-CCC-Bytes"] == "5011"
        assert c.get("/api/workspaces/scratch/download",
                     params={"path": "sub"}).headers["X-CCC-Bytes"] == "11"

    def test_an_empty_workspace_downloads_as_zero_rather_than_404ing(self, monkeypatch, tmp_path):
        """Unlike an empty outputs dir. A Workspace that holds nothing is a Workspace that exists,
        and a tar of nothing is the honest answer to asking for it."""
        c = _client(monkeypatch, tmp_path)
        with db.connect() as conn:
            _workspace(conn, "empty")
        workspaces.dir_of(1, "empty").mkdir(parents=True)
        r = c.get("/api/workspaces/empty/download")
        assert r.status_code == 200
        assert r.headers["X-CCC-Bytes"] == "0"


# --- the client ---------------------------------------------------------------

def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _serving(body: bytes, headers: dict | None = None) -> api.Api:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=body, headers=headers or {}))
    return api.Api(httpx.Client(transport=transport, base_url="http://head/api"), token="t")


class TestTheClientReadsTheDeclaredSize:
    def test_the_total_reaches_the_progress_callback_from_the_first_call(self, ccc_home, tmp_path):
        """Before any byte is counted. The callback is what opens the progress line, so a total
        arriving later than the first call would mean a line that starts without a bar."""
        seen = []
        _serving(_tar({"ckpt.pt": b"weights"}), {"X-CCC-Bytes": "7"}).download_extract(
            "/spawns/1/outputs", tmp_path / "out", on_progress=lambda d, t: seen.append((d, t)))
        assert seen[0] == (0, 7)
        assert seen[-1] == (7, 7)

    def test_a_head_that_declares_nothing_still_downloads(self, ccc_home, tmp_path):
        """The header is a courtesy. Losing it costs a bar, never a transfer."""
        seen = []
        _serving(_tar({"ckpt.pt": b"weights"})).download_extract(
            "/spawns/1/outputs", tmp_path / "out", on_progress=lambda d, t: seen.append((d, t)))
        assert {t for _, t in seen} == {None}
        assert (tmp_path / "out" / "ckpt.pt").read_bytes() == b"weights"

    @pytest.mark.parametrize("value", ["", "lots", "12.5"])
    def test_an_unparseable_declaration_is_ignored_rather_than_fatal(self, value):
        r = httpx.Response(200, headers={"X-CCC-Bytes": value})
        assert api._declared_bytes(r) is None


class TestTheWorkspaceTarIsCountedUncompressed:
    """`download` writes the compressed tar to disk byte for byte, but reports uncompressed progress.
    A compressed numerator against an uncompressed total would show a 16GB Workspace finishing at
    45% — the bar would be actively lying rather than merely absent."""

    def test_progress_is_reported_in_uncompressed_bytes(self, ccc_home, tmp_path):
        payload = b"weights" * 5000        # compresses hard, so the two counts cannot coincide
        body = _tar({"ckpt.pt": payload})
        seen = []
        written = _serving(body, {"X-CCC-Bytes": str(len(payload))}).download(
            "/workspaces/w/download", tmp_path / "w.tar.gz",
            on_progress=lambda d, t: seen.append((d, t)))
        assert written == len(body)                        # the file on disk is what arrived
        assert (tmp_path / "w.tar.gz").read_bytes() == body
        assert seen[-1][0] > len(body)                     # what was reported is the plain size
        assert seen[-1][1] == len(payload)

    def test_the_count_runs_a_little_past_the_declared_total(self, ccc_home, tmp_path):
        """Tar pads every member to 512 bytes and heads it with 512 more, so the stream is always
        larger than the files in it. The bar arrives at 100% slightly early; it never reports past
        it, because `ui.progress` clamps. Worth it to keep the header meaning "what this tree holds",
        the number every listing already quotes, rather than a wire artifact."""
        seen = []
        _serving(_tar({"a": b"x" * 10}), {"X-CCC-Bytes": "10"}).download(
            "/workspaces/w/download", tmp_path / "w.tar.gz",
            on_progress=lambda d, t: seen.append(d))
        assert seen[-1] > 10

    def test_a_corrupted_body_is_refused_and_nothing_is_left_behind(self, ccc_home, tmp_path):
        """The gzip CRC32 is checked here and nowhere else — `download_extract` cannot reach it,
        because tarfile stops at the archive's end-of-file blocks and never feeds the trailer
        through. Free, now that the bytes pass through a decompressor anyway."""
        body = bytearray(_tar({"ckpt.pt": b"weights" * 100}))
        body[len(body) // 2] ^= 0xFF
        dest = tmp_path / "w.tar.gz"
        with pytest.raises(api.ApiError) as caught:
            _serving(bytes(body)).download("/workspaces/w/download", dest)
        assert "corrupted" in caught.value.detail
        assert not dest.exists()
        assert not dest.with_suffix(".gz.part").exists()


# --- the line -----------------------------------------------------------------

@pytest.fixture
def drawn(monkeypatch) -> list[str]:
    """Every line `ui.progress` paints, as plain text. Captured at `_Surface.show`, which is the one
    point every one of the three drawing places goes through — so this reads what a terminal would
    without needing one."""
    lines: list[str] = []
    real = ui._Surface.show
    monkeypatch.setattr(ui._Surface, "show",
                        lambda self, renderable, height=1: lines.append(str(renderable)))
    monkeypatch.setattr(ui, "VERBOSE", False)
    monkeypatch.setattr(ui, "_REPAINT_S", 0.0)      # every update paints, so none is missed
    return lines


class TestTheLineLearnsItsTotalLate:
    """The size is read off a response header, which is not readable until the stream is already
    open — by which time the caller has drawn its first line. So the bar appears mid-transfer, and
    `update` takes the total as an optional second argument to make that possible."""

    def test_it_starts_without_a_bar_and_gains_one(self, drawn):
        with ui.progress("downloading") as update:
            update(0)
            first = drawn[-1]
            update(500_000, 1_000_000)
        assert "%" not in first
        assert "50%" in drawn[-1]

    def test_a_total_given_up_front_is_still_honoured(self, drawn):
        """Not every caller learns late — `progress(label, total=…)` still works, and the latch must
        not clobber it."""
        with ui.progress("building", total=1000) as update:
            update(250)
        assert "25%" in drawn[-1]

    def test_the_first_total_wins(self, drawn):
        """One fact about one transfer. A second value would be the Head changing its mind mid-
        stream, and the bar was already drawn against the first."""
        with ui.progress("downloading", total=1000) as update:
            update(500, 4000)
        assert "50%" in drawn[-1]

    def test_both_numbers_are_shown_not_just_a_percentage(self, drawn):
        '''"82%" of an unnamed quantity does not say whether the wait left is seconds or an hour,
        which is the question actually being asked.'''
        with ui.progress("downloading") as update:
            update(512 * 1024, 1024 * 1024)
        assert "50%" in drawn[-1]
        assert "512.0 KiB/1.0 MiB" in drawn[-1]

    def test_the_count_never_renders_past_the_total(self, drawn):
        """The workspace numerator counts tar padding the Head did not weigh, so a small overshoot
        is normal. "1.1/1.0 GiB" would read as a bug in the transfer rather than in the arithmetic."""
        with ui.progress("downloading") as update:
            update(1100, 1000)
        assert "100%" in drawn[-1]
        assert "1000 B/1000 B" in drawn[-1]

    def test_without_a_total_it_stays_bytes_and_a_rate(self, drawn):
        """The honest rendering when nobody knows the denominator — an older Head, or a route that
        does not declare one."""
        with ui.progress("downloading") as update:
            update(4096)
        assert "%" not in drawn[-1]
        assert "4.0 KiB" in drawn[-1]

    def test_verbose_mode_takes_the_second_argument_too(self, monkeypatch):
        """`--verbose` swaps the live line for a no-op. A signature that did not match would make
        every download crash under the one flag people reach for when a download misbehaves."""
        monkeypatch.setattr(ui, "VERBOSE", True)
        with ui.progress("downloading") as update:
            update(0, 100)
            update(100, 100)
