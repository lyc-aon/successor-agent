"""User preferences persistence — `~/.config/successor/chat.json`.

Tiny stdlib JSON read/write for the chat's user-toggleable settings:
theme, display_mode, density, mouse, autorecord, active_profile. Survives `successor chat`
restarts so the user doesn't have to re-pick their preferences every
session.

Pure stdlib (json + pathlib). Failures are non-fatal — if the config
file is missing, malformed, or unwritable, we silently fall back to
defaults. Settings only persist if you successfully wrote them once.

Schema is intentionally tiny and forward-compatible: unknown keys are
ignored on load, missing keys use the supplied defaults. Bumping to a
new schema is "add a key with a default" — old configs continue to
work without migration.

V2 fields (added 2026-04-06 with the theme refactor):

  version           int — schema version, 2 for current, 1 for legacy
  theme             str — registered theme name (`steel` or `paper`)
  display_mode      str — "dark" or "light"
  density           str — "compact" / "normal" / "spacious"
  mouse             bool — mouse reporting enabled
  active_profile    str — registered profile name (added in phase 3,
                          slot reserved here)

V3 fields (added 2026-04-08 as a compatibility stamp):

  same shape as v2; version bump reserved the mouse preference split
  without changing persisted semantics

V1 → V2 fixup (one-shot, idempotent, runs on every load):

  Old v1 stored a flat `theme` key whose value conflated the visual
  identity and the display mode. The fixup translates:

    {"theme": "dark"}   → {"theme": "steel", "display_mode": "dark"}
    {"theme": "light"}  → {"theme": "steel", "display_mode": "light"}
    {"theme": "forge"}  → {"theme": "paper", "display_mode": "dark"}
    {"theme": "<other>"}→ {"theme": "<other>", "display_mode": "dark"}

  The fixup is idempotent because it only runs when `version` is
  missing or < 2. Once a config is saved with version=2 the fixup is
  a no-op on subsequent loads.

V2 → V3 fixup (one-shot, idempotent, runs on every load):

  Compatibility-only. Preserve the existing `mouse` value exactly. The
  intended split remains:
    - mouse off  → terminal owns wheel/selection
    - mouse on   → Successor owns wheel/clicks

V4 fields (added 2026-04-08 for local recording bundles):

  autorecord        bool — record normal chat sessions to a local
                     playback bundle by default

V3 → V4 fixup (one-shot, idempotent, runs on every load):

  Missing `autorecord` defaults to True. Recording bundles are local-only
  debugging artifacts, stored outside the repo by default.

V4 → V5 fixup (one-shot, idempotent, runs on every load):

  Clamp saved theme names to the supported built-in catalog. Legacy
  `forge` maps to `paper`; legacy `cobalt` maps to `steel`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .render.theme import normalize_theme_name


CONFIG_DIR_ENV = "SUCCESSOR_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "successor"
CHAT_CONFIG_FILE = "chat.json"

CURRENT_SCHEMA_VERSION = 5

# Legacy v1 theme names → (v2 theme name, v2 display_mode). Anything not
# in this map is passed through unchanged with display_mode defaulting
# to "dark".
_V1_THEME_MAP: dict[str, tuple[str, str]] = {
    "dark": ("steel", "dark"),
    "light": ("steel", "light"),
    "forge": ("paper", "dark"),
    "cobalt": ("steel", "dark"),
}


def _config_dir() -> Path:
    """Resolve the config directory, honoring $SUCCESSOR_CONFIG_DIR for tests."""
    env = os.environ.get(CONFIG_DIR_ENV)
    if env:
        return Path(env)
    return DEFAULT_CONFIG_DIR


def _chat_config_path() -> Path:
    return _config_dir() / CHAT_CONFIG_FILE


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def write_local_json(path: Path, payload: dict[str, Any]) -> bool:
    """Write a local JSON config file with user-only permissions when possible."""
    try:
        _mkdir_private(path.parent)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


def load_chat_config() -> dict[str, Any]:
    """Read the chat config file, migrating v1 → v2 if needed.

    Returns a dict that always includes a `version` key set to the
    current schema version. Returns {"version": CURRENT_SCHEMA_VERSION}
    on any error so callers can treat the result uniformly.
    """
    path = _chat_config_path()
    try:
        if not path.exists():
            return {"version": CURRENT_SCHEMA_VERSION}
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return {"version": CURRENT_SCHEMA_VERSION}
    except (OSError, json.JSONDecodeError):
        return {"version": CURRENT_SCHEMA_VERSION}

    return migrate_config(data)


def migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    """Apply schema migrations to a freshly-loaded config dict.

    Idempotent — running it twice has the same result as running it
    once. Pure function so it's directly testable without touching
    the filesystem.
    """
    version = data.get("version")
    if not isinstance(version, int):
        version = 1  # missing version field implies v1

    if version < 2:
        data = _migrate_v1_to_v2(data)
        data["version"] = 2

    if version < 3:
        data = _migrate_v2_to_v3(data)
        data["version"] = 3

    if version < 4:
        data = _migrate_v3_to_v4(data)
        data["version"] = 4

    if version < 5:
        data = _migrate_v4_to_v5(data)
        data["version"] = 5

    return data


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Translate the v1 conflated `theme` key into separate v2 keys.

    The v1 schema had:
        theme:    "dark" | "light" | "forge"   (plus density, mouse)
    The v2 schema has:
        theme:        registered theme name (`steel` or `paper`)
        display_mode: "dark" | "light"
        plus the rest unchanged.

    Already-v2 fields in `data` (theme + display_mode coexisting) are
    preserved as-is. Missing fields are filled with sensible defaults.
    """
    out = dict(data)  # don't mutate the caller's dict

    legacy_theme = out.get("theme")
    has_display_mode = "display_mode" in out

    # Only translate when the v1 shape is detected: a known v1 theme
    # name AND no display_mode key already present. If display_mode is
    # set, we trust the caller knows what they're doing.
    if (
        isinstance(legacy_theme, str)
        and not has_display_mode
        and legacy_theme.lower() in _V1_THEME_MAP
    ):
        new_theme, new_mode = _V1_THEME_MAP[legacy_theme.lower()]
        out["theme"] = new_theme
        out["display_mode"] = new_mode
    elif isinstance(legacy_theme, str) and not has_display_mode:
        # Unknown legacy theme name (maybe a user-defined v1 theme).
        # Pass through unchanged and default mode to dark.
        out["display_mode"] = "dark"

    return out


