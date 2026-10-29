"""A laptop's cuttle keeps itself on the Head's build.

Before a process's first call to the Head API, cuttle asks the Head which build it serves (`GET
/build`, one small request). If it differs from its own, it installs the Head's wheel over itself and
re-runs the same command, so nothing has happened at the Head yet and nothing runs twice.

Only a wheel install does this (`buildid.stamped`): the Head's own cuttle is a checkout, and the copy
inside Tidepools and Dives is rebuilt by the Head itself.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import buildid, config, ui

# Set on the re-run, so a build that still does not match after installing (it should) cannot loop.
_ENV = "CCC_JUST_UPDATED"


def enabled() -> bool:
    return buildid.stamped() and not config.ambient_session() and not os.environ.get(_ENV)


def check(client, timeout: float | None) -> None:
    """`client` is the Api's httpx client. Any failure here is silent: the real call that follows
    reports an unreachable Head far better than this would."""
    if not enabled():
        return
    extra = {"timeout": timeout} if timeout else {}
    try:
        r = client.get("/build", **extra)
        head = r.json()["build"] if r.status_code == 200 else None
    except Exception:
        return
    if not head or head == buildid.current():
        return
    if not config.SESSION_FILE.exists():
        ui.warn("this cuttle is a different build from the Head's. It updates itself once you've "
                "logged in (`cuttle login`)")
        return
    _install(client, config.SESSION_FILE.read_text().strip())
    os.environ[_ENV] = "1"
    os.execvp(sys.argv[0], sys.argv)


def _install(client, token: str) -> None:
    ui.say("[muted]Updating cuttle to the Head's build…[/muted]")
    r = client.get("/cuttle/wheel", headers={"Authorization": f"Bearer {token}"}, timeout=300)
    if r.status_code != 200:
        raise ui.abort(f"couldn't download the Head's cuttle (HTTP {r.status_code})",
                       next_step="try again, or ask your Admin for a new installer")
    name = re.search(r'filename="?([^";]+)"?', r.headers.get("content-disposition", ""))
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    with tempfile.TemporaryDirectory() as td:
        wheel = Path(td) / (name.group(1) if name else "cuttle-0-py3-none-any.whl")
        wheel.write_bytes(r.content)
        done = subprocess.run([uv, "tool", "install", "--force", str(wheel)],
                              capture_output=True, text=True)
    if done.returncode:
        raise ui.abort("couldn't install the Head's cuttle:\n" + (done.stdout + done.stderr)[-2000:],
                       next_step="fix the error above, or ask your Admin for a new installer")
