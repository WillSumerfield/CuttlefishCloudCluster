"""`~/.ssh/ccc.config`: `Host glados` + one `Host glados-<id>` per live Dive, all with the User's own
key and the Tidepool's pinned host key; the single `Include` line cuttle adds to the User's config;
the attach that writes it. Where `ssh` is installed the file is parsed by ssh ITSELF.

NOT tested: a real VS Code, a real remote session — hardware list.
"""

import shutil
import stat
import subprocess

import pytest

from cuttle import config, sshconfig, tidepool
from cuttle import dive as cli_dive


def _row(dive_id=58, port=31007):
    sshconfig.record(dive_id, port=port, user="alice", host="head")


class TestTheAlias:
    def test_a_dive_is_glados_dash_id(self):
        assert sshconfig.alias(58) == "glados-58"


class TestTheGeneratedConfig:
    def test_a_live_dive_gets_a_pinned_block(self, ccc_home):
        _row()
        sshconfig.refresh(live={58})
        text = config.SSH_CCC_CONFIG.read_text()
        assert "Host glados-58" in text and "  Port 31007" in text
        assert f"  IdentityFile {config.TIDEPOOL_KEY}" in text
        assert f"  HostKeyAlias {config.TIDEPOOL_HOSTKEY_ALIAS}" in text
        assert "  StrictHostKeyChecking yes" in text and "/dev/null" not in text

    def test_it_is_rewritten_whole_rather_than_appended_to(self, ccc_home):
        _row()
        sshconfig.refresh(live={58})
        _row(59, 31008)
        sshconfig.refresh(live={59})
        text = config.SSH_CCC_CONFIG.read_text()
        assert "glados-58" not in text and text.count("Host glados-59") == 1

    def test_an_ended_dive_loses_its_record(self, ccc_home):
        _row()
        sshconfig.refresh(live=set())
        assert list(config.DIVE_DIR.iterdir()) == []

    def test_an_unreachable_head_prunes_nothing(self, ccc_home):
        _row()
        sshconfig.refresh(live=None)
        assert "Host glados-58" in config.SSH_CCC_CONFIG.read_text()

    def test_a_corrupt_record_is_dropped_rather_than_raised(self, ccc_home):
        _row()
        (config.DIVE_DIR / "58.json").write_text("{not json")
        sshconfig.refresh(live={58})
        assert "glados-58" not in config.SSH_CCC_CONFIG.read_text()

    def test_it_is_not_group_readable(self, ccc_home):
        _row()
        sshconfig.refresh(live={58})
        assert stat.S_IMODE(config.SSH_CCC_CONFIG.stat().st_mode) == 0o600


class TestTheUsersOwnConfig:
    def test_one_include_line_is_added_at_the_top(self, ccc_home):
        """Top, because ssh takes the FIRST value it sees for a keyword — a `Host *` block at the
        head of a hand-written config would otherwise win over the IdentityFile that matters."""
        config.SSH_DIR.mkdir(parents=True)
        config.SSH_CONFIG.write_text("Host *\n  IdentityFile ~/.ssh/id_ed25519\n")
        _row()
        sshconfig.refresh(live={58})
        text = config.SSH_CONFIG.read_text()
        assert text.splitlines()[0].startswith(sshconfig.INCLUDE_LINE)
        assert "Host *" in text   # nothing the User wrote is touched

    def test_it_is_not_added_twice(self, ccc_home):
        _row()
        sshconfig.refresh(live={58})
        sshconfig.refresh(live={58})
        text = config.SSH_CONFIG.read_text()
        assert text.count(sshconfig.INCLUDE_LINE) == 1

    def test_a_users_own_include_is_recognised(self, ccc_home):
        """Written by hand, in their own spelling — cuttle must not add a second."""
        config.SSH_DIR.mkdir(parents=True)
        config.SSH_CONFIG.write_text(f"Host work\n\n  {sshconfig.INCLUDE_LINE}\n")
        _row()
        sshconfig.refresh(live={58})
        assert config.SSH_CONFIG.read_text().count(sshconfig.INCLUDE_LINE) == 1

    def test_an_ssh_dir_that_cannot_be_made_is_not_fatal(self, ccc_home, monkeypatch, tmp_path):
        """A missing convenience, never a reason to fail an attach that is otherwise working."""
        blocker = tmp_path / "blocker"
        blocker.write_text("")   # a FILE where the directory would go
        monkeypatch.setattr(config, "SSH_DIR", blocker / "ssh")
        monkeypatch.setattr(config, "SSH_CONFIG", blocker / "ssh" / "config")
        monkeypatch.setattr(config, "SSH_CCC_CONFIG", blocker / "ssh" / "ccc.config")
        _row()
        sshconfig.refresh(live={58})   # must not raise


