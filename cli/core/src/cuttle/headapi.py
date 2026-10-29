"""Install + run the head-side API services.

Two FastAPI apps from one package (`ccc_head_api`), one shared venv under /opt/ccc (kept out of the
CLI's own venv):
- **Enroll API** (`ccc_head_api.enroll`) — mesh-only, runs as `munge` (the only user that can read
  0400 munge:munge /etc/munge/munge.key), serves the munge key to admitted Arms.
- **Head API** (`ccc_head_api.main`) — LAN+mesh, backs the web Dashboard + CLI (read board, auth,
  submission).

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
Environment=CCC_CLUSTER={config.SLURM_CLUSTER}
Environment=CCC_PARTITION={config.PARTITION}
# Handed to the Arm in /enroll's response, so it trusts the Head with no fingerprint ceremony.
Environment=CCC_CA_CERT={config.HEAD_CA_CERT}
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
    # --python/--no-managed-python: the version this package requires, off the SYSTEM interpreter.
    # See config.API_PYTHON — the failure either one prevents is a venv that builds and a service that
    # cannot start, which is a far worse place to find out.
    #
    # --clear: setup is re-run routinely (teardown+setup is the normal redeploy here), and a venv built
    # on the wrong interpreter must be REPLACED rather than reused. uv already replaces it, but warns
    # that a future version will refuse to without this — asking now, while it is a warning, rather
    # than on the upgrade that turns it into a failed setup.
    run([uv, "venv", "--clear", "--python", config.API_PYTHON, "--no-managed-python",
         str(config.API_VENV)], sudo=True)
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


# --- the Enroll API's one sudo grant ------------------------------------------
# `POST /enroll` verifies who Headscale actually thinks is calling rather than trusting a claimed
# `mesh_ip`. `munge` can't reach Headscale's unix socket itself (`750 headscale:headscale`, munge is
# in neither), so it gets exactly this one read-only lookup, nothing else headscale's CLI can do
# (nodes delete, preauthkeys create, ...). NOPASSWD: fires inside a request handler, no tty to prompt.
_SUDOERS_MUNGE_HEADSCALE = f"""\
# Managed by `cuttle setup --head` — do not hand-edit, re-run setup instead.
# The Enroll API (User={config.ENROLL_API_USER}; see ccc_head_api.enroll) verifies an /enroll
# caller's real mesh identity by asking Headscale who is registered at the peer's address. One
# command, no wildcard, no argument taken from the request.
{config.ENROLL_API_USER} ALL=(root) NOPASSWD: /usr/bin/headscale nodes list --output json
"""


def install_enroll_sudoers(write_unit) -> None:
    write_unit(config.SUDOERS_MUNGE_HEADSCALE, _SUDOERS_MUNGE_HEADSCALE, "440")


# --- the Mantle's one sudo grant ---------------------------------------------
# A Dive belongs to a **Guise**, not the Head/Mantle, so submit/re-nice/cancel/signal (each normally
# free to the job's owner) need the Head to act as that Guise to touch its own cluster's queue.
#
# `(%ccc-guises)`, never `(root)`: lets `ccc` act as an account owning nothing but one Dive's working
# files — it can't read head.db, head.key, or another User's results, same as any Guise. Root would
# undo that isolation. This is the only unattended sudo grant here; `cuttle user add`'s useradd runs
# under the operator's own sudo at a tty.
_SUDOERS_CCC_SLURM = f"""\
# Managed by `cuttle setup --head` — do not hand-edit, re-run setup instead.
# The Head API and the Karma tick (User={config.HEAD_API_USER}) act on Dives as the Guise that owns
# them: submit, re-nice, cancel, signal. Runas is the Guise group — NOT root.
# The Head API runs no User's code and starts no container; Slurm's verbs are the whole grant.
{config.HEAD_API_USER} ALL=(%{config.GUISE_GROUP}) NOPASSWD: /usr/bin/sbatch, /usr/bin/scancel, \
/usr/bin/scontrol
"""


def install_slurm_sudoers(write_unit) -> None:
    write_unit(config.SUDOERS_CCC_SLURM, _SUDOERS_CCC_SLURM, "440")


# --- Head API (⑥): read API + SPA, bound to LAN + mesh ----------------------
# A second unit off the SAME venv (enroll app = munge key; main app = read board + auth +
# SPA). Binds 0.0.0.0 so a LAN box reaches it too (the Enroll API stays mesh-only). See config.HEAD_API_*.

def _head_unit(mesh_ip: str) -> str:
    return f"""\
[Unit]
Description=CCC Head API (board + SPA + auth; bound to LAN + mesh)
# squeue/sinfo/sbatch need munge to authenticate and slurmctld to answer; until they're up we just 503.
After=munge.service slurmctld.service
Wants=slurmctld.service

[Service]
User={config.HEAD_API_USER}
# {config.HEAD_API_PORT} is privileged (<1024) and this service is not root, so without this the bind
# fails outright with EACCES. Ambient, not a setuid binary or a root process that drops: uvicorn
# inherits the one capability and nothing else. It does permit binding ANY low port, not just
# {config.HEAD_API_PORT} — accepted because the alternatives (socket activation, an nftables redirect)
# each add a moving part to setup and teardown to save an account that already holds head.db and the
# TLS key from a privilege it has no way to use.
AmbientCapabilities=CAP_NET_BIND_SERVICE
Environment=CCC_PARTITION={config.PARTITION}
Environment=CCC_WEB_DIR={config.HEAD_API_WEB_DIR}
Environment=CCC_HEAD_DB={config.HEAD_DB}
# Served open at GET /ca.pem: the public half of the CA, so a box can trust this Head instead of
# clicking through a warning. The key beside it is 0600 {config.HEAD_API_USER}-owned and stays here.
Environment=CCC_CA_CERT={config.HEAD_CA_CERT}
# The Head's mesh IP (a remote Arm's Dive rsyncs home to it) and the state dir. Paths match
# cuttle.config defaults.
Environment=CCC_MESH_IP={mesh_ip}
Environment=CCC_HEAD_STATE={config.HEAD_STATE_DIR}
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
    mesh-only, and WireGuard already encrypts that path).

    The port is left off when it is https's own default, so every surface that shows an address —
    settings, `cuttle dashboard`, the closing summary, a baked bundle — reads `https://ccc.<name>`
    and not `https://ccc.<name>:443`. Same URL either way; only one of them is worth typing."""
    port = "" if config.HEAD_API_PORT == 443 else f":{config.HEAD_API_PORT}"
    return f"https://{host}{port}"


# --- Karma tick: the queue's ranking loop ------------------------------------
# A third unit off the SAME venv and the same head.db. Its own service, not a task inside the Head API:
# the economy must not be able to wedge the API, nor the API the economy. Runs as the Mantle — which
# owns every job in the queue, and so re-ranks them all with no privilege at all.

def _karma_unit() -> str:
    return f"""\
[Unit]
Description=CCC Karma (ranks the Dive queue by each submitter's standing)
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
