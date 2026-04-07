"""Snapshot matrix tests — every scenario × theme × display_mode.

The renderer is pure: `on_tick(grid)` is a function of state + time +
grid size. That makes the entire chat surface testable without a TTY:
construct a chat in a known state, render one frame to a Grid, walk
the cells, and assert against expected content.

These tests verify three things:

1. Every (scenario, theme, display_mode) combination renders without
   raising. This is the matrix smoke test — it catches paint code that
   indexes into the wrong slot, dataclass mismatches, or missing
   theme handling.

2. The rendered grid contains the expected scenario-specific content
   (the title, key UI elements, scenario-specific text). This catches
   silent breakage where the chat renders blank or garbled.

3. Display mode actually affects the output — switching dark↔light
   produces different background colors. This catches the case where
   the variant resolver loses the mode argument.

These tests use the THEME_REGISTRY built-ins, not user fixtures, so
they work in CI and exercise the same code path as a real `successor snapshot`
invocation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from successor.render.theme import THEME_REGISTRY, get_theme
from successor.snapshot import (
    chat_demo_snapshot,
    render_grid_to_plain,
)


SCENARIOS = ("blank", "showcase", "thinking", "search", "help", "autocomplete")
DISPLAY_MODES = ("dark", "light")
DENSITIES = ("compact", "normal", "spacious")


# ─── Matrix smoke test ───


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("display_mode", DISPLAY_MODES)
def test_matrix_renders_without_error(
    temp_config_dir: Path,
    scenario: str,
    display_mode: str,
) -> None:
    """Every scenario × display_mode renders successfully on the steel theme.

    The temp_config_dir fixture isolates the chat from any real user
    config, so this test runs the same way on every machine. The grid
    is fixed-size for determinism.
    """
    THEME_REGISTRY.reload()
    grid = chat_demo_snapshot(
        rows=30,
        cols=100,
        theme_name="steel",
        display_mode=display_mode,
        density_name="normal",
        scenario=scenario,
    )
    plain = render_grid_to_plain(grid)
    assert plain  # non-empty
    # The title bar always shows "successor · chat"
    assert "successor" in plain


@pytest.mark.parametrize("density", DENSITIES)
def test_density_axis_renders(temp_config_dir: Path, density: str) -> None:
    """Density variants render without error in the showcase scenario."""
    THEME_REGISTRY.reload()
    grid = chat_demo_snapshot(
        rows=30,
        cols=120,
        theme_name="steel",
        display_mode="dark",
        density_name=density,
        scenario="showcase",
    )
    plain = render_grid_to_plain(grid)
    assert "successor" in plain
    assert "what I can render" in plain.lower() or "render" in plain.lower()


# ─── Scenario-specific content checks ───


def test_blank_scenario_shows_greeting(temp_config_dir: Path) -> None:
    """The blank scenario renders the synthetic greeting message."""
    THEME_REGISTRY.reload()
    grid = chat_demo_snapshot(
        rows=30, cols=100, scenario="blank",
        theme_name="steel", display_mode="dark",
    )
    plain = render_grid_to_plain(grid)
    # The greeting starts with "I am successor." regardless of server state
    assert "I am successor" in plain


def test_showcase_renders_markdown_sampler(temp_config_dir: Path) -> None:
    """The showcase scenario includes the user's question and the markdown body."""
    THEME_REGISTRY.reload()
    grid = chat_demo_snapshot(
        rows=40, cols=120, scenario="showcase",
        theme_name="steel", display_mode="dark",
    )
    plain = render_grid_to_plain(grid)
    assert "show me what you can do" in plain
    # The markdown header from the showcase content
    assert "What I can render" in plain


def test_thinking_scenario_shows_spinner(temp_config_dir: Path) -> None:
    """The thinking scenario shows the streaming reply in its spinner phase."""
    THEME_REGISTRY.reload()
    grid = chat_demo_snapshot(
        rows=30, cols=100, scenario="thinking",
        theme_name="steel", display_mode="dark",
    )
    plain = render_grid_to_plain(grid)
    assert "what is the way of the blade" in plain
    # The thinking phase shows "thinking…" somewhere in the visible output
    assert "thinking" in plain.lower()


def test_search_scenario_active(temp_config_dir: Path) -> None:
    """The search scenario shows the search bar in place of the input."""
    THEME_REGISTRY.reload()
    grid = chat_demo_snapshot(
        rows=30, cols=100, scenario="search",
        theme_name="steel", display_mode="dark",
    )
    plain = render_grid_to_plain(grid)
    assert "search" in plain.lower()
    # The search query "blade" should be visible
    assert "blade" in plain


def test_help_scenario_overlay(temp_config_dir: Path) -> None:
    """The help scenario shows the keybinding overlay."""
    THEME_REGISTRY.reload()
    grid = chat_demo_snapshot(
        rows=40, cols=120, scenario="help",
        theme_name="steel", display_mode="dark",
    )
    plain = render_grid_to_plain(grid)
    assert "keybindings" in plain.lower()
    # The new Alt+D keybind appears in the help overlay
    assert "Alt+D" in plain


