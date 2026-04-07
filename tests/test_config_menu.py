"""Tests for the SuccessorConfig three-pane menu.

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

from successor.config import load_chat_config
from successor.input.keys import Key, KeyEvent
from successor.profiles import PROFILE_REGISTRY, Profile, get_profile
from successor.render.theme import THEME_REGISTRY
from successor.snapshot import config_demo_snapshot, render_grid_to_plain
from successor.input.keys import MOD_CTRL
from successor.wizard.config import (
    FieldKind,
    Focus,
    SuccessorConfig,
    _SETTINGS_TREE,
    _profile_to_json_dict,
    run_config_menu,
)
from successor.wizard.prompt_editor import PromptEditor


def _field_idx(name: str) -> int:
    """Find the index of a field by its name in the settings tree."""
    return next(i for i, f in enumerate(_SETTINGS_TREE) if f.name == name)


# ─── Construction ───


def test_construct_loads_profiles(temp_config_dir: Path) -> None:
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    menu = SuccessorConfig()
    assert len(menu._working_profiles) >= 2  # default + successor-dev builtins
    assert len(menu._initial_profiles) == len(menu._working_profiles)


def test_construct_starts_on_active_profile(temp_config_dir: Path) -> None:
    """The profile cursor starts on whichever profile is active in chat.json."""
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "active_profile": "successor-dev",
    }))
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    menu = SuccessorConfig()
    assert menu._working_profiles[menu._profile_cursor].name == "successor-dev"


def test_construct_has_no_dirty(temp_config_dir: Path) -> None:
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    menu = SuccessorConfig()
    assert not menu._any_dirty()


def test_settings_cursor_starts_on_editable_row(temp_config_dir: Path) -> None:
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    menu = SuccessorConfig()
    assert _SETTINGS_TREE[menu._settings_cursor].kind.value != "readonly"


# ─── Navigation ───


def test_tab_cycles_focus(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    initial = menu._focus
    menu._handle_key(KeyEvent(key=Key.TAB))
    assert menu._focus != initial
    menu._handle_key(KeyEvent(key=Key.TAB))
    assert menu._focus == initial


def test_settings_arrows_skip_readonly(temp_config_dir: Path) -> None:
    """Up/Down in the settings pane only land on editable fields."""
    menu = SuccessorConfig()
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
    menu = SuccessorConfig()
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
    menu = SuccessorConfig()
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
    menu = SuccessorConfig()
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


def test_toggle_intro_flips_none_to_successor(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
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
    menu = SuccessorConfig()
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

    menu = SuccessorConfig()
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
    menu = SuccessorConfig()
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

    menu = SuccessorConfig()
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
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    mode_idx = next(
        i for i, f in enumerate(_SETTINGS_TREE) if f.name == "display_mode"
    )
    menu._settings_cursor = mode_idx
    menu._handle_key(KeyEvent(key=Key.ENTER))
    assert menu._any_dirty()


def test_revert_clears_dirty(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
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
    menu = SuccessorConfig()
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
    menu = SuccessorConfig()
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
    menu = SuccessorConfig()
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
    menu = SuccessorConfig()
    menu._handle_key(KeyEvent(char="s"))
    # Toast is set but no error
    assert menu._toast is not None
    assert "nothing to save" in menu._toast.text


# ─── Esc / exit ───


def test_first_esc_warns_on_dirty(temp_config_dir: Path) -> None:
    """First esc with dirty changes shows a warning toast, doesn't exit."""
    menu = SuccessorConfig()
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
    menu = SuccessorConfig()
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
    menu = SuccessorConfig()
    menu._handle_key(KeyEvent(key=Key.ESC))
    assert menu.exit_to_chat


def test_enter_on_profile_activates_and_exits(temp_config_dir: Path) -> None:
    """Enter on a profile in the left pane activates it and exits."""
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
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
    assert "successor · config" in plain
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
    assert "successor-dev" in plain


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


# ─── Inline TEXT field editing ───


