"""Reproducible package pinning — the three places a "pinned" version could silently stop being one.

Every failure here is invisible on the box that works and only surfaces on the *next* one: a donor
enrolled six months later gets a different apptainer, a rebuilt Pod image gets a different torch, or
`setup` dies at apt with an error that names neither the pin nor the file holding it.
"""

import hashlib
import json
import re
import subprocess
import tomllib

import pytest
import typer
from fastapi.testclient import TestClient

from ccc_head_api import enroll as enroll_api
from ccc_head_api import slurm
from cuttle import config, enroll, headapi, mesh, setup, templates


class TestUbuntuSnapshot:
    """The Ubuntu archive publishes one version per pocket, so an exact pin goes uninstallable the day
    it is superseded. An extra source pointed at snapshot.ubuntu.com keeps the pinned versions
    fetchable — without taking the donor's own apt with it."""

    def test_source_names_the_pinned_timestamp(self):
        src = templates.render_ubuntu_snapshot_list()
        assert config.UBUNTU_SNAPSHOT in src
        assert f"{config.UBUNTU_SNAPSHOT_URL}/{config.UBUNTU_SNAPSHOT}" in src

    def test_covers_both_pockets_of_the_pinned_release(self):
        """noble carries the slurm pins today; noble-updates is where one would land if it were ever
        security-updated, and the release pocket's snapshot would not have it."""
        suites = [line.split()[3] for line in templates.render_ubuntu_snapshot_list().splitlines()
                  if line.startswith("deb ")]
        assert suites == [config.UBUNTU_CODENAME, f"{config.UBUNTU_CODENAME}-updates"]

    def test_verified_by_the_stock_archive_keyring(self):
        """The snapshot serves Ubuntu's original signed Release files, so setup installs no key of its
        own — an unsigned source would be a far bigger change to a donor's box than an extra mirror."""
        assert f"[signed-by={config.UBUNTU_ARCHIVE_KEYRING}]" in \
            templates.render_ubuntu_snapshot_list()

    def test_the_source_is_additive(self, monkeypatch):
        """The load-bearing property. CCC installs on machines that are not ours: replacing or editing
        the box's own sources would freeze that donor's ENTIRE system at our timestamp. So setup only
        ever adds a file of its own, and a later `apt upgrade` still prefers the live archive."""
        written, ran = [], []
        monkeypatch.setattr(setup, "_write_root_file", lambda path, content, mode="644":
                            written.append((path, content)))
        monkeypatch.setattr(setup, "run", lambda cmd, **kw: ran.append(" ".join(cmd)))
        monkeypatch.setattr(setup, "sh", lambda script, **kw: ran.append(script))

        setup.add_repos()

        assert [p for p, _ in written] == [config.UBUNTU_SNAPSHOT_LIST]
        assert config.UBUNTU_SNAPSHOT_LIST.parent.name == "sources.list.d"  # never sources.list itself
        for touched in ("/etc/apt/sources.list ", "ubuntu.sources"):
            assert not any(touched in c for c in ran)

    def test_only_the_head_gains_the_apptainer_ppa(self, monkeypatch):
        """A donor's box gets no third-party repo: it installs the Head's .deb by path instead."""
        ran = []
        monkeypatch.setattr(setup, "_write_root_file", lambda *a, **kw: None)
        monkeypatch.setattr(setup, "run", lambda cmd, **kw: ran.append(" ".join(cmd)))
        monkeypatch.setattr(setup, "sh", lambda script, **kw: None)

        setup.add_repos()
        assert not any(config.APPTAINER_PPA in c for c in ran)
        setup.add_repos(head=True)
        assert any(config.APPTAINER_PPA in c for c in ran)

    def test_only_the_head_gains_node(self, monkeypatch):
        """Node builds the Dashboard, which only the Head does — so NodeSource, like the apptainer
        PPA, never reaches a donor's box. The install list follows the repo: pinning Node into an
        Arm's apt run would add a repo an Arm has no source for."""
        written, ran = [], []
        monkeypatch.setattr(setup, "_write_root_file", lambda path, content, mode="644":
                            written.append((path, content)))
        monkeypatch.setattr(setup, "run", lambda cmd, **kw: ran.append(" ".join(cmd)))
        monkeypatch.setattr(setup, "sh", lambda script, **kw: ran.append(script))
        installed = []
        monkeypatch.setattr(setup, "_apt_install", lambda pkgs: installed.extend(pkgs))
        monkeypatch.setattr(setup.system, "installed_packages", lambda: set())

        setup.add_repos()
        setup.install_packages(config.SLURMD_PKG)
        assert config.NODESOURCE_LIST not in [p for p, _ in written]
        assert config.NODE_PKG not in installed

        setup.add_repos(head=True)
        setup.install_packages(head=True)
        assert config.NODESOURCE_LIST in [p for p, _ in written]
        assert any(config.NODESOURCE_KEY_URL in c and config.NODESOURCE_KEYRING in c for c in ran)
        assert config.NODE_PKG in installed

    def test_node_repo_is_signed_and_pinned(self):
        """An unsigned source, or a floating `nodejs`, and the Dashboard build stops being the thing
        that was tested. node_22.x retains its old builds, so the exact pin stays fetchable."""
        assert config.NODE_PKG.startswith("nodejs=")
        assert config.NODESOURCE_KEYRING.endswith(".gpg")
        assert config.NODESOURCE_REPO.startswith("https://")


