"""Workspaces: `cuttle workspace new/list/update/open/download/delete`.

A **Workspace** is a durable working directory held on the Head — the **Tide Pool**'s persistent form.
Opening one resumes where the last visit left off, because the session's end mirrors the tree home
instead of throwing it away. This module is the CLI half of the Dashboard's Workspaces page, at the
operation **parity** CONTEXT.md asks of the two clients.

The seed upload reuses `cuttle run`'s exactly (same gitignore-aware file set, gzip-tar, `X-CCC-Meta`
header — see `spawns._make_tar`), and it is the ONLY upload a Workspace ever takes: everything after it
is written from inside the Workspace's own sessions.

**Opening one costs Karma; holding one does not.** A visit is a Tide Pool and is billed like any other
Spawn. The bytes are free — storage never costs Karma — but they are bounded: each Workspace reserves a
**Storage** out of a 32GB per-User Allowance when it is created, and the User divides that up themselves.

**The pool is the Head's**, like the Recipe wall: nothing here pre-checks it, and the Head's refusal
carries the numbers and the remedy — printed verbatim.
"""

import threading
import time
from pathlib import Path

from . import api, auth, spawns, tidepool as tidepool_mod, ui, watch

GIB = 1024 ** 3

# How often a live session re-asks the Head whether the mirror is refusing to sync. The mirror itself
# runs every 60s, so anything faster only asks the same question twice.
_FULL_POLL_S = 60
# ...and how long between repeats of the warning once it IS full. Frequent enough that a User who
# stepped away sees it, rare enough that it does not shout over the shell they are working in.
_FULL_REPEAT_S = 600


def _client():
    client = auth._client()
    client.token = auth._load_token()
    return client


def _list() -> list[dict]:
    return auth._api("GET", "/workspaces")


def _find(name: str) -> dict:
    """One Workspace by name, or abort. Same shape as `recipes._find`: the shelf is small and private,
    so the list IS the lookup and one call answers both "does it exist" and "what does it hold"."""
    for w in _list():
        if w["name"] == name:
            return w
    raise ui.abort(f"you have no workspace named {name!r} — `cuttle workspace list` shows yours")


def _meta(name: str, storage_gb: int, vram: int, minutes: int, gpu: int, manifest: str | None,
          outputs: list[str], features: list[str]) -> dict:
    # vram_mb / storage_bytes, not the GB the flags take: the Head stores the declared need the way a
    # Recipe's schema does, and the client converts once, here.
    return {"name": name, "storage_bytes": storage_gb * GIB, "vram_mb": vram * 1024,
            "minutes": minutes, "gpus": gpu, "manifest": manifest or "",
            "outputs": outputs, "features": features}


def new(name: str, storage_gb: int, vram: int, minutes: int, gpu: int, manifest: str | None,
        outputs: list[str], features: list[str], root: Path, no_gitignore: bool = False,
        yes: bool = False) -> None:
    """Seed a directory into a new Workspace."""
    if manifest and not (root / manifest).is_file():
        raise ui.abort(f"manifest {manifest!r} was not found (looked for it relative to the project "
                       f"directory, {root})")
    files = None if no_gitignore else spawns._project_files(root)
    paths = [root / f for f in files] if files is not None else [p for p in root.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in paths if p.exists())
    tar_bytes = spawns._make_tar(root, files)
    if not yes:
        ui.kv([
            ("workspace", name),
            ("directory", str(root)),
            ("storage", f"{storage_gb} GB"),
            ("environment", manifest or "—"),
            ("seed", f"{len(paths)} files, {total / 1_048_576:.1f} MB"
                     f"{'' if no_gitignore else '  (.gitignore honored)'}"),
            ("visits", f"{minutes} min, {vram} GB VRAM, {gpu} GPU{'s' if gpu != 1 else ''}"),
            ("outputs", ", ".join(outputs) or "—"),
        ], title="About to create")
        # Said before the bytes move, because this is the one thing a User uploads that they then write
        # back into: after this, the Workspace's contents are whatever their own sessions leave there.
        ui.hint(f"this is the only upload — from here it changes from inside its own sessions. "
                f"{storage_gb} GB comes out of your 32 GB workspace allowance until you delete it")
        if not ui.ask_confirm("Create?", True):
            raise ui.abort("not created")
    try:
        made = _client().upload("/workspaces", tar_bytes,
                                _meta(name, storage_gb, vram, minutes, gpu, manifest, outputs, features))
    except api.ApiError as e:
        if ui.DEBUG:
            raise
        raise ui.abort(f"could not create workspace {name!r}: {e.user_message()}") from None
    ui.ok(f"created workspace {made['name']} ({_human(made['used_bytes'])} of "
          f"{_human(made['storage_bytes'])})")
    ui.data(made["name"])


