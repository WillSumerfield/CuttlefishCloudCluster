"""`cuttle build-installer` — package a portable client installer bundle.

Emits a `.tar.gz` holding the built `cuttle` wheel plus `install.sh` / `uninstall.sh`. Built **on a
Head** it also bakes that Head's address + pinned certificate in, so the installed client comes
pre-pointed and pre-pinned — `cuttle login` with nothing to type or confirm. Built off a Head it is
generic, and the client points itself with `cuttle setup --client`.

Deliberately NOT a Head-served `curl … | sh`: the bundle is built here and hand-carried (or the
target box installs cuttle straight from the repo with `uv tool install ./cli/core`).

Intended path only — no fallbacks (test box; failures decided live).
"""

import shutil
import tarfile
import tempfile
from pathlib import Path

import yaml

from . import config, headapi, menu, system, tls, ui
from .system import run

# install.sh / uninstall.sh ship as package data beside this module, so they resolve whether cuttle
# was installed editable from the checkout or from a wheel.
SCRIPTS_DIR = Path(__file__).resolve().parent / "installer_scripts"


def _bake_head_creds(stage: Path, head_url: str | None, generic: bool = False) -> str | None:
    """When run on a Head, lay this Head's config + CA into the bundle at exactly the names `install.sh`
    will copy them to (so the script parses no URLs). Returns the baked URL, or None if this isn't a
    Head / has no CA yet / `generic` was asked for — a generic bundle.

    What is baked is the **Cluster name**, plus — in `head/addr` — the LAN IP it has to resolve to,
    because nothing resolves that name for the target box until `install.sh` writes its hosts entry.
    A baked bundle therefore only works for boxes on that LAN. A remote box has no route to the IP (and
    cannot use the mesh address either — it joins the mesh during `setup --arm`, after this bundle has
    already been installed). `generic` is the honest answer for those: ship the tool, let enrollment
    point the box.

    The CA, not the serving cert: that is what a client pins now, and it is also what has to reach the
    box's trust stores for the Dashboard to load without a warning.
    """
    if generic or not menu.is_head():
        return None
    url = head_url or headapi.head_url(config.domain())
    if not config.HEAD_CA_CERT.exists():
        ui.warn(f"no CA certificate at {config.HEAD_CA_CERT} — building a generic bundle")
        return None
    head_dir = stage / "head"
    (head_dir / "pins").mkdir(parents=True)
    (head_dir / "config.yaml").write_text(yaml.safe_dump({"head": url, "cluster": config.cluster_name()}, sort_keys=False))
    # The whole hosts line, ready to append — not just the address. install.sh is static package data
    # with no templating, so anything it had to RECONSTRUCT (the name, the marker) would be a literal
    # duplicating config.py, drifting silently the day either changed. Baking the finished line leaves
    # the script with nothing to know but where to put it. Its own file, not a key in config.yaml,
    # which is cuttle's settings file and gets copied verbatim.
    (head_dir / "hosts").write_text(
        f"{system.primary_ip()}\t{config.domain()} {config.HOSTS_MARKER}\n")
    (head_dir / "ca-nickname").write_text(config.HEAD_CA_NICKNAME + "\n")
    # tls._pin_path is the single source of truth for a pin's filename (host_port.pem), so a baked
    # CA lands exactly where cuttle will later look for it — now the same filename on every client,
    # since every client dials the same name.
    shutil.copy(config.HEAD_CA_CERT, head_dir / "pins" / tls._pin_path(url).name)
    shutil.copy(config.HEAD_CA_CERT, head_dir / "ccc-ca.crt")
    return url


def build_installer(out: Path | None = None, head_url: str | None = None,
                    generic: bool = False) -> None:
    core = headapi.REPO_ROOT / "cli" / "core"
    if not (core / "pyproject.toml").exists():
        # build-installer needs the source tree to build the wheel. On a Head cuttle is installed
        # editable from the checkout (see headapi.REPO_ROOT), so this holds; off a wheel install it
        # won't, and we say so rather than letting `uv build` fail obscurely.
        raise ui.abort(f"can't find the cuttle source at {core} — build-installer needs the repo "
                       "checkout (it builds the wheel from it)")
    out = (out or Path.cwd() / "cuttle-installer.tar.gz").resolve()
    baked = None
    with ui.section("Building the installer", lambda: out.name):
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td) / "cuttle-installer"
            stage.mkdir()
            with ui.step("Building the wheel"):
                # Build into a scratch dir, not `stage`: `uv build` drops a `.gitignore` beside the
                # wheel, which we don't want in the tarball. Copy just the wheel across.
                wheeldir = Path(td) / "wheel"
                run(["uv", "build", "--wheel", "--out-dir", str(wheeldir), str(core)])
                shutil.copy(next(wheeldir.glob("cuttle-*.whl")), stage)
            with ui.step("Adding the install scripts"):
                for name in ("install.sh", "uninstall.sh"):
                    dst = stage / name
                    shutil.copy(SCRIPTS_DIR / name, dst)
                    dst.chmod(0o755)
            with ui.step("Baking Head credentials"):
                baked = _bake_head_creds(stage, head_url, generic=generic)
            with ui.step("Writing the tarball"):
                with tarfile.open(out, "w:gz") as tar:
                    tar.add(stage, arcname="cuttle-installer")
        # Inside the box: `section` flushes its results panel on exit, so facts appended after it
        # closes are dropped.
        ui.result("installer", str(out))
        ui.result("mode", f"pre-pointed at {baked} (for boxes on that LAN)" if baked
                  else "generic — the box points itself: `cuttle setup --client`, "
                       "or `--arm --token …` to enroll over the mesh")
    # Outside the box: an open frame turns a hint into a transient row that the collapse erases.
    ui.hint("Copy it over, then: tar xzf cuttle-installer.tar.gz && cuttle-installer/install.sh")
