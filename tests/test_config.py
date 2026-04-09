"""Tests for config.py — load/save and v1 → v2 migration.

The migration is the load-bearing part of this module: every existing
Successor user has a v1 config from before the theme refactor, and we need
their settings to keep working without manual edits.

Tests use the temp_config_dir fixture for hermetic isolation, and use
the pure migrate_config function for migration tests so they don't
need to touch the filesystem at all.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from successor.config import (
    CURRENT_SCHEMA_VERSION,
    load_chat_config,
    migrate_config,
    save_chat_config,
)


# ─── load_chat_config / save_chat_config ───


def test_load_returns_default_when_file_missing(temp_config_dir: Path) -> None:
    cfg = load_chat_config()
    # Always includes the version key so callers can treat it uniformly.
    assert cfg["version"] == CURRENT_SCHEMA_VERSION


def test_save_then_load_roundtrip(temp_config_dir: Path) -> None:
    """A saved config loads back with all fields preserved."""
    payload = {
        "theme": "paper",
        "display_mode": "dark",
        "density": "spacious",
        "mouse": True,
    }
    assert save_chat_config(payload) is True

    cfg = load_chat_config()
    assert cfg["theme"] == "paper"
    assert cfg["display_mode"] == "dark"
    assert cfg["density"] == "spacious"
    assert cfg["mouse"] is True
    assert cfg["version"] == CURRENT_SCHEMA_VERSION


def test_save_creates_config_dir(temp_config_dir: Path) -> None:
    """Saving when ~/.config/successor doesn't exist creates it."""
    # The fixture's temp dir already exists; remove its contents.
    for child in temp_config_dir.iterdir():
        if child.is_dir():
            for nested in child.iterdir():
                nested.unlink()
            child.rmdir()
        else:
            child.unlink()

    assert save_chat_config({"theme": "steel"}) is True
    assert (temp_config_dir / "chat.json").exists()


def test_load_returns_default_on_corrupt_file(temp_config_dir: Path) -> None:
    """A corrupt config file falls back to defaults instead of crashing."""
    (temp_config_dir / "chat.json").write_text("{ not json")
    cfg = load_chat_config()
    assert cfg["version"] == CURRENT_SCHEMA_VERSION


def test_load_returns_default_on_non_object_top_level(
    temp_config_dir: Path,
) -> None:
    """A JSON array at top-level is invalid and falls back to defaults."""
    (temp_config_dir / "chat.json").write_text("[1, 2, 3]")
    cfg = load_chat_config()
    assert cfg["version"] == CURRENT_SCHEMA_VERSION


def test_save_is_atomic(temp_config_dir: Path) -> None:
    """save_chat_config writes via a temp file + rename so a partial
    write can never corrupt the existing config."""
    save_chat_config({"theme": "steel"})
    save_chat_config({"theme": "paper"})

    # No leftover .tmp file after a successful write
    tmp_files = list(temp_config_dir.glob("*.tmp"))
    assert tmp_files == []


def test_save_writes_private_permissions_when_supported(temp_config_dir: Path) -> None:
    save_chat_config({"theme": "steel"})

    config_dir_mode = stat.S_IMODE(temp_config_dir.stat().st_mode)
    file_mode = stat.S_IMODE((temp_config_dir / "chat.json").stat().st_mode)
    assert config_dir_mode == 0o700
    assert file_mode == 0o600


# ─── migrate_config (pure function tests, no filesystem) ───


def test_migrate_v1_dark() -> None:
    """v1 'dark' theme → v2 ('steel', 'dark')."""
    result = migrate_config({"theme": "dark"})
    assert result["theme"] == "steel"
    assert result["display_mode"] == "dark"
    assert result["version"] == CURRENT_SCHEMA_VERSION
    assert result["autorecord"] is True


def test_migrate_v1_light() -> None:
    """v1 'light' theme → v2 ('steel', 'light')."""
    result = migrate_config({"theme": "light"})
    assert result["theme"] == "steel"
    assert result["display_mode"] == "light"
    assert result["version"] == CURRENT_SCHEMA_VERSION


def test_migrate_v1_forge() -> None:
    """v1 'forge' theme maps forward to paper dark."""
    result = migrate_config({"theme": "forge"})
    assert result["theme"] == "paper"
    assert result["display_mode"] == "dark"
    assert result["version"] == CURRENT_SCHEMA_VERSION


def test_migrate_v4_cobalt_maps_to_steel() -> None:
    result = migrate_config({"version": 4, "theme": "cobalt", "display_mode": "dark"})
    assert result["theme"] == "steel"
    assert result["display_mode"] == "dark"
    assert result["version"] == CURRENT_SCHEMA_VERSION


