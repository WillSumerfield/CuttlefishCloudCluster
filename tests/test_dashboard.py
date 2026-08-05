"""`cuttle dashboard` — the address is on disk, so nobody has to remember it.

**Consequence tests only** (see `TESTING.md`). What outlives the screen here is *which* address gets
handed to a browser: the saved one is wrong on a WSL Arm, where the Windows browser has no route to
the mesh, and a silently-wrong URL is indistinguishable from a Head that is down.
"""

import subprocess

import pytest
import typer

from cuttle import config, dashboard, settings


@pytest.fixture
def wsl(monkeypatch, tmp_path):
    """A WSL box, with or without the dashboard forward `setup --arm` installs there."""
    def install(forwarded: bool):
        monkeypatch.setattr(dashboard.system, "is_wsl", lambda: True)
        unit = tmp_path / "ccc-dash-forward.socket"
        if forwarded:
            unit.write_text("[Socket]\n")
        monkeypatch.setattr(config, "DASH_FORWARD_SOCKET_UNIT", unit)
    return install


class TestTheAddressItOpens:

    def test_it_is_the_head_this_box_already_talks_to(self, ccc_home):
        settings.save({"head": "https://100.64.0.1:8082"})
        assert dashboard.url() == "https://100.64.0.1:8082/"

    def test_an_unpointed_box_says_so_instead_of_opening_nothing(self, ccc_home, capsys):
        settings.save({})
        with pytest.raises(typer.Exit):
            dashboard.url()
        assert "not pointed at a Head" in capsys.readouterr().err

    def test_a_wsl_arm_gets_the_localhost_forward_not_the_mesh_ip(self, ccc_home, wsl):
        """The CCC mesh lives inside WSL; the Windows browser cannot route to the Head's mesh IP.
        Handing it the saved URL opens a page that can never load."""
        wsl(forwarded=True)
        settings.save({"head": "https://100.64.0.1:8082"})
        assert dashboard.url() == f"https://localhost:{config.HEAD_API_PORT}/"

    def test_a_wsl_client_keeps_its_saved_url(self, ccc_home, wsl):
        """No `setup --arm` there, so no proxy is listening on localhost — the forward's URL would be
        a connection refused, while the saved LAN address works."""
        wsl(forwarded=False)
        settings.save({"head": "https://192.168.1.10:8082"})
        assert dashboard.url() == "https://192.168.1.10:8082/"


class TestHandingItToABrowser:

    def test_wsl_launches_through_windows(self, monkeypatch, wsl):
        """`webbrowser` probes for Linux handlers and, finding none under WSL, opens nothing while
        reporting success — the failure mode is a command that appears to work and does not."""
        wsl(forwarded=True)
        seen = {}
        monkeypatch.setattr(subprocess, "run", lambda argv, **k: seen.setdefault("argv", argv))
        monkeypatch.setattr(dashboard.webbrowser, "open",
                            lambda *a: pytest.fail("used webbrowser under WSL"))
        assert dashboard.launch("https://localhost:8082/") is True
        assert seen["argv"][0] == "explorer.exe"

    def test_the_url_is_printed_even_when_no_browser_opens(self, monkeypatch, capsys, ccc_home):
        """Over SSH or on a headless Head there is nothing to launch. The address is still the
        answer, so it must reach the terminal rather than being swallowed by the failure."""
        settings.save({"head": "https://100.64.0.1:8082"})
        monkeypatch.setattr(dashboard, "launch", lambda target: False)
        dashboard.open_dashboard()
        assert "https://100.64.0.1:8082/" in capsys.readouterr().out