def _migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """Compatibility-only v2 → v3 migration.

    Preserve the existing mouse preference exactly. V3 exists so future
    releases can reason about post-v2 configs without clobbering the
    user's terminal-vs-app mouse ownership choice.
    """
    return dict(data)


def _migrate_v3_to_v4(data: dict[str, Any]) -> dict[str, Any]:
    """Add the local autorecord preference with a safe default."""
    out = dict(data)
    out.setdefault("autorecord", True)
    return out


def _migrate_v4_to_v5(data: dict[str, Any]) -> dict[str, Any]:
    """Clamp saved theme names to the supported paper/steel catalog."""
    out = dict(data)
    theme = normalize_theme_name(out.get("theme"))
    if theme is not None:
        out["theme"] = theme
    return out


def save_chat_config(data: dict[str, Any]) -> bool:
    """Write the chat config file. Returns True on success.

    Creates the config directory if missing. Atomic-ish write via a
    temp file + rename so a crash during write doesn't corrupt the
    existing config. Stamps the current schema version into the saved
    payload so future loads skip the v1 migration.
    """
    path = _chat_config_path()
    payload = dict(data)
    payload.setdefault("autorecord", True)
    payload["version"] = CURRENT_SCHEMA_VERSION
    return write_local_json(path, payload)
