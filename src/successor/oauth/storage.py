"""Token persistence for OAuth.

Stores tokens as JSON in ``~/.config/successor/credentials/{key}.json``
with ``0o600`` permissions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import OAuthToken


def _credentials_dir() -> Path:
    from ..loader import config_dir
    return config_dir() / "credentials"


def _ensure_dir() -> Path:
    d = _credentials_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _credentials_path(key: str) -> Path:
    name = key.removeprefix("oauth/").split("/")[-1] or key
    return _ensure_dir() / f"{name}.json"


def _ensure_private_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def save_token(key: str, token: OAuthToken) -> None:
    """Persist an OAuth token to disk."""
    path = _credentials_path(key)
    path.write_text(
        json.dumps(token.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )
    _ensure_private_file(path)


def load_token(key: str) -> OAuthToken | None:
    """Load a saved OAuth token, or None if missing / corrupt."""
    path = _credentials_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return OAuthToken.from_dict(data)
    except (TypeError, ValueError):
        return None


def delete_token(key: str) -> None:
    """Remove a saved OAuth token."""
    path = _credentials_path(key)
    if path.exists():
        path.unlink()
