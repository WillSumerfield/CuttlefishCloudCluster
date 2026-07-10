"""Read/write ~/.ccc/config/config.yaml + the setup-time W&B prompt."""

import os

import typer
import yaml

from . import config


def load() -> dict:
    if config.CONFIG_FILE.exists():
        return yaml.safe_load(config.CONFIG_FILE.read_text()) or {}
    return {}


def save(data: dict) -> None:
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(yaml.safe_dump(data, sort_keys=False))
    config.CONFIG_FILE.chmod(0o600)


def prompt_wandb() -> None:
    """Prompt (up front) for the W&B key + default project/entity, persist to config.yaml.

    The key is a machine/user secret, so it lives here. Project + entity belong to the *run*, not the
    box — these are only the per-run defaults; `cuttle run --project/--entity` overrides either."""
    data = load()
    wandb = data.get("wandb", {})
    have = "set" if wandb.get("api_key") else "unset"
    key = typer.prompt(
        f"WANDB_API_KEY (currently {have}; blank = keep/disable)",
        default="", show_default=False,  # visible so a pasted key is verifiable
    )
    if key:
        wandb["api_key"] = key
    wandb.setdefault("project", typer.prompt("Default W&B project", default=wandb.get("project", "ccc")))
    # Entity is the URL slug (wandb.ai/<slug>/<project>), NOT the display name. Blank = the account's
    # own default entity — leave WANDB_ENTITY unset rather than storing an empty one.
    entity = typer.prompt("Default W&B entity/slug (blank = account default)",
                          default=wandb.get("entity", ""), show_default=False)
    if entity:
        wandb["entity"] = entity
    data["wandb"] = wandb
    save(data)
    print(f"wandb: api_key {'set' if wandb.get('api_key') else 'disabled'}, "
          f"project={wandb['project']}, entity={wandb.get('entity', '(account default)')} "
          f"→ {config.CONFIG_FILE}")


def wandb_env(project: str | None = None, entity: str | None = None) -> dict[str, str]:
    """Env vars to inject into a Spawn Pod for W&B (or disable it if no key). `project`/`entity` are
    the `cuttle run` overrides; each falls back to the config default, then project to 'ccc' and
    entity to the account default (WANDB_ENTITY left unset — an empty one breaks wandb.init)."""
    wandb = load().get("wandb", {})
    key = wandb.get("api_key") or os.environ.get("WANDB_API_KEY")
    if not key:
        return {"WANDB_MODE": "disabled"}
    env = {
        "WANDB_API_KEY": key,
        "WANDB_PROJECT": project or wandb.get("project", "ccc"),
        "WANDB_MODE": "online",
    }
    entity = entity or wandb.get("entity")
    if entity:
        env["WANDB_ENTITY"] = entity
    return env
