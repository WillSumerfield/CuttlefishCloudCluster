"""The **Cluster name** and the trust that makes it usable.

Two changes that only work together. The name (`ccc.lab`) is carried by /etc/hosts because CCC has
no DNS, and the cluster's CA is installed into every box's trust stores so the Dashboard loads without
a warning. Each failure here is silent on the box that works: a hosts line that accumulates duplicates
resolves to whichever came first, a teardown that leaves the CA behind leaves a machine trusting an
authority it no longer talks to, and a CA served without checking it signed anything is a stranger's
certificate installed as a root.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from fastapi.testclient import TestClient

from ccc_head_api import main as head_main
from cuttle import config, headapi, settings, system, teardown, tls
from cuttle import setup as setup_mod


@pytest.fixture
def hosts(monkeypatch, tmp_path):
    """A writable stand-in for /etc/hosts. `install_file` normally goes through sudo; here it just
    writes, so the line-management logic is what is under test rather than the privilege dance."""
    path = tmp_path / "hosts"
    path.write_text("127.0.0.1\tlocalhost\n127.0.1.1\tthis-box\n")
    real = system.install_file
    monkeypatch.setattr(system, "install_file",
                        lambda content, dest, mode="644", **kw: dest.write_text(content))
    monkeypatch.setattr(system, "Path", lambda p: path if str(p) == "/etc/hosts" else __import__(
        "pathlib").Path(p))
    yield path
    monkeypatch.setattr(system, "install_file", real)


class TestTheHostsLine:
    def test_the_name_resolves_to_the_address_it_was_given(self, hosts):
        system.write_hosts_entry("10.0.0.5")
        assert f"10.0.0.5\tccc.lab {config.HOSTS_MARKER}" in hosts.read_text().splitlines()

    def test_the_box_s_own_entries_survive(self, hosts):
        system.write_hosts_entry("10.0.0.5")
        assert "127.0.0.1\tlocalhost" in hosts.read_text()
        assert "127.0.1.1\tthis-box" in hosts.read_text()

    def test_re_pointing_rewrites_rather_than_appends(self, hosts):
        """A full teardown+setup is the normal redeploy here, and the Head's address can change
        between them. Appending would leave the box resolving the name to whichever line came first —
        the old address — with the new one below it and no symptom but a timeout."""
        system.write_hosts_entry("10.0.0.5")
        system.write_hosts_entry("10.0.0.9")
        lines = [ln for ln in hosts.read_text().splitlines() if "ccc.lab" in ln]
        assert lines == [f"10.0.0.9\tccc.lab {config.HOSTS_MARKER}"]

    def test_writing_the_same_line_twice_touches_nothing(self, hosts, monkeypatch):
        """setup runs this on every invocation. Rewriting an unchanged root-owned file is a mutation
        with nothing to show for it."""
        system.write_hosts_entry("10.0.0.5")
        writes = []
        monkeypatch.setattr(system, "install_file",
                            lambda *a, **kw: writes.append(a))
        system.write_hosts_entry("10.0.0.5")
        assert writes == []

    def test_an_unmarked_line_is_replaced_rather_than_joined(self, hosts):
        """A line for the name without our marker (added by hand, say). Matching on the marker alone
        would leave it and append a second line for the same name, which resolves to whichever came
        first — not the address setup just wrote."""
        hosts.write_text(f"127.0.0.1\tlocalhost\n10.0.0.5\tccc.lab\n")
        system.write_hosts_entry("10.0.0.9")
        lines = [ln for ln in hosts.read_text().splitlines() if "ccc.lab" in ln]
        assert lines == [f"10.0.0.9\tccc.lab {config.HOSTS_MARKER}"]

    def test_teardown_takes_an_inherited_line_too(self, hosts):
        """The same line, at the other end of the box's life. A teardown that left it behind would
        leave the box resolving the name to a Head it no longer talks to — which is the one outcome
        `untrust_and_unname` exists to prevent."""
        hosts.write_text(f"127.0.0.1\tlocalhost\n10.0.0.5\tccc.lab\n")
        system.remove_hosts_entry()
        assert "ccc.lab" not in hosts.read_text()
        assert "127.0.0.1\tlocalhost" in hosts.read_text()

    def test_a_comment_mentioning_the_name_is_not_a_line_that_resolves_it(self, hosts):
        """Matching by name means matching the NAME FIELDS. Someone's note about the cluster is not
        an entry, and eating it would make cuttle a thing that edits your comments."""
        note = f"# ccc.lab is the cluster, ask the admin"
        hosts.write_text(f"127.0.0.1\tlocalhost\n{note}\n")
        system.write_hosts_entry("10.0.0.9")
        system.remove_hosts_entry()
        assert note in hosts.read_text().splitlines()

    def test_teardown_takes_our_line_and_only_ours(self, hosts):
        system.write_hosts_entry("10.0.0.5")
        system.remove_hosts_entry()
        text = hosts.read_text()
        assert "ccc.lab" not in text
        assert "127.0.0.1\tlocalhost" in text and "127.0.1.1\tthis-box" in text


    def test_teardown_on_a_box_that_forgot_the_name_takes_our_marked_line(self, hosts, monkeypatch,
                                                                           ccc_home):
        system.write_hosts_entry("10.0.0.5")
        config.CLUSTER_NAME_FILE.unlink()
        system.remove_hosts_entry()
        assert "ccc.lab" not in hosts.read_text() and "localhost" in hosts.read_text()


class TestChoosingTheName:
    """`setup --head` asks it once; Arms get it at enrollment, clients from `/cluster-name` or login."""

    def test_a_client_falls_back_to_what_login_saved(self, ccc_home):
        config.CLUSTER_NAME_FILE.unlink()
        settings.save({"cluster": "kraken"})
        assert (config.domain(), config.registry()) == ("ccc.kraken", "ccc.kraken:5000")

    def test_a_box_that_knows_nothing_says_log_in(self, ccc_home):
        config.CLUSTER_NAME_FILE.unlink()
        with pytest.raises(RuntimeError, match="cuttle login"):
            config.cluster_name()

    def test_the_default_is_the_hostname_made_legal(self, monkeypatch):
        monkeypatch.setattr(setup_mod.system, "hostname", lambda: "Dex_Box.doc.ic.ac.uk")
        assert setup_mod._default_cluster_name() == "dex-box"

    def test_a_bad_answer_is_re_asked(self, monkeypatch):
        config.CLUSTER_NAME_FILE.unlink()
        said = iter(["Glados!", "glados"])
        monkeypatch.setattr(setup_mod.ui, "ask_text", lambda msg, default="": next(said))
        assert setup_mod.ask_cluster_name(update=False) == "glados"

    def test_renaming_a_named_head_is_refused(self, monkeypatch):
        monkeypatch.setattr(setup_mod.ui, "ask_text", lambda msg, default="": "other")
        with pytest.raises((typer.Exit, SystemExit, Exception)):
            setup_mod.ask_cluster_name(update=False)

    def test_an_update_asks_nothing(self, monkeypatch):
        monkeypatch.setattr(setup_mod.ui, "ask_text", lambda *a, **k: pytest.fail("asked"))
        assert setup_mod.ask_cluster_name(update=True) == "lab"

    def test_the_head_serves_it_without_a_session(self):
        r = TestClient(head_main.app).get("/cluster-name")
        assert r.status_code == 200 and r.text.strip() == "lab"

    def test_login_hands_it_over(self, head):
        r = head.post("/login", json={"username": "alice", "password": "hunter2"})
        assert r.json()["cluster"] == "lab"

    def test_a_cli_login_remembers_it(self, cli, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "CLUSTER_NAME_FILE", tmp_path / "absent")   # a bare client
        cli("login", input="alice\nhunter2\n")
        assert settings.load()["cluster"] == "lab" and config.cluster_name() == "lab"


class TestFirefoxPolicy:
    """Firefox reads neither the OS store nor the NSS db the Chromium family shares, so it gets its
    own enterprise policy. Two things have to be right at once and each fails *silently*: the policy
    has to be one Linux honours, and it has to name a path Firefox can actually read.

    The policy file is also shared — Firefox reads exactly one, and an organisation may already be
    using it — so cuttle merges in and unwinds only what it put there.
    """

    PEM = "-----BEGIN CERTIFICATE-----\nca\n-----END CERTIFICATE-----\n"

    @pytest.fixture
    def policies(self, monkeypatch, tmp_path):
        path = tmp_path / "policies.json"
        monkeypatch.setattr(config, "FIREFOX_POLICIES", path)
        monkeypatch.setattr(config, "FIREFOX_CA", tmp_path / "ccc-head.crt")
        monkeypatch.setattr(system, "install_file",
                            lambda content, dest, mode="644", **kw: dest.write_text(content))
        monkeypatch.setattr(system, "run", lambda argv, **kw: (
            Path(argv[2]).unlink(missing_ok=True) if argv[:2] == ["rm", "-f"] else None))
        return path

    def test_it_installs_the_certificate_rather_than_importing_roots(self, policies):
        """`ImportEnterpriseRoots` is a Windows/macOS policy. On Linux it is accepted, applied, and
        trusts nothing — which is indistinguishable from working until a page loads."""
        system._firefox_trust(True, self.PEM)
        certs = json.loads(policies.read_text())["policies"]["Certificates"]
        assert certs["Install"] == [str(config.FIREFOX_CA)]
        assert "ImportEnterpriseRoots" not in certs

    def test_the_certificate_lands_where_a_confined_firefox_can_read_it(self, policies):
        """Ubuntu's Firefox is a snap that can read `/etc/firefox` and nothing else of the host's.
        Pointing the policy at the copy under /usr/local/share/ca-certificates installs nothing."""
        system._firefox_trust(True, self.PEM)
        assert config.FIREFOX_CA.read_text() == self.PEM
        installed = json.loads(policies.read_text())["policies"]["Certificates"]["Install"][0]
        assert Path(installed).read_text() == self.PEM, "the policy names a path with no cert at it"

    def test_the_shipped_path_is_inside_firefox_s_readable_tree(self):
        """Guards the constant itself: /etc/firefox is the one host tree the snap is granted."""
        assert str(config.FIREFOX_CA).startswith("/etc/firefox/")

    def test_an_existing_policy_file_keeps_its_other_settings(self, policies):
        policies.write_text(json.dumps({"policies": {"DisableTelemetry": True}}))
        system._firefox_trust(True, self.PEM)
        doc = json.loads(policies.read_text())
        assert doc["policies"]["DisableTelemetry"] is True
        assert doc["policies"]["Certificates"]["Install"] == [str(config.FIREFOX_CA)]

    def test_someone_else_s_certificate_survives_both_ways(self, policies):
        """Install is a list, and an admin may already be trusting their own CA through it."""
        policies.write_text(json.dumps(
            {"policies": {"Certificates": {"Install": ["/etc/firefox/theirs.crt"]}}}))
        system._firefox_trust(True, self.PEM)
        assert json.loads(policies.read_text())["policies"]["Certificates"]["Install"] == [
            "/etc/firefox/theirs.crt", str(config.FIREFOX_CA)]
        system._firefox_trust(False)
        assert json.loads(policies.read_text())["policies"]["Certificates"]["Install"] == [
            "/etc/firefox/theirs.crt"]

    def test_teardown_takes_the_certificate_with_it(self, policies):
        system._firefox_trust(True, self.PEM)
        system._firefox_trust(False)
        assert not config.FIREFOX_CA.exists()

    def test_teardown_leaves_the_other_settings_behind(self, policies):
        policies.write_text(json.dumps({"policies": {"DisableTelemetry": True}}))
        system._firefox_trust(True, self.PEM)
        system._firefox_trust(False)
        doc = json.loads(policies.read_text())
        assert doc["policies"] == {"DisableTelemetry": True}

    def test_teardown_removes_a_file_that_was_only_ever_ours(self, policies):
        system._firefox_trust(True, self.PEM)
        system._firefox_trust(False)
        assert not policies.exists()

    def test_teardown_does_not_disturb_an_admin_s_own_enterprise_roots(self, policies):
        """Someone may have turned this on for their own CA. A CCC teardown turning it back off
        would break a trust decision that was never ours to make."""
        policies.write_text(json.dumps(
            {"policies": {"Certificates": {"ImportEnterpriseRoots": True}}}))
        system._firefox_trust(False)
        doc = json.loads(policies.read_text())
        assert doc["policies"]["Certificates"]["ImportEnterpriseRoots"] is True


class TestServingTheCa:
    """`GET /ca.pem` is open on purpose — it is a public key, and a trust anchor nobody can fetch is
    one nobody installs. It sits on the main API rather than the mesh-only Enroll API because a LAN
    client has no mesh to reach."""

    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        monkeypatch.setattr(head_main, "CA_CERT", tmp_path / "ccc-ca.crt")
        return TestClient(head_main.app)

    def test_it_needs_no_session(self, client):
        head_main.CA_CERT.write_text("-----BEGIN CERTIFICATE-----\nca\n")
        r = client.get("/ca.pem")
        assert r.status_code == 200
        assert r.text.startswith("-----BEGIN CERTIFICATE-----")

    def test_a_head_with_no_ca_says_so(self, client):
        assert client.get("/ca.pem").status_code == 503


class TestTrustOnFirstUse:
    """Fetching the CA over an unverified connection proves nothing by itself. What makes it a fact is
    checking that this CA actually signed the certificate the Head is serving — done by dialling back
    with it as the trust anchor, so stdlib checks the signature chain AND the name, where a hand-rolled
    issuer comparison would fail open."""

    def test_a_ca_that_signed_nothing_here_is_refused(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "PINS_DIR", tmp_path / "pins")
        monkeypatch.setattr(tls, "fetch_ca", lambda url: "-----BEGIN CERTIFICATE-----\nstranger\n")
        monkeypatch.setattr(tls, "signs_leaf", lambda url, pem: False)
        installed = []
        monkeypatch.setattr(tls, "install_ca", lambda url, pem: installed.append(pem))
        with pytest.raises(Exception):
            tls.ensure_ca(headapi.head_url("ccc.lab"))
        assert installed == [], "a CA that did not sign the served cert must never be installed"

    def test_an_existing_pin_asks_nothing(self, monkeypatch, tmp_path):
        pins = tmp_path / "pins"
        pins.mkdir()
        monkeypatch.setattr(config, "PINS_DIR", pins)
        url = headapi.head_url("ccc.lab")
        tls._pin_path(url).write_text("already trusted")
        monkeypatch.setattr(tls, "fetch_ca", lambda url: pytest.fail("re-fetched a known CA"))
        tls.ensure_ca(url)

    def test_a_plain_http_head_has_nothing_to_trust(self, monkeypatch):
        monkeypatch.setattr(tls, "fetch_ca", lambda url: pytest.fail("fetched over http"))
        tls.ensure_ca("http://ccc.lab")

    def test_the_pin_is_the_same_filename_on_every_client(self):
        """Every box in the cluster dials the same name now, so every box's pin has one name. It is
        also what build-installer bakes into a bundle, so the two must agree by construction."""
        assert tls._pin_path(headapi.head_url("ccc.lab")).name == f"ccc.lab_443.pem"


class TestCertutilArrivesOnDemand:
    """`certutil` (libnss3-tools) is what writes the Chromium-family store, and two callers reach the
    browser trust without it: an Arm trusts the CA at /enroll, a dozen steps ahead of its apt block,
    and a client that never ran `setup --client` trusts it on first contact with an https Head. Both
    used to die on a bare FileNotFoundError — `run` catches CalledProcessError alone, so nothing was
    even printed. It is installed here instead, and refused loudly when it cannot be."""

    @pytest.fixture
    def apt(self, monkeypatch):
        """Every `run` this path makes, with certutil absent until an install goes through."""
        calls: list[list[str]] = []
        present = []

        def fake_run(cmd, sudo=False, check=True, **kw):
            calls.append(cmd)
            if "install" in cmd:
                present.append(True)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(system, "run", fake_run)
        monkeypatch.setattr(system.shutil, "which", lambda _: "/usr/bin/certutil" if present else None)
        return calls

    def test_a_box_without_it_installs_it(self, apt):
        system.ensure_certutil()
        assert any("install" in c and config.NSS_TOOLS_PKG in c for c in apt)

    def test_the_index_is_refreshed_first(self, apt):
        """A client box may never have run `apt-get update`, and an install against no index fails
        with `unable to locate package` — which reads as a wrong package name."""
        system.ensure_certutil()
        assert "update" in apt[0]

    def test_a_box_that_already_has_it_never_touches_apt(self, apt, monkeypatch):
        monkeypatch.setattr(system.shutil, "which", lambda _: "/usr/bin/certutil")
        system.ensure_certutil()
        assert apt == []

    def test_a_failed_install_is_refused_not_warned(self, apt, monkeypatch):
        """The half-trusted box — OS store yes, browsers no — is the outcome worth refusing: it is
        exactly the state where cuttle says it worked and Chrome goes on warning."""
        import subprocess
        def boom(cmd, **kw):
            raise subprocess.CalledProcessError(1, cmd)
        monkeypatch.setattr(system, "run", boom)
        with pytest.raises(typer.Exit):
            system.ensure_certutil()

    def test_an_install_that_leaves_no_binary_is_refused_too(self, monkeypatch):
        monkeypatch.setattr(system, "run", lambda cmd, **kw: SimpleNamespace(returncode=0))
        monkeypatch.setattr(system.shutil, "which", lambda _: None)
        with pytest.raises(typer.Exit):
            system.ensure_certutil()

    def test_the_message_names_the_package_to_install_by_hand(self, capsys, monkeypatch):
        monkeypatch.setattr(system, "run", lambda cmd, **kw: SimpleNamespace(returncode=0))
        monkeypatch.setattr(system.shutil, "which", lambda _: None)
        with pytest.raises(typer.Exit):
            system.ensure_certutil()
        assert config.NSS_TOOLS_PKG in capsys.readouterr().err


class TestTeardownUndoesTheHostMutations:
    """Unconditionally, on every role — unlike the Head's own keypair, which `--all` gates so that a
    routine redeploy does not un-trust the fleet. A box that has LEFT is a different case: resolving
    the name to a Head it no longer talks to, and trusting a CA whose private key lives there, is
    exactly the residue a teardown exists to remove."""

    def test_it_removes_all_three(self, monkeypatch):
        done = []
        monkeypatch.setattr(teardown.system, "remove_hosts_entry", lambda: done.append("hosts"))
        monkeypatch.setattr(teardown.system, "run", lambda argv, **kw: done.append(argv))
        monkeypatch.setattr(teardown.system, "untrust_ccc_ca", lambda: done.append("ca"))
        teardown.untrust_and_unname()
        assert done == ["hosts", ["rm", "-f", str(config.CLUSTER_NAME_FILE)], "ca"]

    def test_it_runs_before_packages_are_purged(self):
        """`untrust_ccc_ca` needs `certutil`, which lives in libnss3-tools — and the purge takes that
        with it. Ordered the other way round, the NSS entry survives every teardown, silently."""
        import inspect
        for flow in (teardown._teardown_arm, teardown._teardown_local, teardown.teardown_full):
            src = inspect.getsource(flow)
            assert src.index("untrust_and_unname") < src.index("purge_packages()"), flow.__name__

    def test_every_role_does_it(self):
        import inspect
        for flow in (teardown._teardown_arm, teardown._teardown_local,
                     teardown.teardown_client, teardown.teardown_full):
            assert "untrust_and_unname" in inspect.getsource(flow), flow.__name__


class TestTheNameIsTheWholeAddress:
    """`https://ccc.lab`, with no port to remember or type. The CLI could always fill a port in
    for a bare host; a browser cannot be taught one, and the Dashboard is a browser surface — so the
    Head API serves https's own default instead."""

    def test_the_head_api_serves_the_default_https_port(self):
        assert config.HEAD_API_PORT == 443

    def test_the_unit_may_bind_a_privileged_port(self):
        """The Head API runs as the Mantle, not root, and nothing but this capability lets a non-root
        process bind below 1024. Without it the service does not misbehave — it never starts, with an
        EACCES buried in journalctl."""
        unit = headapi._head_unit("100.64.0.1")
        assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in unit
        assert f"--port {config.HEAD_API_PORT}" in unit

    def test_no_surface_shows_the_default_port(self):
        """`https://ccc.lab:443` is the same URL and half the point of the change."""
        assert headapi.head_url("ccc.lab") == f"https://ccc.lab"

    def test_a_bare_address_is_enough_for_login(self, ccc_home):
        """`--head` is a flag, so it takes a URL — but what a person has to hand is an address, and
        storing that verbatim leaves `urlparse` reading the whole thing as a path: empty hostname,
        a pin filename of `_443.pem`, and a dial that goes nowhere."""
        from cuttle import auth, settings
        auth._set_head("ccc.lab")
        assert settings.load()["head"] == f"https://ccc.lab"

    def test_a_deliberate_port_still_survives(self, ccc_home):
        from cuttle import auth, settings
        auth._set_head("https://head.lan:9000")
        assert settings.load()["head"] == "https://head.lan:9000"
