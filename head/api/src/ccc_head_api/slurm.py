"""Slurm read-side: shell out to `squeue`/`sinfo --json` and project into the CCC-domain contract.

Shared by the dashboard app (⑥). Shells out to the CLI's `--json` rather than running slurmrestd —
identical data model, no daemon/JWT/bind (slurmrestd deferred to Phase 3 with job submission). Every
response is a hand-picked projection: Slurm job -> Dive, node -> Arm, so the SPA never couples to
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

from . import guises

# Shares the Head API's `ccc.api` logger (configured in main.py) so the diagnostic we keep OUT of the
# client's 503 still lands in the journal.
log = logging.getLogger("ccc.api")

REGISTRY = Path(os.environ.get("CCC_ARMS_REGISTRY", "/var/lib/ccc/arms.json"))
PARTITION = os.environ.get("CCC_PARTITION", "ccc")
# The catalog of Arm Features — mirrors the same constant in ccc_head_api.enroll and cuttle.config
# (neither app can import the others). All three must agree. Built into CCC rather than curated, since
# CCC ships the runtime for each Feature — there is nothing here for an operator to mutate.
FEATURE_NAMES = ("vlearn", "isaaclab")

# squeue keeps a finished job around (in a terminal state: COMPLETED, FAILED, CANCELLED, TIMEOUT, ...)
# for MinJobAge (slurm.conf, default 300s) after it ends — dives() must not hand those back, or a Dive
# that just finished briefly reappears in the live queue as "waiting" (LiveQueue.tsx's isRunning() has no
# terminal-state case; the Dashboard trusts /dives to already be queue-only, per dive.history()'s
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
    """Which of the node's GPUs a RUNNING Dive actually holds — the **Devices** Settlement prices.

    Slurm's `gres_detail` is one string per allocated node, `gpu:2(IDX:0-1)`. `IDX` is a bitmap of the
    node's GRES indices, and a node's GRES are indexed in the order its gres.conf `File=` records were
    read — the same order templates.render_gres_conf writes them in. So index i here is the i'th entry
    of that Arm's registry `gpu_devices`, and therefore its `powers`: the whole mechanism by which a
    Dive is billed for the card it held rather than a node average.

    `squeue --json` must set `detail_flag` (SHOW_DETAIL) or slurmctld packs this empty and every Dive
    looks device-less. Also empty by construction for a job not yet started. Every Dive requests GRES
    (there is no Device-less work here), so an empty list on a RUNNING one is always a failure to
    report, never a Dive that asked for nothing.

    CCC Dives are single-node, so the list is flattened rather than kept per-node.
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
    # Per node: sinfo groups nodes sharing a state into one record whose gres is the group's, not each
    # node's (dex's 2 GPUs were shown on a 1-GPU Arm).
    gres = {n["name"]: n.get("gres", "") for n in _slurm_json("scontrol", "show", "nodes", "--json").get("nodes", [])}
    out = []
    for rec in _slurm_json("sinfo", "--json").get("sinfo", []):
        state = "+".join(s.lower() for s in rec.get("node", {}).get("state", []))
        for name in rec.get("nodes", {}).get("nodes", []):
            e = reg.get(name, {})
            out.append({
                "name": name,
                "state": state,
                "gpus": _gpu_count(gres.get(name, "")),
                "owner": e.get("owner"),
                "mesh_ip": e.get("mesh_ip"),
                "admitted": e.get("admitted", True),
                # The Head's OWN co-located Arm (enroll.register_head_arm). Same box as the Head, which
                # is why a Dive there needs no relay — tidepool.ensure_relay.
                "head": bool(e.get("head")),
                # The hardware profile Karma prices from: what each card can DO (**Power**, measured
                # at enroll by a benchmark Dive — one number per **Device**) and how much it can HOLD
                # (the thing --vram matches; one number for the node, since a mixed-GPU box is refused
                # at enrollment).
                "gpu": e.get("gpu"),
                "vram_mb": e.get("vram_mb"),
                "gpu_devices": e.get("gpu_devices"),
                "powers": e.get("powers"),
                # Arm Features this Arm declared at enroll — [{"name","path"}, ...].
                "features": e.get("features") or [],
                # Node-level summaries, DERIVED here and stored nowhere — a summary saved beside the
                # list it came from is a second number that can silently disagree with it. Both are
                # display/description only: `power` is what the Dashboard's one-per-Arm column shows,
                # and nothing prices from it (tick._meter bills each held Device at its own Power).
                "power": _mean(e.get("powers")),
                "gpu_device": ", ".join(e.get("gpu_devices") or []) or None,
            })
    return sorted(out, key=lambda a: a["name"])