class TestBaseImageDigest:
    """`ubuntu:24.04` is a MUTABLE tag — Canonical re-pushes it monthly — so two nodes building
    base.sif months apart would get different builds of "the same" image, and nothing would say so."""

    def test_def_pins_a_digest_not_a_tag(self):
        image = [line for line in templates.render_base_def().splitlines()
                 if line.startswith("From:")][0]
        assert "@sha256:" in image
        assert ":24.04" not in image, "a mutable tag came back"

    def test_digest_is_a_full_sha256(self):
        digest = config.BASE_IMAGE_DOCKER.split("@sha256:")[1]
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")

    def test_one_ubuntu_release_fleet_wide(self):
        """The Pod image and the apt snapshot every node pins against are the same release, so a
        version pinned for the host is a version the image could install too."""
        assert config.UBUNTU_CODENAME == "noble"  # 24.04
        assert config.BASE_IMAGE_DOCKER.startswith("ubuntu@")


class TestTheImageCarriesNoRuntime:
    """base.sif is Ubuntu plus what a Pod cannot bring with it, and nothing else. Every **Environment**
    now carries its own interpreter and its own torch, so anything here would only be shadowed — and
    paid for in durable disk on every donated box regardless."""

    def test_no_python_and_no_torch(self):
        d = templates.render_base_def()
        for word in ("python", "torch", "pip", "conda", "cuda"):
            assert word not in d.lower(), f"{word} came back into the image"

    def test_sshd_stays(self):
        """A **Tide Pool** runs sshd INSIDE the Pod, so this one cannot come from an Environment."""
        assert "openssh-server" in templates.render_base_def()

    def test_ca_certificates_stay(self):
        """An Environment is BUILT in a Pod off this image and uv fetches every package over TLS. A
        stock Ubuntu image has no trust store, so without this every build dies at the first
        connection — and the PyTorch image's happening to have one is why nothing noticed before."""
        assert "ca-certificates" in templates.render_base_def()

    def test_a_toolchain_stays(self):
        """Triton compiles a `cuda_utils` shim at runtime, on the first torch.compile'd kernel, by
        shelling out to gcc/clang. No PyPI wheel ships a compiler, so an Environment cannot bring one,
        and `--contain` hides the host's — without this every torch.compile Spawn dies mid-run, after
        the card is held and the Environment built."""
        assert "build-essential" in templates.render_base_def()

    def test_it_is_the_metapackage_and_not_a_hand_picked_closure(self):
        """Two fleet-wide teardowns were spent learning what a toolchain is: `gcc` alone has no
        libc6-dev (a RECOMMENDS, dropped by --no-install-recommends, and the owner of stdio.h and the
        crt objects), and gcc+libc6-dev still has no C++ compiler for Inductor's CPU kernels or
        cpp_extension. Naming the parts again would re-enter that loop, so this asserts the
        metapackage — and that --no-install-recommends is still what makes hand-picking dangerous."""
        d = templates.render_base_def()
        assert "build-essential" in d
        assert "--no-install-recommends" in d
        for part in ("gcc ", "g++", "libc6-dev", "make "):
            assert part not in d, f"{part!r} named by hand — build-essential already carries it"

    def test_the_mantle_account_is_baked_in(self):
        """Unprivileged sshd needs the connecting uid to resolve via getpwuid or it cannot find a home
        for authorized_keys. Nothing in an Environment can supply an /etc/passwd entry."""
        d = templates.render_base_def()
        assert f"mantle:x:{config.CCC_UID}:{config.CCC_GID}" in d


