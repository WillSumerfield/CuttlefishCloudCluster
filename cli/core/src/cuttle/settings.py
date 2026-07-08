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
    """Prompt (up front) for W&B key + project, persist to config.yaml."""
    data = load()
    wandb = data.get("wandb", {})
    have = "set" if wandb.get("api_key") else "unset"
    key = typer.prompt(
        f"WANDB_API_KEY (currently {have}; blank = keep/disable)",
        default="", show_default=False,  # visible so a pasted key is verifiable
    )
    if key:
        wandb["api_key"] = key
    wandb.setdefault("project", typer.prompt("W&B project", default=wandb.get("project", "ccc")))
    data["wandb"] = wandb
    save(data)
    print(f"wandb: api_key {'set' if wandb.get('api_key') else 'disabled'}, "
          f"project={wandb['project']} → {config.CONFIG_FILE}")


def wandb_env() -> dict[str, str]:
    """Env vars to inject into a Spawn Pod for W&B (or disable it if no key)."""
    wandb = load().get("wandb", {})
    key = wandb.get("api_key") or os.environ.get("WANDB_API_KEY")
    if not key:
        return {"WANDB_MODE": "disabled"}
    return {
        "WANDB_API_KEY": key,
        "WANDB_PROJECT": wandb.get("project", "ccc"),
        "WANDB_MODE": "online",
    }
