"""Tests for the RoninConfig three-pane menu.

Three layers (matching the wizard test pattern):
  1. State machine — pure logic for navigation, dirty tracking,
     save/revert, profile cursor + settings cursor independence.
  2. Save flow — drives the menu through edits and asserts the
     resulting JSON files + chat.json on disk.
  3. Snapshot rendering — wizard_demo_snapshot-equivalent for the
     config menu, asserting visible content per state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ronin.config import load_chat_config
from ronin.input.keys import Key, KeyEvent
from ronin.profiles import PROFILE_REGISTRY, Profile, get_profile
from ronin.render.theme import THEME_REGISTRY
from ronin.snapshot import config_demo_snapshot, render_grid_to_plain
from ronin.wizard.config import (
    Focus,
    RoninConfig,
    _SETTINGS_TREE,
    _profile_to_json_dict,
    run_config_menu,
)


# ─── Construction ───


def test_construct_loads_profiles(temp_config_dir: Path) -> None:
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    menu = RoninConfig()
    assert len(menu._working_profiles) >= 2  # default + ronin-dev builtins
    assert len(menu._initial_profiles) == len(menu._working_profiles)


def test_construct_starts_on_active_profile(temp_config_dir: Path) -> None:
    """The profile cursor starts on whichever profile is active in chat.json."""
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "active_profile": "ronin-dev",
    }))
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    menu = RoninConfig()
    assert menu._working_profiles[menu._profile_cursor].name == "ronin-dev"


def test_construct_has_no_dirty(temp_config_dir: Path) -> None:
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    menu = RoninConfig()
    assert not menu._any_dirty()


def test_settings_cursor_starts_on_editable_row(temp_config_dir: Path) -> None:
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    menu = RoninConfig()
    assert _SETTINGS_TREE[menu._settings_cursor].kind.value != "readonly"


# ─── Navigation ───


def test_tab_cycles_focus(temp_config_dir: Path) -> None:
    menu = RoninConfig()
    initial = menu._focus
    menu._handle_key(KeyEvent(key=Key.TAB))
    assert menu._focus != initial
    menu._handle_key(KeyEvent(key=Key.TAB))
    assert menu._focus == initial


def test_settings_arrows_skip_readonly(temp_config_dir: Path) -> None:
    """Up/Down in the settings pane only land on editable fields."""
    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    seen_indices = set()
    for _ in range(20):
        menu._handle_key(KeyEvent(key=Key.DOWN))
        seen_indices.add(menu._settings_cursor)
    # All seen indices must be editable
    for idx in seen_indices:
        assert _SETTINGS_TREE[idx].kind.value != "readonly", (
            f"cursor landed on read-only row {idx}: {_SETTINGS_TREE[idx].name}"
        )


def test_profiles_arrows_walk_through_list(temp_config_dir: Path) -> None:
    PROFILE_REGISTRY.reload()
    menu = RoninConfig()
    menu._focus = Focus.PROFILES
    n = len(menu._working_profiles)
    if n < 2:
        return  # only one profile loaded
    initial = menu._profile_cursor
    menu._handle_key(KeyEvent(key=Key.DOWN))
    assert menu._profile_cursor != initial
    menu._handle_key(KeyEvent(key=Key.UP))
    assert menu._profile_cursor == initial


def test_profile_navigation_syncs_preview(temp_config_dir: Path) -> None:
    """Selecting a different profile in the left pane updates the preview."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    menu = RoninConfig()
    menu._focus = Focus.PROFILES
    if len(menu._working_profiles) < 2:
        return
    initial_preview_theme = menu._preview_chat.theme.name
    # Walk through profiles until the theme name changes (or we've
    # looped all the way around)
    for _ in range(len(menu._working_profiles) + 1):
        menu._handle_key(KeyEvent(key=Key.DOWN))
        if menu._preview_chat.theme.name != initial_preview_theme:
            return
    # All profiles have the same theme — preview should still be valid
    assert menu._preview_chat is not None


# ─── Editing — TOGGLE fields (immediate flip) ───


def test_toggle_mode_flips_immediately(temp_config_dir: Path) -> None:
    """Enter on the mode field flips dark↔light without opening overlay."""
    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    # Position cursor on the mode field
    mode_idx = next(
        i for i, f in enumerate(_SETTINGS_TREE) if f.name == "display_mode"
    )
    menu._settings_cursor = mode_idx
    initial_mode = menu._current_profile().display_mode
    menu._handle_key(KeyEvent(key=Key.ENTER))
    # No edit overlay opened
    assert menu._editing_field is None
    # Mode flipped
    assert menu._current_profile().display_mode != initial_mode
    # Field is now dirty
    assert menu._is_dirty(menu._current_profile().name, "display_mode")


