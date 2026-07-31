"""Slurm read-side: shell out to `squeue`/`sinfo --json` and project into the CCC-domain contract.

Shared by the dashboard app (⑥). Shells out to the CLI's `--json` rather than running slurmrestd —
identical data model, no daemon/JWT/bind (slurmrestd deferred to Phase 3 with job submission). Every
response is a hand-picked projection: Slurm job -> Spawn, node -> Arm, so the SPA never couples to
Slurm's ~120-field, versioned schema. Read-only and safe as the unprivileged `nobody` dashboard user;
nothing here touches secrets or the Head user's home.
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path

from fastapi import HTTPException

# Shares the Head API's `ccc.api` logger (configured in main.py) so the diagnostic we keep OUT of the
# client's 503 still lands in the journal.
log = logging.getLogger("ccc.api")

REGISTRY = Path(os.environ.get("CCC_ARMS_REGISTRY", "/var/lib/ccc/arms.json"))
PARTITION = os.environ.get("CCC_PARTITION", "ccc")
# The admin-curated catalog of Arm Features (docs/TODO.md) — mirrors ccc_head_api.enroll's own
# FEATURES_CATALOG constant (that app can't import this one's module; see its docstring on why the two
# LAN/mesh apps stay split). Read-only here: mutation is `cuttle feature add/remove`, CLI+sudo only —
# this (authed, LAN-facing) API exposes no admin writes, same as everywhere else in it.
FEATURES_CATALOG = Path(os.environ.get("CCC_FEATURES_CATALOG", "/var/lib/ccc/features.json"))

# squeue keeps a finished job around (in a terminal state: COMPLETED, FAILED, CANCELLED, TIMEOUT, ...)
# for MinJobAge (slurm.conf, default 300s) after it ends — spawns() must not hand those back, or a Spawn
# that just finished briefly reappears in the live queue as "waiting" (LiveQueue.tsx's isRunning() has no
# terminal-state case; the Dashboard trusts /spawns to already be queue-only, per spawns_history()'s
# "queue is now, history is done").
LIVE_JOB_STATES = {"PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED"}


def reachable() -> bool:
    """slurmctld liveness for `/readyz`. `scontrol ping` is the cheapest canonical check — it asks the
    controller directly and returns non-zero when it can't be reached, with no --json parse or queue
    walk. True iff the primary controller answers. Never raises: a readiness probe reports down, it
    doesn't crash."""
    try:
        r = subprocess.run(["scontrol", "ping"], capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return False
    return r.returncode == 0


def load_registry() -> dict:
    """The enrollment registry (written by the enroll app). World-readable (0644), so the dashboard's
    `nobody` user reads it for the mesh_ip/admitted facts Slurm never sees."""
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}


def _slurm_json(*cmd: str) -> dict:
    """Run a Slurm CLI with --json and parse it. Surfaces failures as 503 rather than a 500 stack —
    the likely cause is slurmctld being down, which the SPA should show as 'cluster unreachable'."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        # The command line and raw exception are a diagnostic for the operator, not the client — a
        # user seeing `squeue --json` and a Python exception repr learns nothing they can act on. Log
        # them, and answer with the one thing that's true and useful: the scheduler isn't answering.
        log.warning("slurm command failed: %s -> %s", " ".join(cmd), e)
        raise HTTPException(503, "the cluster scheduler is not responding — the Head can't read the "
                                 "queue right now; try again shortly")
    return json.loads(r.stdout)


def _num(v):
    """Unwrap Slurm's `{set,infinite,number}` scalar. Returns the int when set+finite, else None."""
    if isinstance(v, dict):
        if not v.get("set", True) or v.get("infinite"):
            return None
        return v.get("number")
    return v


def _gpu_count(gres: str) -> int:
    """Sum GPU counts from any Slurm gres string form: 'gpu:1', 'gres/gpu:1' (tres_per_node),
    'gres/gpu=2', or typed 'gpu:a100:2'. Returns 0 when there's no gpu term."""
    return sum(int(n) for n in re.findall(r"gpu(?::[a-z0-9_]+)?[:=](\d+)", gres or "", re.I))