def _devices(entry: dict) -> int:
    """How many **Device**s an Arm enrolled with. `gpu_devices` is the per-card list Powers align to,
    so its length is the count — not `vram_mb`, which is one number for the whole node."""
    return len(entry.get("gpu_devices") or [])


def max_devices() -> int:
    """The most Devices any admitted Arm carries — the ceiling on a `--gpus` count, and how far the
    Dashboard's dropdown runs."""
    return max((_devices(e) for e in load_registry().values() if e.get("admitted")), default=0)


def card_gb(vram_mb: int) -> int:
    """How many whole GB a card counts as — **rounded up**.

    Mirrors `cuttle.config.card_gb`, which is what an Arm advertises its Slurm feature with; the two
    packages do not import each other (same reason FEATURE_NAMES is duplicated) and MUST agree. This
    side selects, that side advertises: a card the Head calls 16 GB and the Arm labels `vram15` is one
    no eligible Dive can ever be placed on.

    Up rather than nearest, because a card almost never reports the number it is sold as — ECC and
    virtualization both take a bite, so a 16 GB card can report under 15.5 and be advertised `vram15`,
    refusing `--vram 16` on hardware a User can see is 16 GB. The bounded cost is that a card counts as
    up to 1 GB larger than it is.
    """
    return -(-vram_mb // 1024)


def vram_constraint(vram_gb: int, gpus: int = 1) -> str:
    """Turn `--vram 20 --gpus 2` into a Slurm constraint over the fleet's actual cards:
    `vram24|vram48|vram80`.

    Slurm constraints are string **set-membership** — there is no `>=`. So an arbitrary integer can
    only be honoured by whoever knows every distinct card in the cluster, and expands the request into
    the OR of the ones that qualify. That is the Head, here. The alternative (fixed buckets baked into
    the Arms) would cap what a User may ask for at whatever list we guessed at design time.

    `--vram` is a **total across the Devices asked for**, so the per-card need is `ceil(vram/gpus)` —
    exact, never an approximation, because a mixed-GPU box is refused at enrollment and every card on a
    node is therefore the same size.

    The two needs are one **joint** question, which is why the registry is narrowed to Arms with enough
    Devices *first* and the card sizes are drawn only from that set. Asked separately they pass a Dive
    nothing can run: a 2×40GB box satisfies `--gpus 2` and a 1×96GB box satisfies the 90GB per-card
    need, and neither box can host the Dive.
    """
    admitted = [e for e in load_registry().values() if e.get("admitted") and e.get("vram_mb")]
    roomy = [e for e in admitted if _devices(e) >= gpus]
    if not roomy:
        most = max((_devices(e) for e in admitted), default=0)
        raise HTTPException(422, f"no Arm has {gpus} GPUs — the largest has {most}")
    per_card = -(-vram_gb // gpus)  # ceil
    sizes = sorted({card_gb(e["vram_mb"]) for e in roomy})
    eligible = [g for g in sizes if g >= per_card]
    if not eligible:
        # What a User can act on: the per-card figure their own two numbers imply, the card sizes that
        # exist (already public — a User picks --vram against them), and the smallest count that would
        # make the same total fit. WHICH machine has what is the Admin's business and stays out.
        have = ", ".join(f"{g}GB" for g in sizes) or "none"
        fix = next((n for n in range(gpus + 1, max_devices() + 1)
                    if any(card_gb(e["vram_mb"]) >= -(-vram_gb // n)
                           for e in admitted if _devices(e) >= n)), None)
        hint = f" — try --gpus {fix}" if fix else ""
        asked = (f"no Arm has {vram_gb}GB of VRAM" if gpus == 1 else
                 f"{vram_gb}GB across {gpus} GPUs needs {per_card}GB per card")
        raise HTTPException(422, f"{asked} — the cluster has: {have}{hint}")
    return "|".join(f"vram{g}" for g in eligible)


def feature_catalog() -> list[str]:
    """The Arm Features CCC supports (`GET /features`).

    What the cluster can actually PLACE is narrower — see `all_feature_names`, which reports only what
    an admitted Arm carries. This is the vocabulary; that is the availability."""
    return sorted(FEATURE_NAMES)


def feature_constraint(features: list[str]) -> str:
    """Turn a Dive's declared Arm Features into the AND-clause of a Slurm constraint: `vsim&isaacsim`.

    Unlike VRAM (set-membership OR over size buckets an exact number rarely matches), a Feature is an
    exact name a node either has or doesn't — so requiring several is a plain conjunction, no catalog
    expansion needed. Feasibility (does any ADMITTED Arm carry every one?) is `arms_with_features`'
    job, checked separately before this is ever combined into a constraint string.
    """
    return "&".join(sorted(set(features)))


def arms_with_features(features: list[str]) -> list[str]:
    """Every ADMITTED Arm whose declared Feature set is a superset of `features` — the feasibility
    check a submission runs before ever calling sbatch, mirroring `vram_constraint`'s "no Arm has
    that much VRAM" refusal."""
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
    """Every live Dive — PENDING and RUNNING — and the Karma tick's whole view of Slurm. One poll per
    pass, because the tick needs both halves and they must agree with each other:

    * **PENDING** Dives are what it re-ranks (by `nice`) and what generates queued **Demand**.
    * **RUNNING** Dives are what it meters — `node` says which Arm's Demand to bill, `devices` says
      which of that Arm's cards it holds (and so which **Power**s it is priced at), and `start_time`
      is where the very first accrual bills *from* (a Dive that started between two ticks still owes
      for the seconds before the tick noticed).

    Absence from a *successful* poll is the only signal a Dive has **ended** — it triggers Settlement.
    There is no other: a killed Dive writes no end-time, Slurm forgets jobs after `MinJobAge`, and
    `sacct` would need the slurmdbd we deliberately don't run. If slurmctld is down `_slurm_json` raises,
    the tick logs and skips, and nothing settles — which is the point.

    `nice` is read back rather than remembered: the one piece of state the tick shares with an operator.
    A **negative** nice is a Queue bump — only an operator can set one (Slurm refuses a negative nice
    from a job's own owner), so its mere presence is the flag. No bump table, no coordination state.

    `user` is the Dive's **Guise**, riding along because it's who the tick has to become to re-nice the
    job — taken from this same poll rather than looked up per job, to avoid a `squeue` subprocess per
    pending Dive per pass.
    """
    # This poll is the whole truth again. There used to be one thing it could not see — a Dive
    # `sbatch --hold`ed while the Head built its **Environment**, reported by squeue as plain PENDING —
    # which is why the Head kept a `build_state` of its own. The Arm builds its Environment inside its
    # own allocation now, so a building Dive is a RUNNING one and Slurm knows it.
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
            "user": j.get("user_name"),
            "node": (j.get("nodes") or None) if live == "RUNNING" else None,
            "gpus": _gpu_count(gres),
            # Empty until the Dive starts — a PENDING Dive holds no Device, which is exactly why the
            # Escrow is a forecast over eligible cards and the Meter is not.
            "devices": _gpu_indices(j.get("gres_detail")),
            "start_time": _num(j.get("start_time")),
        })
    return out


def owner_of(job_id: int) -> str | None:
    """Which account Slurm says owns this Dive, or None if it no longer knows the job.

    Ground truth for `guises.as_owner`, deliberately squeue rather than the Attribution row: it's what
    `sudo -u` has to MATCH. Also naturally covers the enrollment benchmark (no row at all) and an
    already-ended job (None, and the command below is a no-op anyway).

    One extra squeue per call, which is why the tick does NOT use it: `queue()` already carries `user`
    for every live Dive. This is for one-shot paths (cancel, signal) where the caller has only an id.
    """
    r = subprocess.run(["squeue", "-h", "-j", str(job_id), "-o", "%u"],
                       capture_output=True, text=True, timeout=15)
    return r.stdout.strip() or None


def set_nice(job_id: int, nice: int, owner: str | None = None) -> None:
    """Lower a queued Dive's priority — as the **Guise** that owns it, which is what makes this need
    no privilege beyond `sudo -u` (a job's owner may always nice their own job; only a NEGATIVE nice, a
    boost, needs a Slurm operator, which is exactly what keeps the admin Queue bump unforgeable).

    `owner` comes from the caller because it already has it: the Karma tick re-nices from the same
    `queue()` poll that reported who owns each job. None means the Mantle — a Dive from before Guises,
    run plain exactly as it always was."""
    subprocess.run(guises.as_owner(["scontrol", "update", f"JobId={job_id}", f"Nice={nice}"], owner),
                   capture_output=True, text=True, timeout=15, check=True)


def state(job_id: int) -> str | None:
    """Slurm state if the job is still known to squeue, else None. Slurm's answer, unedited."""
    r = subprocess.run(["squeue", "-h", "-j", str(job_id), "-o", "%T"],
                       capture_output=True, text=True)
    return r.stdout.strip() or None


def dives() -> list[dict]:
    """The live Dive queue — pending/running jobs in the CCC partition. Finished ones are dropped here
    (LIVE_JOB_STATES) rather than left to squeue's own MinJobAge aging-out, which lags well behind a
    Dive actually ending; durable history waits for Phase 3 slurmdbd (see module docstring)."""
    out = []
    for j in _slurm_json("squeue", "--json").get("jobs", []):
        if j.get("partition") != PARTITION:
            continue
        if not LIVE_JOB_STATES.intersection(j.get("job_state") or []):
            continue  # squeue hasn't aged it out yet, but it's already done — history's job now
        # gres_detail when running (names the exact cards); tres_per_node ('gres/gpu:1') otherwise —
        # slurmctld only packs gres_detail for a started job, so a pending Dive falls through to what
        # it asked for. See _gpu_indices.
        gres = ",".join(j.get("gres_detail") or []) or j.get("tres_per_node", "")
        state = "+".join(s.lower() for s in j.get("job_state", []))
        out.append({
            "id": _num(j.get("job_id")),
            "name": j.get("name"),
            "state": state,
            "node": j.get("nodes") or None,
            "gpus": _gpu_count(gres),
            "user": j.get("user_name"),
            # The declared duration (`--time`), in minutes — what a waiting Dive is priced against and
            # the one declared need squeue carries cleanly. `--vram` becomes a Slurm *constraint*
            # string, which is not worth parsing back out here.
            "time_limit": _num(j.get("time_limit")),
            "submit_time": _num(j.get("submit_time")),
            "start_time": _num(j.get("start_time")),
        })
    return sorted(out, key=lambda s: s["id"] or 0)


def cancel(job_id: int) -> None:
    """End a live Dive now, as the **Guise** that owns it — `scancel` from a job's own owner needs no
    privilege, which is why this is a `sudo -u` and not a root grant.

    Settlement is NOT done here: the Karma tick settles a Dive on its *disappearance from squeue*
    (slurm.queue()), which is exactly what this causes, so a cancel bills for the seconds actually held
    with no second code path. Idempotent — a job squeue no longer lists has already ended, so there
    is nothing to cancel (scancel as anyone but its owner is refused while Slurm still remembers it)."""
    if (owner := owner_of(job_id)) is None:
        return
    subprocess.run(guises.as_owner(["scancel", str(job_id)], owner),
                   capture_output=True, text=True, timeout=15, check=True)


def signal(job_id: int, sig: str) -> None:
    """Send a signal to a live Dive, without ending it the way `cancel` does. As its owner, for the
    same reason. Used by a Retract's initial nudge (TERM, so the job's own EXIT trap can stage out what
    it can) and by the Karma tick's force-kill once its grace period has passed (KILL)."""
    if (owner := owner_of(job_id)) is None:              # already ended: nothing to signal
        return
    subprocess.run(guises.as_owner(["scancel", f"--signal={sig}", str(job_id)], owner),
                   capture_output=True, text=True, timeout=15, check=True)
