"""`cuttle dashboard` — the address is on disk, so nobody has to remember it.

**Consequence tests only** (see `TESTING.md`). What outlives the screen here is *which* address gets
handed to a browser, and on a guest the browser is not on this box at all: the saved URL carries the
**Cluster name**, which the OS around a Crostini install does not resolve. A silently-wrong
URL is indistinguishable from a Head that is down.
"""

import pytest
import typer

from cuttle import config, dashboard, settings


@pytest.fixture
def resolves(monkeypatch):
    """The **Cluster name**, resolvable — inside the guest, which is the only side that ever can."""
    monkeypatch.setattr(dashboard.socket, "gethostbyname",
                        lambda host: "192.168.1.10" if host == config.DOMAIN else host)


@pytest.fixture
def crostini(monkeypatch):
    """A Chromebook client. ChromeOS has no hosts file anyone can write, so the name is never an
    option there, and the owner cannot fix that either."""
    monkeypatch.setattr(dashboard.guest.system, "is_crostini", lambda: True)


class TestTheAddressItOpens:

    def test_it_is_the_head_this_box_already_talks_to(self, ccc_home):
        settings.save({"head": "https://100.64.0.1:8082"})
        assert dashboard.url() == "https://100.64.0.1:8082/"

    def test_an_unpointed_box_says_so_instead_of_opening_nothing(self, ccc_home, capsys):
        settings.save({})
        with pytest.raises(typer.Exit):
            dashboard.url()
        assert "not pointed at a Head" in capsys.readouterr().err

    def test_a_chromebook_always_gets_the_address(self, ccc_home, crostini, resolves):
        """Crostini's own /etc/hosts is fine and `cuttle` works; the browser is on the ChromeOS side,
        which has no hosts file at any privilege. Nothing can ever make the name work there."""
        settings.save({"head": f"https://{config.DOMAIN}:8082"})
        assert dashboard.url() == "https://192.168.1.10:8082/"

    def test_an_unresolvable_name_is_left_alone_rather_than_mangled(self, ccc_home, crostini,
                                                                    monkeypatch):
        """A box whose own /etc/hosts line is gone has a bigger problem than its browser's URL, and
        inventing an address for it would report that problem as a Dashboard that will not load."""
        monkeypatch.setattr(dashboard.socket, "gethostbyname",
                            lambda host: (_ for _ in ()).throw(OSError("no")))
        settings.save({"head": f"https://{config.DOMAIN}:8082"})
        assert dashboard.url() == f"https://{config.DOMAIN}:8082/"


class TestHandingItToABrowser:

    def test_the_url_is_printed_even_when_no_browser_opens(self, monkeypatch, capsys, ccc_home):
        """Over SSH or on a headless Head there is nothing to launch. The address is still the
        answer, so it must reach the terminal rather than being swallowed by the failure."""
        settings.save({"head": "https://100.64.0.1:8082"})
        monkeypatch.setattr(dashboard, "launch", lambda target: False)
        dashboard.open_dashboard()
        assert "https://100.64.0.1:8082/" in capsys.readouterr().out