class TestAptWorksInsideAPod:
    """`apt install` inside a Pod, which a **Substrate** needs and a **Tide Pool**'s User expects.

    The image ships shims in /usr/local/bin (ahead of /usr/bin on PATH) rather than teaching every
    caller an incantation. What they must do was settled on hardware (2026-08-18) and each of these
    guards one finding, because every one of them looks removable and none is."""

    def test_it_is_a_nested_namespace(self):
        """apptainer's userns leaves uid 0 unmapped, so a chown answers EINVAL and dpkg-deb dies in
        its tar subprocess. `unshare -r` maps a real uid 0 one level down, so the chown is permitted
        rather than faked."""
        d = templates.render_base_def()
        assert "unshare -r" in d

    def test_a_chown_to_an_unmapped_group_does_not_kill_the_install(self):
        """`unshare -r` maps ONE id pair, so a package shipping a file owned by any group but root —
        utmp (tmux), crontab, ssl-cert — makes dpkg chown to a gid that is not in the namespace. The
        kernel answers EINVAL, not EPERM, and dpkg treats it as fatal.

        fakeroot alone does NOT fix it: libfakeroot issues the real chown underneath and swallows only
        EPERM. FAKEROOTDONTTRYCHOWN stops it attempting the real call, and is the entire fix —
        measured 2026-08-18, `unshare -r fakeroot chown 0:43` fails and the same with the variable
        set succeeds. Both halves are asserted because either alone is inert."""
        d = templates.render_base_def()
        assert "fakeroot" in d
        assert "FAKEROOTDONTTRYCHOWN=1" in d
        # ...and it has to BE in the image, or the shim is a "command not found" on every install.
        assert "neovim curl fakeroot" in d

    def test_apt_does_not_drop_to_a_uid_that_does_not_exist(self):
        """apt fetches as `_apt`, and a namespace holding one mapped uid has no such account — every
        download dies on "setgroups: Operation not permitted" long before dpkg is reached."""
        assert "APT::Sandbox::User=root" in templates.render_base_def()

    def test_nothing_stops_to_ask_a_question(self):
        """A Substrate is installed from the job script with no terminal attached; a package that
        prompts would hang the Spawn holding a Device until its wall clock ran out."""
        assert "DEBIAN_FRONTEND=noninteractive" in templates.render_base_def()

    def test_the_index_is_fetched_once_per_pod(self):
        """The image ships no package list (`rm -rf /var/lib/apt/lists/*`), so a first install would
        otherwise fail with "unable to locate package" — which reads as a broken name, not a missing
        index. The marker sits on the bound disk scratch, so it dies with the Pod."""
        d = templates.render_base_def()
        assert "/ccc-tmp/.apt-updated" in d

    def test_only_the_known_noise_is_filtered(self):
        """Three lines print on every invocation in a one-uid namespace and none means anything
        failed. Filtering more would hide real failures, which arrive on the same stream."""
        d = templates.render_base_def()
        noise = [line for line in d.splitlines() if line.startswith("ccc_noise=")]
        assert len(noise) == 1
        assert noise[0].count("|") == 2

    def test_apts_own_exit_status_survives_the_filter(self):
        """A pipe would report grep's status instead, so `apt-get install nonesuch` would look like a
        success and a **Substrate** naming a bad package would install nothing and pass."""
        d = templates.render_base_def()
        assert "return $ccc_rc" in d
        assert "| grep" not in d

    def test_sudo_grants_nothing_and_still_works(self):
        """People type it reflexively. Inside a Pod there is nothing left to grant, so it execs its
        arguments — refusing would only send them looking for a privilege that does not exist here."""
        d = templates.render_base_def()
        assert "/usr/local/bin/sudo" in d
        assert 'exec "$@"' in d

    def test_editors_ship_with_the_image(self):
        """A Tide Pool is a shell on a donated box: nothing can be edited without one, and no
        **Environment** can supply a system editor."""
        d = templates.render_base_def()
        assert "nano" in d and "neovim" in d


DEB_BYTES = b"!<arch>\ndebian-binary   pretend apptainer .deb\n"
DONOR_IP = "100.99.0.2"  # where the fake "donor-box" node in `head_pin` is registered