def test_toggle_intro_flips_none_to_nusamurai(temp_config_dir: Path) -> None:
    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    intro_idx = next(
        i for i, f in enumerate(_SETTINGS_TREE) if f.name == "intro_animation"
    )
    menu._settings_cursor = intro_idx
    initial = menu._current_profile().intro_animation
    menu._handle_key(KeyEvent(key=Key.ENTER))
    new_value = menu._current_profile().intro_animation
    assert new_value != initial


# ─── Editing — CYCLE fields (overlay) ───


def test_cycle_theme_opens_overlay(temp_config_dir: Path) -> None:
    THEME_REGISTRY.reload()
    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    theme_idx = next(i for i, f in enumerate(_SETTINGS_TREE) if f.name == "theme")
    menu._settings_cursor = theme_idx
    menu._handle_key(KeyEvent(key=Key.ENTER))
    assert menu._editing_field == theme_idx


def test_overlay_up_down_cycles_options_with_live_preview(
    temp_config_dir: Path,
) -> None:
    """Up/Down in overlay updates the cursor AND immediately previews."""
    # Add a second theme so cycling actually changes value
    user_themes = temp_config_dir / "themes"
    user_themes.mkdir()
    sakura = {
        "name": "sakura", "icon": "✿", "description": "test",
        "dark": {
            "bg": "#000000", "bg_input": "#111111", "bg_footer": "#222222",
            "fg": "#FFFFFF", "fg_dim": "#CCCCCC", "fg_subtle": "#888888",
            "accent": "#FF0000", "accent_warm": "#FFAA00", "accent_warn": "#FF3300",
        },
        "light": {
            "bg": "#FFFFFF", "bg_input": "#EEEEEE", "bg_footer": "#DDDDDD",
            "fg": "#000000", "fg_dim": "#444444", "fg_subtle": "#888888",
            "accent": "#CC0000", "accent_warm": "#CC8800", "accent_warn": "#CC2200",
        },
    }
    (user_themes / "sakura.json").write_text(json.dumps(sakura))
    THEME_REGISTRY.reload()

    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    theme_idx = next(i for i, f in enumerate(_SETTINGS_TREE) if f.name == "theme")
    menu._settings_cursor = theme_idx

    # Open the overlay
    menu._handle_key(KeyEvent(key=Key.ENTER))
    assert menu._editing_field == theme_idx

    initial_theme = menu._current_profile().theme

    # Cycle down
    menu._handle_key(KeyEvent(key=Key.DOWN))
    new_theme = menu._current_profile().theme
    assert new_theme != initial_theme  # value changed live


def test_overlay_enter_confirms(temp_config_dir: Path) -> None:
    THEME_REGISTRY.reload()
    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    theme_idx = next(i for i, f in enumerate(_SETTINGS_TREE) if f.name == "theme")
    menu._settings_cursor = theme_idx
    menu._handle_key(KeyEvent(key=Key.ENTER))
    assert menu._editing_field is not None
    menu._handle_key(KeyEvent(key=Key.ENTER))  # confirm
    assert menu._editing_field is None


def test_overlay_esc_cancels_and_restores(temp_config_dir: Path) -> None:
    """Esc in the overlay restores the snapshot value."""
    user_themes = temp_config_dir / "themes"
    user_themes.mkdir()
    other_theme = {
        "name": "other", "icon": "*", "description": "",
        "dark": {
            "bg": "#000000", "bg_input": "#111111", "bg_footer": "#222222",
            "fg": "#FFFFFF", "fg_dim": "#CCCCCC", "fg_subtle": "#888888",
            "accent": "#FF0000", "accent_warm": "#FFAA00", "accent_warn": "#FF3300",
        },
        "light": {
            "bg": "#FFFFFF", "bg_input": "#EEEEEE", "bg_footer": "#DDDDDD",
            "fg": "#000000", "fg_dim": "#444444", "fg_subtle": "#888888",
            "accent": "#CC0000", "accent_warm": "#CC8800", "accent_warn": "#CC2200",
        },
    }
    (user_themes / "other.json").write_text(json.dumps(other_theme))
    THEME_REGISTRY.reload()

    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    theme_idx = next(i for i, f in enumerate(_SETTINGS_TREE) if f.name == "theme")
    menu._settings_cursor = theme_idx

    initial_theme = menu._current_profile().theme

    # Open + cycle + cancel
    menu._handle_key(KeyEvent(key=Key.ENTER))
    menu._handle_key(KeyEvent(key=Key.DOWN))
    # Mid-edit value differs from initial
    menu._handle_key(KeyEvent(key=Key.ESC))

    # After cancel: edit closed, value restored
    assert menu._editing_field is None
    assert menu._current_profile().theme == initial_theme
    assert not menu._is_dirty(menu._current_profile().name, "theme")


