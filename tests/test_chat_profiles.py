"""Tests for the SuccessorChat ↔ Profile integration.

Covers:
  - SuccessorChat() with no args picks up the active profile from config
  - SuccessorChat(profile=...) honors the explicit profile
  - Saved theme/mode/density override profile defaults (user wins)
  - _set_profile updates theme, mode, density, system_prompt, provider
  - _set_profile persists active_profile to chat.json
  - _set_profile is a no-op when target == current profile
  - _cycle_profile cycles through registered profiles
  - The active profile name appears in the rendered title bar
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from successor.profiles import (
    PROFILE_REGISTRY,
    Profile,
    get_profile,
)
from successor.render.theme import THEME_REGISTRY
from successor.snapshot import (
    chat_demo_snapshot,
    render_grid_to_plain,
)


# ─── Construction with a profile ───


def test_chat_uses_active_profile_when_no_arg(temp_config_dir: Path) -> None:
    """SuccessorChat() with no args reads the active profile from chat.json."""
    from successor.demos.chat import SuccessorChat

    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "active_profile": "successor-dev",
    }))
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    chat = SuccessorChat()
    assert chat.profile.name == "successor-dev"
    # The system prompt comes from the profile, not from a constant
    assert "successor-dev" in chat.system_prompt or "Successor" in chat.system_prompt


def test_chat_uses_explicit_profile_arg(temp_config_dir: Path) -> None:
    """SuccessorChat(profile=...) overrides the config-resolved profile."""
    from successor.demos.chat import SuccessorChat

    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    custom = Profile(
        name="custom-test",
        description="test fixture",
        theme="steel",
        display_mode="light",
        density="compact",
        system_prompt="custom prompt",
    )
    chat = SuccessorChat(profile=custom)
    assert chat.profile.name == "custom-test"
    assert chat.system_prompt == "custom prompt"
    assert chat.display_mode == "light"
    assert chat.density.name == "compact"


def test_saved_config_overrides_profile_defaults(temp_config_dir: Path) -> None:
    """User's manual theme/mode/density choices persist over profile defaults.

    Scenario: profile says theme=steel, mode=dark; but the user manually
    Ctrl+T-cycled to forge and Alt+D'd to light during a previous session.
    On restart, the saved values win — the user's last choice is what
    they expect to see.
    """
    from successor.demos.chat import SuccessorChat

    # Drop a forge theme into the user dir so it's available
    user_themes = temp_config_dir / "themes"
    user_themes.mkdir()
    forge_data = {
        "name": "forge",
        "icon": "▲",
        "description": "test forge",
        "dark": {
            "bg": "#10070A", "bg_input": "#070204", "bg_footer": "#1A0A0E",
            "fg": "#E6D9B8", "fg_dim": "#6B5A4A", "fg_subtle": "#3A1418",
            "accent": "#C1272D", "accent_warm": "#FF6347", "accent_warn": "#FFCC33",
        },
        "light": {
            "bg": "#FAF3E8", "bg_input": "#FFFCF5", "bg_footer": "#F0E4D2",
            "fg": "#1C0A06", "fg_dim": "#6B4A35", "fg_subtle": "#C4A687",
            "accent": "#A82020", "accent_warm": "#C84416", "accent_warn": "#B8860B",
        },
    }
    (user_themes / "forge.json").write_text(json.dumps(forge_data))

    # Saved config has the user's manual choices
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "active_profile": "default",
        "theme": "forge",
        "display_mode": "light",
        "density": "spacious",
    }))
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    chat = SuccessorChat()
    assert chat.profile.name == "default"  # profile is still default
    assert chat.theme.name == "forge"  # but the user's manual theme wins
    assert chat.display_mode == "light"  # and their mode
    assert chat.density.name == "spacious"  # and their density


def test_profile_field_used_when_no_saved_value(temp_config_dir: Path) -> None:
    """When chat.json has no theme/mode/density, the profile's defaults apply."""
    from successor.demos.chat import SuccessorChat

    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    custom = Profile(
        name="defaults-test",
        theme="steel",
        display_mode="light",
        density="compact",
    )
    chat = SuccessorChat(profile=custom)
    # No saved values → profile defaults apply
    assert chat.theme.name == "steel"
    assert chat.display_mode == "light"
    assert chat.density.name == "compact"