def test_migrate_v1_unknown_theme_passes_through() -> None:
    """Unknown custom theme names survive migration unchanged."""
    result = migrate_config({"theme": "my_custom_theme"})
    assert result["theme"] == "my_custom_theme"
    assert result["display_mode"] == "dark"
    assert result["version"] == CURRENT_SCHEMA_VERSION


def test_migrate_preserves_other_keys() -> None:
    """Other v1 keys (density, mouse) survive the migration unchanged."""
    result = migrate_config({
        "theme": "dark",
        "density": "compact",
        "mouse": True,
    })
    assert result["density"] == "compact"
    assert result["mouse"] is True


def test_migrate_v3_autorecord_default_is_added() -> None:
    """Older configs pick up the local autorecord default on migrate."""
    v3 = {
        "version": 3,
        "theme": "steel",
        "display_mode": "light",
        "density": "normal",
        "mouse": False,
    }
    result = migrate_config(v3)
    assert result["theme"] == "steel"
    assert result["display_mode"] == "light"
    assert result["mouse"] is False
    assert result["autorecord"] is True
    assert result["version"] == CURRENT_SCHEMA_VERSION


def test_migrate_is_idempotent() -> None:
    """Running migrate twice produces the same result as once."""
    once = migrate_config({"theme": "dark", "density": "spacious"})
    twice = migrate_config(once)
    assert once == twice


def test_migrate_doesnt_clobber_explicit_display_mode() -> None:
    """If a v1-shaped config somehow already has display_mode set,
    we trust the caller and don't overwrite it."""
    result = migrate_config({
        "theme": "light",
        "display_mode": "dark",  # contradicts the legacy "light" theme
    })
    # display_mode is preserved because it was explicitly set.
    assert result["display_mode"] == "dark"
    # theme was NOT translated because the v1 fixup only fires when
    # display_mode is absent.
    assert result["theme"] == "steel"


def test_migrate_empty_dict() -> None:
    """An empty dict gets the version stamp but no theme fixup."""
    result = migrate_config({})
    assert result["version"] == CURRENT_SCHEMA_VERSION
    assert result["autorecord"] is True
    assert "theme" not in result
    assert "display_mode" not in result


def test_migrate_doesnt_mutate_input() -> None:
    """migrate_config returns a new dict — the original is untouched."""
    original = {"theme": "dark"}
    result = migrate_config(original)
    assert "display_mode" not in original  # original unchanged
    assert result["display_mode"] == "dark"


# ─── load + migrate integration ───


def test_load_migrates_v1_file_on_disk(temp_config_dir: Path) -> None:
    """A v1 config file on disk is migrated transparently on load."""
    legacy = {"theme": "dark", "density": "compact", "mouse": False}
    (temp_config_dir / "chat.json").write_text(json.dumps(legacy))

    cfg = load_chat_config()
    assert cfg["theme"] == "steel"
    assert cfg["display_mode"] == "dark"
    assert cfg["density"] == "compact"
    assert cfg["mouse"] is False
    assert cfg["version"] == CURRENT_SCHEMA_VERSION
    assert cfg["autorecord"] is True


def test_migrate_v2_mouse_false_preserved() -> None:
    """V3 must preserve the user's off setting exactly."""
    result = migrate_config({
        "version": 2,
        "theme": "steel",
        "display_mode": "light",
        "mouse": False,
    })
    assert result["mouse"] is False
    assert result["autorecord"] is True
    assert result["version"] == CURRENT_SCHEMA_VERSION


def test_migrate_v2_mouse_true_preserved() -> None:
    result = migrate_config({
        "version": 2,
        "theme": "steel",
        "display_mode": "light",
        "mouse": True,
    })
    assert result["mouse"] is True
    assert result["autorecord"] is True
    assert result["version"] == CURRENT_SCHEMA_VERSION


def test_save_stamps_current_version(temp_config_dir: Path) -> None:
    """save_chat_config always writes version=CURRENT_SCHEMA_VERSION,
    even if the caller's payload omits it."""
    save_chat_config({"theme": "steel"})

    text = (temp_config_dir / "chat.json").read_text()
    payload = json.loads(text)
    assert payload["version"] == CURRENT_SCHEMA_VERSION


def test_save_then_load_does_not_re_migrate(temp_config_dir: Path) -> None:
    """Once saved as v2, future loads skip the migration entirely."""
    # Save with explicit v2 fields
    save_chat_config({
        "theme": "paper",
        "display_mode": "light",
    })
    cfg = load_chat_config()
    # The saved values are preserved exactly — no v1 fixup ran.
    assert cfg["theme"] == "paper"
    assert cfg["display_mode"] == "light"
    assert cfg["autorecord"] is True


def test_save_then_load_preserves_autorecord(temp_config_dir: Path) -> None:
    save_chat_config({
        "theme": "steel",
        "autorecord": False,
    })
    cfg = load_chat_config()
    assert cfg["autorecord"] is False
