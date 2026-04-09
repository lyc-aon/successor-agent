"""Visual snapshot tests for the wizard's COMPACTION step.

Verifies the renderer paints the new step correctly across each
preset selection. Hermetic via temp_config_dir; uses the headless
wizard_demo_snapshot helper to render a Grid with no PTY.
"""

from __future__ import annotations

from pathlib import Path


from successor.snapshot import render_grid_to_plain, wizard_demo_snapshot


# ─── Step renders correctly for each preset ───


def test_compaction_step_default_preset(temp_config_dir: Path) -> None:
    g = wizard_demo_snapshot(
        rows=30, cols=120, step="compaction", compaction_preset="default",
    )
    plain = render_grid_to_plain(g)

    # Step heading
    assert "step 9 of 10" in plain
    assert "autocompact behavior" in plain

    # All four presets are listed in order
    assert "default" in plain
    assert "aggressive" in plain
    assert "lazy" in plain
    assert "off" in plain

    # The cursor is on the default preset (▸ marker before "default")
    # Find the line with "default" and confirm it has the cursor glyph
    default_line = next((line for line in plain.splitlines() if "default — 12.5%" in line), "")
    assert "▸" in default_line, f"expected cursor on default line, got: {default_line!r}"

    # Live preview against 200K reference window
    assert "200K context window" in plain
    assert "warning at" in plain
    assert "autocompact at" in plain
    assert "blocking at" in plain

    # Default preset numbers (the chat._agent_budget math at 200K)
    # warning_at = 175_000, autocompact_at = 187_500, blocking_at = 196_875
    assert "175,000" in plain
    assert "187,500" in plain
    assert "196,875" in plain


def test_compaction_step_aggressive_preset(temp_config_dir: Path) -> None:
    g = wizard_demo_snapshot(
        rows=30, cols=120, step="compaction", compaction_preset="aggressive",
    )
    plain = render_grid_to_plain(g)

    # Cursor moved to aggressive
    aggressive_line = next(
        (line for line in plain.splitlines() if "aggressive — 25%" in line), ""
    )
    assert "▸" in aggressive_line, (
        f"expected cursor on aggressive line, got: {aggressive_line!r}"
    )

    # Live preview shows MORE aggressive thresholds — warning at 75% of 200K
    # warning_buffer = 50_000, autocompact_buffer = 25_000, blocking_buffer = 6_000
    # warning_at = 150_000, autocompact_at = 175_000, blocking_at = 194_000
    assert "150,000" in plain
    assert "175,000" in plain
    assert "194,000" in plain


def test_compaction_step_lazy_preset(temp_config_dir: Path) -> None:
    g = wizard_demo_snapshot(
        rows=30, cols=120, step="compaction", compaction_preset="lazy",
    )
    plain = render_grid_to_plain(g)

    lazy_line = next(
        (line for line in plain.splitlines() if "lazy — 5%" in line), ""
    )
    assert "▸" in lazy_line

    # Lazy thresholds are TIGHT to the window
    # warning_buffer = 10_000, autocompact_buffer = 4_000, blocking_buffer = 1_000
    # warning_at = 190_000, autocompact_at = 196_000, blocking_at = 199_000
    assert "190,000" in plain
    assert "196,000" in plain
    assert "199,000" in plain


def test_compaction_step_off_preset_shows_disabled_message(temp_config_dir: Path) -> None:
    g = wizard_demo_snapshot(
        rows=30, cols=120, step="compaction", compaction_preset="off",
    )
    plain = render_grid_to_plain(g)

    off_line = next(
        (line for line in plain.splitlines() if "off — never autocompact" in line), ""
    )
    assert "▸" in off_line

    # The disabled-mode preview message
    assert "autocompact disabled" in plain
    # And the blocking refusal still applies (last line of preview)
    assert "blocking refusal" in plain


# ─── Sidebar accurately shows compact step ───


def test_sidebar_includes_compact_step(temp_config_dir: Path) -> None:
    """The sidebar lists every step including the new 'compact' entry
    between 'tools' and 'review'."""
    g = wizard_demo_snapshot(rows=30, cols=120, step="compaction")
    plain = render_grid_to_plain(g)

    # Find sidebar lines (left column)
    lines = plain.splitlines()
    sidebar_text = "\n".join(line[:18] for line in lines)
    assert "compact" in sidebar_text
    assert "tools" in sidebar_text
    assert "review" in sidebar_text

    # Order: tools should appear ABOVE compact, compact ABOVE review
    tools_idx = next(i for i, line in enumerate(lines) if "tools" in line[:18])
    compact_idx = next(i for i, line in enumerate(lines) if "compact" in line[:18])
    review_idx = next(i for i, line in enumerate(lines) if "review" in line[:18])
    assert tools_idx < compact_idx < review_idx


# ─── Active marker is on the right step ───


def test_compaction_step_active_marker(temp_config_dir: Path) -> None:
    """When the wizard is on the compaction step, the sidebar shows
    the ▸ active marker on the 'compact' row."""
    g = wizard_demo_snapshot(rows=30, cols=120, step="compaction")
    plain = render_grid_to_plain(g)

    lines = plain.splitlines()
    compact_lines = [line for line in lines if "compact" in line[:18]]
    assert len(compact_lines) >= 1
    # The active step has ▸ in front
    assert "▸" in compact_lines[0]


# ─── Renders at narrow + wide widths ───


def test_compaction_step_renders_at_narrow_terminal(temp_config_dir: Path) -> None:
    """The renderer survives a narrow 80-col terminal."""
    g = wizard_demo_snapshot(rows=30, cols=80, step="compaction")
    plain = render_grid_to_plain(g)
    # Headings and at least one preset survive
    assert "autocompact behavior" in plain
    assert "default" in plain


def test_compaction_step_renders_at_wide_terminal(temp_config_dir: Path) -> None:
    """The renderer scales up to a 200-col terminal."""
    g = wizard_demo_snapshot(rows=40, cols=200, step="compaction")
    plain = render_grid_to_plain(g)
    assert "autocompact behavior" in plain
    assert "200K context window" in plain
