"""Reproducible package pinning — the three places a "pinned" version could silently stop being one.

Every failure here is invisible on the box that works and only surfaces on the *next* one: a donor
enrolled six months later gets a different base image, a rebuilt image gets a different torch, or
`setup` dies at apt with an error that names neither the pin nor the file holding it.
"""

import json
import re
import subprocess
import tomllib

import pytest
import typer
from fastapi.testclient import TestClient

from ccc_head_api import enroll as enroll_api
from ccc_head_api import slurm
from cuttle import config, headapi, mesh, podman, setup, templates


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

    def test_podman_everywhere_htpasswd_on_the_head_only(self, monkeypatch):
        """Every node runs containers; only the Head mints registry credentials."""
        installed = []
        monkeypatch.setattr(setup, "run", lambda cmd, **kw: None)
        monkeypatch.setattr(setup, "_apt_install", lambda pkgs: installed.append(list(pkgs)))
        monkeypatch.setattr(setup.system, "installed_packages", lambda: set())
        setup.install_packages(config.SLURMD_PKG)
        setup.install_packages(head=True)
        arm, head = installed
        assert config.PODMAN_PKG in arm and config.PODMAN_PKG in head
        assert config.HTPASSWD_PKG not in arm and config.HTPASSWD_PKG in head

    def test_only_the_head_gains_node(self, monkeypatch, tmp_path):
        """Node builds the Dashboard, which only the Head does — so NodeSource never reaches a
        donor's box. The install list follows the repo: pinning Node into an
        Arm's apt run would add a repo an Arm has no source for."""
        written, ran = [], []
        monkeypatch.setattr(setup, "_write_root_file", lambda path, content, mode="644":
                            written.append((path, content)))
        monkeypatch.setattr(setup, "run", lambda cmd, **kw: ran.append(" ".join(cmd)))
        monkeypatch.setattr(setup, "sh", lambda script, **kw: ran.append(script))
        installed = []
        monkeypatch.setattr(setup, "_apt_install", lambda pkgs: installed.extend(pkgs))
        monkeypatch.setattr(setup.system, "installed_packages", lambda: set())
        monkeypatch.setattr(config, "NODESOURCE_LIST", tmp_path / "ccc-nodesource.list")

        setup.add_repos()
        setup.install_packages(config.SLURMD_PKG)
        assert config.NODESOURCE_LIST not in [p for p, _ in written]
        assert config.NODE_PKG not in installed

        setup.add_repos(head=True)
        setup.install_packages(head=True)
        assert config.NODESOURCE_LIST in [p for p, _ in written]
        assert any(config.NODESOURCE_KEY_URL in c and config.NODESOURCE_KEYRING in c for c in ran)
        assert config.NODE_PKG in installed

    def test_the_boxs_own_nodesource_wins(self, monkeypatch, tmp_path):
        """Same repo under two keyrings stops apt reading ANY source — so a Head that already has
        NodeSource gets no second copy, and a copy an earlier run left is removed."""
        written, ran = [], []
        monkeypatch.setattr(setup, "_write_root_file", lambda path, content, mode="644":
                            written.append((path, content)))
        monkeypatch.setattr(setup, "run", lambda cmd, **kw: ran.append(" ".join(cmd)))
        monkeypatch.setattr(setup, "sh", lambda script, **kw: ran.append(script))
        monkeypatch.setattr(setup.ui, "say", lambda *a, **k: None)
        ours = tmp_path / "ccc-nodesource.list"
        monkeypatch.setattr(config, "NODESOURCE_LIST", ours)
        ours.write_text(f"deb [signed-by={config.NODESOURCE_KEYRING}] {config.NODESOURCE_REPO} nodistro main\n")
        (tmp_path / "nodesource.sources").write_text(
            f"Types: deb\nURIs: {config.NODESOURCE_REPO}\nSigned-By: /usr/share/keyrings/nodesource.gpg\n")

        setup.add_repos(head=True)

        assert ours not in [p for p, _ in written]
        assert not any(config.NODESOURCE_KEY_URL in c for c in ran)
        assert any(c.startswith("rm -f") and str(ours) in c for c in ran)

    def test_the_toolkit_and_its_exact_version_siblings_are_pinned_together(self):
        """The toolkit Depends on its siblings at `=` its own version; apt won't pick an older sibling
        than the repo's newest by itself, so every one is named at the pin."""
        names = {p.split("=")[0]: p.split("=")[1] for p in config.NVIDIA_CTK_PKGS}
        assert set(names) == {"nvidia-container-toolkit", "nvidia-container-toolkit-base",
                              "libnvidia-container-tools", "libnvidia-container1"}
        assert set(names.values()) == {config.NVIDIA_CTK_VERSION}

    def test_node_repo_is_signed_and_pinned(self):
        """An unsigned source, or a floating `nodejs`, and the Dashboard build stops being the thing
        that was tested. node_22.x retains its old builds, so the exact pin stays fetchable."""
        assert config.NODE_PKG.startswith("nodejs=")
        assert config.NODESOURCE_KEYRING.endswith(".gpg")
        assert config.NODESOURCE_REPO.startswith("https://")


