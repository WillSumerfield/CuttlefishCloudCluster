"""`cuttle dive` / `dive attach` from inside a Tidepool: the inner cuttle asks your machine (over the
forwarded socket) to take you to the Dive, then closes the Tidepool session; your machine attaches."""

import signal

import pytest

from cuttle import dive, tidepool, transfer


@pytest.fixture
def fwd(ccc_home, monkeypatch, tmp_path):
    with transfer.Forward("lab", tmp_path) as f:
        monkeypatch.setenv(transfer.FWD_ENV, str(f.local))
        yield f


class TestTheSocket:
    def test_handoff_is_stored_for_after_ssh(self, fwd):
        assert transfer.notify("handoff", 7)
        assert fwd.handoff == 7

    def test_vscode_opens_here_now(self, fwd, monkeypatch):
        opened = []
        monkeypatch.setattr(dive, "open_vscode_quiet", lambda i: opened.append(i) or True)
        assert transfer.notify("vscode", 7)
        assert opened == [7] and fwd.handoff is None

    def test_vscode_failing_is_a_no(self, fwd, monkeypatch):
        monkeypatch.setattr(dive, "open_vscode_quiet", lambda i: False)
        assert not transfer.notify("vscode", 7)

    def test_no_link_is_a_no(self, monkeypatch, tmp_path):
        monkeypatch.delenv(transfer.FWD_ENV, raising=False)
        assert not transfer.notify("handoff", 7)
        monkeypatch.setenv(transfer.FWD_ENV, str(tmp_path / "stale.sock"))
        assert not transfer.notify("handoff", 7)


def _proc(tmp_path, procs):
    """procs: {pid: (ppid, cmdline)}."""
    for pid, (ppid, cmd) in procs.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(cmd.encode() + b"\0")
        (d / "stat").write_text(f"{pid} (x y) S {ppid} 1 1\n")
    return tmp_path


class TestTheSession:
    def test_finds_the_users_sshd_through_nested_shells(self, tmp_path):
        p = _proc(tmp_path, {50: (1, "sshd: will [priv]"), 60: (50, "sshd: will@pts/0"),
                             70: (60, "-zsh"), 80: (70, "bash"), 90: (80, "cuttle\0dive")})
        assert dive._session_sshd(90, p) == 60

    def test_newer_openssh_name(self, tmp_path):
        p = _proc(tmp_path, {60: (1, "sshd-session: will@pts/0"), 90: (60, "zsh")})
        assert dive._session_sshd(90, p) == 60

    def test_a_tmux_pane_has_none(self, tmp_path):
        p = _proc(tmp_path, {40: (1, "tmux"), 70: (40, "zsh"), 90: (70, "cuttle")})
        assert dive._session_sshd(90, p) is None


class TestInside:
    @pytest.fixture
    def inside(self, monkeypatch):
        sent, killed = [], []
        monkeypatch.setattr(dive, "in_container", lambda: True)
        monkeypatch.setattr(dive, "_wait_ready",
                            lambda i: {"head": False, "node": "arm1", "user": "u", "main": "m"})
        monkeypatch.setattr(dive, "holders", lambda f: [])
        monkeypatch.setattr(transfer, "notify", lambda op, i: sent.append((op, i)) or self.link)
        monkeypatch.setattr(dive, "_session_sshd", lambda: self.sshd)
        monkeypatch.setattr(dive.os, "kill", lambda pid, sig: killed.append((pid, sig)))
        self.link, self.sshd = True, 60
        return sent, killed

    def test_hands_off_then_closes_the_session(self, inside):
        dive.attach(7)
        assert inside == ([("handoff", 7)], [(60, signal.SIGHUP)])

    def test_in_tmux_it_hands_off_without_closing(self, inside, capsys):
        self.sshd = None
        dive.attach(7)
        assert inside == ([("handoff", 7)], [])
        assert "detach tmux" in capsys.readouterr().err

    def test_vscode_opens_on_your_machine_and_stays(self, inside):
        dive.attach(7, vscode=True)
        assert inside == ([("vscode", 7)], [])

    def test_no_link_keeps_the_hint(self, inside, capsys):
        self.link = False
        dive.attach(7)
        assert inside[1] == []
        assert "cuttle dive attach 7" in capsys.readouterr().err

    def test_a_head_dive_stays_put(self, inside, monkeypatch):
        monkeypatch.setattr(dive, "_wait_ready", lambda i: {"head": True})
        dive.attach(7)
        assert inside == ([], [])