def _gpu_indices(gres_detail: list[str] | None) -> list[int]:
    """Which of the node's GPUs a RUNNING Spawn actually holds — the **Devices** Settlement prices.

    Slurm's `gres_detail` is one string per allocated node, `gpu:2(IDX:0-1)`, built by slurmctld at
    ALLOCATION time (gres_ctld_job_build_details). `IDX` is a bitmap of the node's GRES indices, and a
    node's GRES are indexed in the order its gres.conf `File=` records were read — which
    templates.render_gres_conf writes in system.gpu_devices() order. So index i here is the i'th entry
    of that Arm's registry `gpu_devices`, and therefore of its `powers`. That correspondence is the
    whole mechanism by which a Spawn is billed for the card it held rather than a node average.

    `squeue --json` sets `detail_flag`, hence SHOW_DETAIL, which is what makes slurmctld pack this at
    all (job_mgr.c `_pack_job`); without it the field is packed as empty and every Spawn would look
    device-less. It is also empty by construction for a job that is not started, or that requested no
    GRES — so this returns [] for a PENDING Spawn and for a CPU-only one, and the caller distinguishes
    those from the pathological case by the Spawn's GPU count.

    CCC Spawns are single-node (`--gres` is per-node and the batch step takes one node), so the list is
    flattened rather than kept per-node — the whole allocation is on `job["node"]`.
    """
    out = []
    for span in re.findall(r"IDX:([\d,\-]+)", ",".join(gres_detail or [])):
        for part in span.split(","):
            lo, _, hi = part.partition("-")
            out.extend(range(int(lo), int(hi or lo) + 1))
    return sorted(out)


def _mean(powers: list[float] | None) -> float | None:
    """A node's Powers as one number, for display. None until the Arm has been benchmarked."""
    return round(sum(powers) / len(powers), 2) if powers else None


def arms() -> list[dict]:
    """Every compute node's live Slurm status, enriched with enrollment facts the scheduler never
    sees: mesh address, admitted, and the **owner** — the User who donated the machine, named by the
    Admin when they authorized it (and what Karma will pay passive income to). `sinfo` is the
    authoritative node list; the Head's own co-located Arm is in it and carries an admin-owned
    registry row despite never enrolling."""
    reg = load_registry()
    out = []
    for rec in _slurm_json("sinfo", "--json").get("sinfo", []):
        state = "+".join(s.lower() for s in rec.get("node", {}).get("state", []))
        gpus = _gpu_count(rec.get("gres", {}).get("total", ""))
        for name in rec.get("nodes", {}).get("nodes", []):
            e = reg.get(name, {})
            out.append({
                "name": name,
                "state": state,
                "gpus": gpus,
                "owner": e.get("owner"),
                "mesh_ip": e.get("mesh_ip"),
                "admitted": e.get("admitted", True),
                # The hardware profile Karma prices from: what each card can DO (**Power**, measured
                # at enroll by a benchmark Spawn — one number per **Device**) and how much it can HOLD
                # (the thing --vram matches; one number for the node, since a mixed-GPU box is refused
                # at enrollment).
                "gpu": e.get("gpu"),
                "vram_mb": e.get("vram_mb"),
                "gpu_devices": e.get("gpu_devices"),
                "powers": e.get("powers"),
                # Arm Features (docs/TODO.md) this Arm declared at enroll — [{"name","path"}, ...].
                "features": e.get("features") or [],
                # Node-level summaries, DERIVED here and stored nowhere — a summary saved beside the
                # list it came from is a second number that can silently disagree with it. Both are
                # display/description only: `power` is what the Dashboard's one-per-Arm column shows,
                # and nothing prices from it (tick._meter bills each held Device at its own Power).
                "power": _mean(e.get("powers")),
                "gpu_device": ", ".join(e.get("gpu_devices") or []) or None,
            })
    return sorted(out, key=lambda a: a["name"])


