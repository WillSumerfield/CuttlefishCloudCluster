"""Recipes: a User's shelf of frozen, re-runnable snapshots, bounded by a **24GB** per-User wall.

A Recipe is kept because its owner deliberately asked for it — unlike an upload (deleted at stage-in) or
an output (TTL'd against an Allowance), nothing here has a tick, a TTL, or eviction. Only a wall.

**A hard wall, never a silent eviction** (CONTEXT.md, **Allowance**): a save that doesn't fit fails
loudly and the User deletes something to make room — the opposite of `retention.py`'s "always accept,
evict silently" (there the Karma's already spent; here the save itself is the deliberate act).

**Saving runs nothing** — a Recipe is uploaded to, not submitted, so a full shelf can never block a User
from working. No promoting a finished Spawn into a Recipe either: `projects.sweep` deletes the ad-hoc
upload seconds after stage-in, so there's nothing left to promote.

**Update is delete-then-save**: the old snapshot is released before the new one is charged, so growing a
Recipe costs only the delta. The wall is weighed before a byte is written, so a refused update leaves the
old version untouched.

**Bytes are counted uncompressed**, from the tar's own member sizes, not the extracted tree — the same
number the browser's `File.size` check already used, so the Head never refuses a save the composer
promised would fit, or accepts one it had blocked.
"""

import json
import os
import re
import shutil
import tarfile
from pathlib import Path

from . import db

# Per-User. Also stated in the frontend (`api.ts` RECIPE_ALLOWANCE_BYTES) so the composer can block a
# doomed save before spending the upload — but only the Head's number is trusted.
MAX_USER_BYTES = int(os.environ.get("CCC_RECIPE_MAX_USER", 24 * 1024**3))

# A Recipe's name is a directory component and an URL path segment, so it is checked rather than escaped.
# Same shape as spawns.OUTPUT_NAME_RE: no slashes, no '..', no leading dot.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class WallExceeded(Exception):
    """The 24GB wall refused a save. Carried to the HTTP boundary as 413 with these numbers in it —
    a wall a User cannot see the far side of is one they cannot act on."""

    def __init__(self, used: int, incoming: int) -> None:
        self.used, self.incoming = used, incoming
        super().__init__(f"{incoming} bytes over an allowance with {MAX_USER_BYTES - used} free")


def _root() -> Path:
    # Lazy import, like retention._outputs_dir: avoids dragging the whole submit path (slurm, siphons,
    # tidepool) into a module that only writes files.
    from . import spawns
    return spawns.HEAD_STATE_DIR / "recipes"


def _dir(user_id: int, name: str) -> Path:
    return _root() / str(user_id) / name


def weigh(tar_path: Path, script: str | None) -> int:
    """Uncompressed bytes in the upload, and the `script`-is-really-in-there check, in one pass.

    Both happen before anything is written, so a save the wall refuses — or one naming an entrypoint the
    tar does not contain — leaves nothing behind at all. The script check mirrors `spawns.submit_checked`:
    a Recipe frozen around a missing entrypoint is one that fails every time it is ever run, discovered
    long after the folder that would have fixed it is gone."""
    with tarfile.open(tar_path, mode="r:gz") as tf:
        members = tf.getmembers()
        if script is not None and script not in [m.name for m in members]:
            raise ValueError(f"script {script!r} was not found in the uploaded project — check the path "
                             f"matches exactly (and isn't excluded by .gitignore)")
        return sum(m.size for m in members if m.isfile())


