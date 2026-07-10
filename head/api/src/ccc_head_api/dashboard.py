"""CCC dashboard app (Phase 1 ⑥) — the read API + the SPA, served together on the LAN + mesh.

Deliberately a **separate** service from the enroll app (`ccc_head_api.main`). They have opposite
exposure needs: enrollment hands out the munge key and stays bound to the mesh only, while this must
reach the LAN so anyone on the CCC mesh *or* the local network can open the board. It holds no
secrets — it only runs read-only `squeue`/`sinfo` (via `slurm.py`) and serves static files — so it
runs as the unprivileged `nobody` user and binds `0.0.0.0`.

Same origin for API + SPA, so the front-end fetches `/arms` and `/spawns` with no CORS. Read-only:
job control and submission (mutations) arrive with the Phase-3 login, not here.
"""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import slurm

# The built SPA, copied here by `cuttle setup --head` (world-readable; served as nobody). Kept out of
# the Head user's home on purpose — nobody can't traverse a 0750 home.
WEB_DIR = Path(os.environ.get("CCC_WEB_DIR", "/opt/ccc/dashboard/web"))

app = FastAPI(title="CCC Dashboard", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/arms")
def arms() -> list[dict]:
    """Every compute node's live status + enrollment facts. See slurm.arms()."""
    return slurm.arms()


@app.get("/spawns")
def spawns() -> list[dict]:
    """The live Spawn queue (pending/running, CCC partition). See slurm.spawns()."""
    return slurm.spawns()


# Mounted LAST so the API routes above win. html=True serves index.html at `/`. Guarded so the app
# still imports/serves the API before the SPA has been built (fresh checkout, tests).
if (WEB_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="spa")