@pytest.fixture
def head_pin(monkeypatch, tmp_path):
    """A Head that has pinned an apptainer .deb, with its Enroll API pointed at it."""
    debs, registry = tmp_path / "debs", tmp_path / "arms.json"
    debs.mkdir()
    (debs / "apptainer_1.5.3-1~noble_amd64.deb").write_bytes(DEB_BYTES)
    record = {"version": "1.5.3-1~noble", "filename": "apptainer_1.5.3-1~noble_amd64.deb",
              "sha256": hashlib.sha256(DEB_BYTES).hexdigest()}
    (tmp_path / "apptainer.json").write_text(json.dumps(record))
    registry.write_text(json.dumps({"donor-box": {"owner": "alice", "admitted": True}}))

    monkeypatch.setattr(enroll_api, "DEBS_DIR", debs)
    monkeypatch.setattr(enroll_api, "APPTAINER_RECORD", tmp_path / "apptainer.json")
    monkeypatch.setattr(enroll_api, "REGISTRY", registry)
    monkeypatch.setattr(enroll_api, "MUNGE_KEY", tmp_path / "munge.key")
    (tmp_path / "munge.key").write_bytes(b"x" * 32)
    for var, val in (("CCC_CTLD_ADDR", "100.99.0.1:6817"), ("CCC_CLUSTER", "ccc"),
                     ("CCC_PARTITION", "ccc")):
        monkeypatch.setenv(var, val)
    # /enroll asks Headscale who is registered at the connecting peer. "donor-box" is registered
    # at DONOR_IP below — tests that POST /enroll dial the TestClient at that address to satisfy
    # the check, rather than each re-deriving this mock.
    payload = [{"given_name": "donor-box", "ip_addresses": [DONOR_IP]}]
    monkeypatch.setattr(enroll_api.subprocess, "run",
                       lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload)))
    return record


class TestHeadServesApptainer:
    """The PPA keeps only its newest build, so the reproducibility available is fleet-internal: the
    Head downloads the .deb ONCE and every Arm — today's and next year's — installs those same bytes."""

    def test_enroll_tells_the_arm_what_to_expect(self, head_pin):
        # Dialed FROM DONOR_IP: /enroll checks the connecting peer against Headscale's node list
        # (mocked in `head_pin`), not the request body's claimed mesh_ip.
        client = TestClient(enroll_api.app, client=(DONOR_IP, 9))
        body = client.post("/enroll", json={"node_name": "donor-box", "gpu_devices": ["/dev/nvidia0"],
                                            "mesh_ip": "9.9.9.9", "vram_mb": 24564}).json()
        assert body["apptainer"] == head_pin

    def test_the_deb_served_is_the_deb_recorded(self, head_pin):
        """The round trip that matters: the sha256 in /enroll must describe the bytes /apptainer.deb
        actually hands over, or every Arm refuses a file that is perfectly fine."""
        served = TestClient(enroll_api.app).get("/apptainer.deb").content
        assert hashlib.sha256(served).hexdigest() == head_pin["sha256"]

    def test_a_head_with_no_pin_refuses_to_enroll(self, head_pin, monkeypatch, tmp_path):
        """No fallback to the PPA — falling back is precisely the divergence this prevents."""
        monkeypatch.setattr(enroll_api, "APPTAINER_RECORD", tmp_path / "gone.json")
        r = TestClient(enroll_api.app).post(
            "/enroll", json={"node_name": "donor-box", "gpu_devices": ["/dev/nvidia0"],
                             "mesh_ip": "100.99.0.2", "vram_mb": 24564})
        assert r.status_code == 503

    def test_a_re_run_keeps_the_pinned_deb(self, monkeypatch, tmp_path, head_pin):
        """`setup --head` is routinely re-run. Re-fetching would hand every Arm enrolled after it a
        build the rest of the fleet is not running — so the recorded .deb wins over a newer PPA."""
        monkeypatch.setattr(config, "APPTAINER_RECORD", tmp_path / "apptainer.json")
        monkeypatch.setattr(config, "HEAD_DEBS_DIR", tmp_path / "debs")
        monkeypatch.setattr(setup, "run", lambda *a, **kw: pytest.fail("re-fetched a pinned .deb"))

        assert setup.pin_apptainer_deb().name == head_pin["filename"]


