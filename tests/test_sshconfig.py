"""`~/.ssh/ccc.config` and `--vscode` — reaching a Pod with something that is not this CLI.

What is tested here: the alias namespaces, the key's new lifetime (a file under ~/.ccc that dies with
its Tide Pool, not a tempfile that died with `ssh`), the generated-in-full config and the single
`Include` line cuttle adds to the User's own file, and the `--vscode` branch of the attach. Where
`ssh` is installed the generated file is parsed by ssh ITSELF rather than by a regex here.

NOT tested: a real VS Code, a real remote session — same hardware carve-out as the rest of the Tide
Pool (see test_tidepool.py).
"""

import shutil
import stat
import subprocess

import pytest

from ccc_head_api import spawns as head_spawns
from cuttle import config, sshconfig
from cuttle import tidepool as cli_tidepool


def _row(spawn_id=58, workspace=None, port=41007, proj="/tmp/ccc-58/proj"):
    return sshconfig.record(spawn_id, private_key="-----BEGIN OPENSSH PRIVATE KEY-----",
                            port=port, user="ccc-u60002", host="head", workspace=workspace,
                            proj=proj)


class TestTheTwoNamespaces:
    """A **Workspace** may legally be named `58`; so may a Spawn be numbered 58."""

    def test_a_tide_pool_is_addressed_by_id(self):
        assert sshconfig.alias(58, None) == "ccc-tp-58"

    def test_a_visit_is_addressed_by_workspace_name(self):
        """Not by the visiting Spawn's id: that changes every visit, and an alias that changed with
        it is one nobody could put in a bookmark."""
        assert sshconfig.alias(4242, "vlearn") == "ccc-ws-vlearn"

    def test_a_workspace_named_like_an_id_does_not_collide(self):
        assert sshconfig.alias(1, "58") != sshconfig.alias(58, None)


class TestTheKeyOutlivesTheCommand:
    def test_it_is_written_where_the_alias_points(self, ccc_home):
        path = _row()
        assert path == config.TIDEPOOL_DIR / "58.key"
        assert path.read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY-----")

    def test_it_is_private_from_the_moment_it_exists(self, ccc_home):
        """ssh refuses a group-readable IdentityFile outright — and a key that is briefly world-
        readable on a shared box has already leaked by the time a chmod lands."""
        path = _row()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(config.TIDEPOOL_DIR.stat().st_mode) == 0o700

    def test_it_ends_in_a_newline(self, ccc_home):
        """ssh rejects a private key whose final line is unterminated."""
        assert _row().read_text().endswith("\n")

    def test_forgetting_takes_both_files(self, ccc_home):
        _row()
        sshconfig.forget(58)
        assert list(config.TIDEPOOL_DIR.iterdir()) == []


class TestTheGeneratedConfig:
    def test_a_live_session_gets_a_host_block(self, ccc_home):
        _row()
        sshconfig.refresh(live={58})
        text = config.SSH_CCC_CONFIG.read_text()
        assert "Host ccc-tp-58" in text
        assert "  Port 41007" in text
        assert "  User ccc-u60002" in text
        assert f"  IdentityFile {config.TIDEPOOL_DIR / '58.key'}" in text

    def test_it_is_rewritten_whole_rather_than_appended_to(self, ccc_home):
        """No merge, no marker-delimited region to get out of step. The file IS the session list."""
        _row(58)
        sshconfig.refresh(live={58})
        _row(59, port=41008, proj=None)
        sshconfig.refresh(live={59})
        text = config.SSH_CCC_CONFIG.read_text()
        assert text.count("Host ") == 1
        assert "ccc-tp-59" in text and "ccc-tp-58" not in text

    def test_an_ended_session_loses_its_key_too(self, ccc_home):
        _row(58)
        sshconfig.refresh(live=set())
        assert not (config.TIDEPOOL_DIR / "58.key").exists()
        assert "Host " not in config.SSH_CCC_CONFIG.read_text()

    def test_an_unreachable_head_prunes_nothing(self, ccc_home):
        """`None` is "could not ask", not "none of them". A Head that is down is no evidence a
        session ended, and deleting the key on a failed request locks a User out of a Pod that is
        still running and still billing them."""
        _row(58)
        sshconfig.refresh(live=None)
        assert (config.TIDEPOOL_DIR / "58.key").exists()
        assert "Host ccc-tp-58" in config.SSH_CCC_CONFIG.read_text()

    def test_a_corrupt_record_is_dropped_rather_than_raised(self, ccc_home):
        _row(58)
        (config.TIDEPOOL_DIR / "58.json").write_text("{not json")
        sshconfig.refresh(live={58})
        assert "Host " not in config.SSH_CCC_CONFIG.read_text()

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
        got = self._resolved("ccc-tp-58")
        assert got["hostname"] == "head"
        assert got["port"] == "41007"
        assert got["user"] == "ccc-u60002"
        assert got["identitiesonly"] == "yes"
        assert str(config.TIDEPOOL_DIR / "58.key") in got["identityfile"]

    def test_a_visits_alias_resolves_too(self, ccc_home):
        _row(4242, workspace="vlearn", port=41008)
        sshconfig.refresh(live={4242})
        assert self._resolved("ccc-ws-vlearn")["port"] == "41008"

    def test_nothing_else_matches_it(self, ccc_home):
        """The blocks are `Host <alias>`, not `Host ccc-*` — an unknown name must fall through."""
        _row()
        sshconfig.refresh(live={58})
        assert self._resolved("ccc-tp-999")["hostname"] == "ccc-tp-999"