def vram_constraint(vram_gb: int) -> str:
    """Turn `--vram 20` into a Slurm constraint over the fleet's actual cards: `vram24|vram48|vram80`.

    Slurm constraints are string **set-membership** — there is no `>=`. So an arbitrary integer can
    only be honoured by whoever knows every distinct card in the cluster, and expands the request into
    the OR of the ones that qualify. That is the Head, here. The alternative (fixed buckets baked into
    the Arms) would cap what a User may ask for at whatever list we guessed at design time.
    """
    sizes = sorted({round(e["vram_mb"] / 1024) for e in load_registry().values()
                    if e.get("admitted") and e.get("vram_mb")})
    eligible = [g for g in sizes if g >= vram_gb]
    if not eligible:
        have = ", ".join(f"{g}GB" for g in sizes) or "none"
        raise HTTPException(422, f"no Arm has {vram_gb}GB of VRAM — the cluster has: {have}")
    return "|".join(f"vram{g}" for g in eligible)


def feature_catalog() -> list[str]:
    """The admin-curated catalog of Arm Features, for the Dashboard's submit composer (`GET
    /features`) and the CLI's `--feature` picker."""
    return sorted(json.loads(FEATURES_CATALOG.read_text())) if FEATURES_CATALOG.exists() else []


def feature_constraint(features: list[str]) -> str:
    """Turn a Spawn's declared Arm Features into the AND-clause of a Slurm constraint: `vsim&isaacsim`.

    Unlike VRAM (set-membership OR over size buckets an exact number rarely matches), a Feature is an
    exact name a node either has or doesn't — so requiring several is a plain conjunction, no catalog
    expansion needed. Feasibility (does any ADMITTED Arm carry every one?) is `arms_with_features`'
    job, checked separately before this is ever combined into a constraint string.
    """
    return "&".join(sorted(set(features)))


def arms_with_features(features: list[str]) -> list[str]:
    """Every ADMITTED Arm whose declared Feature set is a superset of `features` — the feasibility
    check `submit_checked` runs before ever calling sbatch, mirroring `vram_constraint`'s "no Arm has
    that much VRAM" refusal (docs/TODO.md "Arm Features")."""
    need = set(features)
    return [name for name, e in load_registry().items()
            if e.get("admitted") and need <= {f["name"] for f in (e.get("features") or [])}]


def all_feature_names() -> list[str]:
    """Every Arm Feature any ADMITTED Arm actually carries — narrower than `feature_catalog()` (which
    includes names no live Arm has yet), and what an "unsatisfiable request" error names as "known"."""
    names: set[str] = set()
    for e in load_registry().values():
        if e.get("admitted"):
            names.update(f["name"] for f in (e.get("features") or []))
    return sorted(names)


def queue() -> list[dict]:
    """Every live Spawn — PENDING and RUNNING — and the Karma tick's whole view of Slurm. One poll per
    pass, because the tick needs both halves and they must agree with each other:

    * **PENDING** Spawns are what it re-ranks (by `nice`) and what generates queued **Demand**.
    * **RUNNING** Spawns are what it meters — `node` says which Arm's Demand to bill, `devices` says
      which of that Arm's cards it holds (and so which **Power**s it is priced at), and `start_time`
      is where the very first accrual bills *from* (a Spawn that started between two ticks still owes
      for the seconds before the tick noticed).

    Absence from a *successful* poll is the only signal a Spawn has **ended** — it is what triggers
    Settlement. There is no other: a killed Spawn writes no end-time, Slurm forgets jobs after
    `MinJobAge`, and `sacct` would need the slurmdbd we deliberately don't run. If slurmctld is down
    `_slurm_json` raises, the tick logs and skips, and nothing settles — which is the point.

    `nice` is read back rather than remembered: it is the one piece of state the tick shares with an
    operator, and Slurm is where they meet. A **negative** nice is a Queue bump — only an operator can
    set one (Slurm refuses a negative nice from a job's owner, and every Spawn is owned by the Mantle),
    so its mere presence is the bump flag. No bump table, no coordination state.
    """
    out = []
    for j in _slurm_json("squeue", "--json").get("jobs", []):
        state = j.get("job_state") or []
        if j.get("partition") != PARTITION:
            continue
        if "PENDING" in state:
            live = "PENDING"
        elif "RUNNING" in state:
            live = "RUNNING"
        else:
            continue  # COMPLETING/COMPLETED/FAILED — on its way out; the next poll settles it
        gres = ",".join(j.get("gres_detail") or []) or j.get("tres_per_node", "")
        out.append({
            "id": _num(j.get("job_id")),
            "state": live,
            "nice": _num(j.get("nice")) or 0,
            "node": (j.get("nodes") or None) if live == "RUNNING" else None,
            "gpus": _gpu_count(gres),
            # Empty until the Spawn starts — a PENDING Spawn holds no Device, which is exactly why the
            # Escrow is a forecast over eligible cards and the Meter is not.
            "devices": _gpu_indices(j.get("gres_detail")),
            "start_time": _num(j.get("start_time")),
        })
    return out