class TestBaseImageDigest:
    """`ubuntu:24.04` is a MUTABLE tag — Canonical re-pushes it monthly — so a rebuilt base would
    silently differ. The registry image is pinned the same way."""

    def test_containerfile_pins_a_digest_not_a_tag(self):
        image = [line for line in templates.render_base_containerfile().splitlines()
                 if line.startswith("FROM ")][0]
        assert "@sha256:" in image
        assert ":24.04" not in image, "a mutable tag came back"

    @pytest.mark.parametrize("ref", [config.BASE_IMAGE_DOCKER, config.REGISTRY_IMAGE])
    def test_digest_is_a_full_sha256(self, ref):
        digest = ref.split("@sha256:")[1]
        assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")

    def test_one_ubuntu_release_fleet_wide(self):
        assert config.UBUNTU_CODENAME == "noble"  # 24.04
        assert config.BASE_IMAGE_DOCKER.startswith("docker.io/library/ubuntu@")


class TestTheBaseImage:
    """The base is frozen per release and every User's system starts from it: tools yes, Python
    packages no (every library comes from a Dive's dependency file)."""

    def test_no_python_packages(self):
        d = templates.render_base_containerfile()
        assert "torch" not in d and "pip install" not in d and "uv pip" not in d

    @pytest.mark.parametrize("pkg", ["openssh-server", "ca-certificates", "build-essential", "sudo",
                                     "python3.12-dev", "zsh", "rsync", "git", "tmux", "locales"])
    def test_the_apt_list(self, pkg):
        assert pkg in templates.BASE_APT_PKGS.split()

    def test_uv_micromamba_and_nvcc_only(self):
        d = templates.render_base_containerfile()
        assert "COPY uv micromamba /usr/local/bin/" in d
        assert "cuda-nvcc-12-8" in d and config.CUDA_REPO_KEYRING in d
        assert "cuda-libraries" not in d    # runtime libs come in wheels

    def test_the_profile_is_bridged_into_zsh(self):
        assert "source /etc/profile" in templates.render_base_containerfile()

    def test_the_benchmark_image_adds_only_cublas_and_the_bench(self):
        d = templates.render_bench_containerfile()
        final = d.split("\nFROM ")[-1]
        assert d.count("FROM ") == 2 and f"/ccc/base:{config.IMAGE_TAG}" in final
        assert f"install -y --no-install-recommends {config.BENCH_CUBLAS_PKG} " in final
        assert config.BENCH_CUBLAS_DEV_PKG in d and "-dev" not in final and "torch" not in d     # the dev package only builds
        assert config.BENCH_BIN in final and config.BENCH_PY_PATH in final
        assert f"-DBENCH_N={config.BENCH_MATRIX_N}" in d and f"-DBENCH_ITERS={config.BENCH_ITERS}" in d


DONOR_IP = "100.99.0.2"  # where the fake "donor-box" node in `head_pin` is registered


IMAGES = {"base": "ccc.lab:5000/ccc/base@sha256:" + "b" * 64,
          "bench": "ccc.lab:5000/ccc/bench@sha256:" + "c" * 64}


@pytest.fixture
def head_pin(monkeypatch, tmp_path):
    """A Head that has built its images and minted donor-box a pull credential."""
    registry = tmp_path / "arms.json"
    (tmp_path / "images.json").write_text(json.dumps(IMAGES))
    (tmp_path / "creds.json").write_text(json.dumps({"donor-box": "s3cret"}))
    registry.write_text(json.dumps({"donor-box": {"owner": "alice", "admitted": True}}))

    monkeypatch.setattr(enroll_api, "IMAGES_RECORD", tmp_path / "images.json")
    monkeypatch.setattr(enroll_api, "REGISTRY_CREDS", tmp_path / "creds.json")
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
    return IMAGES


def _enroll(client_ip=DONOR_IP):
    return TestClient(enroll_api.app, client=(client_ip, 9)).post(
        "/enroll", json={"node_name": "donor-box", "gpu_devices": ["/dev/nvidia0"],
                         "mesh_ip": "9.9.9.9", "vram_mb": 24564})


class TestEnrollHandsOverImages:
    """Every Arm pulls the Head's exact base by digest, with its own revocable pull credential."""

    def test_enroll_hands_over_digests_and_this_arms_credential(self, head_pin):
        body = _enroll().json()
        assert body["images"] == head_pin
        assert body["cluster_name"] == "lab"
        assert body["registry"] == {"user": "arm-donor-box", "password": "s3cret"}

    def test_a_head_with_no_images_refuses_to_enroll(self, head_pin, monkeypatch, tmp_path):
        monkeypatch.setattr(enroll_api, "IMAGES_RECORD", tmp_path / "gone.json")
        assert _enroll().status_code == 503

    def test_an_arm_with_no_credential_is_refused(self, head_pin, tmp_path):
        (tmp_path / "creds.json").write_text("{}")
        assert _enroll().status_code == 503


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


class TestArmPullsByDigest:
    def test_a_digest_mismatch_refuses(self, monkeypatch):
        def fake(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="sha256:" + "f" * 64 + "\n")
        monkeypatch.setattr(podman, "run", fake)
        with pytest.raises(typer.Exit):
            podman.pull_images({"base": IMAGES["base"]})

    def test_a_match_passes(self, monkeypatch):
        ran = []

        def fake(cmd, **kw):
            ran.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="sha256:" + "b" * 64 + "\n")
        monkeypatch.setattr(podman, "run", fake)
        podman.pull_images({"base": IMAGES["base"]})
        assert ["podman", "pull", "--authfile", str(config.REGISTRY_AUTH), IMAGES["base"]] in ran


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