class TestArmFeatureDeclaration:
    """Arm Features (docs/TODO.md, temp/FEATURES_BUILTIN.md): a donor's `POST /enroll` may declare
    `[{"name","path"}, ...]`, hard-validated against CCC's BUILT-IN catalog — no free typing reaches
    the registry, on the same "refuse, don't warn" stance as every other check in this endpoint."""

    def _post(self, features):
        return TestClient(enroll_api.app, client=(DONOR_IP, 9)).post(
            "/enroll", json={"node_name": "donor-box", "gpu_devices": ["/dev/nvidia0"],
                             "mesh_ip": "9.9.9.9", "vram_mb": 24564, "features": features})

    def test_a_catalogued_feature_is_recorded(self, head_pin):
        r = self._post([{"name": "vlearn", "path": "/opt/vlearn"}])
        assert r.status_code == 200
        reg = json.loads(enroll_api.REGISTRY.read_text())
        assert reg["donor-box"]["features"] == [{"name": "vlearn", "path": "/opt/vlearn"}]

    def test_an_uncatalogued_name_is_refused(self, head_pin):
        """No free typing: a name outside the built-in catalog is refused outright — the exact stance
        `POST /enroll` already takes on an unauthorized node name.

        The remedy named is now an upgrade, not an operator command: both ends carry the same constant,
        so a name this Head does not know means the DONOR's CCC is newer than the Head's."""
        r = self._post([{"name": "ghost", "path": "/opt/ghost"}])
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "ghost" in detail
        assert "upgrading" in detail
        assert "vlearn" in detail   # and it says what this Head does support

    def test_an_empty_path_is_refused(self, head_pin):
        r = self._post([{"name": "vlearn", "path": "  "}])
        assert r.status_code == 422

    def test_a_re_enroll_that_declares_none_clears_a_stale_declaration(self, head_pin):
        """/enroll is an UPDATE: a re-run of `setup --arm` after the owner un-picked a feature must
        actually remove it, not leave the registry claiming a card the Arm no longer reports."""
        assert self._post([{"name": "vlearn", "path": "/opt/vlearn"}]).status_code == 200
        assert self._post([]).status_code == 200
        reg = json.loads(enroll_api.REGISTRY.read_text())
        assert reg["donor-box"]["features"] == []

    def test_the_three_copies_of_the_catalog_agree(self):
        """cuttle.config, ccc_head_api.enroll and ccc_head_api.slurm each carry the list because the
        three packages cannot import each other. Nothing but this test keeps them honest."""
        assert tuple(enroll_api.FEATURE_NAMES) == tuple(config.FEATURE_NAMES)
        assert tuple(slurm.FEATURE_NAMES) == tuple(config.FEATURE_NAMES)

    def test_features_are_optional(self, head_pin):
        """The common case: a donor with no Arm Features omits the field entirely (older CLIs never
        sent it) and enrolls exactly as before."""
        assert self._post([]).status_code == 200