def update(name: str, storage_gb: int | None = None, vram: int | None = None, minutes: int | None = None,
           gpu: int | None = None, manifest: str | None = None, outputs: list[str] | None = None,
           features: list[str] | None = None, yes: bool = False) -> None:
    """Change what a visit asks for, and the **Storage** the tree may fill. Never the files.

    Every setting defaults to what the Workspace already holds, so `cuttle workspace update foo --time
    30` changes the limit and nothing else — the same shape `cuttle recipe update` uses, and for the
    same reason: re-typing every flag is a chance to silently change one.
    """
    current = _find(name)
    body = _meta(
        name,
        storage_gb if storage_gb is not None else current["storage_bytes"] // GIB,
        vram if vram is not None else current["vram_mb"] // 1024,
        minutes or current["minutes"],
        gpu if gpu is not None else current["gpus"],
        manifest if manifest is not None else current["manifest"],
        list(outputs) if outputs else current["outputs"],
        list(features) if features else current["features"],
    )
    if storage_gb is not None and not yes:
        # Only for a resize: every other field here takes effect on the next visit and is trivially
        # reversible, while shrinking is the one edit the Head can refuse outright.
        ui.hint(f"{name!r} currently holds {_human(current['used_bytes'])}; a workspace can never be "
                f"shrunk below what its tree already holds")
    try:
        saved = _client().json("PATCH", f"/workspaces/{name}", body)
    except api.ApiError as e:
        if ui.DEBUG:
            raise
        raise ui.abort(f"could not update workspace {name!r}: {e.user_message()}") from None
    ui.ok(f"updated workspace {saved['name']} ({_human(saved['used_bytes'])} of "
          f"{_human(saved['storage_bytes'])})")
    if current["live"]:
        # Said out loud rather than refused: the running session's Storage is a constant baked into its
        # job script, and moving it under a session mid-write is the one thing a resize must not do.
        ui.hint(f"spawn {current['live']} is visiting right now — these take effect on the next visit")


def open_(name: str, vram: int | None = None, minutes: int | None = None, gpu: int | None = None,
          manifest: str | None = None, outputs: list[str] | None = None,
          features: list[str] | None = None) -> None:
    """Start a visit (or attach to the live one) and drop into the shell.

    Reuses the Tide Pool attach path wholesale — a visit IS a Tide Pool, so placement, the ephemeral
    keypair and the relay all work exactly as they do for `cuttle run --tidepool`.
    """
    over = {k: v for k, v in (("vram_mb", vram * 1024 if vram else None), ("minutes", minutes),
                              ("gpus", gpu), ("manifest", manifest),
                              ("outputs", list(outputs) if outputs else None),
                              ("features", list(features) if features else None))
            if v is not None}
    try:
        resp = _client().json("POST", f"/workspaces/{name}/open", over)
    except api.ApiError as e:
        if ui.DEBUG:
            raise
        raise ui.abort(f"could not open workspace {name!r}: {e.user_message()}") from None
    spawn_id = resp["id"]
    if resp.get("attached"):
        # Not an error and not a warning: a Workspace holds one visit at a time by design, and this is
        # how a second terminal joins the session that is already running.
        ui.ok(f"workspace {name!r} is already open — attaching to spawn {spawn_id}")
    else:
        ui.ok(f"opening workspace {name!r} as spawn {spawn_id}")
    stop = threading.Event()
    _storage_warner(name, stop)
    # A visit's declared Outputs are shipped to the same `[out-…]` module as any Spawn's, "so it can be
    # fetched like any Spawn's results" (siphons.WS_BLOCK) — but nothing here ever asked for them, so
    # they sat on the Head until retention took them. Armed at attach and again at detach, exactly like
    # a Tide Pool: a Workspace is where an Output is most likely to be declared mid-session, since the
    # tree persists and the `--output` above overrides only THIS visit.
    tidepool_mod.arm(spawn_id)
    try:
        # The name, not just the id: every message the attach prints — what becomes of the files, how
        # to get back in, how the session ended — differs for a visit, and this is what tells it so.
        # It also owns the ending now. Saying "everything is saved" here as well would have collided
        # with the `--time` ending's "being mirrored home now" and reproduced, in two lines, exactly
        # the contradiction that made a timed-out visit read as a disaster.
        tidepool_mod._wait_and_attach(spawn_id, workspace=name)
    finally:
        stop.set()
    tidepool_mod.arm(spawn_id)
    watch.ensure_sweeper()