def save(conn, user_id: int, meta: dict, tar_path: Path, replacing: str | None = None) -> dict:
    """Freeze an upload into a Recipe, or re-freeze an existing one. Returns the saved Recipe.

    `replacing` names the Recipe being updated — the delete-then-save path. Its bytes are released from
    the arithmetic BEFORE the incoming ones are weighed (so an update is charged the delta, not the peak)
    but its files are not touched until the wall has said yes, so a refusal costs the owner nothing.

    Raises ValueError for a bad name/entrypoint (422) and WallExceeded for a full shelf (413)."""
    name = meta.get("name") or ""
    if not NAME_RE.match(name):
        raise ValueError(f"invalid recipe name {name!r} — letters/digits/'.'/'_'/'-' only, must start "
                         f"with a letter or digit, 64 characters or fewer")
    script = meta.get("script") or ""
    if not script:
        raise ValueError("missing 'script' in X-CCC-Meta — a Recipe freezes an entrypoint, not just code")
    for field in ("minutes", "vram_mb"):
        if not meta.get(field):
            raise ValueError(f"missing '{field}' in X-CCC-Meta — a Recipe carries the same declared "
                             f"need and length a Spawn does, frozen with the code")
    # Declared outputs, required and validated HERE: a Recipe with none could never be run (`/submit`
    # refuses it), better caught at save time than the day it's needed. Same regex as spawns.submit_checked.
    from .spawns import OUTPUT_NAME_RE
    outputs = meta.get("outputs") or []
    if not isinstance(outputs, list) or not outputs:
        raise ValueError("missing 'outputs' in X-CCC-Meta — at least one output folder name is required "
                         "(a Recipe with none could never be run)")
    for out in outputs:
        if not isinstance(out, str) or not OUTPUT_NAME_RE.match(out):
            raise ValueError(f"invalid output name {out!r} — letters/digits/'.'/'_'/'-' only, may not "
                             f"start with '.' (no slashes, no '..')")

    old = db.get_recipe(conn, user_id, replacing) if replacing else None
    if replacing and old is None:
        raise ValueError(f"no recipe named {replacing!r}")
    if not replacing and db.get_recipe(conn, user_id, name) is not None:
        raise ValueError(f"you already have a recipe named {name!r} — delete it or update it instead")

    size = weigh(tar_path, script)
    # Delete-then-save arithmetic: the old snapshot is already released here, so an update is weighed on
    # what it GROWS BY. Nothing on disk has moved yet.
    used = db.user_recipe_bytes(conn, user_id) - (old["bytes"] if old else 0)
    if used + size > MAX_USER_BYTES:
        raise WallExceeded(used, size)

    # Past the wall, so now the old version can go. A rename would leave the update non-atomic in a
    # different way (two trees, one row) without buying anything: the row is rewritten either way.
    if old is not None:
        _evict(conn, user_id, replacing)
    dest = _dir(user_id, name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, mode="r:gz") as tf:
        dest.mkdir()
        tf.extractall(dest, filter="data")  # 'data' filter blocks path-traversal entries
    row = db.add_recipe(conn, user_id, name, script, meta.get("args") or [], outputs,
                        int(meta.get("gpus", 1)), int(meta["vram_mb"]), int(meta["minutes"]),
                        bool(meta.get("wandb", True)), meta.get("wandb_project") or "",
                        meta.get("wandb_entity") or "", size)
    return to_json(row)


def _evict(conn, user_id: int, name: str) -> None:
    """Drop one Recipe, on disk and in the ledger, in that order — a row without a tree is a Recipe that
    fails when run, a tree without a row is bytes nobody is charged for and `sweep_orphans` cannot see
    (it only knows about deleted *Users*). The first is worse."""
    shutil.rmtree(_dir(user_id, name), ignore_errors=True)
    db.delete_recipe(conn, user_id, name)


def delete(conn, user_id: int, name: str) -> bool:
    """A User deleting their own Recipe — the only way bytes ever leave a shelf. False if there is no
    such Recipe (a 404 at the boundary)."""
    if db.get_recipe(conn, user_id, name) is None:
        return False
    _evict(conn, user_id, name)
    return True


def sweep_orphans(conn) -> int:
    """Delete the shelves of Users who no longer exist. Returns how many.

    `recipes.user_id` CASCADEs, so a deleted User's *rows* are already gone — but nothing in SQLite can
    reach the files, and a Recipe has no TTL to eventually collect them the way an expired output has.
    Without this a deleted account's code would sit on the Head forever, charged to nobody. Directory
    names are User ids, so "is this a live User?" is the whole test; a non-numeric entry is left alone
    rather than guessed at."""
    live = db.recipe_owner_ids(conn)
    swept = 0
    try:
        entries = sorted(_root().iterdir())
    except OSError:
        return 0  # nobody has ever saved one here
    for entry in entries:
        if entry.name.isdigit() and int(entry.name) not in live:
            shutil.rmtree(entry, ignore_errors=True)
            swept += 1
    return swept


def to_json(row) -> dict:
    """The wire shape `api.ts`'s `Recipe` type already describes.

    `frozen_at` is epoch **ms**, what the browser renders staleness from. `updating`/`running` are always
    false here, not a stub: a save is synchronous (never mid-upload by GET time), and nothing can yet
    spawn from a Recipe (no `POST /recipes/{name}/run` yet), so nothing can yet be running one."""
    from datetime import datetime, timezone
    frozen = datetime.strptime(row["frozen_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return {
        "name": row["name"],
        "script": row["script"],
        "args": json.loads(row["args"]),
        "outputs": json.loads(row["outputs"]),
        "gpus": row["gpus"],
        "vram_mb": row["vram_mb"],
        "minutes": row["minutes"],
        "size_bytes": row["bytes"],
        "frozen_at": int(frozen.timestamp() * 1000),
        "updating": False,
        "running": False,
        # Sent back, not just stored: the update form prefills from this, so re-saving never silently
        # resets W&B config to defaults (CONTEXT.md — a Recipe holds it).
        "wandb": bool(row["wandb"]),
        "wandb_project": row["wandb_project"],
        "wandb_entity": row["wandb_entity"],
    }
