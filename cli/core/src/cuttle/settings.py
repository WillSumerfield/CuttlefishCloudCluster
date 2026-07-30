"""Read/write ~/.ccc/config/config.yaml + the setup-time W&B prompt."""

import yaml

from . import config, ui


def load() -> dict:
    if config.CONFIG_FILE.exists():
        return yaml.safe_load(config.CONFIG_FILE.read_text()) or {}
    return {}


def save(data: dict) -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(yaml.safe_dump(data, sort_keys=False))
    config.CONFIG_FILE.chmod(0o600)


def prompt_wandb() -> None:
    """Prompt (up front) for the default W&B project/entity, persist to config.yaml.

    **No key prompt.** The W&B key is per-User and lives on the Head now, encrypted (`cuttle wandb
    set`, or the Dashboard's Account page), because the Dashboard is the primary submission surface
    and a browser has no config file to keep one in. Project + entity belong to the *run*, not the
    box, and are not secrets — these are only the per-run defaults; `cuttle run --project/--entity`
    overrides either.

    Asked through `ui`, like every other prompt. These were `typer.prompt`, which is click's own —
    it writes straight to the terminal and reads a line in cooked mode, so inside a box it drew
    outside the borders, echoed wherever the cursor happened to be, and was overpainted on the
    frame's next refresh. Nothing may write to the screen behind the frame's back.
    """
    data = load()
    wandb = data.get("wandb", {})
    # A key left behind by an older setup is now dead weight — nothing reads it — so it is REMOVED
    # rather than ignored. An unused secret sitting in a file is strictly worse than no secret at all.
    moved = wandb.pop("api_key", None)
    # Plain assignment, not setdefault: the prompt is what the operator just answered, and setdefault
    # evaluated it and then threw it away whenever a project was already configured — so re-running
    # `cuttle setup` could never change the default. The old value is the prompt's *default*, which is
    # where "keep what I have" already lives (bare Enter).
    wandb["project"] = ui.ask_text("Default W&B project", default=wandb.get("project", "ccc"))
    # Entity is the URL slug (wandb.ai/<slug>/<project>), NOT the display name. Blank = the account's
    # own default entity — leave WANDB_ENTITY unset rather than storing an empty one. A blank answer
    # therefore has to REMOVE a configured entity, or clearing one would be impossible for the same
    # reason above: the answer would be read and discarded.
    entity = ui.ask_text("Default W&B entity/slug (blank = account default)",
                         default=wandb.get("entity", ""))
    if entity:
        wandb["entity"] = entity
    else:
        wandb.pop("entity", None)
    data["wandb"] = wandb
    save(data)
    if moved:
        print("wandb: removed the api_key from config.yaml — your key lives on the Head now "
              "(`cuttle wandb set`, or the Dashboard's Account page)")
    print(f"wandb: project={wandb['project']}, "
          f"entity={wandb.get('entity', '(account default)')} → {config.CONFIG_FILE}")


def wandb_env(project: str | None = None, entity: str | None = None) -> dict[str, str]:
    """Which W&B *run* this Spawn is — project and entity, and deliberately nothing else.

    No key, and no WANDB_MODE. Both used to come from here; both are now the Head's, which holds one
    key per User and composes the Spawn's whole W&B environment itself (ccc_head_api.spawns
    compose_wandb_env). A key sent from here would be ignored, so it is not sent.

    `project`/`entity` are the `cuttle run` overrides; each falls back to the config default, then
    project to 'ccc' and entity to the account default (WANDB_ENTITY left unset — an empty one breaks
    wandb.init).
    """
    wandb = load().get("wandb", {})
    env = {"WANDB_PROJECT": project or wandb.get("project", "ccc")}
    entity = entity or wandb.get("entity")
    if entity:
        env["WANDB_ENTITY"] = entity
    return env
