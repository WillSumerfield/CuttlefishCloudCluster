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


def ensure_sign_key() -> None:
    """The Head's ed25519 rollout-signing key (F3), generated once and then persisted (teardown keeps
    CCC_TLS_DIR unless `--all`). Idempotent: present ⇒ no-op, so a redeploy keeps the key Arms pinned."""
    run(["install", "-d", "-m", "0755", str(config.CCC_TLS_DIR)], sudo=True)
    if run(["test", "-f", str(config.ROLLOUT_SIGN_KEY)], sudo=True, check=False).returncode == 0:
        return
    run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(config.ROLLOUT_SIGN_KEY)], sudo=True)
    run(["chmod", "0600", str(config.ROLLOUT_SIGN_KEY)], sudo=True)
    run(["sh", "-c", f"openssl pkey -in {config.ROLLOUT_SIGN_KEY} -pubout -out {config.ROLLOUT_SIGN_PUB}"],
        sudo=True)
    run(["chmod", "0644", str(config.ROLLOUT_SIGN_PUB)], sudo=True)


def build(write_root_file) -> str:
    """Build and publish the wheel, and sign it (F3); returns its build hash."""
    core = headapi.REPO_ROOT / "cli" / "core"
    ensure_sign_key()
    with tempfile.TemporaryDirectory() as td:
        run(["uv", "build", "--wheel", "--out-dir", td, str(core)])
        wheel = next(Path(td).glob("cuttle-*.whl"))
        bid = buildid.source_hash(core / "src" / "cuttle")
        run(["install", "-d", "-m", "0755", str(config.CUTTLE_DIST_DIR)], sudo=True)
        run(["find", str(config.CUTTLE_DIST_DIR), "-name", "cuttle-*.whl*", "-delete"], sudo=True)
        dest = config.CUTTLE_DIST_DIR / wheel.name
        run(["install", "-m", "0644", str(wheel), str(dest)], sudo=True)
        # Detached ed25519 signature beside the wheel, served at /cuttle/wheel.sig. `-rawin` is the
        # one-shot (pure EdDSA) mode ed25519 requires. Signed as root — the key is 0600 ccc/root.
        sig = config.CUTTLE_DIST_DIR / (wheel.name + ".sig")
        run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(config.ROLLOUT_SIGN_KEY),
             "-in", str(dest), "-out", str(sig)], sudo=True)
        run(["chmod", "0644", str(sig)], sudo=True)
    write_root_file(config.CUTTLE_DIST_META, json.dumps({"build": bid, "wheel": wheel.name}) + "\n")
    return bid


def served() -> dict | None:
    """What this Head serves, or None before its first build."""
    try:
        return json.loads(config.CUTTLE_DIST_META.read_text())
    except FileNotFoundError:
        return None
