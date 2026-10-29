"""Phase 4c: a Dive's apt installs/removals (and a rebuilt Tidepool's own) replay into the Tidepool."""

import json
import subprocess

import pytest

from cuttle import ccc_tidepool as H
from cuttle import templates

HIST = """Start-Date: 2026-10-01  10:00:00
Commandline: apt-get install -y build-essential
Install: make:amd64 (4.3-4.1build2)
End-Date: 2026-10-01  10:01:00

Start-Date: 2026-10-29  12:00:00
Commandline: apt install ffmpeg
Requested-By: bob (1000)
Install: libavcodec60:amd64 (7:6.1.1-3ubuntu5, automatic), ffmpeg:amd64 (7:6.1.1-3ubuntu5)
End-Date: 2026-10-29  12:00:30

Start-Date: 2026-10-29  12:05:00
Commandline: apt remove nano
Requested-By: bob (1000)
Remove: nano:amd64 (7.2-2)
Upgrade: curl:amd64 (8.5.0-2ubuntu10.4, 8.5.0-2ubuntu10.5)
End-Date: 2026-10-29  12:05:10

Start-Date: 2026-10-29  12:06:00
Commandline: apt purge vim
Requested-By: bob (1000)
Purge: vim:amd64 (2:9.1.0016-1ubuntu7)
End-Date: 2026-10-29  12:06:10
"""


class TestParse:
    def test_net_effect_in_log_order(self):
        c = H.parse_history(HIST)
        assert c["ffmpeg:amd64"] == {"op": "install", "ver": "7:6.1.1-3ubuntu5", "auto": False}
        assert c["libavcodec60:amd64"]["auto"] is True
        # an upgrade replays the new version and leaves the auto mark as it was (unknown)
        assert c["curl:amd64"] == {"op": "install", "ver": "8.5.0-2ubuntu10.5", "auto": None}
        assert c["nano:amd64"] == {"op": "remove"} and c["vim:amd64"] == {"op": "purge"}
        assert c["make:amd64"]["op"] == "install"

    def test_install_then_remove_is_a_remove(self):
        more = HIST + "\nStart-Date: x\nRequested-By: bob (1000)\nRemove: ffmpeg:amd64 (7:6.1.1-3ubuntu5)\n"
        assert H.parse_history(more)["ffmpeg:amd64"] == {"op": "remove"}

    def test_rebuild_reads_only_sudo_entries(self):
        """The base image's own build has no Requested-By: never replayed as the User's."""
        assert "make:amd64" not in H.parse_history(HIST, requested_only=True)

    def test_odd_names_never_reach_apt(self):
        c = H.parse_history("Install: --option=x (1), Foo (2), ok:amd64 (1.0)\n")
        assert list(c) == ["ok:amd64"]

    def test_dpkg_i_found_from_dpkg_log(self):
        log = ("2026-10-29 12:00:01 install ffmpeg:amd64 <none> 7:6.1.1-3ubuntu5\n"
               "2026-10-29 12:00:01 status installed ffmpeg:amd64 7:6.1.1-3ubuntu5\n"
               "2026-10-29 12:07:00 install mytool:amd64 <none> 1.0\n")
        assert H.dpkg_only(log, {"ffmpeg"}) == ["mytool"]


class FakeCtr:
    """`podman exec` into the Tidepool: apt answers per argument; records every call."""

    def __init__(self, missing=(), auto=()):
        self.calls, self.missing, self.auto = [], set(missing), " ".join(auto)

    def __call__(self, user, *argv):
        self.calls.append(argv)
        out, rc = "", 0
        if argv[:2] == ("apt-mark", "showauto"):
            out = self.auto
        elif "apt-get" in argv:
            args = argv[argv.index("--allow-downgrades") + 1:]
            rc = 100 if set(args) & self.missing else 0
        elif argv[0] == "dpkg-query":
            out = "ffmpeg 7:7.0-1\n"
        return subprocess.CompletedProcess(argv, rc, out, "")

    def apt(self):
        return [c[c.index("--allow-downgrades") + 1:] for c in self.calls if "apt-get" in c]


class TestReplay:
    def test_dive_replay_pinned_removals_first(self, monkeypatch):
        ctr = FakeCtr(missing={"foo:amd64=1.2"}, auto=["curl"])
        monkeypatch.setattr(H, "_exec", ctr)
        changes = {**H.parse_history(HIST.split("\n\n", 1)[1]), "foo:amd64": {"op": "install", "ver": "1.2", "auto": False}}
        res = H.replay_changes("bob", changes, pinned=True, log=[])
        apt = ctr.apt()
        assert apt[0] == ("update",)
        assert apt[1] == ("remove", "nano:amd64") and apt[2] == ("purge", "vim:amd64")
        assert apt[3][0] == "install" and "ffmpeg:amd64=7:6.1.1-3ubuntu5" in apt[3]
        # the batch failed → one by one, naming the failure
        assert res["failed"] == ["foo"]
        assert sorted(res["back"]) == ["ffmpeg"]
        # auto deps and an upgraded auto package keep their mark
        mark = next(c for c in ctr.calls if c[:2] == ("apt-mark", "auto"))
        assert set(mark[2:]) == {"libavcodec60:amd64", "curl:amd64"}
        assert res["removed"] == ["nano", "vim"]
        # logged as the User's, so a later rebuild keeps it
        assert all("SUDO_UID=1000" in c for c in ctr.calls if "apt-get" in c)

    def test_rebuild_replay_by_name_notes_new_versions(self, monkeypatch):
        ctr = FakeCtr()
        monkeypatch.setattr(H, "_exec", ctr)
        res = H.replay_changes("bob", {"ffmpeg:amd64": {"op": "install", "ver": "7:6.1.1-3ubuntu5", "auto": False}},
                               pinned=False, log=[])
        assert ("install", "ffmpeg:amd64") in ctr.apt()
        assert res["changed"] == ["ffmpeg 7:6.1.1-3ubuntu5 → 7:7.0-1"]

    def test_nothing_to_do_runs_nothing(self, monkeypatch):
        ctr = FakeCtr()
        monkeypatch.setattr(H, "_exec", ctr)
        H.replay_changes("bob", {}, pinned=True, log=[])
        assert ctr.calls == []

    def test_summary(self):
        res = {"back": ["ffmpeg"], "failed": ["foo"], "skipped": ["mytool"], "removed": []}
        assert H.summary("Dive 9", res) == "Dive 9 brought back: ffmpeg · failed: foo · not replayed (dpkg -i): mytool"
        assert H.summary("Dive 9", {"back": []}) is None


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    for name in ("DIVE_STATE", "APT_PENDING", "MNT_ROOT", "RECORDS"):
        d = tmp_path / name.lower()
        d.mkdir()
        monkeypatch.setattr(H, name, d)
    (H.RECORDS / "bob.json").write_text(json.dumps({"uid": 60003}))
    spawned = []
    monkeypatch.setattr(H, "spawn_replay", lambda u, t: spawned.append(u))
    return spawned