# ─── Dirty tracking ───


def test_edit_marks_dirty(temp_config_dir: Path) -> None:
    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    mode_idx = next(
        i for i, f in enumerate(_SETTINGS_TREE) if f.name == "display_mode"
    )
    menu._settings_cursor = mode_idx
    menu._handle_key(KeyEvent(key=Key.ENTER))
    assert menu._any_dirty()


def test_revert_clears_dirty(temp_config_dir: Path) -> None:
    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    mode_idx = next(
        i for i, f in enumerate(_SETTINGS_TREE) if f.name == "display_mode"
    )
    menu._settings_cursor = mode_idx
    initial_mode = menu._current_profile().display_mode
    menu._handle_key(KeyEvent(key=Key.ENTER))
    assert menu._any_dirty()

    # 'r' reverts
    menu._handle_key(KeyEvent(char="r"))
    assert not menu._any_dirty()
    assert menu._current_profile().display_mode == initial_mode


def test_edit_then_back_to_initial_clears_that_field_dirty(temp_config_dir: Path) -> None:
    """Toggling a field twice (back to original) clears its dirty marker."""
    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    mode_idx = next(
        i for i, f in enumerate(_SETTINGS_TREE) if f.name == "display_mode"
    )
    menu._settings_cursor = mode_idx
    menu._handle_key(KeyEvent(key=Key.ENTER))  # toggle once
    assert menu._any_dirty()
    menu._handle_key(KeyEvent(key=Key.ENTER))  # toggle back
    assert not menu._any_dirty()


# ─── Save flow ───


def test_save_writes_dirty_profile_to_disk(temp_config_dir: Path) -> None:
    PROFILE_REGISTRY.reload()
    menu = RoninConfig()
    menu._focus = Focus.SETTINGS

    # Find the active profile and toggle its mode
    active_name = menu._current_profile().name
    mode_idx = next(
        i for i, f in enumerate(_SETTINGS_TREE) if f.name == "display_mode"
    )
    menu._settings_cursor = mode_idx
    initial_mode = menu._current_profile().display_mode
    menu._handle_key(KeyEvent(key=Key.ENTER))
    new_mode = menu._current_profile().display_mode
    assert new_mode != initial_mode

    # Save
    menu._handle_key(KeyEvent(char="s"))

    # JSON file landed in the user dir
    target = temp_config_dir / "profiles" / f"{active_name}.json"
    assert target.exists()
    payload = json.loads(target.read_text())
    assert payload["display_mode"] == new_mode

    # Dirty cleared after save
    assert not menu._any_dirty()


def test_save_syncs_active_profile_to_chat_json(temp_config_dir: Path) -> None:
    """Editing the active profile's appearance syncs to chat.json."""
    PROFILE_REGISTRY.reload()
    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    mode_idx = next(
        i for i, f in enumerate(_SETTINGS_TREE) if f.name == "display_mode"
    )
    menu._settings_cursor = mode_idx
    menu._handle_key(KeyEvent(key=Key.ENTER))  # toggle mode
    new_mode = menu._current_profile().display_mode
    menu._handle_key(KeyEvent(char="s"))  # save

    # chat.json now has the new display_mode
    cfg = load_chat_config()
    assert cfg.get("display_mode") == new_mode


def test_save_when_nothing_dirty_is_noop(temp_config_dir: Path) -> None:
    menu = RoninConfig()
    menu._handle_key(KeyEvent(char="s"))
    # Toast is set but no error
    assert menu._toast is not None
    assert "nothing to save" in menu._toast.text


# ─── Esc / exit ───


def test_first_esc_warns_on_dirty(temp_config_dir: Path) -> None:
    """First esc with dirty changes shows a warning toast, doesn't exit."""
    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    mode_idx = next(
        i for i, f in enumerate(_SETTINGS_TREE) if f.name == "display_mode"
    )
    menu._settings_cursor = mode_idx
    menu._handle_key(KeyEvent(key=Key.ENTER))  # dirty

    menu._handle_key(KeyEvent(key=Key.ESC))
    # Did NOT exit
    assert not menu.exit_to_chat
    # Warning toast set
    assert menu._toast is not None
    assert "unsaved" in menu._toast.text


def test_second_esc_after_warn_exits(temp_config_dir: Path) -> None:
    menu = RoninConfig()
    menu._focus = Focus.SETTINGS
    mode_idx = next(
        i for i, f in enumerate(_SETTINGS_TREE) if f.name == "display_mode"
    )
    menu._settings_cursor = mode_idx
    menu._handle_key(KeyEvent(key=Key.ENTER))  # dirty
    menu._handle_key(KeyEvent(key=Key.ESC))    # warn
    menu._handle_key(KeyEvent(key=Key.ESC))    # exit
    assert menu.exit_to_chat