@pytest.mark.skipif(not shutil.which("ssh"), reason="needs a real ssh to parse the config")
class TestSshItselfAgrees:
    """`ssh -G` resolves a host exactly as a connection would. A block this CLI is sure of and ssh
    rejects is worth nothing, and no regex here can tell the difference."""

    def _resolved(self, alias):
        out = subprocess.run(["ssh", "-F", str(config.SSH_CCC_CONFIG), "-G", alias],
                             capture_output=True, text=True, check=True).stdout
        return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)

    def test_the_alias_resolves_to_the_relay(self, ccc_home):
        _row()
        sshconfig.refresh(live={58})
        got = self._resolved("glados-58")
        assert got["hostname"] == "head"
        assert got["port"] == "31007"
        assert got["user"] == "alice"
        assert got["identitiesonly"] == "yes"
        assert got["hostkeyalias"] == config.TIDEPOOL_HOSTKEY_ALIAS
        assert str(config.TIDEPOOL_KEY) in got["identityfile"]

    def test_nothing_else_matches_it(self, ccc_home):
        """The blocks are `Host <alias>`, not `Host glados-*` — an unknown name must fall through."""
        _row()
        sshconfig.refresh(live={58})
        assert self._resolved("glados-999")["hostname"] == "glados-999"


class TestTheAttach:
    @pytest.fixture
    def placed(self, ccc_home, monkeypatch):
        ran = []
        info = {"id": 58, "ready": True, "state": "running", "head": False, "node": "arm1",
                "port": 31007, "user": "alice", "main": "proj", "host_key": None}
        monkeypatch.setattr(tidepool, "_head_host", lambda: "head")
        monkeypatch.setattr(cli_dive.auth, "_api", lambda m, p, **k: info if p == "/dives/58" else
                            [{"id": 58, "state": "running", "start_time": None, "time_limit": None}])
        monkeypatch.setattr(cli_dive.subprocess, "run", lambda a, **k: ran.append(a))
        return ran

    def test_a_terminal_session_execs_ssh_on_the_alias(self, placed):
        cli_dive.attach(58)
        assert placed == [["ssh", "glados-58"]]
        assert "Host glados-58" in config.SSH_CCC_CONFIG.read_text()

    def test_vscode_opens_the_main_folder(self, placed, monkeypatch):
        opened = []
        monkeypatch.setattr(sshconfig, "open_vscode", lambda n, f: opened.append((n, f)) or True)
        cli_dive.attach(58, vscode=True)
        assert opened == [("glados-58", "/home/alice/proj")] and placed == []


class TestMainFolder:
    def test_outside_the_tidepool_it_is_named_from_home(self, monkeypatch):
        monkeypatch.setattr(cli_dive, "in_tidepool", lambda: False)
        assert cli_dive.main_rel("proj/a/") == "proj/a"
        assert cli_dive.main_rel("~/proj") == "proj"
        assert cli_dive.main_rel("/home/alice/proj") == "proj"

    def test_inside_it_defaults_to_the_cwd(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli_dive, "in_tidepool", lambda: True)
        (tmp_path / "proj" / "sub").mkdir(parents=True)
        assert cli_dive.main_rel(None, cwd=tmp_path / "proj", home=tmp_path) == "proj"
        assert cli_dive.main_rel("sub", cwd=tmp_path / "proj", home=tmp_path) == "proj/sub"
        with pytest.raises(BaseException):
            cli_dive.main_rel(None, cwd=tmp_path, home=tmp_path)    # ~ itself is not a main folder


class TestTeardownLeavesNothing:
    """"CCC leaves nothing behind" includes the User's own ssh config."""

    def test_the_generated_file_and_the_include_both_go(self, ccc_home):
        config.SSH_DIR.mkdir(parents=True)
        config.SSH_CONFIG.write_text("Host work\n  HostName work.example\n")
        _row()
        sshconfig.refresh(live={58})
        sshconfig.remove()
        assert not config.SSH_CCC_CONFIG.exists()
        text = config.SSH_CONFIG.read_text()
        assert sshconfig.INCLUDE_LINE not in text
        assert text.startswith("Host work")   # every other line, byte for byte

    def test_it_is_a_no_op_where_cuttle_never_wrote(self, ccc_home):
        sshconfig.remove()   # must not raise
        assert not config.SSH_CONFIG.exists()

    def test_teardown_calls_it(self, ccc_home, monkeypatch):
        """Wired, not merely available: ~/.ccc holds the keys the aliases name, and teardown removes
        it — so an untorn-down alias would point at a file that is gone."""
        from cuttle import teardown
        monkeypatch.setattr(teardown, "revoke_session", lambda: None)
        config.SSH_DIR.mkdir(parents=True)
        _row()
        sshconfig.refresh(live={58})
        teardown.remove_user_dirs()
        assert not config.SSH_CCC_CONFIG.exists()
        assert sshconfig.INCLUDE_LINE not in config.SSH_CONFIG.read_text()
