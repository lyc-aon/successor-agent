"""Tests for bash/render.py — paint_tool_card and measure_tool_card_height.

The render layer is pure: it takes a ToolCard + a Grid and mutates the
grid. We test by:
  1. Painting a card and reading visible chars from the resulting grid
     (via render_grid_to_plain) — the standard snapshot pattern.
  2. Asserting computed heights match what paint_tool_card actually drew.
  3. Spot-checking risk-tinted visual treatments (the verb glyphs).
"""

from __future__ import annotations

import pytest

from successor.bash import (
    ToolCard,
    dispatch_bash,
    measure_tool_card_height,
    paint_tool_card,
    parse_bash,
    preview_bash,
)
from successor.render.cells import Grid
from successor.render.theme import find_theme_or_fallback
from successor.snapshot import render_grid_to_plain


THEME = find_theme_or_fallback("steel").variant("dark")


def _paint(card: ToolCard, *, w: int = 80, h: int = 30) -> tuple[Grid, str, int]:
    """Helper: paint a card to a fresh grid and return the plain text."""
    g = Grid(h, w)
    height = paint_tool_card(g, card, x=0, y=0, w=w, theme=THEME)
    return g, render_grid_to_plain(g), height


# ─── Smoke / structure ───


def test_paint_executed_card_smoke() -> None:
    card = dispatch_bash("echo hello world")
    _, plain, h = _paint(card)
    assert h > 0
    assert "print-text" in plain
    assert "echo hello world" in plain
    assert "hello world" in plain
    assert "exit 0" in plain


def test_paint_preview_card_no_output_section() -> None:
    """preview_bash returns a card without exit_code — paint should
    skip the output and status sections."""
    card = preview_bash("ls -la /etc")
    _, plain, h = _paint(card)
    assert "list-directory" in plain
    assert "/etc" in plain
    # No output / status line
    assert "exit" not in plain


# ─── Verb-class glyphs ───


def test_list_card_uses_list_glyph() -> None:
    """LIST class (ls) uses the ☰ glyph."""
    card = preview_bash("ls")
    _, plain, _ = _paint(card)
    assert "☰" in plain


def test_read_card_uses_read_glyph() -> None:
    """READ class (cat / head / tail) uses the ◲ glyph."""
    card = preview_bash("cat README.md")
    _, plain, _ = _paint(card)
    assert "◲" in plain


def test_search_card_uses_search_glyph() -> None:
    """SEARCH class (grep / find) uses the ⌕ glyph."""
    card = preview_bash("grep -rn TODO src/")
    _, plain, _ = _paint(card)
    assert "⌕" in plain


def test_mutate_card_uses_mutate_glyph() -> None:
    """MUTATE class (mkdir / touch / rm / cp / mv) uses ✎."""
    card = preview_bash("mkdir foo")
    _, plain, _ = _paint(card)
    assert "✎" in plain


def test_dangerous_card_uses_danger_glyph() -> None:
    """Any dangerous-risk card overrides its verb class and uses ⚠."""
    card = preview_bash("rm -rf /")
    _, plain, _ = _paint(card)
    assert "⚠" in plain


def test_inspect_card_uses_inspect_glyph() -> None:
    """INSPECT class (pwd / git-status / git-log) uses ⊙."""
    card = preview_bash("pwd")
    _, plain, _ = _paint(card)
    assert "⊙" in plain


def test_exec_card_uses_exec_glyph() -> None:
    """EXEC class (python / echo) uses ▶."""
    card = preview_bash("python -c 'print(42)'")
    _, plain, _ = _paint(card)
    assert "▶" in plain


def test_low_confidence_shows_question_badge() -> None:
    card = preview_bash("totally_unknown_thing arg")
    _, plain, _ = _paint(card)
    # The "?" badge appears in the verb header
    assert "?" in plain


def test_high_confidence_no_question_badge_in_header() -> None:
    """A confident parser should NOT add the ? badge."""
    card = preview_bash("ls -la")
    g, plain, _ = _paint(card)
    # Check the first line specifically (where the header is) — the "?"
    # might appear elsewhere as a literal glyph
    first_line = plain.split("\n")[0]
    assert "?" not in first_line


# ─── Param table rendering ───


def test_params_render_in_table() -> None:
    card = preview_bash("ls -la /etc")
    _, plain, _ = _paint(card)
    assert "path" in plain
    assert "/etc" in plain
    assert "hidden" in plain


def test_no_params_shows_placeholder() -> None:
    """An empty-params card shows '(no parameters)' instead of an empty box."""
    card = ToolCard(
        verb="test-empty",
        params=(),
        risk="safe",
        raw_command="test-empty",
        confidence=1.0,
        parser_name="test",
    )
    _, plain, _ = _paint(card)
    assert "no parameters" in plain


# ─── Raw command on bottom border ───


