"""In-container Feature arrangement, run from a Dive's `DIVE_INIT` background build block.

Two hidden `cuttle` subcommands (registered in `main.py`) wrap this:

  _feature-stub  <project_root>                  — BEFORE the env build. Write the Head's placeholder
                                                   wheels into the project and repoint `uv.lock` at
                                                   them, so `uv sync` resolves without the real,
                                                   licensed wheel the submitter never uploaded.
  _feature-setup <venv> <env_file> <features>    — AFTER the build. For each profiled Feature: match the
                                                   built interpreter's ABI against the Arm's wheels,
                                                   delete the stub from the venv, lay out the Feature's
                                                   working dir, and append its runtime env to `env_file`
                                                   (sourced by the Dive's zshenv).

The two Feature packages never import each other, so none of `ccc_head_api` is here: the stub payload
arrives as data (ccc-launch writes it to `STUBS_FILE`), and the per-Feature setup is the Arm's own, run
where the built `.venv` is. The build block redirects this whole block's
output to `/run/ccc-env.log` and records `ok`/`failed`, so a failure here is just a loud message + a
non-zero exit.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from .features import FEATURE_MOUNT, INSTALL_DIRNAME, VLWD_MOUNT

# ccc-launch writes the Head's stub payload here; it is `$jobdir/sys/feature-stubs.json`, bound at
# /etc/ccc/dive read-only (ccc_launch.STUBS_REL / DIVE_SYS_MOUNT).
STUBS_FILE = Path("/etc/ccc/dive/feature-stubs.json")


def fail(msg: str) -> None:
    print(f"ccc: {msg}", file=sys.stderr)
    sys.exit(2)


# --- pre-build: the stub --------------------------------------------------------

def _relock(text: str, package: str, digest: str) -> str:
    """`uv.lock` with `package`'s recorded hash pointed at the stub. Unchanged if it says nothing.

    Not an optimisation — without it the build cannot succeed. uv records a sha256 of a path-sourced
    wheel in the lock and verifies it on EVERY sync, plain or `--locked`; there is no re-resolve escape
    from a mismatch. The stub is a different file, so its hash has to be the one in the lock.

    A targeted text edit rather than a TOML round-trip: `uv.lock` is uv's own file, and only the `hash`
    inside this package's own `[[package]]` block moves; every other package stays frozen.
    """
    block = re.compile(
        r'\[\[package\]\]\nname = "' + re.escape(package) + r'"\n(?:(?!\n\[\[package\]\]).)*',
        re.S,
    )
    hit = block.search(text)
    if not hit:
        return text
    fixed = re.sub(r'hash = "sha256:[0-9a-f]+"', f'hash = "sha256:{digest}"', hit.group(0))
    return text[:hit.start()] + fixed + text[hit.end():]


def stub(project_root: Path) -> None:
    """Write every stub wheel the Head computed into the project, and relock `uv.lock`.

    `STUBS_FILE` is `{files: [{rel, b64}], digests: {package: sha256}, lock: <rel>}`, where each `rel`
    and `lock` are relative to the project root (the Dive's main folder). No payload — an ordinary Dive,
    or one whose Feature declares no path-sourced package — means nothing to do.
    """
    if not STUBS_FILE.exists():
        return
    payload = json.loads(STUBS_FILE.read_text())
    for f in payload.get("files", []):
        dest = project_root / f["rel"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(f["b64"]))
    digests = payload.get("digests") or {}
    lock = project_root / payload["lock"] if payload.get("lock") else None
    if digests and lock and lock.is_file():
        text = lock.read_text(encoding="utf-8", errors="replace")
        fixed = text
        for pkg, digest in digests.items():
            fixed = _relock(fixed, pkg, digest)
        if fixed != text:
            lock.write_text(fixed)


# --- post-build: per-Feature arrangement ----------------------------------------

def abi_tag(venv: Path) -> str:
    """The wheel ABI tag of the venv's interpreter, e.g. `cp311` — the name of the Arm's wheel dir."""
    r = subprocess.run([str(venv / "bin" / "python"), "-c",
                        "import sys;print('cp%d%d' % sys.version_info[:2])"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def site_packages(venv: Path) -> Path | None:
    hits = sorted(venv.glob("lib/python*/site-packages"))
    return hits[0] if hits else None


def _append_env(env_file: Path, entries: dict[str, str]) -> None:
    """Append `export K="V${K:+:$K}"` lines to `env_file` (the Dive's zshenv sources it). Prepending to
    any value the User already set keeps their own PATHs reachable behind the Feature's."""
    env_file.parent.mkdir(parents=True, exist_ok=True)
    with env_file.open("a") as fh:
        for k, v in entries.items():
            fh.write(f'export {k}="{v}${{{k}:+:${k}}}"\n')


def setup_vlearn(venv: Path, env_file: Path, abi: str) -> None:
    """Arrange vlearn around the built Environment, in-container and after the build: the wheel dirs and
    runtime data are already bound read-only under FEATURE_MOUNT (`cuttle.features.descriptor_binds`);
    this picks the ABI, makes the licensing invariant true, and sets the runtime env."""
    root = Path(FEATURE_MOUNT) / "vlearn"
    pkg = root / abi
    if not pkg.is_dir():
        held = " ".join(sorted(p.name for p in root.iterdir() if re.fullmatch(r"cp\d{2,3}", p.name))) \
            if root.is_dir() else "(none)"
        fail(f"this Dive's Environment is Python {abi}, and this Arm's vlearn is built for: {held}. "
             f"vlearn ships binary wheels only, so the two have to match — pin that Python in your "
             f"environment.")

    # The Environment must not contain a WORKING vlearn: the Head installed an empty stub, and only the
    # Arm's own licensed build (on PYTHONPATH below) may answer `import vlearn`. Deleting the stub means
    # there is only ever one vlearn to find, rather than resting the invariant on sys.path ordering.
    sp = site_packages(venv)
    if sp:
        for pat in ("vlearn", "vlearn-*.dist-info", "vlearn_bindings*.so", "*vlearn*.pth"):
            for p in sp.glob(pat):
                (shutil.rmtree if p.is_dir() else os.remove)(p)

    # vlearn resolves assets/, shaders/, cache/ and TurboActivate.dat against VL_WORKING_DIRECTORY — a
    # directory of CCC's own, never the project's. Three come straight from the Arm's install as
    # symlinks (~1 GB of assets, not copied per Dive); cache/ is the one vlearn WRITES, so it is copied.
    install = root / INSTALL_DIRNAME
    vlwd = Path(VLWD_MOUNT)
    for name in ("assets", "shaders", "TurboActivate.dat"):
        link = vlwd / name
        if link.is_symlink() or link.exists():
            (shutil.rmtree if link.is_dir() and not link.is_symlink() else os.remove)(link)
        link.symlink_to(install / name)
    cache = vlwd / "cache"
    if cache.exists():
        shutil.rmtree(cache)
    if (install / "cache").is_dir():
        shutil.copytree(install / "cache", cache)
    else:
        cache.mkdir(parents=True, exist_ok=True)

    # The whole unpacked wheel on PYTHONPATH (`vlearn/`, its .dist-info, and the sibling
    # vlearn_bindings.<abi>.so `import vlearn` fails without). No LD_LIBRARY_PATH: the wheel's `.so`
    # files carry the Feature's system-library prefixes in their own RUNPATH (features.rpath_wheel_libs),
    # so the loader resolves libcudart/libstdc++/... from the `.so` itself. A Dive-wide LD_LIBRARY_PATH
    # would be read by every process in the shell and shadow the base system's own libs, breaking
    # ordinary tools (tmux and friends link the libtinfo/ncurses the prefix also ships).
    _append_env(env_file, {"PYTHONPATH": str(pkg),
                           "VL_WORKING_DIRECTORY": str(vlwd)})


SETUP = {"vlearn": setup_vlearn}


def setup(venv: Path, env_file: Path, features: list[str]) -> None:
    abi = abi_tag(venv)
    for name in features:
        fn = SETUP.get(name)
        if fn:
            fn(venv, env_file, abi)