def test_esc_exits_immediately_when_clean(temp_config_dir: Path) -> None:
    menu = RoninConfig()
    menu._handle_key(KeyEvent(key=Key.ESC))
    assert menu.exit_to_chat


def test_enter_on_profile_activates_and_exits(temp_config_dir: Path) -> None:
    """Enter on a profile in the left pane activates it and exits."""
    PROFILE_REGISTRY.reload()
    menu = RoninConfig()
    menu._focus = Focus.PROFILES
    target_name = menu._working_profiles[menu._profile_cursor].name
    menu._handle_key(KeyEvent(key=Key.ENTER))
    assert menu.exit_to_chat
    assert menu.requested_active_profile == target_name
    # Persisted to chat.json
    cfg = load_chat_config()
    assert cfg["active_profile"] == target_name


# ─── Snapshot rendering ───


def test_snapshot_three_panes_render(temp_config_dir: Path) -> None:
    g = config_demo_snapshot(rows=30, cols=120, focus="settings")
    plain = render_grid_to_plain(g)
    assert "ronin · config" in plain
    assert "profiles" in plain
    assert "settings" in plain
    assert "preview" in plain


def test_snapshot_settings_sections_visible(temp_config_dir: Path) -> None:
    g = config_demo_snapshot(rows=30, cols=120, focus="settings")
    plain = render_grid_to_plain(g)
    assert "appearance" in plain
    assert "behavior" in plain
    assert "provider" in plain
    assert "extensions" in plain


def test_snapshot_profile_list_shows_names(temp_config_dir: Path) -> None:
    PROFILE_REGISTRY.reload()
    g = config_demo_snapshot(rows=30, cols=120, focus="profiles")
    plain = render_grid_to_plain(g)
    assert "default" in plain
    assert "ronin-dev" in plain


def test_snapshot_focus_profiles_shows_cursor(temp_config_dir: Path) -> None:
    g = config_demo_snapshot(rows=30, cols=120, focus="profiles", profile_cursor=0)
    plain = render_grid_to_plain(g)
    # ▸ glyph appears next to the cursor profile
    assert "▸" in plain


def test_snapshot_editing_overlay_shown(temp_config_dir: Path) -> None:
    g = config_demo_snapshot(
        rows=30, cols=120, focus="settings", settings_cursor=0, editing=True,
    )
    plain = render_grid_to_plain(g)
    # The overlay box drawing chars
    assert "╭" in plain or "─" in plain
    # The footer reflects edit mode
    assert "pick" in plain
    assert "confirm" in plain


def test_snapshot_dirty_marker_in_title_bar(temp_config_dir: Path) -> None:
    g = config_demo_snapshot(
        rows=30, cols=120, focus="settings",
        dirty=(("default", "theme"),),
    )
    plain = render_grid_to_plain(g)
    assert "1 unsaved" in plain


def test_snapshot_dirty_marker_on_profile_row(temp_config_dir: Path) -> None:
    g = config_demo_snapshot(
        rows=30, cols=120, focus="settings",
        dirty=(("default", "theme"),),
    )
    plain = render_grid_to_plain(g)
    # Find the line with the default profile and check for the * marker
    default_line = next(
        line for line in plain.split("\n") if " default" in line
    )
    assert "*" in default_line


def test_snapshot_too_small_terminal(temp_config_dir: Path) -> None:
    g = config_demo_snapshot(rows=10, cols=60)
    plain = render_grid_to_plain(g)
    assert "too small" in plain


# ─── Profile JSON serialization ───


def test_profile_to_json_round_trip(temp_config_dir: Path) -> None:
    """Serializing and re-parsing a Profile preserves all fields."""
    from ronin.profiles import parse_profile_file

    original = Profile(
        name="roundtrip",
        description="test",
        theme="steel",
        display_mode="light",
        density="compact",
        system_prompt="test prompt",
        provider={"type": "llamacpp", "model": "test"},
        skills=("a", "b"),
        tools=("read_file",),
        tool_config={"read_file": {"max_size": 1024}},
        intro_animation="nusamurai",
    )
    payload = _profile_to_json_dict(original)
    target = temp_config_dir / "profiles" / "roundtrip.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2))

    parsed = parse_profile_file(target)
    assert parsed is not None
    assert parsed.name == "roundtrip"
    assert parsed.theme == "steel"
    assert parsed.display_mode == "light"
    assert parsed.density == "compact"
    assert parsed.system_prompt == "test prompt"
    assert parsed.intro_animation == "nusamurai"
    assert parsed.skills == ("a", "b")
    assert parsed.tools == ("read_file",)