def test_raw_command_on_bottom_border() -> None:
    card = preview_bash("ls -la /etc")
    _, plain, _ = _paint(card)
    assert "$ ls -la /etc" in plain


def test_raw_command_long_truncates() -> None:
    """A super-long raw command gets ellipsized."""
    long_cmd = "echo " + "x" * 200
    card = preview_bash(long_cmd)
    _, plain, _ = _paint(card, w=60)
    # The header "$ echo xxx…" is shown but doesn't bleed past the box
    assert "…" in plain
    for line in plain.split("\n"):
        assert len(line.rstrip()) <= 60


# ─── Output rendering ───


def test_executed_output_appears_below_box() -> None:
    card = dispatch_bash("echo line1; echo line2; echo line3")
    _, plain, _ = _paint(card)
    assert "line1" in plain
    assert "line2" in plain
    assert "line3" in plain


def test_status_line_shows_exit_and_duration() -> None:
    card = dispatch_bash("true")
    _, plain, _ = _paint(card)
    assert "exit 0" in plain
    assert "ms" in plain or "s" in plain


def test_failed_command_status_shows_exit_code() -> None:
    card = dispatch_bash("false")
    _, plain, _ = _paint(card)
    assert "exit 1" in plain
    assert "✗" in plain  # failure glyph


def test_no_output_placeholder() -> None:
    card = dispatch_bash("true")  # no output
    _, plain, _ = _paint(card)
    assert "(no output)" in plain


def test_long_output_shows_head_window_plus_overflow_marker() -> None:
    """The settled card caps its visible output region at
    DEFAULT_MAX_OUTPUT_LINES rows, showing a head window plus a
    "⋯ +N more lines ⋯" marker. This is a DISPLAY-only cap —
    the full output is still passed to the model via the tool
    result message. Keeps reads/grep/ls from flooding the chat.
    """
    from successor.bash import DEFAULT_MAX_OUTPUT_LINES
    card = dispatch_bash("for i in $(seq 1 30); do echo line$i; done")
    g = Grid(20, 80)
    paint_tool_card(g, card, x=0, y=0, w=80, theme=THEME)
    plain = render_grid_to_plain(g)
    # Overflow marker is present
    assert "more line" in plain
    # Head lines are visible (first few)
    assert "line1" in plain
    # Tail lines are NOT visible
    assert "line30" not in plain
    # Neither is something well past the cap
    assert "line25" not in plain


# ─── measure_tool_card_height ───


def test_measure_returns_consistent_height_with_paint() -> None:
    """measure should match what paint actually draws — these go out
    of sync silently if you don't test them."""
    cases = [
        preview_bash("ls"),
        preview_bash("ls -la /etc"),
        preview_bash("rm -rf /"),
        preview_bash("totally_unknown arg"),
    ]
    for card in cases:
        measured = measure_tool_card_height(card, width=80, show_output=False)
        _, _, painted = _paint(card)
        # Measure includes only the box section when show_output=False
        # and paint with a preview card also skips output, so they should
        # match on the box section
        assert measured == painted, (
            f"measured={measured} painted={painted} for {card.verb}"
        )


def test_measure_executed_card_includes_output_lines() -> None:
    """A real executed card with output should be measurably taller
    than its preview equivalent."""
    preview = preview_bash("echo hi")
    executed = dispatch_bash("echo hi")
    pm = measure_tool_card_height(preview, width=80, show_output=False)
    em = measure_tool_card_height(executed, width=80, show_output=True)
    assert em > pm  # output rows + status line added


# ─── Width robustness ───


def test_paint_skips_when_too_narrow() -> None:
    """Width below 20 should refuse to paint."""
    card = preview_bash("ls")
    g = Grid(10, 15)
    h = paint_tool_card(g, card, x=0, y=0, w=15, theme=THEME)
    assert h == 0


def test_paint_handles_grid_overflow_gracefully() -> None:
    """Painting outside the grid bounds should clip silently, not crash."""
    card = dispatch_bash("for i in 1 2 3 4 5; do echo $i; done")
    g = Grid(5, 80)  # only 5 rows for a card that wants ~10
    paint_tool_card(g, card, x=0, y=0, w=80, theme=THEME)
    # Just survive — the assertion is "no exception"


# ─── Output indentation ───


def test_output_lines_indented_inside_box_alignment() -> None:
    """Output lines should be indented to align with the box body.

    Use `pwd` so the output is a path that won't appear in the param
    table (which would otherwise match first)."""
    card = dispatch_bash("pwd")
    g, plain, _ = _paint(card)
    lines = plain.split("\n")
    # pwd's output starts with /; the box-body lines all start with │
    # Find a line that contains the cwd path but is NOT a box-side line
    import os
    cwd = os.getcwd()
    for line in lines:
        if cwd in line and not line.lstrip().startswith("│"):
            stripped = line.lstrip()
            assert len(line) - len(stripped) >= 3
            return
    pytest.fail(f"output line containing cwd ({cwd}) not found")