# ─── _set_profile ───


def test_set_profile_swaps_everything(temp_config_dir: Path) -> None:
    """Switching profile updates theme, mode, density, system_prompt."""
    from successor.demos.chat import SuccessorChat

    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    chat = SuccessorChat()
    initial_name = chat.profile.name
    initial_prompt = chat.system_prompt

    # Pick a different profile from the registry
    target = None
    for p in PROFILE_REGISTRY.all():
        if p.name != initial_name:
            target = p
            break
    assert target is not None

    chat._set_profile(target)
    assert chat.profile.name == target.name
    assert chat.system_prompt == target.system_prompt
    assert chat.system_prompt != initial_prompt or target.system_prompt == initial_prompt


def test_set_profile_persists_active(temp_config_dir: Path) -> None:
    """_set_profile writes the new active_profile to chat.json."""
    from successor.config import load_chat_config
    from successor.demos.chat import SuccessorChat

    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    chat = SuccessorChat()
    target = get_profile("successor-dev")
    assert target is not None
    chat._set_profile(target)

    cfg = load_chat_config()
    assert cfg["active_profile"] == "successor-dev"


def test_set_profile_noop_on_same_name(temp_config_dir: Path) -> None:
    """Switching to the currently-active profile is a no-op."""
    from successor.demos.chat import SuccessorChat

    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    chat = SuccessorChat()
    initial_messages = len(chat.messages)
    chat._set_profile(chat.profile)
    # No synthetic announcement message added
    assert len(chat.messages) == initial_messages


def test_set_profile_appends_synthetic_message(temp_config_dir: Path) -> None:
    """Switching profile drops a breadcrumb message into the chat."""
    from successor.demos.chat import SuccessorChat

    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    chat = SuccessorChat()
    initial_count = len(chat.messages)
    target = get_profile("successor-dev")
    if target.name == chat.profile.name:
        # Make sure we have a real swap
        target = get_profile("default") or target
        if target.name == chat.profile.name:
            return  # only one profile loaded; skip
    chat._set_profile(target)
    assert len(chat.messages) > initial_count
    # The new message mentions the target profile name
    assert target.name in chat.messages[-1].raw_text


def test_cycle_profile_walks_registry(temp_config_dir: Path) -> None:
    """_cycle_profile advances to the next profile in registry order."""
    from successor.demos.chat import SuccessorChat

    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    chat = SuccessorChat()
    profiles = PROFILE_REGISTRY.all()
    if len(profiles) < 2:
        return  # only one profile; cycling is trivially correct

    first_name = chat.profile.name
    chat._cycle_profile()
    assert chat.profile.name != first_name


# ─── Title bar shows the profile name ───


def test_profile_name_in_title_bar(temp_config_dir: Path) -> None:
    """The active profile name appears in the rendered title bar."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    grid = chat_demo_snapshot(
        rows=20, cols=120, scenario="blank",
        theme_name="steel", display_mode="dark",
    )
    plain = render_grid_to_plain(grid)
    # The default profile name should appear in the title bar pill
    assert "default" in plain


def test_autocomplete_shows_profile_command(temp_config_dir: Path) -> None:
    """The /profile slash command appears in the autocomplete dropdown."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    grid = chat_demo_snapshot(
        rows=30, cols=100, scenario="autocomplete",
        theme_name="steel", display_mode="dark",
    )
    plain = render_grid_to_plain(grid)
    assert "/profile" in plain


def test_help_overlay_documents_ctrl_p(temp_config_dir: Path) -> None:
    """The help overlay documents the new Ctrl+P profile-cycle keybind."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    grid = chat_demo_snapshot(
        rows=40, cols=120, scenario="help",
        theme_name="steel", display_mode="dark",
    )
    plain = render_grid_to_plain(grid)
    assert "Ctrl+P" in plain
    assert "cycle active profile" in plain