def set_nice(job_id: int, nice: int) -> None:
    """Lower a queued Spawn's priority. Needs no privilege at all: every Spawn runs as the Mantle, so
    the Head *owns* every job in the queue, and setting your own job's nice is an owner's right (only a
    negative nice — a boost — needs a Slurm operator)."""
    subprocess.run(["scontrol", "update", f"JobId={job_id}", f"Nice={nice}"],
                   capture_output=True, text=True, timeout=15, check=True)


def spawns() -> list[dict]:
    """The live Spawn queue — pending/running jobs in the CCC partition. Completed Spawns drop out of
    squeue after MinJobAge; durable history waits for Phase 3 slurmdbd (see module docstring)."""
    out = []
    for j in _slurm_json("squeue", "--json").get("jobs", []):
        if j.get("partition") != PARTITION:
            continue
        # gres_detail when the Spawn is running (it names the exact cards); tres_per_node
        # ('gres/gpu:1') otherwise. The *allocated* TRES is no use for either — it omits gpu entirely,
        # since we run no gres accounting. slurmctld packs gres_detail only for a STARTED job that
        # requested GRES, so a pending Spawn falls through to what it asked for. See _gpu_indices.
        gres = ",".join(j.get("gres_detail") or []) or j.get("tres_per_node", "")
        out.append({
            "id": _num(j.get("job_id")),
            "name": j.get("name"),
            "state": "+".join(s.lower() for s in j.get("job_state", [])),
            "node": j.get("nodes") or None,
            "gpus": _gpu_count(gres),
            "user": j.get("user_name"),
            # The declared duration (`--time`), in minutes — what a waiting Spawn is priced against and
            # the one declared need squeue carries cleanly. `--vram` becomes a Slurm *constraint*
            # string, which is not worth parsing back out here.
            "time_limit": _num(j.get("time_limit")),
            "submit_time": _num(j.get("submit_time")),
            "start_time": _num(j.get("start_time")),
        })
    return sorted(out, key=lambda s: s["id"] or 0)


def cancel(job_id: int) -> None:
    """End a live Spawn now. `scancel` needs no privilege here — every Spawn runs as the Mantle, so the
    Head owns every job in the queue.

    Settlement is NOT done here: the Karma tick settles a Spawn on its *disappearance from squeue*
    (slurm.queue()), which is exactly what this causes, so a cancel bills for the seconds actually held
    with no second code path. Idempotent — scancel on an already-gone job is a no-op, not an error."""
    subprocess.run(["scancel", str(job_id)], capture_output=True, text=True, timeout=15, check=True)


def signal(job_id: int, sig: str) -> None:
    """Send a signal to a live Spawn, without ending it the way `cancel` does. Needs no privilege here
    either, for the same reason — every Spawn runs as the Mantle. Used by a Retract's initial nudge
    (TERM, so the job's own EXIT trap can stage out what it can) and by the Karma tick's force-kill once
    its grace period has passed (KILL)."""
    subprocess.run(["scancel", f"--signal={sig}", str(job_id)],
                   capture_output=True, text=True, timeout=15, check=True)
