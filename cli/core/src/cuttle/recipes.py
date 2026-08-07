"""Recipes: `cuttle recipe list/save/update/delete/run`.

A **Recipe** is a frozen, re-runnable snapshot — the project code plus how to run it — held on the Head
against a 32GB per-User **Allowance**. This module is the CLI half of the Dashboard's Recipes page, at
the operation **parity** CONTEXT.md asks of the two clients.

The upload reuses `cuttle run`'s exactly (same gitignore-aware file set, gzip-tar, `X-CCC-Meta`
header — see `spawns._make_tar`), so a Recipe packs identically to a Spawn. Saving a Recipe **runs
nothing** though: no Spawn, no Escrow, no queue place — so no `--follow` and no id, a save answers
with a name.

**The wall is the Head's, not ours.** Nothing here pre-checks the 24GB: only the Head knows what the
User already holds, and a client that guessed would refuse saves the Head would take. The Head answers
413 with the numbers and the remedy in it, and that message is printed verbatim — see `_upload`.
"""

from pathlib import Path

from . import api, auth, settings, spawns, ui


def _client():
    client = auth._client()
    client.token = auth._load_token()
    return client


def _upload(path: str, tar_bytes: bytes, meta: dict, what: str) -> dict:
    """POST an upload and translate a refusal. The Head's `detail` reaches the user intact — for a 413
    it is the only message that says how far over they are and that the fix is deleting a Recipe, and
    replacing it with a guess of our own would lose exactly that."""
    try:
        return _client().upload(path, tar_bytes, meta)
    except api.ApiError as e:
        if ui.DEBUG:
            raise
        raise ui.abort(f"could not {what}: {e.user_message()}") from None


def _pack(no_gitignore: bool) -> tuple[bytes, int, int]:
    """The working dir as a gzip-tar, plus (files, bytes) for the preview. Same set `cuttle run` sends."""
    root = Path.cwd()
    files = None if no_gitignore else spawns._project_files(root)
    paths = [root / f for f in files] if files is not None else [p for p in root.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in paths if p.exists())
    return spawns._make_tar(root, files), len(paths), total


def _meta(name: str, script: str, args: list[str], minutes: int, vram: int, gpu: int,
          outputs: list[str], project: str | None, entity: str | None, manifest: str) -> dict:
    # vram_mb, not the `vram` (GB) a Spawn sends: a Recipe stores the declared need rather than handing
    # it straight to Slurm, and the Head's Recipe schema keeps it in MB.
    wandb_env = settings.wandb_env(project, entity)
    return {"name": name, "script": script, "args": args, "outputs": outputs, "gpus": gpu,
            "vram_mb": vram * 1024, "minutes": minutes, "manifest": manifest,
            "wandb": True, "wandb_project": wandb_env.get("WANDB_PROJECT", ""),
            "wandb_entity": wandb_env.get("WANDB_ENTITY", "")}


def _validate(script: str, outputs: list[str], manifest: str) -> None:
    """The gates `cuttle run` applies before uploading, applied here for the same reasons — and they
    matter MORE here. A Spawn with a missing script or manifest fails now; a Recipe with one fails
    every time it is ever run, discovered long after the folder that would have fixed it is gone."""
    if not outputs:
        raise ui.abort("at least one --output/-o is required — a recipe with none could never be run "
                       "(e.g. `--output checkpoints`)")
    if not manifest:
        raise ui.abort("--manifest is required — a Recipe freezes the environment its work runs in, "
                       "not just the code (e.g. `--manifest pyproject.toml`)")
    for label, rel in (("script", script), ("manifest", manifest)):
        if not (Path.cwd() / rel).is_file():
            raise ui.abort(f"{label} {rel!r} was not found (looked for it relative to the current "
                           f"directory, {Path.cwd()})")


def save(name: str, script: str, minutes: int, vram: int, gpu: int, args: list[str],
         outputs: list[str], manifest: str, project: str | None = None, entity: str | None = None,
         no_gitignore: bool = False, yes: bool = False) -> None:
    """Freeze the working dir into a new Recipe."""
    _validate(script, outputs, manifest)
    tar_bytes, count, total = _pack(no_gitignore)
    if not yes:
        ui.kv([
            ("recipe", name),
            ("script", script),
            # A Recipe freezes the **Environment** as well as the code, so this line is as much part of
            # "what am I about to freeze" as the script is.
            ("environment", manifest),
            ("upload", f"{count} files, {total / 1_048_576:.1f} MB"
                       f"{'' if no_gitignore else '  (.gitignore honored)'}"),
            ("limit", f"{minutes} min"),
            ("needs", f"{vram} GB VRAM, {gpu} GPU{'s' if gpu != 1 else ''}"),
            ("outputs", ", ".join(outputs)),
        ], title="About to save")
        # Said before the bytes move, because this is the one upload in CCC that is kept indefinitely:
        # a Spawn's project is deleted at stage-in, its results expire in 7 days, and a Recipe does
        # neither. Nothing on the Head ever deletes one for you.
        ui.hint("held on the Head until you delete it — counts against your 24GB recipe allowance")
        if not ui.ask_confirm("Save?", True):
            raise ui.abort("not saved")
    saved = _upload("/recipes", tar_bytes,
                    _meta(name, script, args, minutes, vram, gpu, outputs, project, entity, manifest),
                    f"save recipe {name!r}")
    ui.ok(f"saved recipe {saved['name']} ({_human(saved['size_bytes'])})")
    ui.data(saved["name"])


