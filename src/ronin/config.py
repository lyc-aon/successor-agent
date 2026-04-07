"""User preferences persistence — `~/.config/ronin/chat.json`.

Tiny stdlib JSON read/write for the chat's user-toggleable settings:
theme, density, mouse mode. Survives `rn chat` restarts so the user
doesn't have to re-pick their preferences every session.

Pure stdlib (json + pathlib). Failures are non-fatal — if the config
file is missing, malformed, or unwritable, we silently fall back to
defaults. Settings only persist if you successfully wrote them once.

Schema is intentionally tiny and forward-compatible: unknown keys
are ignored on load, missing keys use the supplied defaults. Bumping
to a new schema is "add a key with a default" — old configs continue
to work without migration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


CONFIG_DIR_ENV = "RONIN_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "ronin"
CHAT_CONFIG_FILE = "chat.json"


def _config_dir() -> Path:
    """Resolve the config directory, honoring $RONIN_CONFIG_DIR for tests."""
    env = os.environ.get(CONFIG_DIR_ENV)
    if env:
        return Path(env)
    return DEFAULT_CONFIG_DIR


def _chat_config_path() -> Path:
    return _config_dir() / CHAT_CONFIG_FILE


def load_chat_config() -> dict[str, Any]:
    """Read the chat config file. Returns {} on any error."""
    path = _chat_config_path()
    try:
        if not path.exists():
            return {}
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def save_chat_config(data: dict[str, Any]) -> bool:
    """Write the chat config file. Returns True on success.

    Creates the config directory if missing. Atomic-ish write via a
    temp file + rename so a crash during write doesn't corrupt the
    existing config.
    """
    path = _chat_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)
        return True
    except OSError:
        return False
