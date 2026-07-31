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


# --- the Enroll API's one sudo grant (Phase 9 #2) ----------------------------
# `POST /enroll` used to trust the caller's claimed `mesh_ip` outright. Now it checks who Headscale
# actually thinks is calling — but `munge` can't reach Headscale's unix socket to ask (it's
# `750 headscale:headscale`, and `munge` is in neither), so it needs exactly enough privilege to run
# the one read-only lookup that answers that question, and nothing headscale's CLI can otherwise do
# (nodes delete, preauthkeys create, ...). NOPASSWD: this fires inside a request handler with no tty
# to prompt.
_SUDOERS_MUNGE_HEADSCALE = f"""\
# Managed by `cuttle setup --head` — do not hand-edit, re-run setup instead.
# The Enroll API (User={config.ENROLL_API_USER}; see ccc_head_api.enroll) verifies an /enroll
# caller's real mesh identity by asking Headscale who is registered at the peer's address. One
# command, no wildcard, no argument taken from the request.
{config.ENROLL_API_USER} ALL=(root) NOPASSWD: /usr/bin/headscale nodes list --output json
"""


def install_enroll_sudoers(write_unit) -> None:
    write_unit(config.SUDOERS_MUNGE_HEADSCALE, _SUDOERS_MUNGE_HEADSCALE, "440")


# --- the Mantle's one sudo grant (temp/spawn-isolation.md Phase 3b) ----------
# Every Slurm operation CCC performs on a Spawn — submit it, re-nice it for Karma, cancel it, signal it
# on a Retract — is one the job's OWNER may perform with no privilege at all. That was free while every
# Spawn ran as the Mantle and the Head *was* the owner of every job in the queue. Now a Spawn belongs
# to a **Guise**, so the Head has to become one to touch its own cluster's queue.
#
# **`(%ccc-guises)`, never `(root)`** — and that distinction is the whole of Phase 3's privilege story.
# This grant lets `ccc` act as an account that owns nothing but one Spawn's working files; it cannot
# read head.db, head.key, or another User's upload, because a Guise cannot. Escalating it to root would
# hand back everything the phase just took away. It is the ONE unattended grant Phase 3 adds; `cuttle
# user add`'s useradd runs under the operator's own sudo, at a tty, and is not here.
#
# `env_keep` is load-bearing, not hygiene. sbatch propagates ITS OWN environment to the job, which is
# how the submitting User's W&B key reaches the Pod (ccc_head_api.spawns.compose_wandb_env writes it
# into the child env). sudo scrubs the environment by default, so without this every Spawn would run
# with no key and silently log nothing — a failure a User only discovers afterwards, in an empty W&B
# project. Four named variables, scoped to this user: not `SETENV:`+`--preserve-env` (a far wider grant
# than needed) and never `sbatch --export=...WANDB_API_KEY=`, which would put a User's secret in argv
# where `ps` can read it.
_SUDOERS_CCC_SLURM = f"""\
# Managed by `cuttle setup --head` — do not hand-edit, re-run setup instead.
# The Head API and the Karma tick (User={config.HEAD_API_USER}) act on Spawns as the Guise that owns
# them: submit, re-nice, cancel, signal. Runas is the Guise group — NOT root.
Defaults:{config.HEAD_API_USER} env_keep += "WANDB_API_KEY WANDB_MODE WANDB_PROJECT WANDB_ENTITY"
{config.HEAD_API_USER} ALL=(%{config.GUISE_GROUP}) NOPASSWD: /usr/bin/sbatch, /usr/bin/scancel, \
/usr/bin/scontrol
"""


def install_slurm_sudoers(write_unit) -> None:
    write_unit(config.SUDOERS_CCC_SLURM, _SUDOERS_CCC_SLURM, "440")


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
# --no-access-log: the app's own middleware logs one enriched line per request (with the authed user
# and latency), so uvicorn's bare access log would only duplicate it. Its error log (startup, 500s
# it re-raises) stays on.
ExecStart={config.API_VENV}/bin/uvicorn ccc_head_api.main:app \
--host 0.0.0.0 --port {config.HEAD_API_PORT} --no-access-log \
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


# --- Karma tick (Phase 4 slice 1): the queue's ranking loop -----------------
# A third unit off the SAME venv and the same head.db. Its own service, not a task inside the Head API:
# the economy must not be able to wedge the API, nor the API the economy. Runs as the Mantle — which
# owns every job in the queue, and so re-ranks them all with no privilege at all.

def _karma_unit() -> str:
    return f"""\
[Unit]
Description=CCC Karma (ranks the Spawn queue by each submitter's standing)
# scontrol/squeue need munge to authenticate and slurmctld to answer. Neither being up is fatal — the
# tick logs the failure and re-reads the world on the next pass — but don't race them at boot.
After=munge.service slurmctld.service {config.HEAD_API_SERVICE}.service
Wants=slurmctld.service

[Service]
User={config.HEAD_API_USER}
Environment=CCC_PARTITION={config.PARTITION}
Environment=CCC_HEAD_DB={config.HEAD_DB}
ExecStart={config.API_VENV}/bin/python -m ccc_head_api.tick
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def install_karma(write_unit) -> None:
    """Install + start the Karma tick. Assumes the shared venv (install_enroll) and head.db
    (provision_head_auth) exist."""
    write_unit(config.KARMA_UNIT, _karma_unit(), "644")
    run(["systemctl", "daemon-reload"], sudo=True)
    run(["systemctl", "enable", config.KARMA_SERVICE], sudo=True)
    run(["systemctl", "restart", config.KARMA_SERVICE], sudo=True)
