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

    No key prompt: the W&B key is per-User, lives encrypted on the Head (`cuttle wandb set` or
    Dashboard Account page) since the Dashboard is the primary submission surface and a browser has
    no config file. Project/entity are per-run defaults, not secrets; `cuttle run --project/--entity`
    overrides either.

    Uses `ui.ask_text`, not `typer.prompt` — click's prompt writes/reads the terminal directly in
    cooked mode, breaking out of the drawn frame.
    """
    data = load()
    wandb = data.get("wandb", {})
    # Drop any leftover api_key — nothing reads it, an unused secret on disk is worse than none.
    moved = wandb.pop("api_key", None)
    # Plain assignment not setdefault: setdefault would discard the operator's new answer whenever
    # a project was already configured, so re-running `cuttle setup` could never change it.
    wandb["project"] = ui.ask_text("Default W&B project", default=wandb.get("project", "ccc"))
    # Entity is the URL slug, not display name. Blank = account default -> unset WANDB_ENTITY
    # rather than store empty; must actively pop() or a blank answer could never clear it.
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
    """Which W&B run this Spawn is: project + entity only.

    No key, no WANDB_MODE — the Head composes those (ccc_head_api.spawns.compose_wandb_env) from its
    per-User key; a key sent from here would be ignored.

    project/entity are `cuttle run` overrides, falling back to config default then 'ccc' / account
    default (WANDB_ENTITY left unset since empty breaks wandb.init).
    """
    wandb = load().get("wandb", {})
    env = {"WANDB_PROJECT": project or wandb.get("project", "ccc")}
    entity = entity or wandb.get("entity")
    if entity:
        env["WANDB_ENTITY"] = entity
    return env
