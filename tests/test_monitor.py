"""Head-box monitor — reading back what's already configured.

SMTP conf lives under HEAD_STATE_DIR (`drwx------ ccc ccc`), so every path op on it is a
permission question. Guards here exist because an unguarded `.exists()` can raise EACCES instead
of returning False, which crashed `setup --head` on an unreadable dir.
"""

import os
import subprocess

import pytest

from cuttle import config, monitor


def _fake_run(result: subprocess.CompletedProcess):
    return lambda *a, **k: result


class TestReadExistingConf:
    def test_an_unreadable_state_dir_reads_as_unconfigured(self, tmp_path, monkeypatch):
        """The regression: `Path.exists()` swallows only ENOENT/ENOTDIR/EBADF/ELOOP, never EACCES,
        so stat'ing a file in a 0700 dir owned by someone else RAISES — aborting setup for a
        prompt whose entire purpose is to be skippable."""
        if os.geteuid() == 0:
            pytest.skip("root traverses any directory; the failure needs an unprivileged euid")
        state = tmp_path / "head"
        state.mkdir(mode=0o000)
        monkeypatch.setattr(config, "MONITOR_CONF", state / "monitor.yaml")
        # sudo `cat` is what actually answers; a non-zero rc means absent OR unreadable, both of which
        # mean "nothing configured yet".
        monkeypatch.setattr(monitor, "run", _fake_run(subprocess.CompletedProcess([], 1, "", "")))
        assert monitor._read_existing_conf() == {}

    def test_existing_values_come_back_for_defaults(self, tmp_path, monkeypatch):
        """Why this function exists at all: a re-run of `setup --head` must let the operator hit Enter
        through every field and change nothing."""
        monkeypatch.setattr(config, "MONITOR_CONF", tmp_path / "monitor.yaml")
        out = "smtp_host: smtp.example.com\nsmtp_port: 587\nto_addr: me@example.com\n"
        monkeypatch.setattr(monitor, "run", _fake_run(subprocess.CompletedProcess([], 0, out, "")))
        conf = monitor._read_existing_conf()
        assert conf["smtp_host"] == "smtp.example.com"
        assert conf["to_addr"] == "me@example.com"
