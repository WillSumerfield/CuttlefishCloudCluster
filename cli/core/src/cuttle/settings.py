"""Read/write ~/.ccc/config/config.yaml."""

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

