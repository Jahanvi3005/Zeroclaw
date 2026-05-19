"""CLI config loading: flags > env > file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import tomllib


DEFAULT_API_URL = "http://localhost:8000"
DEFAULT_CONFIG_PATH = "~/.config/claw-admin/config.toml"
DEFAULT_TOKEN_PATH = "~/.config/claw-admin/token"


@dataclass(frozen=True)
class CliConfig:
    api_url: str
    token: str | None


def _read_token(path: str | None) -> str | None:
    if not path:
        return None
    file_path = Path(path).expanduser()
    if not file_path.exists():
        return None
    return file_path.read_text(encoding="utf-8").strip()


def load_cli_config(
    *,
    api_url: str | None,
    token: str | None,
    token_file: str | None,
    config_path: str | None = None,
) -> CliConfig:
    file_data: dict[str, object] = {}
    cfg_path = Path(config_path or DEFAULT_CONFIG_PATH).expanduser()
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            file_data = tomllib.load(fh)

    file_api_url = file_data.get("api_url")
    file_token_file = file_data.get("token_file")
    final_api_url = (
        api_url
        or os.environ.get("CLAW_ADMIN_API_URL")
        or (file_api_url if isinstance(file_api_url, str) else None)
        or DEFAULT_API_URL
    )
    final_token = (
        token
        or os.environ.get("CLAW_ADMIN_TOKEN")
        or _read_token(
            token_file
            or os.environ.get("CLAW_ADMIN_TOKEN_FILE")
            or (file_token_file if isinstance(file_token_file, str) else None)
            or DEFAULT_TOKEN_PATH
        )
    )
    return CliConfig(api_url=final_api_url, token=final_token)
