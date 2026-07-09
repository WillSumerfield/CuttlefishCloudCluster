"""Install + run the Head API (Phase 1 ③).

Lives in its own venv under /opt/ccc so it doesn't share the CLI's, and runs as `munge` — the only
user that can read 0400 munge:munge /etc/munge/munge.key.

Intended path only — no fallbacks (test box; failures decided live).
"""

import shutil
from pathlib import Path

from . import config
from .system import run

# /repo/cli/core/src/cuttle/headapi.py → /repo  (cuttle is installed editable from the checkout)
REPO_ROOT = Path(__file__).resolve().parents[4]
API_SRC = REPO_ROOT / "head" / "api"


def _unit(mesh_ip: str) -> str:
    ctld = f"{mesh_ip}:{config.SLURMCTLD_PORT}"
    return f"""\
[Unit]
Description=CCC Head API (Arm enrollment; bound to the CCC mesh address only)
# uvicorn cannot bind {mesh_ip} until CCC's tailscaled has brought up {config.CCC_TS_TUN}.
After={config.CCC_TS_SERVICE}.service munge.service
Wants={config.CCC_TS_SERVICE}.service
# Never stop retrying: the mesh may take longer than us to come up at boot.
StartLimitIntervalSec=0

[Service]
User={config.HEAD_API_USER}
Environment=CCC_CTLD_ADDR={ctld}
Environment=CCC_CLUSTER={config.CLUSTER_NAME}
Environment=CCC_PARTITION={config.PARTITION}
ExecStart={config.HEAD_API_VENV}/bin/uvicorn ccc_head_api.main:app \
--host {mesh_ip} --port {config.HEAD_API_PORT}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
"""


def install(write_unit, mesh_ip: str) -> None:
    """Build the API venv, install the unit, start it bound to `mesh_ip`."""
    uv = shutil.which("uv")
    run(["install", "-d", "-o", "root", "-g", "root", str(config.HEAD_API_VENV.parent)], sudo=True)
    run([uv, "venv", str(config.HEAD_API_VENV)], sudo=True)
    run([uv, "pip", "install", "--python", str(config.HEAD_API_VENV / "bin" / "python"),
         str(API_SRC)], sudo=True)
    # The venv is root-owned; munge must be able to read/execute it.
    run(["chmod", "-R", "a+rX", str(config.HEAD_API_VENV.parent.parent)], sudo=True)
    write_unit(config.HEAD_API_UNIT, _unit(mesh_ip), "644")
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", config.HEAD_API_SERVICE], sudo=True)
    run(["systemctl", "restart", config.HEAD_API_SERVICE], sudo=True)


def url(mesh_ip: str) -> str:
    return f"http://{mesh_ip}:{config.HEAD_API_PORT}"