def update(name: str, script: str | None = None, minutes: int | None = None, vram: int | None = None,
           gpu: int | None = None, args: list[str] | None = None, outputs: list[str] | None = None,
           manifest: str | None = None, project: str | None = None, entity: str | None = None,
           no_gitignore: bool = False, yes: bool = False) -> None:
    """Re-capture the working dir into an existing Recipe.

    Every setting defaults to what the Recipe already holds, so `cuttle recipe update foo` in a changed
    directory refreshes the CODE and nothing else — the common case, and the one where re-typing
    --time/--vram/--output would be a chance to silently change them. Fetched first for exactly that.
    """
    current = _find(name)
    script = script or current["script"]
    manifest = manifest or current["manifest"]
    _validate(script, list(outputs) if outputs else current["outputs"], manifest)
    tar_bytes, count, total = _pack(no_gitignore)
    meta = _meta(name, script,
                 list(args) if args else current["args"],
                 minutes or current["minutes"],
                 vram if vram is not None else current["vram_mb"] // 1024,
                 gpu if gpu is not None else current["gpus"],
                 list(outputs) if outputs else current["outputs"],
                 project, entity, manifest)
    if not project and not entity:
        # Keep what the Recipe already froze rather than the local config's answer: an update is about
        # the code, and silently re-pointing someone's runs at a different W&B project is not.
        meta.update(wandb=current["wandb"], wandb_project=current["wandb_project"],
                    wandb_entity=current["wandb_entity"])
    if not yes:
        ui.kv([
            ("recipe", name),
            ("script", script),
            ("environment", manifest),
            ("upload", f"{count} files, {total / 1_048_576:.1f} MB"),
            ("replaces", _human(current["size_bytes"])),
        ], title="About to update")
        # Delete-then-save, and the part worth saying out loud: until this lands the Recipe still runs
        # the old snapshot, and a save the wall refuses leaves it doing exactly that.
        ui.hint("the old snapshot is replaced; it keeps running until this lands")
        if not ui.ask_confirm("Update?", True):
            raise ui.abort("not updated")
    saved = _upload(f"/recipes/{name}/update", tar_bytes, meta, f"update recipe {name!r}")
    ui.ok(f"updated recipe {saved['name']} ({_human(saved['size_bytes'])})")


def delete(name: str, yes: bool = False) -> None:
    """Delete a Recipe and free its bytes. The only thing that ever removes one — Recipes have no
    expiry and are never evicted, so a shelf only shrinks when its owner says so."""
    current = _find(name)
    if not yes and not ui.ask_confirm(f"Delete recipe {name!r} ({_human(current['size_bytes'])})?", False):
        raise ui.abort("not deleted")
    try:
        _client().json("DELETE", f"/recipes/{name}")
    except api.ApiError as e:
        if ui.DEBUG:
            raise
        raise ui.abort(e.user_message()) from None
    ui.ok(f"deleted recipe {name}")


def run(name: str) -> None:
    """Run a Recipe's frozen snapshot — one call, from any machine, nothing to hand. Every setting was
    already answered when it was saved, so this sends nothing but the name."""
    _find(name)  # so a typo'd name says "no recipe named" rather than a 404 with no context
    try:
        resp = _client().json("POST", f"/recipes/{name}/run")
    except api.ApiError as e:
        if ui.DEBUG:
            raise
        raise ui.abort(e.user_message()) from None
    spawn_id = resp["id"]
    # The id goes to stdout and the confirmation to stderr, matching `cuttle run` — so
    # `id=$(cuttle recipe run foo)` captures exactly the id while a human still sees what happened.
    ui.ok(f"running recipe {name!r} as spawn {spawn_id}")
    ui.data(str(spawn_id))


def _find(name: str) -> dict:
    """One Recipe by name, or abort. There is no `GET /recipes/{name}` — the shelf is small and private,
    so the list IS the lookup, and one call answers both "does it exist" and "what does it hold"."""
    for r in _list():
        if r["name"] == name:
            return r
    raise ui.abort(f"you have no recipe named {name!r} — `cuttle recipe list` shows yours")


def _list() -> list[dict]:
    return auth._api("GET", "/recipes")


def listing(as_json: bool = False) -> None:
    """Your shelf. Yours alone — a Recipe holds a User's actual source, so there is no roster view of
    them the way there is for the queue."""
    rows = _list()

    def render():
        if not rows:
            ui.say("no recipes yet — `cuttle recipe save <name> <script>` freezes this directory into one")
            return
        ui.table(
            ["NAME", "SCRIPT", "SIZE", "GPU", "VRAM", "MIN", "OUTPUTS"],
            [[r["name"], r["script"], _human(r["size_bytes"]), r["gpus"],
              f"{r['vram_mb'] // 1024}G", r["minutes"], ", ".join(r["outputs"])] for r in rows])
        total = sum(r["size_bytes"] for r in rows)
        ui.hint(f"{_human(total)} of your 24.0 GiB recipe allowance")

    ui.emit(rows, as_json, render)


def _human(n: int) -> str:
    """Mirrors the Head's `main._human`, so a size reads the same wherever it is printed."""
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"  # unreachable