class TestQueue:
    TOK = "ab" * 16

    def test_dive_close_queues_the_back_bundle(self, dirs):
        back = H.DIVE_STATE / self.TOK / "back"
        back.mkdir(parents=True)
        (back / "job").write_text("9\n")
        (back / "var_log_apt_history.log").write_text(HIST)
        (H.DIVE_STATE / self.TOK / "apt-offsets.json").write_text(json.dumps({"history": 5, "dpkg": 7}))
        H.queue_dive_replay({"token": self.TOK, "user": "bob"})
        q = H.APT_PENDING / "bob" / self.TOK
        assert json.loads((q / "meta.json").read_text()) == {"kind": "dive", "job": 9,
                                                             "offsets": {"history": 5, "dpkg": 7}}
        assert (q / "var_log_apt_history.log").read_text() == HIST and not back.exists()
        assert dirs == ["bob"]

    def test_nothing_sent_nothing_queued(self, dirs):
        (H.DIVE_STATE / self.TOK / "back").mkdir(parents=True)
        H.queue_dive_replay({"token": self.TOK, "user": "bob"})
        assert not (H.APT_PENDING / "bob").exists() and dirs == []

    def test_dive_item_reads_only_its_own_entries(self, dirs, monkeypatch):
        item = H.APT_PENDING / "bob" / self.TOK
        item.mkdir(parents=True)
        head = HIST.split("\n\n", 1)[0] + "\n\n"
        (item / "var_log_apt_history.log").write_text(HIST)
        (item / "meta.json").write_text(json.dumps({"kind": "dive", "job": 9,
                                                    "offsets": {"history": len(head), "dpkg": 0}}))
        seen = {}
        monkeypatch.setattr(H, "copy_sources", lambda u, s, log: [])
        monkeypatch.setattr(H, "replay_changes", lambda u, c, pinned, log: seen.update(c=c, p=pinned)
                            or {"back": ["ffmpeg"]})
        wrote = {}
        monkeypatch.setattr(H, "write_home", lambda u, rel, text: wrote.update({rel: text}))
        H.replay_item("bob", item)
        assert "make:amd64" not in seen["c"] and "ffmpeg:amd64" in seen["c"] and seen["p"] is True
        assert wrote[".ccc/installs/dive-9.msg"].startswith("Dive 9 brought back: ffmpeg")

    def test_rebuild_keeps_an_unreplayed_list(self, dirs, monkeypatch):
        q = H.APT_PENDING / "bob" / "rebuild"
        q.mkdir(parents=True)
        (q / "meta.json").write_text(json.dumps({"kind": "rebuild", "changes": {"a": {"op": "remove"}}}))

        def cp(*a, **k):
            if "cp" in a:
                open(a[-1], "w").write(HIST)
        monkeypatch.setattr(H, "sh", cp)
        H.queue_rebuild("bob")
        ch = json.loads((q / "meta.json").read_text())["changes"]
        assert "a" in ch and "ffmpeg:amd64" in ch and "make:amd64" not in ch

    def test_reset_drops_the_rebuild_list(self, dirs, monkeypatch):
        (H.APT_PENDING / "bob" / "rebuild").mkdir(parents=True)
        monkeypatch.setattr(H, "sh", lambda *a, **k: None)
        monkeypatch.setattr(H, "ensure_container", lambda *a: None)
        monkeypatch.setattr(H, "settings", lambda: {})
        H.reset("bob")
        assert not (H.APT_PENDING / "bob" / "rebuild").exists()


class TestSources:
    def test_only_new_regular_files_copied(self, monkeypatch, tmp_path):
        d = tmp_path / "etc_apt_sources.list.d"
        d.mkdir()
        (d / "new.list").write_text("deb x")
        (d / "have.list").write_text("deb y")
        (d / "link.list").symlink_to("/etc/shadow")
        monkeypatch.setattr(H, "_exec", lambda u, *a: subprocess.CompletedProcess(a, 0 if "have.list" in a[-1] else 1))
        cps = []
        monkeypatch.setattr(H, "sh", lambda *a, **k: cps.append(a))
        assert H.copy_sources("bob", tmp_path, []) == ["new.list"]
        assert cps[0][-1].endswith(":/etc/apt/sources.list.d/new.list") and "--archive=false" in cps[0]


def test_tidepool_shell_prints_the_result_once():
    z = templates.render_tidepool_zshrc()
    assert "$HOME/.ccc/installs/*.msg(N)" in z and "rm -f -- $_ccc_f" in z