class TestArmVerifiesTheDeb:
    def test_matching_checksum_installs(self, monkeypatch, tmp_path, head_pin):
        monkeypatch.setattr(enroll.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(enroll.urllib.request, "urlretrieve",
                            lambda url, dest: dest.write_bytes(DEB_BYTES))
        assert enroll.fetch_apptainer_deb("100.99.0.1", head_pin).read_bytes() == DEB_BYTES

    def test_a_mismatch_refuses_to_install(self, monkeypatch, tmp_path, head_pin):
        """This file becomes root-installed software. A truncated download is the ordinary cause and
        would otherwise reach `apt-get install` as an unreadable archive, mid-setup."""
        monkeypatch.setattr(enroll.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(enroll.urllib.request, "urlretrieve",
                            lambda url, dest: dest.write_bytes(DEB_BYTES[:10]))
        with pytest.raises(typer.Exit):
            enroll.fetch_apptainer_deb("100.99.0.1", head_pin)
        assert not (tmp_path / head_pin["filename"]).exists()  # never left for apt to find


class TestOneVersionPerFact:
    """A pin written twice is a pin one careless edit away from being no pin at all. These are the two
    facts CCC used to spell out in more than one place."""

    def test_slurm_is_pinned_once_for_both_roles(self):
        """A Head on one Slurm version and an Arm on another do not fail at install — they fail later,
        at the RPC layer, as a node that will not register."""
        assert config.SLURM_PKG == f"slurm-wlm={config.SLURM_VERSION}"
        assert config.SLURMD_PKG == f"slurmd={config.SLURM_VERSION}"

    def test_the_two_slurm_packages_name_the_same_version(self):
        """The property that matters, asserted independently of how it is currently derived."""
        assert config.SLURM_PKG.split("=", 1)[1] == config.SLURMD_PKG.split("=", 1)[1]

    def test_the_tailscale_repo_follows_the_ubuntu_codename(self):
        """The release these URLs name and the release UBUNTU_SNAPSHOT pins are the same fact. A
        second hardcoded codename is one that stays on noble the day the rest moves."""
        assert config.UBUNTU_CODENAME in config.TAILSCALE_REPO_KEY
        assert config.UBUNTU_CODENAME in config.TAILSCALE_REPO_LIST
        for url in (config.TAILSCALE_REPO_KEY, config.TAILSCALE_REPO_LIST):
            tail = url.rsplit("/", 1)[1]
            assert tail.startswith(f"{config.UBUNTU_CODENAME}.")


class TestHeadscaleIsPinned:
    """Headscale is not in apt, so it comes from a GitHub release. Tracking "latest" meant two Heads
    built weeks apart ran different coordinators with nothing recording how — and the coordinator owns
    the database mapping every node to its mesh IP."""

    def test_the_version_is_a_constant_not_a_lookup(self):
        assert config.HEADSCALE_VERSION.startswith("v")
        assert not hasattr(mesh, "_latest_headscale")
        assert not hasattr(config, "HEADSCALE_RELEASES")

    def test_the_deb_url_is_composed_from_the_pin(self):
        """The tag keeps its `v`, the filename drops it — getting that backwards 404s at install."""
        url = mesh._headscale_deb(config.HEADSCALE_VERSION)
        bare = config.HEADSCALE_VERSION.lstrip("v")
        assert f"/download/{config.HEADSCALE_VERSION}/" in url
        assert url.endswith(f"headscale_{bare}_linux_amd64.deb")

    def test_installing_reaches_for_the_pinned_version(self, monkeypatch):
        monkeypatch.setattr(mesh, "installed_headscale", lambda: None)
        got = []
        monkeypatch.setattr(mesh, "_install_deb", lambda url, **kw: got.append(url))
        assert mesh.install_headscale() == config.HEADSCALE_VERSION
        assert got == [mesh._headscale_deb(config.HEADSCALE_VERSION)]

    def test_an_existing_head_is_never_moved_onto_the_pin(self, monkeypatch, tmp_path):
        """The pin must not become an upgrade trigger. A Head already running an older Headscale keeps
        it — migrating its node database as a side effect of an unrelated re-setup is the failure this
        skip has always existed to prevent."""
        monkeypatch.setattr(mesh, "installed_headscale", lambda: "v0.26.1")
        monkeypatch.setattr(config, "HEADSCALE_CONF", tmp_path / "config.yaml")
        (tmp_path / "config.yaml").write_text("x")
        monkeypatch.setattr(mesh, "_install_deb",
                            lambda *a, **kw: pytest.fail("must not touch an installed Headscale"))
        assert mesh.install_headscale() == "v0.26.1"

    def test_a_restored_config_refetches_what_is_installed(self, monkeypatch, tmp_path):
        """`teardown --keep-packages` leaves the binary and drops /etc/headscale. The re-fetch must
        name the INSTALLED version, not the pin, or restoring a config silently upgrades the node db."""
        monkeypatch.setattr(mesh, "installed_headscale", lambda: "v0.26.1")
        monkeypatch.setattr(config, "HEADSCALE_CONF", tmp_path / "missing.yaml")
        got = []
        monkeypatch.setattr(mesh, "_install_deb", lambda url, **kw: got.append(url))
        assert mesh.install_headscale() == "v0.26.1"
        assert got == [mesh._headscale_deb("v0.26.1")]


class TestTheApiVenvInterpreter:
    """`uv venv` with no interpreter named picks one for itself, and both ways it can pick wrong end
    the same way: the venv builds, setup reports success, and the SERVICE fails to start.

    Under `sudo` uv does not see the operator's UV_PYTHON_INSTALL_DIR, so it reaches into root's own
    uv-managed set — which produced a 3.11 venv for a package that declares requires-python >=3.12, and
    would have produced an unreadable one even at the right version, because a uv-managed interpreter
    lives under /root (0700) where `munge` and `ccc` cannot execute it.
    """

    def _venv_argv(self, monkeypatch) -> list[str]:
        calls: list[list[str]] = []
        monkeypatch.setattr(headapi, "run", lambda argv, **kw: calls.append([str(a) for a in argv]))
        monkeypatch.setattr(headapi.shutil, "which", lambda _: "/usr/bin/uv")
        headapi.install_enroll(lambda *a, **kw: None, "100.99.0.1")
        return next(c for c in calls if c[1:2] == ["venv"])

    def test_it_names_the_version_the_package_requires(self, monkeypatch):
        argv = self._venv_argv(monkeypatch)
        assert "--python" in argv and config.API_PYTHON in argv
        req = tomllib.loads(
            (headapi.API_SRC / "pyproject.toml").read_text())["project"]["requires-python"]
        assert req == f">={config.API_PYTHON}", \
            "the venv's Python and the package's requires-python have drifted apart"

    def test_it_refuses_a_uv_managed_interpreter(self, monkeypatch):
        """The one that cannot be caught by reading the version afterwards: /root is 0700, so a
        managed interpreter is unexecutable by the service accounts and no chmod under /opt/ccc
        reaches it."""
        assert "--no-managed-python" in self._venv_argv(monkeypatch)

    def test_a_rerun_replaces_rather_than_reuses(self, monkeypatch):
        """Teardown+setup is the normal redeploy here, and a venv built on the wrong interpreter has
        to be replaced. uv warns that it will one day refuse to without this."""
        assert "--clear" in self._venv_argv(monkeypatch)


class TestCuttleDependsOnTyperInternals:
    """`ui.run_app` renders usage errors with typer's own renderer, and `menu.py` walks typer's
    command tree — both reach past the public API into `typer._click`, typer's vendored copy of
    click. Typer reshuffles that copy in *patch* releases: 0.27.2 moved `Abort` out of
    `typer._click.exceptions`, and because `uv tool install` resolves against pyproject.toml and
    ignores uv.lock, every installed CLI died with an ImportError on the next `cuttle` anything.

    These tests hold the two halves of the fix together — the ceiling that stops the resolver, and
    the symbols themselves, so a lockfile bump onto a typer that moved them fails here first.
    """

    CORE = headapi.REPO_ROOT / "cli" / "core" / "pyproject.toml"

    def _requirement(self, name: str) -> str:
        deps = tomllib.loads(self.CORE.read_text())["project"]["dependencies"]
        return next(d for d in deps if d.split("~")[0].split(">")[0].split("=")[0].strip() == name)

    def test_the_typer_requirement_has_a_ceiling(self):
        """A floor-only `typer>=x` lets the resolver hand a fresh install whatever typer shipped
        today. `~=0.26.8` is `>=0.26.8,<0.27` — patch fixes flow, a new minor does not."""
        req = self._requirement("typer")
        assert req.startswith("typer~=") or "<" in req, \
            f"{req!r} has no upper bound; cuttle uses typer internals and cannot take a blind bump"

    def test_the_floor_is_where_typer_vendored_click(self):
        """Below 0.26 typer depended on click and `typer._click` does not exist at all, so the old
        `typer>=0.12` was broken at both ends — it just happened to resolve upwards."""
        floor = re.search(r"[\d.]+", self._requirement("typer")).group()
        assert tuple(int(n) for n in floor.split(".")) >= (0, 26)

    def test_the_internals_the_cli_reaches_for_are_there(self):
        """Named one by one: an ImportError here is the whole failure, caught in CI rather than on
        a user's box after a `uv tool install`."""
        from typer import Abort, rich_utils
        from typer._click.exceptions import ClickException

        assert issubclass(Abort, BaseException)
        assert issubclass(ClickException, BaseException)
        assert callable(rich_utils.rich_format_error)
        assert callable(typer.main.get_command)

    def test_the_installed_typer_satisfies_the_declared_pin(self):
        """The venv the tests run in has to be the one the pin describes, or everything above is
        checking a typer nobody ships."""
        floor = re.search(r"[\d.]+", self._requirement("typer")).group()
        assert typer.__version__.split(".")[:2] == floor.split(".")[:2], \
            f"tests run on typer {typer.__version__}, pyproject pins ~={floor}"
