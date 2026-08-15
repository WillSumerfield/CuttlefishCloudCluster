"""Recipes: a User's shelf of frozen, re-runnable snapshots, bounded by a **32GB** per-User wall.

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

**A Recipe freezes a declaration, not a built tree.** It keeps the code, the manifest it names and the
Features it asks for, and every Spawn from it builds that declaration fresh on whichever Arm runs it.
A Recipe briefly kept the **Environment** the Head had built for it; there is no Head-built env any
more, so there is nothing to keep. What makes a re-run reproducible is the pinned declaration plus the
Arm's own package cache, which turns the rebuild into hard links (`ccc_head_api.environments`).
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
#
# 32GB. Raised from 24GB when a Recipe also kept its built **Environment**, and deliberately LEFT there
# now that it does not: a Recipe holds only what its owner uploaded again, so this is headroom rather
# than necessity, and taking an allowance back from people who have already filled it is not a change
# worth making for tidiness.
MAX_USER_BYTES = int(os.environ.get("CCC_RECIPE_MAX_USER", 32 * 1024**3))

# A Recipe's name is a directory component and an URL path segment, so it is checked rather than escaped.
# Same shape as one segment of a declared output path (spawns.OUTPUT_SEG_RE): no '..', no leading dot.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class WallExceeded(Exception):
    """The 32GB wall refused a save. Carried to the HTTP boundary as 413 with these numbers in it —
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


def weigh(tar_path: Path, script: str | None, manifest: str | None = None) -> int:
    """Uncompressed bytes in the upload, plus the two is-it-really-in-there checks, in one pass.

    All three happen before anything is written, so a save the wall refuses — or one naming an entrypoint
    or a manifest the tar does not contain — leaves nothing behind at all. Both checks mirror
    `spawns.submit_checked`: a Recipe frozen around a missing file is one that fails every time it is ever
    run, discovered long after the folder that would have fixed it is gone. The manifest matters as much
    as the script here, because a Recipe freezes its **Environment** and not merely its code."""
    with tarfile.open(tar_path, mode="r:gz") as tf:
        members = tf.getmembers()
        names = [m.name for m in members]
        for label, rel in (("script", script), ("manifest", manifest)):
            if rel is not None and rel not in names:
                raise ValueError(f"{label} {rel!r} was not found in the uploaded project — check the "
                                 f"path matches exactly (and isn't excluded by .gitignore)")
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
    # refuses it), better caught at save time than the day it's needed. The same `output_plan` a submit
    # runs, so a Recipe cannot freeze a declaration that would be refused the day it is used.
    from .spawns import output_plan
    outputs = meta.get("outputs") or []
    if not isinstance(outputs, list) or not outputs:
        raise ValueError("missing 'outputs' in X-CCC-Meta — at least one output folder name is required "
                         "(a Recipe with none could never be run)")
    output_plan(outputs)
    # `features` is frozen structurally only, exactly like `vram_mb`/`gpus` above — never checked against
    # the fleet's current capability here. That check is `spawns.submit_checked`'s, and it runs for real
    # when the Recipe is actually run, not months earlier when it happened to be saved.
    features = meta.get("features") or []
    if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
        raise ValueError("'features' in X-CCC-Meta must be a list of strings")
    # Required, like `script` and unlike `features`: a Recipe captures its **Environment** as well as its
    # code (CONTEXT.md, **Recipe**), and every Spawn names the file it declares. A Recipe saved without
    # one is a Recipe that could never be run — the same defect `outputs` is checked for above, caught
    # here rather than the day it is needed. `weigh` checks the named file is actually in the upload.
    manifest = meta.get("manifest") or ""
    if not manifest:
        raise ValueError("missing 'manifest' in X-CCC-Meta — a Recipe freezes the environment its work "
                         "runs in, not just the code, so it must name the dependency file to build "
                         "(e.g. pyproject.toml, uv.lock, requirements.txt, environment.yml)")

    old = db.get_recipe(conn, user_id, replacing) if replacing else None
    if replacing and old is None:
        raise ValueError(f"no recipe named {replacing!r}")
    if not replacing and db.get_recipe(conn, user_id, name) is not None:
        raise ValueError(f"you already have a recipe named {name!r} — delete it or update it instead")

    size = weigh(tar_path, script, manifest)
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
                        max(1, int(meta.get("gpus", 1))), int(meta["vram_mb"]), int(meta["minutes"]),
                        bool(meta.get("wandb", True)), meta.get("wandb_project") or "",
                        meta.get("wandb_entity") or "", size, manifest, features)
    return to_json(row)


