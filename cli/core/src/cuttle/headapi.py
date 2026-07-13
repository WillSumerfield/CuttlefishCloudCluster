"""Install + run the head-side API services (Phase 1).

Two FastAPI apps from one package (`ccc_head_api`), one shared venv under /opt/ccc (kept out of the
CLI's own venv):
- **Enroll API** (`ccc_head_api.enroll`) — mesh-only, runs as `munge` (the only user that can read
  0400 munge:munge /etc/munge/munge.key), serves the munge key to admitted Arms.
- **Head API** (`ccc_head_api.main`) — LAN+mesh, backs the web Dashboard + CLI (read board now; auth +
  submission from Phase 3a).

Intended path only — no fallbacks (test box; failures decided live).
"""

import shutil
from pathlib import Path

from . import config
from .system import run

# /repo/cli/core/src/cuttle/headapi.py → /repo  (cuttle is installed editable from the checkout)
REPO_ROOT = Path(__file__).resolve().parents[4]
API_SRC = REPO_ROOT / "head" / "api"


# --- Enroll API (③): mesh-only, serves the munge key ------------------------

def _enroll_unit(mesh_ip: str) -> str:
    ctld = f"{mesh_ip}:{config.SLURMCTLD_PORT}"
    return f"""\
[Unit]
Description=CCC Enroll API (Arm enrollment; bound to the CCC mesh address only)
# uvicorn cannot bind {mesh_ip} until CCC's tailscaled has brought up {config.CCC_TS_TUN}.
After={config.CCC_TS_SERVICE}.service munge.service
Wants={config.CCC_TS_SERVICE}.service
# Never stop retrying: the mesh may take longer than us to come up at boot.
StartLimitIntervalSec=0

[Service]
User={config.ENROLL_API_USER}
Environment=CCC_CTLD_ADDR={ctld}
Environment=CCC_CLUSTER={config.CLUSTER_NAME}
Environment=CCC_PARTITION={config.PARTITION}
ExecStart={config.API_VENV}/bin/uvicorn ccc_head_api.enroll:app \
--host {mesh_ip} --port {config.ENROLL_API_PORT}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
"""


def install_enroll(write_unit, mesh_ip: str) -> None:
    """Build the shared API venv, install the Enroll API unit, start it bound to `mesh_ip`."""
    uv = shutil.which("uv")
    run(["install", "-d", "-o", "root", "-g", "root", str(config.API_VENV.parent)], sudo=True)
    run([uv, "venv", str(config.API_VENV)], sudo=True)
    run([uv, "pip", "install", "--python", str(config.API_VENV / "bin" / "python"),
         str(API_SRC)], sudo=True)
    # The venv is root-owned; the service users (munge, ccc, nobody) must read/execute it.
    run(["chmod", "-R", "a+rX", str(config.API_VENV.parent.parent)], sudo=True)
    write_unit(config.ENROLL_API_UNIT, _enroll_unit(mesh_ip), "644")
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", config.ENROLL_API_SERVICE], sudo=True)
    run(["systemctl", "restart", config.ENROLL_API_SERVICE], sudo=True)


def enroll_url(mesh_ip: str) -> str:
    return f"http://{mesh_ip}:{config.ENROLL_API_PORT}"


# --- Head API (⑥): read API + SPA, bound to LAN + mesh ----------------------
# A second unit off the SAME venv (enroll app = munge key; main app = read board + SPA). Phase 1 runs
# as `nobody` (holds no secrets; only read-only squeue/sinfo + static files). Binds 0.0.0.0 so a LAN
# box reaches it too (the Enroll API stays mesh-only). See config.HEAD_API_*.

def _head_unit(mesh_ip: str) -> str:
    return f"""\
[Unit]
Description=CCC Head API (board + SPA + auth + submit; bound to LAN + mesh)
# squeue/sinfo/sbatch need munge to authenticate and slurmctld to answer; until they're up we just 503.
After=munge.service slurmctld.service
Wants=slurmctld.service

[Service]
User={config.HEAD_API_USER}
Environment=CCC_PARTITION={config.PARTITION}
Environment=CCC_WEB_DIR={config.HEAD_API_WEB_DIR}
Environment=CCC_HEAD_DB={config.HEAD_DB}
# Submission: the Head's mesh IP is baked into each rendered job script so a remote Arm can rsync
# results home. Fixed submission paths (state dir, sif) match cuttle.config defaults. CCC_SIF is the
# cluster-wide image path — the same on every node, so the script names it once.
Environment=CCC_MESH_IP={mesh_ip}
Environment=CCC_HEAD_STATE={config.HEAD_STATE_DIR}
Environment=CCC_SIF={config.CCC_SIF}
ExecStart={config.API_VENV}/bin/uvicorn ccc_head_api.main:app \
--host 0.0.0.0 --port {config.HEAD_API_PORT} \
--ssl-keyfile {config.HEAD_KEY} --ssl-certfile {config.HEAD_CERT}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
"""


def install_head(write_unit, mesh_ip: str) -> None:
    """Install + start the Head API unit. Assumes `install_enroll()` built the shared venv, the SPA is
    staged in config.HEAD_API_WEB_DIR (setup.build_spa), and the TLS cert exists (setup.install_tls) —
    uvicorn will not start without the key/cert it's pointed at."""
    write_unit(config.HEAD_API_UNIT, _head_unit(mesh_ip), "644")
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", config.HEAD_API_SERVICE], sudo=True)
    run(["systemctl", "restart", config.HEAD_API_SERVICE], sudo=True)


def head_url(host: str) -> str:
    """https: the Head API carries passwords + session tokens (the Enroll API stays plain HTTP — it's
    mesh-only, and WireGuard already encrypts that path)."""
    return f"https://{host}:{config.HEAD_API_PORT}"