def test_text_field_opens_inline_edit(temp_config_dir: Path) -> None:
    """Pressing Enter on a TEXT field opens the inline text editor."""
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_model")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    assert menu._inline_text_edit is not None
    assert menu._inline_text_edit.kind == FieldKind.TEXT


def test_text_field_buffer_starts_with_current_value(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_model")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    current_model = menu._current_profile().provider.get("model", "")
    assert menu._inline_text_edit.buffer == current_model
    assert menu._inline_text_edit.cursor == len(current_model)


def test_text_field_typing_appends(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_model")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    for ch in "_v2":
        menu._handle_key(KeyEvent(char=ch))
    assert menu._inline_text_edit.buffer.endswith("_v2")


def test_text_field_backspace_deletes(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_model")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    initial_len = len(menu._inline_text_edit.buffer)
    menu._handle_key(KeyEvent(key=Key.BACKSPACE))
    menu._handle_key(KeyEvent(key=Key.BACKSPACE))
    assert len(menu._inline_text_edit.buffer) == initial_len - 2


def test_text_field_left_right_moves_cursor(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_model")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    end_pos = menu._inline_text_edit.cursor
    menu._handle_key(KeyEvent(key=Key.LEFT))
    assert menu._inline_text_edit.cursor == end_pos - 1
    menu._handle_key(KeyEvent(key=Key.HOME))
    assert menu._inline_text_edit.cursor == 0
    menu._handle_key(KeyEvent(key=Key.END))
    assert menu._inline_text_edit.cursor == end_pos


def test_text_field_enter_commits(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_model")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    while menu._inline_text_edit.cursor > 0:
        menu._handle_key(KeyEvent(key=Key.BACKSPACE))
    for ch in "newmodel":
        menu._handle_key(KeyEvent(char=ch))
    menu._handle_key(KeyEvent(key=Key.ENTER))

    assert menu._inline_text_edit is None
    assert menu._current_profile().provider["model"] == "newmodel"
    assert menu._is_dirty(menu._current_profile().name, "provider_model")


def test_text_field_esc_cancels(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_model")
    original = menu._current_profile().provider.get("model")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    for ch in "garbage":
        menu._handle_key(KeyEvent(char=ch))
    menu._handle_key(KeyEvent(key=Key.ESC))

    assert menu._inline_text_edit is None
    assert menu._current_profile().provider.get("model") == original
    assert not menu._is_dirty(menu._current_profile().name, "provider_model")


# ─── NUMBER field editing ───


def test_number_field_filters_letters(temp_config_dir: Path) -> None:
    """Typing letters into a NUMBER field is silently rejected."""
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_temperature")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    while menu._inline_text_edit.cursor > 0:
        menu._handle_key(KeyEvent(key=Key.BACKSPACE))
    for ch in "1.5xyz":
        menu._handle_key(KeyEvent(char=ch))
    assert menu._inline_text_edit.buffer == "1.5"


def test_number_int_field_rejects_decimal(temp_config_dir: Path) -> None:
    """Typing '.' into an int field is filtered."""
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_max_tokens")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    while menu._inline_text_edit.cursor > 0:
        menu._handle_key(KeyEvent(key=Key.BACKSPACE))
    for ch in "100.5":
        menu._handle_key(KeyEvent(char=ch))
    assert menu._inline_text_edit.buffer == "1005"


def test_number_field_commits_parsed_float(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_temperature")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    while menu._inline_text_edit.cursor > 0:
        menu._handle_key(KeyEvent(key=Key.BACKSPACE))
    for ch in "0.42":
        menu._handle_key(KeyEvent(char=ch))
    menu._handle_key(KeyEvent(key=Key.ENTER))

    assert menu._inline_text_edit is None
    value = menu._current_profile().provider["temperature"]
    assert isinstance(value, float)
    assert value == 0.42


def test_number_int_field_commits_int(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_max_tokens")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    while menu._inline_text_edit.cursor > 0:
        menu._handle_key(KeyEvent(key=Key.BACKSPACE))
    for ch in "65536":
        menu._handle_key(KeyEvent(char=ch))
    menu._handle_key(KeyEvent(key=Key.ENTER))

    value = menu._current_profile().provider["max_tokens"]
    assert isinstance(value, int)
    assert value == 65536


def test_number_field_invalid_buffer_warns(temp_config_dir: Path) -> None:
    """Empty buffer on Enter triggers a warning toast and stays in edit mode."""
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_temperature")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    while menu._inline_text_edit.cursor > 0:
        menu._handle_key(KeyEvent(key=Key.BACKSPACE))
    menu._handle_key(KeyEvent(key=Key.ENTER))

    assert menu._inline_text_edit is not None
    assert menu._toast is not None
    assert menu._toast.kind == "warn"


# ─── SECRET field editing ───


def test_secret_field_displays_masked(temp_config_dir: Path) -> None:
    """A non-empty SECRET field renders as bullets in the display."""
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_api_key")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    for ch in "sk-secret-key":
        menu._handle_key(KeyEvent(char=ch))
    menu._handle_key(KeyEvent(key=Key.ENTER))

    field = _SETTINGS_TREE[_field_idx("provider_api_key")]
    display = menu._profile_value_for_field(menu._current_profile(), field)
    assert "•" in display
    assert "sk-secret-key" not in display


def test_secret_field_editing_shows_plaintext(temp_config_dir: Path) -> None:
    """While editing, the buffer is plaintext (so the user can verify)."""
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_api_key")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    for ch in "sk-test":
        menu._handle_key(KeyEvent(char=ch))
    assert menu._inline_text_edit.buffer == "sk-test"


def test_secret_field_commits_to_provider_dict(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_api_key")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    for ch in "sk-12345":
        menu._handle_key(KeyEvent(char=ch))
    menu._handle_key(KeyEvent(key=Key.ENTER))

    assert menu._current_profile().provider["api_key"] == "sk-12345"


# ─── Provider type CYCLE field ───


def test_provider_type_field_opens_cycle_overlay(temp_config_dir: Path) -> None:
    """The provider_type field is now a CYCLE."""
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("provider_type")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    assert menu._editing_field == _field_idx("provider_type")


# ─── PromptEditor — multiline editor unit tests ───


def test_prompt_editor_initial_state() -> None:
    ed = PromptEditor("hello\nworld")
    assert ed.lines == ["hello", "world"]
    assert ed.cursor_row == 0
    assert ed.cursor_col == 0
    assert not ed.is_done
    assert not ed.is_dirty


def test_prompt_editor_empty_initial_has_one_line() -> None:
    ed = PromptEditor("")
    assert ed.lines == [""]


def test_prompt_editor_insert_char() -> None:
    ed = PromptEditor("ab")
    ed.cursor_col = 1
    ed.handle_key(KeyEvent(char="X"))
    assert ed.lines[0] == "aXb"
    assert ed.cursor_col == 2
    assert ed.is_dirty


def test_prompt_editor_newline_splits_line() -> None:
    ed = PromptEditor("hello world")
    ed.cursor_col = 5
    ed.handle_key(KeyEvent(key=Key.ENTER))
    assert ed.lines == ["hello", " world"]
    assert ed.cursor_row == 1
    assert ed.cursor_col == 0


def test_prompt_editor_backspace_within_line() -> None:
    ed = PromptEditor("hello")
    ed.cursor_col = 3
    ed.handle_key(KeyEvent(key=Key.BACKSPACE))
    assert ed.lines[0] == "helo"
    assert ed.cursor_col == 2


def test_prompt_editor_backspace_at_line_start_merges() -> None:
    ed = PromptEditor("hello\nworld")
    ed.cursor_row = 1
    ed.cursor_col = 0
    ed.handle_key(KeyEvent(key=Key.BACKSPACE))
    assert ed.lines == ["helloworld"]
    assert ed.cursor_row == 0
    assert ed.cursor_col == 5


def test_prompt_editor_delete_within_line() -> None:
    ed = PromptEditor("hello")
    ed.cursor_col = 2
    ed.handle_key(KeyEvent(key=Key.DELETE))
    assert ed.lines[0] == "helo"
    assert ed.cursor_col == 2


def test_prompt_editor_delete_at_line_end_merges_next() -> None:
    ed = PromptEditor("hello\nworld")
    ed.cursor_row = 0
    ed.cursor_col = 5
    ed.handle_key(KeyEvent(key=Key.DELETE))
    assert ed.lines == ["helloworld"]


def test_prompt_editor_arrow_left_at_line_start_wraps_up() -> None:
    ed = PromptEditor("hello\nworld")
    ed.cursor_row = 1
    ed.cursor_col = 0
    ed.handle_key(KeyEvent(key=Key.LEFT))
    assert ed.cursor_row == 0
    assert ed.cursor_col == 5


def test_prompt_editor_arrow_right_at_line_end_wraps_down() -> None:
    ed = PromptEditor("hello\nworld")
    ed.cursor_row = 0
    ed.cursor_col = 5
    ed.handle_key(KeyEvent(key=Key.RIGHT))
    assert ed.cursor_row == 1
    assert ed.cursor_col == 0


def test_prompt_editor_up_clamps_col() -> None:
    """Up arrow clamps cursor col to the new line's length."""
    ed = PromptEditor("hi\nlonger line")
    ed.cursor_row = 1
    ed.cursor_col = 10
    ed.handle_key(KeyEvent(key=Key.UP))
    assert ed.cursor_row == 0
    assert ed.cursor_col == 2


def test_prompt_editor_ctrl_s_commits() -> None:
    ed = PromptEditor("original")
    ed.cursor_col = 8
    for ch in " EDITED":
        ed.handle_key(KeyEvent(char=ch))
    ed.handle_key(KeyEvent(char="s", mods=MOD_CTRL))
    assert ed.is_done
    assert ed.result == "original EDITED"


def test_prompt_editor_esc_cancels() -> None:
    ed = PromptEditor("original")
    for ch in "WILL_DISCARD":
        ed.handle_key(KeyEvent(char=ch))
    ed.handle_key(KeyEvent(key=Key.ESC))
    assert ed.is_done
    assert ed.result is None


def test_prompt_editor_char_count() -> None:
    ed = PromptEditor("hello\nworld")
    assert ed.char_count() == 11


def test_prompt_editor_home_end() -> None:
    ed = PromptEditor("hello world")
    ed.cursor_col = 5
    ed.handle_key(KeyEvent(key=Key.HOME))
    assert ed.cursor_col == 0
    ed.handle_key(KeyEvent(key=Key.END))
    assert ed.cursor_col == 11


def test_prompt_editor_dirty_tracking() -> None:
    ed = PromptEditor("hello")
    assert not ed.is_dirty
    ed.handle_key(KeyEvent(char="X"))
    assert ed.is_dirty
    ed.handle_key(KeyEvent(key=Key.BACKSPACE))
    assert not ed.is_dirty


# ─── Config menu MULTILINE integration ───


def test_multiline_field_opens_prompt_editor(temp_config_dir: Path) -> None:
    """Pressing Enter on the system_prompt field opens the prompt editor."""
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("system_prompt")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    assert menu._prompt_editor is not None
    expected_lines = menu._current_profile().system_prompt.split("\n")
    assert menu._prompt_editor.lines == expected_lines


def test_multiline_save_commits_prompt_to_profile(temp_config_dir: Path) -> None:
    """Ctrl+S in the prompt editor commits the new text to the profile."""
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("system_prompt")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    ed = menu._prompt_editor
    ed.cursor_row = len(ed.lines) - 1
    ed.cursor_col = len(ed.lines[-1])
    for ch in "TESTED":
        menu._handle_key(KeyEvent(char=ch))
    menu._handle_key(KeyEvent(char="s", mods=MOD_CTRL))

    assert menu._prompt_editor is None
    assert menu._current_profile().system_prompt.endswith("TESTED")
    assert menu._is_dirty(menu._current_profile().name, "system_prompt")


def test_multiline_esc_cancels(temp_config_dir: Path) -> None:
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("system_prompt")
    original = menu._current_profile().system_prompt
    menu._handle_key(KeyEvent(key=Key.ENTER))
    for ch in "WILL_DISCARD":
        menu._handle_key(KeyEvent(char=ch))
    menu._handle_key(KeyEvent(key=Key.ESC))

    assert menu._prompt_editor is None
    assert menu._current_profile().system_prompt == original
    assert not menu._is_dirty(menu._current_profile().name, "system_prompt")


def test_multiline_save_persists_to_disk(temp_config_dir: Path) -> None:
    """Edit prompt, Ctrl+S in editor, then s in menu — JSON file has new prompt."""
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.SETTINGS
    active_name = menu._current_profile().name
    menu._settings_cursor = _field_idx("system_prompt")
    menu._handle_key(KeyEvent(key=Key.ENTER))

    ed = menu._prompt_editor
    # Move cursor to end of document, then backspace until empty
    ed.cursor_row = len(ed.lines) - 1
    ed.cursor_col = len(ed.lines[-1])
    while ed.lines != [""]:
        ed.handle_key(KeyEvent(key=Key.BACKSPACE))
    for ch in "you are a custom assistant":
        ed.handle_key(KeyEvent(char=ch))
    menu._handle_key(KeyEvent(char="s", mods=MOD_CTRL))

    menu._handle_key(KeyEvent(char="s"))

    target = temp_config_dir / "profiles" / f"{active_name}.json"
    assert target.exists()
    payload = json.loads(target.read_text())
    assert payload["system_prompt"] == "you are a custom assistant"


# ─── Profile JSON round-trip ───


def test_profile_to_json_round_trip(temp_config_dir: Path) -> None:
    """Serializing and re-parsing a Profile preserves all fields."""
    from successor.profiles import parse_profile_file

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
        intro_animation="successor",
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
    assert parsed.intro_animation == "successor"
    assert parsed.skills == ("a", "b")
    assert parsed.tools == ("read_file",)


# ─── Delete profile flow ───


def _drop_user_profile(temp_config_dir: Path, name: str, **overrides: object) -> Path:
    """Helper — write a minimal user profile JSON to the temp config dir."""
    payload = {
        "name": name,
        "description": "test profile",
        "theme": "steel",
        "display_mode": "dark",
        "density": "normal",
        "system_prompt": "you are a test profile",
        "provider": {"type": "llamacpp", "model": "qwopus"},
        "skills": [],
        "tools": [],
        "tool_config": {},
        "intro_animation": None,
    }
    payload.update(overrides)
    profiles_dir = temp_config_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    target = profiles_dir / f"{name}.json"
    target.write_text(json.dumps(payload, indent=2))
    return target


def _select_profile(menu: SuccessorConfig, name: str) -> None:
    """Helper — move the profile cursor onto the named profile."""
    for i, p in enumerate(menu._working_profiles):
        if p.name == name:
            menu._profile_cursor = i
            menu._sync_preview()
            return
    raise AssertionError(f"profile {name!r} not in menu")


def test_delete_capital_d_opens_modal_for_user_profile(temp_config_dir: Path) -> None:
    """Capital D on a pure user profile arms the delete confirmation modal."""
    _drop_user_profile(temp_config_dir, "scratch")
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "scratch")
    menu._handle_key(KeyEvent(char="D"))

    assert menu._delete_confirm is not None
    assert menu._delete_confirm.profile_name == "scratch"
    assert menu._delete_confirm.mode == "delete"


def test_delete_lowercase_d_does_nothing(temp_config_dir: Path) -> None:
    """Lowercase d must not arm the modal — only capital D opens delete."""
    _drop_user_profile(temp_config_dir, "scratch")
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "scratch")
    menu._handle_key(KeyEvent(char="d"))
    assert menu._delete_confirm is None


def test_delete_refused_for_builtin_profile(temp_config_dir: Path) -> None:
    """Capital D on a pure built-in shows a warn toast and no modal."""
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    # successor-dev is built-in and NOT active (default is active)
    _select_profile(menu, "successor-dev")
    menu._handle_key(KeyEvent(char="D"))

    assert menu._delete_confirm is None
    assert menu._toast is not None
    assert menu._toast.kind == "warn"
    assert "built-in" in menu._toast.text


def test_delete_refused_for_active_profile(temp_config_dir: Path) -> None:
    """Capital D on the active profile (per chat.json) refuses with a toast."""
    _drop_user_profile(temp_config_dir, "scratch")
    # Activate scratch so it can't be deleted
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "active_profile": "scratch",
    }))
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "scratch")
    menu._handle_key(KeyEvent(char="D"))

    assert menu._delete_confirm is None
    assert menu._toast is not None
    assert menu._toast.kind == "warn"
    assert "active" in menu._toast.text


def test_delete_refused_when_only_one_profile(temp_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Last-profile guard — refuse to delete the only remaining profile."""
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    # Force the menu into a one-profile state
    menu._working_profiles = [menu._working_profiles[0]]
    menu._initial_profiles = [menu._initial_profiles[0]]
    menu._profile_cursor = 0
    menu._focus = Focus.PROFILES
    menu._handle_key(KeyEvent(char="D"))

    assert menu._delete_confirm is None
    assert menu._toast is not None
    assert menu._toast.kind == "warn"
    assert "last" in menu._toast.text


def test_delete_user_override_uses_revert_mode(temp_config_dir: Path) -> None:
    """Capital D on a user override of a built-in arms the modal in revert mode."""
    # successor-dev exists as a built-in; dropping a user file with the same
    # name creates an override.
    _drop_user_profile(
        temp_config_dir, "successor-dev",
        theme="steel", display_mode="light",
    )
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "successor-dev")
    menu._handle_key(KeyEvent(char="D"))

    assert menu._delete_confirm is not None
    assert menu._delete_confirm.mode == "revert"


def test_delete_modal_y_confirms_and_unlinks_file(temp_config_dir: Path) -> None:
    """Pressing Y on the modal unlinks the JSON file from disk."""
    target = _drop_user_profile(temp_config_dir, "scratch")
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "scratch")
    menu._handle_key(KeyEvent(char="D"))
    assert menu._delete_confirm is not None

    menu._handle_key(KeyEvent(char="y"))

    assert menu._delete_confirm is None
    assert not target.exists()
    # Registry no longer has it
    assert "scratch" not in PROFILE_REGISTRY.names()
    # Toast confirms the action
    assert menu._toast is not None
    assert menu._toast.kind == "ok"
    assert "deleted" in menu._toast.text


def test_delete_modal_lowercase_y_also_confirms(temp_config_dir: Path) -> None:
    """Y is case-insensitive."""
    target = _drop_user_profile(temp_config_dir, "scratch")
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "scratch")
    menu._handle_key(KeyEvent(char="D"))
    menu._handle_key(KeyEvent(char="Y"))
    assert not target.exists()


def test_delete_modal_n_cancels(temp_config_dir: Path) -> None:
    """N cancels the modal without touching disk."""
    target = _drop_user_profile(temp_config_dir, "scratch")
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "scratch")
    menu._handle_key(KeyEvent(char="D"))
    menu._handle_key(KeyEvent(char="n"))
    assert menu._delete_confirm is None
    assert target.exists()


def test_delete_modal_enter_cancels_safe_default(temp_config_dir: Path) -> None:
    """Enter cancels — destructive actions never default to confirm."""
    target = _drop_user_profile(temp_config_dir, "scratch")
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "scratch")
    menu._handle_key(KeyEvent(char="D"))
    menu._handle_key(KeyEvent(key=Key.ENTER))
    assert menu._delete_confirm is None
    assert target.exists()


def test_delete_modal_esc_cancels(temp_config_dir: Path) -> None:
    target = _drop_user_profile(temp_config_dir, "scratch")
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "scratch")
    menu._handle_key(KeyEvent(char="D"))
    menu._handle_key(KeyEvent(key=Key.ESC))
    assert menu._delete_confirm is None
    assert target.exists()


def test_delete_revert_unlinks_user_file_and_builtin_remains(temp_config_dir: Path) -> None:
    """Confirming a revert deletes the user override and the built-in shows again."""
    user_file = _drop_user_profile(
        temp_config_dir, "successor-dev",
        theme="steel", display_mode="light",
    )
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "successor-dev")
    menu._handle_key(KeyEvent(char="D"))
    assert menu._delete_confirm.mode == "revert"
    menu._handle_key(KeyEvent(char="y"))

    # User file gone
    assert not user_file.exists()
    # Built-in still in the registry
    assert "successor-dev" in PROFILE_REGISTRY.names()
    # The reloaded profile is the BUILT-IN, not the user override —
    # the built-in successor-dev is dark, the override we dropped was light
    p = get_profile("successor-dev")
    assert p is not None
    assert p.display_mode == "dark"
    # Toast says reverted, not deleted
    assert menu._toast is not None
    assert "reverted" in menu._toast.text


def test_delete_clears_dirty_for_that_profile(temp_config_dir: Path) -> None:
    """If the deleted profile had dirty edits, those dirty markers go too."""
    _drop_user_profile(temp_config_dir, "scratch")
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "scratch")
    # Toggle a field on scratch to dirty it
    menu._focus = Focus.SETTINGS
    menu._settings_cursor = _field_idx("display_mode")
    menu._handle_key(KeyEvent(key=Key.ENTER))
    assert menu._is_dirty("scratch", "display_mode")

    menu._focus = Focus.PROFILES
    menu._handle_key(KeyEvent(char="D"))
    menu._handle_key(KeyEvent(char="y"))

    assert not menu._is_dirty("scratch")
    assert not any(p == "scratch" for (p, _) in menu._dirty)


def test_delete_modal_blocks_other_input(temp_config_dir: Path) -> None:
    """While the delete modal is open, other keys (Tab, save, etc) do nothing."""
    _drop_user_profile(temp_config_dir, "scratch")
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "scratch")
    menu._handle_key(KeyEvent(char="D"))
    initial_focus = menu._focus

    menu._handle_key(KeyEvent(key=Key.TAB))
    assert menu._focus == initial_focus  # Tab swallowed
    menu._handle_key(KeyEvent(char="s"))
    assert menu._delete_confirm is not None  # save did nothing


def test_delete_cursor_lands_on_valid_row_after_delete(temp_config_dir: Path) -> None:
    """After deletion the profile cursor must point at a still-existing row."""
    _drop_user_profile(temp_config_dir, "scratch")
    PROFILE_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "scratch")
    menu._handle_key(KeyEvent(char="D"))
    menu._handle_key(KeyEvent(char="y"))

    assert 0 <= menu._profile_cursor < len(menu._working_profiles)
    # The cursor profile must be a real registered name
    cursor_name = menu._working_profiles[menu._profile_cursor].name
    assert cursor_name in PROFILE_REGISTRY.names()


def test_delete_modal_renders_without_crashing(temp_config_dir: Path) -> None:
    """Sanity-check the paint method actually runs against a real grid."""
    from successor.render.cells import Grid

    _drop_user_profile(temp_config_dir, "scratch")
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "scratch")
    menu._handle_key(KeyEvent(char="D"))

    g = Grid(30, 120)
    menu.on_tick(g)

    # Read out the painted text and look for the modal title
    from successor.snapshot import render_grid_to_plain
    plain = render_grid_to_plain(g)
    assert "delete profile?" in plain
    assert "scratch" in plain


def test_delete_revert_modal_says_revert(temp_config_dir: Path) -> None:
    """The revert-mode modal title says 'revert' not 'delete'."""
    from successor.render.cells import Grid

    _drop_user_profile(temp_config_dir, "successor-dev", theme="steel")
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES
    _select_profile(menu, "successor-dev")
    menu._handle_key(KeyEvent(char="D"))
    assert menu._delete_confirm.mode == "revert"

    g = Grid(30, 120)
    menu.on_tick(g)
    from successor.snapshot import render_grid_to_plain
    plain = render_grid_to_plain(g)
    assert "revert profile?" in plain