def update_meta(conn, user_id: int, name: str, meta: dict) -> dict:
    """Change a Recipe's settings in place — no code moves, no upload, no wall arithmetic (`bytes` and
    `frozen_at` are untouched). The Dashboard's "Edit" without a folder dropped; dropping one there goes
    through `save(..., replacing=)` instead, which DOES re-freeze the code.

    Same field validation as `save`, minus everything about the tar: `script` is checked against the code
    already extracted on disk (`_dir`) instead of a fresh upload's members, since there isn't one here.
    Raises ValueError for a bad name/entrypoint/no-such-recipe (422/404 at the boundary, same split as
    `save`'s callers already make)."""
    old = db.get_recipe(conn, user_id, name)
    if old is None:
        raise ValueError(f"no recipe named {name!r}")

    script = meta.get("script") or ""
    if not script:
        raise ValueError("missing 'script' in the request — a Recipe freezes an entrypoint, not just code")
    if not (_dir(user_id, name) / script).is_file():
        raise ValueError(f"script {script!r} was not found in this recipe's saved code — drop a folder "
                         f"instead if you meant to point it at a different one")
    for field in ("minutes", "vram_mb"):
        if not meta.get(field):
            raise ValueError(f"missing '{field}' — a Recipe carries the same declared need and length a "
                             f"Spawn does")
    from .spawns import output_plan
    outputs = meta.get("outputs") or []
    if not isinstance(outputs, list) or not outputs:
        raise ValueError("missing 'outputs' — at least one output folder is required (a Recipe with "
                         "none could never be run)")
    output_plan(outputs)
    features = meta.get("features") or []
    if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
        raise ValueError("'features' must be a list of strings")
    # Checked against the code already on disk, exactly as `script` is above — there is no upload here to
    # look inside, and a settings edit must not be able to point a Recipe at a manifest it has not got.
    manifest = meta.get("manifest") or ""
    if not manifest:
        raise ValueError("missing 'manifest' — a Recipe freezes the environment its work runs in, not "
                         "just the code")
    if not (_dir(user_id, name) / manifest).is_file():
        raise ValueError(f"manifest {manifest!r} was not found in this recipe's saved code — drop a "
                         f"folder instead if you meant to point it at a different one")

    row = db.update_recipe_meta(conn, user_id, name, script, meta.get("args") or [], outputs,
                                max(1, int(meta.get("gpus", 1))), int(meta["vram_mb"]),
                                int(meta["minutes"]),
                                bool(meta.get("wandb", True)), meta.get("wandb_project") or "",
                                meta.get("wandb_entity") or "", manifest, features)
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


def run(conn, user_id: int, name: str, is_admin: bool) -> int:
    """Run the frozen snapshot as a new Spawn. Returns the Spawn id.

    Every setting the Spawn needs was already answered when the Recipe was saved (CONTEXT.md — a re-run
    doesn't re-answer it), so this takes nothing from the caller but the name. `spawns.submit()` gets a
    COPY of the Recipe's own directory (`source_dir`), never the directory itself: that Spawn's staging
    is disposable and deleted after stage-in, while the Recipe must survive to be run again.

    Raises ValueError for no-such-recipe (a 404 at the boundary) or anything
    `spawns.submit_checked` itself refuses (422) — a Recipe never names an Arm, so that particular
    failure can't happen here, but the concurrent-Spawn cap and an unsatisfiable feature request still
    can."""
    from . import spawns

    row = db.get_recipe(conn, user_id, name)
    if row is None:
        raise ValueError(f"no recipe named {name!r}")

    # Head owns the key (spawns.compose_wandb_env reads it from storage); only the run's identity is the
    # Recipe's to carry, the same two fields the composer sends for an ad-hoc Spawn. Omitted rather than
    # sent empty for the same reason api.ts's submit() omits them — WANDB_ENTITY="" breaks wandb.init()
    # instead of falling back to the account default.
    wandb_env: dict[str, str] = {}
    if row["wandb_project"]:
        wandb_env["WANDB_PROJECT"] = row["wandb_project"]
    if row["wandb_entity"]:
        wandb_env["WANDB_ENTITY"] = row["wandb_entity"]

    return spawns.submit_checked(
        user_id, is_admin, row["script"], max(1, row["gpus"]), json.loads(row["args"]), wandb_env,
        None, row["minutes"], row["vram_mb"] // 1024, json.loads(row["outputs"]),
        wandb=bool(row["wandb"]), features=json.loads(row["features"]), manifest=row["manifest"],
        # The Recipe's own name is the Spawn's **Name**, INHERITED rather than asked for again: it was
        # chosen by a person when the Recipe was saved, so the rule that a human names every Spawn is
        # already satisfied and there is nothing left to prompt for. Running one Recipe ten times gives
        # ten rows reading the same thing, which is exactly what re-running one piece of work looks like.
        source_dir=_dir(user_id, name), recipe_name=name, name=name,
    )


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


def to_json(row, running: bool = False) -> dict:
    """The wire shape `api.ts`'s `Recipe` type already describes.

    `frozen_at` is epoch **ms**, what the browser renders staleness from. `updating` is always false
    here, not a stub: a save is synchronous, never mid-upload by GET time. `running` is the caller's to
    know — it needs the live Slurm queue (`db.running_recipe_names`), which a single row has no way to
    ask for itself; defaults false for callers (save/update) where the Recipe cannot possibly be running
    yet, since it was just written."""
    from datetime import datetime, timezone
    frozen = datetime.strptime(row["frozen_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return {
        "name": row["name"],
        "script": row["script"],
        "args": json.loads(row["args"]),
        "outputs": json.loads(row["outputs"]),
        # Clamped, not trusted: a Recipe frozen before CCC required a Device could carry 0, and every
        # surface that reads one now assumes at least 1. Silent — the Recipe still runs, on one card.
        "gpus": max(1, row["gpus"]),
        "vram_mb": row["vram_mb"],
        "minutes": row["minutes"],
        "size_bytes": row["bytes"],
        "frozen_at": int(frozen.timestamp() * 1000),
        "updating": False,
        "running": running,
        # Sent back, not just stored: the update form prefills from this, so re-saving never silently
        # resets W&B config to defaults (CONTEXT.md — a Recipe holds it).
        "wandb": bool(row["wandb"]),
        "wandb_project": row["wandb_project"],
        "wandb_entity": row["wandb_entity"],
        # Frozen structurally only — see `save`'s note on why neither is checked against the fleet here.
        "manifest": row["manifest"],
        "features": json.loads(row["features"]),
    }
