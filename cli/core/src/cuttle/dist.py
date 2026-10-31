"""The Head's own `cuttle` build, served to laptops and Arms.

At every `cuttle setup --head` / `cuttle update` the Head builds the wheel from its checkout into
`config.CUTTLE_DIST_DIR`, beside `build.json` naming its hash. Laptops fetch it through the Head API
and update themselves (`selfupdate.py`); Arms get it from the Enroll API, pushed by `rollout.py`.
"""

import json
import tempfile
from pathlib import Path

from . import buildid, config, headapi
from .system import run


def build(write_root_file) -> str:
    """Build and publish the wheel; returns its build hash."""
    core = headapi.REPO_ROOT / "cli" / "core"
    with tempfile.TemporaryDirectory() as td:
        run(["uv", "build", "--wheel", "--out-dir", td, str(core)])
        wheel = next(Path(td).glob("cuttle-*.whl"))
        bid = buildid.source_hash(core / "src" / "cuttle")
        run(["install", "-d", "-m", "0755", str(config.CUTTLE_DIST_DIR)], sudo=True)
        run(["find", str(config.CUTTLE_DIST_DIR), "-name", "cuttle-*.whl", "-delete"], sudo=True)
        run(["install", "-m", "0644", str(wheel), str(config.CUTTLE_DIST_DIR / wheel.name)], sudo=True)
    write_root_file(config.CUTTLE_DIST_META, json.dumps({"build": bid, "wheel": wheel.name}) + "\n")
    return bid


def served() -> dict | None:
    """What this Head serves, or None before its first build."""
    try:
        return json.loads(config.CUTTLE_DIST_META.read_text())
    except FileNotFoundError:
        return None