def test_enter_attaches_after_a_handoff(ccc_home, monkeypatch):
    monkeypatch.setattr(tidepool, "connect",
                        lambda: {"state": "active", "user": "will", "used_gb": 1, "size_gb": 25})
    monkeypatch.setattr(tidepool.config, "cluster_name", lambda: "lab")

    def ssh(argv):   # the inner cuttle, over the forwarded socket, then the session closes
        monkeypatch.setenv(transfer.FWD_ENV, argv[argv.index("-R") + 1].split(":", 1)[1])
        transfer.notify("handoff", 7)
        return type("R", (), {"returncode": 255})()
    monkeypatch.setattr(tidepool.subprocess, "run", ssh)
    attached = []
    monkeypatch.setattr(dive, "attach", lambda i: attached.append(i))
    tidepool.enter()
    assert attached == [7]


class TestHolders:
    """Processes that were in the main folder before its lock: on the folder underneath, not on the
    read-only bind — told apart by mount id."""

    LOCK = 999999

    @pytest.fixture
    def proc(self, tmp_path):
        folder = tmp_path / "home" / "main"
        (folder / "sub").mkdir(parents=True)
        p = tmp_path / "proc"
        (p / "self").mkdir(parents=True)
        (p / "self" / "mountinfo").write_text(
            f"1 0 7:1 / / rw - ext4 /dev/x rw\n{self.LOCK} 1 7:1 /main {folder} ro - ext4 /dev/x rw\n")
        return p, folder

    def _pid(self, p, pid, cwd, fds=(), cmd="zsh"):
        d = p / str(pid)
        (d / "fd").mkdir(parents=True)
        (d / "fdinfo").mkdir()
        (d / "cwd").symlink_to(cwd)
        (d / "cmdline").write_bytes(cmd.encode() + b"\0-i\0")
        for n, (target, mnt) in enumerate(fds):
            (d / "fd" / str(n)).symlink_to(target)
            (d / "fdinfo" / str(n)).write_text(f"pos:\t0\nflags:\t02\nmnt_id:\t{mnt}\n")

    def test_a_shell_already_in_the_folder_is_named(self, proc, tmp_path):
        p, folder = proc
        self._pid(p, 10, folder / "sub")   # its cwd is not on the lock's mount
        self._pid(p, 11, tmp_path)         # elsewhere
        assert dive.holders(str(folder), p) == ["10 zsh -i"]

    def test_an_open_file_off_the_lock_is_named(self, proc, tmp_path):
        p, folder = proc
        self._pid(p, 12, tmp_path, fds=[(folder / "a.txt", 5)], cmd="vim")
        self._pid(p, 13, tmp_path, fds=[(folder / "b.txt", self.LOCK)], cmd="less")   # opened after
        assert dive.holders(str(folder), p) == ["12 vim -i"]

    def test_a_folder_that_is_not_locked_has_none(self, proc, tmp_path):
        p, folder = proc
        self._pid(p, 10, folder)
        assert dive.holders(str(tmp_path / "home" / "other"), p) == []

    def test_a_sibling_with_the_same_prefix_is_not_inside(self):
        assert not dive._under("/home/u/main2", "/home/u/main")
        assert dive._under("/home/u/main", "/home/u/main") and dive._under("/home/u/main/x", "/home/u/main")

    def test_inside_in_tmux_it_warns(self, monkeypatch, capsys):
        monkeypatch.setattr(dive, "in_container", lambda: True)
        monkeypatch.setattr(dive, "_wait_ready",
                            lambda i: {"head": False, "node": "arm1", "user": "u", "main": "m"})
        monkeypatch.setattr(transfer, "notify", lambda op, i: True)
        monkeypatch.setattr(dive, "_session_sshd", lambda: None)
        monkeypatch.setattr(dive, "holders", lambda f: ["10 zsh"] if f == "/home/u/m" else [])
        dive.attach(7)
        err = capsys.readouterr().err
        assert "1 process(es) were in ~/m" in err and "10 zsh" in err


def test_from_your_machine_it_asks_the_tidepool(monkeypatch):
    monkeypatch.setattr(dive.config, "cluster_name", lambda: "lab")
    ran = []
    monkeypatch.setattr(dive.subprocess, "run", lambda a, **k: ran.append(a) or
                        type("R", (), {"returncode": 0, "stdout": "10 zsh\n"})())
    assert dive._remote_holders("/home/u/m") == ["10 zsh"]
    assert ran == [["ssh", "-o", "BatchMode=yes", "lab", "cuttle", "dive", "holders", "/home/u/m"]]

