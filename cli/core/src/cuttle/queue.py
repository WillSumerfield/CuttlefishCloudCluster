"""The cluster's queue and history: `cuttle queue` / `cuttle history` — every User's Dives (Slurm job
id = Dive id). A thin API client; a Dive itself is dive.py.
"""

from . import auth, ui


def queue(as_json: bool = False, limit: int = 0) -> None:
    """The live queue — every pending/running Dive on the cluster, and who started it.

    Everyone's, like the Dashboard's landing table: what is running ahead of yours is exactly what you
    want to see. `WHO` comes from the Head's records (Slurm names a Guise); a `-` is a job the Head has
    no row for (the enrollment benchmark). `limit` (0 = all) caps to the front of the queue."""
    rows = auth._api("GET", f"/dives?limit={limit}" if limit else "/dives")

    def render():
        if not rows:
            ui.say("the queue is empty")
            return
        ui.table(
            ["ID", "STATE", "WHO", "MAIN", "NODE", "GPU", "MIN"],
            [[r["id"], r["state"], r.get("started_by") or "-",
              f"~/{r['main']}" if r.get("main") else "-",
              r.get("node") or "-", r.get("gpus"), r.get("time_limit")] for r in rows])
        if limit and len(rows) >= limit:
            ui.hint(f"showing {limit}; use --limit 0 for all")

    ui.emit(rows, as_json, render)


def _short(ts: str | None) -> str:
    """An ISO timestamp trimmed to the minute, for a table cell — or a dash if there is none yet."""
    return ts[:16].replace("T", " ") if ts else "-"


def history(days: int = 30, as_json: bool = False, limit: int = 0) -> None:
    """Past Dives on the cluster — everything the live queue has forgotten — newest first. Everyone's,
    like `queue`. Each row shows who started it, what it asked for, what it settled at, and how it
    ended (`—` = left the queue seconds ago, not stamped yet). `limit` (0 = all) caps to the newest N."""
    q = f"/dives/history?days={days}"
    if limit:
        q += f"&limit={limit}"
    rows = auth._api("GET", q)

    def render():
        if not rows:
            ui.say(f"no Dives in the last {days} days")
            return
        ui.table(
            ["STARTED", "FINISHED", "ID", "MAIN", "WHO", "GPU", "MIN", "COST", "OUTCOME"],
            [[_short(r["submitted_at"]), _short(r.get("finished_at")), r["job_id"],
              f"~/{r['main']}" if r.get("main") else "-",
              r.get("started_by") or "-", r.get("gpus"), r.get("minutes"),
              f"{r['cost']:.1f}" if r.get("cost") is not None else "—", r.get("outcome") or "—"]
             for r in rows])
        if limit and len(rows) >= limit:
            ui.hint(f"showing {limit}; use --limit 0 for all")

    ui.emit(rows, as_json, render)


def arms(as_json: bool = False) -> None:
    """The Arms as a User sees them, off the Head: which cards exist and whether they're free — what
    sizing a Dive needs. The Head's own `arm list` is the operator's view (enrollment, rollout)."""
    rows = auth._api("GET", "/arms") or []

    def render():
        if not rows:
            ui.say("no Arms")
            return
        ui.table(
            ["NAME", "GPU", "VRAM", "POWER", "STATE", "OWNER"],
            [[a["name"], f"{a['gpus']}x {a.get('gpu') or '?'}" if (a.get("gpus") or 0) > 1 else a.get("gpu") or "-",
              f"{round(a['vram_mb'] / 1024)}G" if a.get("vram_mb") else "-",
              f"{a['power']:.0f} TF" if a.get("power") else "-",
              "retracted" if a.get("retracted") else a["state"], a.get("owner") or "-"] for a in rows])

    ui.emit(rows, as_json, render)