class TestTheProjectFolder:
    """VS Code opens a FOLDER, and the folder is the Pod's working directory."""

    def test_a_donors_arm(self):
        assert head_spawns.pod_project_dir(58, head_arm=False) == "/tmp/ccc-58/proj"

    def test_the_heads_own_arm(self):
        assert head_spawns.pod_project_dir(58, head_arm=True) == f"{head_spawns.SCRATCH_DIR}/58/proj"

    def test_the_two_scratch_forms_agree_with_the_job_script(self):
        """Both forms are written longhand in JOB_SCRIPT, and this helper reads them back. If the
        script's scratch layout moves, this is what says so instead of a VS Code window opening on
        an empty directory."""
        script = head_spawns.JOB_SCRIPT
        assert 'scratch="%%HEAD_SCRATCH%%/$jid"' in script
        assert 'scratch="/tmp/ccc-$jid"' in script
        assert script.count('proj="$scratch/proj"') == 2


class TestTheAttach:
    """`_wait_and_attach` with the network taken out of it."""

    @pytest.fixture
    def placed(self, monkeypatch, ccc_home):
        monkeypatch.setattr(cli_tidepool, "_wait_for_running", lambda _id: None)
        monkeypatch.setattr(cli_tidepool, "_wait_for_relay",
                            lambda _id: (41007, "PRIVATEKEY", "ccc-u60002", "/tmp/ccc-58/proj"))
        monkeypatch.setattr(cli_tidepool, "_deadline", lambda _id: None)
        monkeypatch.setattr(cli_tidepool, "_live_ids", lambda: {58})
        monkeypatch.setattr(cli_tidepool, "_report_ending", lambda *a: None)
        ran: list[list] = []
        monkeypatch.setattr(subprocess, "run", lambda argv, *a, **kw: ran.append(argv))
        launched: list[list] = []
        monkeypatch.setattr(sshconfig.shutil, "which", lambda _n: "/usr/bin/code")
        monkeypatch.setattr(sshconfig.subprocess, "Popen",
                            lambda argv, **kw: launched.append(argv))
        return ran, launched

    def test_a_terminal_session_still_execs_ssh(self, placed):
        ran, launched = placed
        cli_tidepool._wait_and_attach(58)
        assert ran and ran[0][0] == "ssh"
        assert launched == []

    def test_the_key_is_still_there_afterwards(self, placed):
        """It used to be unlinked the moment `ssh` exited, which made it useless to anything that is
        not this process — and VS Code is exactly that."""
        cli_tidepool._wait_and_attach(58)
        assert (config.TIDEPOOL_DIR / "58.key").is_file()

    def test_the_alias_is_live_without_anyone_asking_for_vscode(self, placed):
        cli_tidepool._wait_and_attach(58)
        assert "Host ccc-tp-58" in config.SSH_CCC_CONFIG.read_text()

    def test_a_lagging_queue_does_not_sweep_the_session_being_attached(self, monkeypatch, placed):
        """squeue is a poll behind now and then. Trusting it here would delete the key this very
        attach is about to hand to `ssh`."""
        monkeypatch.setattr(cli_tidepool, "_live_ids", lambda: set())
        cli_tidepool._wait_and_attach(58)
        assert (config.TIDEPOOL_DIR / "58.key").is_file()
        assert "Host ccc-tp-58" in config.SSH_CCC_CONFIG.read_text()

    def test_vscode_is_launched_at_the_project_folder(self, placed):
        ran, launched = placed
        cli_tidepool._wait_and_attach(58, vscode=True)
        assert launched == [["/usr/bin/code", "--remote", "ssh-remote+ccc-tp-58", "/tmp/ccc-58/proj"]]
        assert ran == []   # no terminal shell

    def test_a_visit_is_launched_by_its_workspace_alias(self, placed):
        _, launched = placed
        cli_tidepool._wait_and_attach(58, workspace="vlearn", vscode=True)
        assert launched[0][2] == "ssh-remote+ccc-ws-vlearn"

    def test_an_older_head_sending_no_folder_still_opens(self, monkeypatch, placed):
        """`proj` is new on the wire. Without it VS Code opens on the host and the User picks."""
        monkeypatch.setattr(cli_tidepool, "_wait_for_relay",
                            lambda _id: (41007, "PRIVATEKEY", "ccc-u60002", None))
        _, launched = placed
        cli_tidepool._wait_and_attach(58, vscode=True)
        assert launched == [["/usr/bin/code", "--remote", "ssh-remote+ccc-tp-58"]]

    def test_no_code_on_path_says_so_and_leaves_the_alias(self, monkeypatch, placed, capsys):
        monkeypatch.setattr(sshconfig.shutil, "which", lambda _n: None)
        _, launched = placed
        cli_tidepool._wait_and_attach(58, vscode=True)
        assert launched == []
        assert "Host ccc-tp-58" in config.SSH_CCC_CONFIG.read_text()


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