def _storage_warner(name: str, stop: threading.Event) -> threading.Thread:
    """Warn, inside the live session, when the mirror is refusing to sync.

    Same channel as the Tide Pool's `--time` warning (`tidepool._warn_thread`): a raw write to fd 2
    beside the ssh that owns the screen. Interleaved rather than framed, which is the accepted cost of
    saying anything at all inside a session someone else's process is drawing.

    This is the one thing a User must be told mid-session and cannot discover otherwise: over its
    **Storage** nothing is evicted and nothing fails loudly — the session simply stops being saved, and
    without this they would find out by losing the work at the end of it.
    """
    import os

    def _loop():
        warned_at = 0.0
        while not stop.wait(_FULL_POLL_S):
            try:
                st = auth._api("GET", f"/workspaces/{name}/full")
            except Exception:
                continue    # a hiccup asking is never a reason to interrupt a live shell
            if not st.get("full"):
                warned_at = 0.0
                continue
            if time.monotonic() - warned_at < _FULL_REPEAT_S:
                continue
            warned_at = time.monotonic()
            os.write(2, f"\r\n*** ccc: workspace {name!r} is FULL — it holds "
                        f"{_human(st['bytes'])} against a {_human(st['storage_bytes'])} storage, so "
                        f"nothing is being saved. Free space now, or this session's work is lost when "
                        f"it ends ***\r\n".encode())

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def download(name: str, subpath: str | None = None, dest: Path | None = None) -> None:
    """Fetch the tree (or one subtree) as a tar. Live or idle — a Workspace being visited is still
    readable, it is just a moment older than what the session sees.

    Streamed to disk rather than fetched into memory (`api.Client.download`): the Head streams it
    precisely because a **Storage** runs to 16GB and up, and buffering it on arrival would give that
    back.

    Kept as a tar, unlike a Spawn's results — a Workspace is a tree someone works in and keeps working
    in, so a download of one is a copy to put somewhere, not a set of results to open. The progress
    line counts bytes with no percentage: what a Workspace holds is only knowable by walking it, and
    that walk would delay the first byte for the sake of a number.
    """
    from urllib.parse import quote

    _find(name)  # so a typo says "you have no workspace named" rather than a bare 404
    stem = f"workspace-{name}" + (f"-{Path(subpath).name}" if subpath else "")
    path = f"/workspaces/{name}/download" + (f"?path={quote(subpath)}" if subpath else "")
    target = (Path(dest) if dest else Path.cwd()) / f"{stem}.tar.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ui.progress(f"downloading {name}") as update:
            written = _client().download(path, target, on_progress=update)
    except api.ApiError as e:
        if ui.DEBUG:
            raise
        raise ui.abort(e.user_message()) from None
    ui.ok(f"downloaded {target} ({_human(written)})")


def delete(name: str, yes: bool = False) -> None:
    """Delete a Workspace and free its **Storage**. The only thing that ever removes one.

    The name has to be typed back rather than answering y/N, and this is the one place in the CLI that
    asks for that: a Workspace is the only held thing with no expiry, no eviction and no second copy —
    unlike a Recipe, which can be re-saved from the tree you still have locally.
    """
    current = _find(name)
    if current["live"]:
        raise ui.abort(f"workspace {name!r} is being visited by spawn {current['live']} — end that "
                       f"session first (`cuttle cancel {current['live']}`)")
    if not yes:
        ui.warn(f"{name!r} holds {_human(current['used_bytes'])} and there is no other copy of it — "
                f"deleting is permanent")
        if ui.ask_text(f"Type the workspace's name to delete it:") != name:
            raise ui.abort("not deleted")
    try:
        _client().json("DELETE", f"/workspaces/{name}")
    except api.ApiError as e:
        if ui.DEBUG:
            raise
        raise ui.abort(e.user_message()) from None
    ui.ok(f"deleted workspace {name}")


def listing(as_json: bool = False) -> None:
    """Your Workspaces. Yours alone — a Workspace holds a User's actual files, so there is no roster
    view of them any more than there is for Recipes."""
    rows = _list()

    def render():
        if not rows:
            ui.say("no workspaces yet — `cuttle workspace new <name> --storage 16` makes this directory "
                   "into one that survives the session")
            return
        ui.table(
            ["NAME", "USED", "STORAGE", "GPU", "VRAM", "MIN", "LAST VISIT", "STATE"],
            [[w["name"], _human(w["used_bytes"]), _human(w["storage_bytes"]), w["gpus"],
              f"{w['vram_mb'] // 1024}G", w["minutes"], _ago(w["last_visited_at"]),
              # The Spawn id, not just "live": a User who cannot name the session cannot attach to it
              # or cancel it, which are the only two things they would want to do about one.
              f"live ({w['live']})" if w["live"] else ("starting" if w["starting"] else "idle")]
             for w in rows])
        reserved = sum(w["storage_bytes"] for w in rows)
        # Hardcoded like recipes' own label: only the Head's `workspaces.MAX_USER_BYTES` is the
        # authority and it is not on the wire, so this is a figure to keep in step, not a check.
        ui.hint(f"{_human(reserved)} of your 32.0 GiB workspace allowance is reserved")

    ui.emit(rows, as_json, render)


def _ago(ms: int | None) -> str:
    if not ms:
        return "never"
    secs = max(0, time.time() - ms / 1000)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


def _human(n: int) -> str:
    """Bytes → "4.0 GiB". Matches `recipes._human` exactly, so the two shelves read alike side by side.

    The Head's own `main._human` drops a trailing `.0` ("4 GiB"), so a refusal printed verbatim from it
    is a shade terser than a size printed here. Kept in step with the sibling CLI shelf rather than with
    the Head, because those are the two numbers a User sees next to each other.
    """
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"  # unreachable
