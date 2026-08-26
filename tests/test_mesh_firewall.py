"""The mesh firewall exemption — keeping ccc0 out of a personal Tailscale's anti-spoof DROP.

The bug this covers: a personal tailscaled installs `ts-input: ! -i tailscale0 -s 100.64.0.0/10
-j DROP`, CCC's mesh addresses are inside that /10 and arrive on ccc0, so every inbound mesh packet is
dropped. It hid for months because ufw's `iptables-restore` deletes the chain outright, so whichever of
ufw/tailscaled touched the table last decided whether a box worked — until a reboot re-rolled it.

Tested through the SHIPPED script text (`mesh._FW_SCRIPT`), not a re-implementation of it, since that
string is the artifact that actually runs on every node. The firewall tools are recording fakes on
PATH: what matters is not only *whether* a rule is added but **where**, because the exemption is worth
nothing below the DROP it is meant to pre-empt.
"""

import importlib.util
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

from cuttle import config, mesh

DROP = '-A ts-input ! -i tailscale0 -s 100.64.0.0/10 -j DROP'
NFT_DROP = '  iifname != "tailscale0" ip saddr 100.64.0.0/10 drop'


@pytest.fixture
def fw(tmp_path, monkeypatch):
    """The shipped script, loaded as a module, with root faked.

    `os.geteuid` is patched rather than the script run under sudo: the script refuses to run
    unprivileged on purpose (see `test_unprivileged_refuses`), and every real invocation is a root
    systemd one-shot.

    Via monkeypatch, and that is not a style preference. `mod.os` is not a copy — it is THE `os`
    module, shared with every other test in this process — so a plain assignment leaves the whole
    suite believing it runs as root until it exits. It did: `test_unprivileged_refuses` skipped
    itself.
    """
    path = tmp_path / "ccc-mesh-firewall.py"
    path.write_text(mesh._FW_SCRIPT)
    spec = importlib.util.spec_from_file_location("ccc_mesh_firewall", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    return mod


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """Recording fakes for iptables/nft/systemctl, and nothing else on PATH.

    PATH is REPLACED, not prepended: leaving the real directories reachable means a test that omits
    `nft` to model a box without it silently finds /usr/sbin/nft instead, and the case goes untested.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    log.touch()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setenv("CCC_TEST_LOG", str(log))

    def install(name: str, body: str) -> None:
        p = bindir / name
        p.write_text('#!/bin/sh\necho "' + name + ' $@" >> "$CCC_TEST_LOG"\n' + textwrap.dedent(body))
        p.chmod(0o755)

    install.calls = lambda: log.read_text().splitlines()
    return install


def _iptables(chain_present: bool, exemption_present: bool) -> str:
    return f'''
        case "$1" in
          -S) [ "$2" = ts-input ] && {{ {'echo ' + repr(DROP) + '; exit 0' if chain_present
                                        else 'exit 1'}; }}; exit 1;;
          -C) exit {0 if exemption_present else 1};;
          -I) exit 0;;
          -D) exit 0;;
        esac
        exit 1
    '''


def _nft(chain_present: bool, exemption_present: bool) -> str:
    listing = NFT_DROP + ('\\n  iifname "ccc0" return' if exemption_present else '')
    body = f'printf %s\\\\n {listing!r}; exit 0' if chain_present else 'exit 1'
    return f'''
        if [ "$1 $2 $5" = "list chain ts-input" ]; then
          {body}
        fi
        exit 0
    '''


class TestIptablesBackend:
    def test_a_missing_exemption_is_inserted_above_the_drop(self, fw, tools):
        """Position 1, not appended. A RETURN below the DROP never executes — the packet is already
        gone — so `-A` here would look like a fix and change nothing."""
        tools("iptables", _iptables(chain_present=True, exemption_present=False))
        tools("nft", "exit 1\n")
        tools("systemctl", "exit 0\n")
        assert fw.main(config.CCC_TS_TUN, config.TS_INPUT_CHAIN) == 0
        assert "iptables -I ts-input 1 -i ccc0 -j RETURN" in tools.calls()

    def test_an_existing_exemption_is_left_alone(self, fw, tools):
        """Runs every 60s forever: a re-insert per tick would grow the chain without bound."""
        tools("iptables", _iptables(chain_present=True, exemption_present=True))
        tools("nft", "exit 1\n")
        tools("systemctl", "exit 0\n")
        assert fw.main(config.CCC_TS_TUN, config.TS_INPUT_CHAIN) == 0
        assert not [c for c in tools.calls() if c.startswith("iptables -I")]

    def test_the_nft_backend_is_not_consulted_once_iptables_answers(self, fw, tools):
        """iptables-nft and native nftables both surface a chain called ts-input in table `ip filter`.
        Probing both would insert the exemption twice on every iptables-backed box."""
        tools("iptables", _iptables(chain_present=True, exemption_present=False))
        tools("nft", _nft(chain_present=True, exemption_present=False))
        tools("systemctl", "exit 0\n")
        fw.main(config.CCC_TS_TUN, config.TS_INPUT_CHAIN)
        assert not [c for c in tools.calls() if c.startswith("nft")]


class TestNftablesBackend:
    def test_a_native_nft_chain_is_found_and_prepended_to(self, fw, tools):
        """A tailscaled that chose the native backend puts ts-input where the iptables binary cannot
        see it. Checking only iptables would report 'nothing to do' on a blackholed box — the exact
        silent-success shape of the original bug."""
        tools("iptables", _iptables(chain_present=False, exemption_present=False))
        tools("nft", _nft(chain_present=True, exemption_present=False))
        tools("systemctl", "exit 0\n")
        assert fw.main(config.CCC_TS_TUN, config.TS_INPUT_CHAIN) == 0
        # `insert`, never `add`: add appends, which puts the RETURN below the DROP.
        assert 'nft insert rule ip filter ts-input iifname "ccc0" return' in tools.calls()

    def test_an_existing_nft_exemption_is_left_alone(self, fw, tools):
        tools("iptables", _iptables(chain_present=False, exemption_present=False))
        tools("nft", _nft(chain_present=True, exemption_present=True))
        tools("systemctl", "exit 0\n")
        assert fw.main(config.CCC_TS_TUN, config.TS_INPUT_CHAIN) == 0
        assert not [c for c in tools.calls() if "insert" in c]

    def test_tailscales_own_drop_does_not_read_as_our_exemption(self, fw, tools):
        """The presence check is a substring match on the nft listing. Tailscale's DROP also names an
        interface (`iifname != "tailscale0"`), so a looser match would see it and skip the insert."""
        tools("iptables", _iptables(chain_present=False, exemption_present=False))
        tools("nft", _nft(chain_present=True, exemption_present=False))
        tools("systemctl", "exit 0\n")
        fw.main(config.CCC_TS_TUN, config.TS_INPUT_CHAIN)
        assert [c for c in tools.calls() if "insert" in c]


class TestNoChainToExemptFrom:
    def test_no_personal_tailscale_is_a_silent_no_op(self, fw, tools):
        """Most donor boxes. Also the state of a box whose ufw reload wiped the chain — which removes
        the DROP too, so there is genuinely nothing to be exempt from."""
        tools("iptables", _iptables(chain_present=False, exemption_present=False))
        tools("nft", _nft(chain_present=False, exemption_present=False))
        tools("systemctl", "exit 0\n")
        assert fw.main(config.CCC_TS_TUN, config.TS_INPUT_CHAIN) == 0

    def test_an_unreadable_backend_under_a_live_tailscaled_fails_loudly(self, fw, tools):
        """The one case that must not pass quietly: a personal tailscaled is running and we cannot
        read its chain, which is indistinguishable from 'all clear' unless it is called out. Non-zero
        so `systemctl status ccc-mesh-firewall` shows it."""
        tools("iptables", _iptables(chain_present=False, exemption_present=False))
        tools("systemctl", "exit 0\n")   # nft deliberately absent
        assert fw.main(config.CCC_TS_TUN, config.TS_INPUT_CHAIN) == 1

    def test_a_missing_tool_without_a_personal_tailscaled_stays_quiet(self, fw, tools):
        """Same blindness, no daemon to be blind about. Alerting here would cry wolf every 60s on any
        box that simply has no nftables package installed."""
        tools("iptables", _iptables(chain_present=False, exemption_present=False))
        tools("systemctl", "exit 3\n")   # inactive
        assert fw.main(config.CCC_TS_TUN, config.TS_INPUT_CHAIN) == 0


def test_unprivileged_refuses(tmp_path):
    """Run by hand without sudo, `iptables -S` exits non-zero exactly as it does when the chain is
    absent — so without this guard a hand-run would report 'nothing to do' on a blackholed box."""
    if os.geteuid() == 0:
        pytest.skip("needs an unprivileged euid to exercise the guard")
    path = tmp_path / "ccc-mesh-firewall.py"
    path.write_text(mesh._FW_SCRIPT)
    r = subprocess.run([sys.executable, str(path), config.CCC_TS_TUN, config.TS_INPUT_CHAIN],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "root" in r.stderr


def test_the_timer_pins_its_accuracy():
    """systemd's default AccuracySec is 1min, which would stretch the 60s interval to an effective
    60-120s and quietly double the worst-case blackhole after a tailscaled restart."""
    assert f"OnUnitActiveSec={config.MESH_FW_INTERVAL_S}s" in mesh._FW_TIMER_UNIT
    assert "AccuracySec=1s" in mesh._FW_TIMER_UNIT


def test_the_service_runs_as_root():
    """Unlike ccc-monitor and ccc-backup, which run as `ccc`. Reading and writing netfilter needs
    root, and a User= line here would make every tick a silent no-op."""
    assert "User=" not in mesh._FW_SERVICE_UNIT
    assert str(config.MESH_FW_SCRIPT) in mesh._FW_SERVICE_UNIT