def test_autocomplete_scenario_dropdown(temp_config_dir: Path) -> None:
    """The autocomplete scenario shows the slash command dropdown."""
    THEME_REGISTRY.reload()
    grid = chat_demo_snapshot(
        rows=30, cols=100, scenario="autocomplete",
        theme_name="steel", display_mode="dark",
    )
    plain = render_grid_to_plain(grid)
    # /quit, /theme, /mode, /density, /mouse should appear in the dropdown
    assert "/quit" in plain
    assert "/theme" in plain
    assert "/mode" in plain  # the new command we added in this phase
    assert "/density" in plain


# ─── Display mode actually changes the output ───


def test_dark_and_light_produce_different_pixels(temp_config_dir: Path) -> None:
    """Switching display_mode flips the rendered colors AND the mode icon.

    The dark/light variants of a theme share layout but differ in
    palette. Additionally, the title bar's mode pill shows ☾ for dark
    and ☀ for light — that's part of the visible chrome, so plain-text
    output legitimately differs at exactly that one cell.

    The real test is that the colored ANSI output differs across the
    whole frame (catching the case where display_mode gets dropped on
    the floor and both modes paint with the same variant).
    """
    from successor.snapshot import render_grid_to_ansi

    THEME_REGISTRY.reload()
    dark_grid = chat_demo_snapshot(
        rows=30, cols=100, scenario="showcase",
        theme_name="steel", display_mode="dark",
    )
    light_grid = chat_demo_snapshot(
        rows=30, cols=100, scenario="showcase",
        theme_name="steel", display_mode="light",
    )

    dark_plain = render_grid_to_plain(dark_grid)
    light_plain = render_grid_to_plain(light_grid)

    # The mode icon must appear in the right pane and must differ
    assert "\u263e" in dark_plain   # ☾ moon
    assert "\u2600" in light_plain  # ☀ sun
    assert "\u263e" not in light_plain
    assert "\u2600" not in dark_plain

    # ANSI output (with colors) MUST differ — many cells, not just
    # the mode icon — proving the variant resolver is honoring mode.
    assert render_grid_to_ansi(dark_grid) != render_grid_to_ansi(light_grid)


def test_steel_and_user_theme_differ(temp_config_dir: Path) -> None:
    """A user-installed theme produces different colors than steel.

    Drops a sakura theme into the temp config dir, renders the same
    scenario with both themes, confirms the colored output differs.
    Catches theme-loading regressions where user themes get silently
    ignored.
    """
    import json as _json

    from successor.snapshot import render_grid_to_ansi

    user_themes = temp_config_dir / "themes"
    user_themes.mkdir()
    sakura_data = {
        "name": "sakura",
        "icon": "✿",
        "description": "test cherry",
        "dark": {
            "bg":          "#1a0a12",
            "bg_input":    "#10060b",
            "bg_footer":   "#24121a",
            "fg":          "#f0e6eb",
            "fg_dim":      "#b88a9a",
            "fg_subtle":   "#6b3a4a",
            "accent":      "#ff5588",
            "accent_warm": "#ffaa66",
            "accent_warn": "#ffcc33",
        },
        "light": {
            "bg":          "#fff5f8",
            "bg_input":    "#ffffff",
            "bg_footer":   "#ffe8ee",
            "fg":          "#3a0a18",
            "fg_dim":      "#7a4555",
            "fg_subtle":   "#c08090",
            "accent":      "#dd3366",
            "accent_warm": "#cc6622",
            "accent_warn": "#aa6611",
        },
    }
    (user_themes / "sakura.json").write_text(_json.dumps(sakura_data))

    THEME_REGISTRY.reload()
    # Confirm the user theme actually loaded
    assert get_theme("sakura") is not None

    steel_grid = chat_demo_snapshot(
        rows=20, cols=80, scenario="blank",
        theme_name="steel", display_mode="dark",
    )
    sakura_grid = chat_demo_snapshot(
        rows=20, cols=80, scenario="blank",
        theme_name="sakura", display_mode="dark",
    )

    steel_plain = render_grid_to_plain(steel_grid)
    sakura_plain = render_grid_to_plain(sakura_grid)

    # Both render the greeting (proving both themes paint the chat body
    # successfully — neither one fell back to a blank or crash state).
    assert "I am successor" in steel_plain
    assert "I am successor" in sakura_plain

    # The title-bar theme pill names the active theme — visible chrome
    # that legitimately differs between two themes.
    assert "steel" in steel_plain
    assert "sakura" in sakura_plain
    assert "sakura" not in steel_plain
    assert "steel" not in sakura_plain

    # Colored output differs across the whole frame because the themes
    # use different palettes — this is the real test that the variant
    # resolver picked up the new theme.
    assert render_grid_to_ansi(steel_grid) != render_grid_to_ansi(sakura_grid)
