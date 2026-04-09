"""Tests for the standalone PromptEditor class.

The editor used to live inside config.py — it's now a standalone
module (`wizard/prompt_editor.py`) with soft word wrap, visible-row
cursor navigation, selection, and OSC 52 clipboard integration.

These tests cover:
  1. Soft-wrap primitive (_wrap_source_line) — pure function tests
  2. Cache invalidation behavior on edits
  3. Visual-space navigation (UP/DOWN with wrap)
  4. Selection state machine (extend, clear, normalize)
  5. Selection-aware editing (replace on type, delete on backspace)
  6. Clipboard callback (Ctrl+C, Ctrl+X, select-all)
  7. Existing single-line cursor model (regression for the move)

The basic editor mechanics (insert, delete, newline, etc.) used to
have tests in test_config_menu.py but lived alongside config menu
tests. They've been moved here for cleaner organization. The
config menu still has integration tests in test_config_menu.py that
exercise the editor through the config menu's keystroke dispatch.
"""

from __future__ import annotations


from successor.input.keys import Key, KeyEvent, MOD_CTRL, MOD_SHIFT
from successor.wizard.prompt_editor import (
    PromptEditor,
    _is_in_selection,
    _normalize_selection,
    _VisibleChunk,
    _wrap_source_line,
)


# ─── _wrap_source_line — pure function ───


def test_wrap_empty_line() -> None:
    chunks = _wrap_source_line("", 20)
    assert chunks == (_VisibleChunk(0, ""),)


def test_wrap_short_line_one_chunk() -> None:
    chunks = _wrap_source_line("hello", 20)
    assert chunks == (_VisibleChunk(0, "hello"),)


def test_wrap_breaks_at_space() -> None:
    """Greedy: break at the last space within the width."""
    line = "hello world from the editor"
    chunks = _wrap_source_line(line, 12)
    # First chunk should be "hello world " (12 chars), breaking at the space
    assert chunks[0].text == "hello world "
    assert chunks[0].source_col_start == 0


def test_wrap_no_space_hard_break() -> None:
    """A long word with no space breaks at the width."""
    line = "supercalifragilistic"
    chunks = _wrap_source_line(line, 8)
    # 20 chars / 8 = 3 chunks
    assert len(chunks) == 3
    assert chunks[0].text == "supercal"
    assert chunks[1].text == "ifragili"
    assert chunks[2].text == "stic"


def test_wrap_zero_width_returns_whole_line() -> None:
    """Defensive: width <= 0 returns the line unwrapped."""
    chunks = _wrap_source_line("hello world", 0)
    assert chunks == (_VisibleChunk(0, "hello world"),)


def test_wrap_preserves_every_character() -> None:
    """Joining all chunks reconstructs the original line."""
    line = "the quick brown fox jumps over the lazy dog"
    for width in (5, 10, 15, 20, 30):
        chunks = _wrap_source_line(line, width)
        joined = "".join(c.text for c in chunks)
        assert joined == line, f"width={width}: lost chars"


def test_wrap_source_col_starts_align() -> None:
    """Each chunk's source_col_start equals previous chunk's end_col."""
    line = "hello world from successor"
    chunks = _wrap_source_line(line, 10)
    for i in range(1, len(chunks)):
        assert chunks[i].source_col_start == chunks[i - 1].end_col


# ─── _normalize_selection / _is_in_selection ───


def test_normalize_selection_already_ordered() -> None:
    a, b = (1, 5), (3, 2)
    start, end = _normalize_selection(a, b)
    assert start == (1, 5)
    assert end == (3, 2)


def test_normalize_selection_reversed() -> None:
    a, b = (3, 2), (1, 5)
    start, end = _normalize_selection(a, b)
    assert start == (1, 5)
    assert end == (3, 2)


def test_normalize_selection_same_row_ordered_by_col() -> None:
    a, b = (2, 8), (2, 3)
    start, end = _normalize_selection(a, b)
    assert start == (2, 3)
    assert end == (2, 8)


def test_is_in_selection_single_line() -> None:
    """Half-open on the right — cursor cell isn't included."""
    start, end = (0, 2), (0, 5)
    assert not _is_in_selection(0, 1, start, end)
    assert _is_in_selection(0, 2, start, end)
    assert _is_in_selection(0, 4, start, end)
    assert not _is_in_selection(0, 5, start, end)  # cursor cell excluded
    assert not _is_in_selection(1, 0, start, end)


def test_is_in_selection_multi_line() -> None:
    start, end = (1, 3), (3, 2)
    # Row 0: nothing selected
    assert not _is_in_selection(0, 5, start, end)
    # Row 1: from col 3 to end of line (any col >= 3 included)
    assert not _is_in_selection(1, 2, start, end)
    assert _is_in_selection(1, 3, start, end)
    assert _is_in_selection(1, 99, start, end)
    # Row 2: entire line (interior row)
    assert _is_in_selection(2, 0, start, end)
    assert _is_in_selection(2, 50, start, end)
    # Row 3: cols 0..1 included, col 2 excluded (cursor)
    assert _is_in_selection(3, 0, start, end)
    assert _is_in_selection(3, 1, start, end)
    assert not _is_in_selection(3, 2, start, end)


# ─── PromptEditor — basic state ───


def test_initial_state() -> None:
    ed = PromptEditor("hello\nworld")
    assert ed.lines == ["hello", "world"]
    assert ed.cursor_row == 0
    assert ed.cursor_col == 0
    assert not ed.is_done
    assert not ed.is_dirty
    assert not ed.has_selection


def test_empty_initial_has_one_line() -> None:
    ed = PromptEditor("")
    assert ed.lines == [""]


def test_char_count() -> None:
    ed = PromptEditor("hello\nworld")
    assert ed.char_count() == 11  # 5 + 1 newline + 5


# ─── Editing operations ───


def test_insert_char() -> None:
    ed = PromptEditor("ab")
    ed.cursor_col = 1
    ed.handle_key(KeyEvent(char="X"))
    assert ed.lines[0] == "aXb"
    assert ed.cursor_col == 2
    assert ed.is_dirty


def test_newline_splits_line() -> None:
    ed = PromptEditor("hello world")
    ed.cursor_col = 5
    ed.handle_key(KeyEvent(key=Key.ENTER))
    assert ed.lines == ["hello", " world"]
    assert ed.cursor_row == 1
    assert ed.cursor_col == 0


def test_backspace_within_line() -> None:
    ed = PromptEditor("hello")
    ed.cursor_col = 3
    ed.handle_key(KeyEvent(key=Key.BACKSPACE))
    assert ed.lines[0] == "helo"
    assert ed.cursor_col == 2


def test_backspace_at_line_start_merges() -> None:
    ed = PromptEditor("hello\nworld")
    ed.cursor_row = 1
    ed.cursor_col = 0
    ed.handle_key(KeyEvent(key=Key.BACKSPACE))
    assert ed.lines == ["helloworld"]
    assert ed.cursor_row == 0
    assert ed.cursor_col == 5


def test_delete_within_line() -> None:
    ed = PromptEditor("hello")
    ed.cursor_col = 2
    ed.handle_key(KeyEvent(key=Key.DELETE))
    assert ed.lines[0] == "helo"


def test_delete_at_line_end_merges_next() -> None:
    ed = PromptEditor("hello\nworld")
    ed.cursor_row = 0
    ed.cursor_col = 5
    ed.handle_key(KeyEvent(key=Key.DELETE))
    assert ed.lines == ["helloworld"]


def test_tab_inserts_two_spaces() -> None:
    ed = PromptEditor("hello")
    ed.cursor_col = 5
    ed.handle_key(KeyEvent(key=Key.TAB))
    assert ed.lines[0] == "hello  "


# ─── Source-coordinate navigation ───


def test_left_at_line_start_wraps_up() -> None:
    ed = PromptEditor("hello\nworld")
    ed.cursor_row = 1
    ed.cursor_col = 0
    ed.handle_key(KeyEvent(key=Key.LEFT))
    assert ed.cursor_row == 0
    assert ed.cursor_col == 5


def test_right_at_line_end_wraps_down() -> None:
    ed = PromptEditor("hello\nworld")
    ed.cursor_row = 0
    ed.cursor_col = 5
    ed.handle_key(KeyEvent(key=Key.RIGHT))
    assert ed.cursor_row == 1
    assert ed.cursor_col == 0


def test_home_end_within_line() -> None:
    ed = PromptEditor("hello world")
    ed.cursor_col = 5
    ed.handle_key(KeyEvent(key=Key.HOME))
    assert ed.cursor_col == 0
    ed.handle_key(KeyEvent(key=Key.END))
    assert ed.cursor_col == 11


# ─── Visual-row navigation (UP/DOWN with soft wrap) ───


def test_visible_navigation_no_wrap() -> None:
    """Without wrap (long width), UP/DOWN behaves like source navigation."""
    ed = PromptEditor("line1\nline2\nline3")
    # Trigger a paint with a wide width so cache is built
    from successor.render.cells import Grid
    from successor.render.theme import find_theme_or_fallback
    g = Grid(20, 100)
    ed.paint(g, x=0, y=0, w=80, h=18, theme=find_theme_or_fallback("steel").variant("dark"))

    ed.cursor_row = 0
    ed.cursor_col = 3
    ed.handle_key(KeyEvent(key=Key.DOWN))
    assert ed.cursor_row == 1
    assert ed.cursor_col == 3
    ed.handle_key(KeyEvent(key=Key.UP))
    assert ed.cursor_row == 0
    assert ed.cursor_col == 3


def test_visible_navigation_with_wrap() -> None:
    """With wrap, UP/DOWN moves to adjacent VISIBLE rows.

    Source has 1 line that wraps to multiple visible chunks. UP/DOWN
    should move within those chunks of the same source line, not jump
    source rows.
    """
    line = "hello world from successor terminal renderer that is quite long"
    ed = PromptEditor(line)
    from successor.render.cells import Grid
    from successor.render.theme import find_theme_or_fallback
    g = Grid(20, 100)
    # Modal width 30 → text area width = 30 - 10 = 20. The line is
    # 60 chars so it wraps into ~3 chunks.
    ed.paint(g, x=0, y=0, w=30, h=18, theme=find_theme_or_fallback("steel").variant("dark"))

    # Cursor at source col 5 (within first wrap chunk)
    ed.cursor_row = 0
    ed.cursor_col = 5
    chunks = ed._chunks_for_line(0)
    assert len(chunks) >= 2  # the line should wrap into multiple chunks

    # DOWN should move to the next visible chunk (still source row 0)
    initial_chunk_idx = ed._cursor_chunk_index()
    ed.handle_key(KeyEvent(key=Key.DOWN))
    new_chunk_idx = ed._cursor_chunk_index()
    assert new_chunk_idx == initial_chunk_idx + 1
    # And cursor stays on the same source row (because the long line
    # spans multiple visible rows)
    assert ed.cursor_row == 0


# ─── Selection state machine ───


def test_shift_arrow_starts_selection() -> None:
    ed = PromptEditor("hello world")
    ed.cursor_col = 0
    assert not ed.has_selection
    ed.handle_key(KeyEvent(key=Key.RIGHT, mods=MOD_SHIFT))
    assert ed.has_selection
    assert ed.selection_anchor == (0, 0)
    assert ed.cursor_col == 1


def test_shift_arrow_extends_selection() -> None:
    ed = PromptEditor("hello world")
    ed.cursor_col = 0
    for _ in range(5):
        ed.handle_key(KeyEvent(key=Key.RIGHT, mods=MOD_SHIFT))
    assert ed.selection_anchor == (0, 0)
    assert ed.cursor_col == 5
    assert ed.get_selection_text() == "hello"


def test_non_shift_arrow_clears_selection() -> None:
    ed = PromptEditor("hello world")
    ed.handle_key(KeyEvent(key=Key.RIGHT, mods=MOD_SHIFT))
    assert ed.has_selection
    ed.handle_key(KeyEvent(key=Key.RIGHT))  # no shift
    assert not ed.has_selection


def test_esc_clears_selection_first_press() -> None:
    """Esc with active selection clears the selection but doesn't close."""
    ed = PromptEditor("hello")
    ed.handle_key(KeyEvent(key=Key.RIGHT, mods=MOD_SHIFT))
    assert ed.has_selection
    ed.handle_key(KeyEvent(key=Key.ESC))
    assert not ed.has_selection
    assert not ed.is_done  # editor still open


def test_esc_with_no_selection_cancels_editor() -> None:
    ed = PromptEditor("hello")
    ed.handle_key(KeyEvent(key=Key.ESC))
    assert ed.is_done
    assert ed.result is None


def test_select_all() -> None:
    ed = PromptEditor("hello\nworld")
    ed.handle_key(KeyEvent(char="a", mods=MOD_CTRL))
    assert ed.has_selection
    assert ed.selection_anchor == (0, 0)
    assert ed.cursor_row == 1
    assert ed.cursor_col == 5
    assert ed.get_selection_text() == "hello\nworld"


def test_get_selection_text_single_line() -> None:
    ed = PromptEditor("hello world")
    ed.cursor_col = 6
    ed.selection_anchor = (0, 0)
    # Selection from (0, 0) to (0, 6) — the cursor moved
    ed.cursor_col = 6
    assert ed.get_selection_text() == "hello "


def test_get_selection_text_multi_line() -> None:
    ed = PromptEditor("first\nsecond\nthird")
    ed.selection_anchor = (0, 2)
    ed.cursor_row = 2
    ed.cursor_col = 3
    assert ed.get_selection_text() == "rst\nsecond\nthi"


# ─── Selection-aware editing ───


def test_typing_with_selection_replaces() -> None:
    """Typing a char while selection is active replaces the selection."""
    ed = PromptEditor("hello world")
    ed.cursor_col = 0
    for _ in range(5):
        ed.handle_key(KeyEvent(key=Key.RIGHT, mods=MOD_SHIFT))
    # Selection now: "hello"
    ed.handle_key(KeyEvent(char="X"))
    assert ed.lines[0] == "X world"
    assert not ed.has_selection
    assert ed.cursor_col == 1


def test_backspace_with_selection_deletes_range() -> None:
    ed = PromptEditor("hello world")
    ed.cursor_col = 0
    for _ in range(5):
        ed.handle_key(KeyEvent(key=Key.RIGHT, mods=MOD_SHIFT))
    ed.handle_key(KeyEvent(key=Key.BACKSPACE))
    assert ed.lines[0] == " world"
    assert not ed.has_selection
    assert ed.cursor_col == 0


def test_delete_with_selection_deletes_range() -> None:
    ed = PromptEditor("hello world")
    ed.cursor_col = 0
    for _ in range(5):
        ed.handle_key(KeyEvent(key=Key.RIGHT, mods=MOD_SHIFT))
    ed.handle_key(KeyEvent(key=Key.DELETE))
    assert ed.lines[0] == " world"


def test_multi_line_selection_delete() -> None:
    ed = PromptEditor("first line\nsecond line\nthird line")
    ed.selection_anchor = (0, 5)
    ed.cursor_row = 2
    ed.cursor_col = 5
    ed.handle_key(KeyEvent(key=Key.BACKSPACE))
    assert ed.lines == ["first line"]
    assert ed.cursor_row == 0
    assert ed.cursor_col == 5


# ─── Clipboard callback ───


def test_ctrl_c_calls_callback_with_selection() -> None:
    captured: list[str] = []
    ed = PromptEditor("hello world", copy_callback=captured.append)
    ed.cursor_col = 0
    for _ in range(5):
        ed.handle_key(KeyEvent(key=Key.RIGHT, mods=MOD_SHIFT))
    ed.handle_key(KeyEvent(char="c", mods=MOD_CTRL))
    assert captured == ["hello"]
    # Selection NOT cleared by copy
    assert ed.has_selection


def test_ctrl_c_no_selection_no_callback() -> None:
    captured: list[str] = []
    ed = PromptEditor("hello", copy_callback=captured.append)
    ed.handle_key(KeyEvent(char="c", mods=MOD_CTRL))
    assert captured == []


def test_ctrl_x_cuts_selection() -> None:
    captured: list[str] = []
    ed = PromptEditor("hello world", copy_callback=captured.append)
    ed.cursor_col = 0
    for _ in range(5):
        ed.handle_key(KeyEvent(key=Key.RIGHT, mods=MOD_SHIFT))
    ed.handle_key(KeyEvent(char="x", mods=MOD_CTRL))
    assert captured == ["hello"]
    assert ed.lines[0] == " world"
    assert not ed.has_selection


def test_callback_failure_doesnt_crash() -> None:
    """A callback that raises is silently swallowed."""
    def boom(_text: str) -> None:
        raise RuntimeError("clipboard unavailable")

    ed = PromptEditor("hello", copy_callback=boom)
    ed.cursor_col = 0
    ed.handle_key(KeyEvent(key=Key.RIGHT, mods=MOD_SHIFT))
    # Should not raise
    ed.handle_key(KeyEvent(char="c", mods=MOD_CTRL))


# ─── Save / cancel ───


def test_ctrl_s_commits() -> None:
    ed = PromptEditor("original")
    ed.cursor_col = 8
    for ch in " EDITED":
        ed.handle_key(KeyEvent(char=ch))
    ed.handle_key(KeyEvent(char="s", mods=MOD_CTRL))
    assert ed.is_done
    assert ed.result == "original EDITED"


def test_dirty_tracking_returns_to_clean() -> None:
    ed = PromptEditor("hello")
    assert not ed.is_dirty
    ed.handle_key(KeyEvent(char="X"))
    assert ed.is_dirty
    ed.handle_key(KeyEvent(key=Key.BACKSPACE))
    assert not ed.is_dirty


# ─── Wrap cache invalidation ───


def test_wrap_cache_invalidates_on_edit() -> None:
    """Editing a line invalidates ONLY that line's cache."""
    ed = PromptEditor("line1\nline2\nline3")
    # Trigger cache population
    from successor.render.cells import Grid
    from successor.render.theme import find_theme_or_fallback
    g = Grid(20, 100)
    theme = find_theme_or_fallback("steel").variant("dark")
    ed.paint(g, x=0, y=0, w=80, h=18, theme=theme)

    # All three lines cached
    assert all(c is not None for c in ed._wrap_cache)

    # Edit line 1
    ed.cursor_row = 1
    ed.cursor_col = 5
    ed.handle_key(KeyEvent(char="X"))

    # Line 1's cache is invalidated (None)
    assert ed._wrap_cache[1] is None
    # Lines 0 and 2 still cached
    assert ed._wrap_cache[0] is not None
    assert ed._wrap_cache[2] is not None


def test_wrap_cache_invalidates_all_on_resize() -> None:
    """Changing the paint width invalidates the entire cache."""
    ed = PromptEditor("line1\nline2\nline3")
    from successor.render.cells import Grid
    from successor.render.theme import find_theme_or_fallback
    g = Grid(20, 100)
    theme = find_theme_or_fallback("steel").variant("dark")

    ed.paint(g, x=0, y=0, w=80, h=18, theme=theme)
    assert all(c is not None for c in ed._wrap_cache)

    # Re-paint at a different width — invalidates all
    ed.paint(g, x=0, y=0, w=40, h=18, theme=theme)
    # The first call to _chunks_for_line per row will repopulate; let's
    # verify by checking the cached width matches the new one
    for i, entry in enumerate(ed._wrap_cache):
        if entry is not None:
            cached_width, _ = entry
            assert cached_width == ed._last_paint_width


def test_wrap_cache_invalidates_all_on_newline() -> None:
    """Inserting a newline invalidates the whole cache (line shift)."""
    ed = PromptEditor("line1\nline2\nline3")
    from successor.render.cells import Grid
    from successor.render.theme import find_theme_or_fallback
    g = Grid(20, 100)
    theme = find_theme_or_fallback("steel").variant("dark")
    ed.paint(g, x=0, y=0, w=80, h=18, theme=theme)
    assert all(c is not None for c in ed._wrap_cache)

    ed.cursor_row = 0
    ed.cursor_col = 5
    ed.handle_key(KeyEvent(key=Key.ENTER))

    # All cleared because line indices shifted
    assert all(c is None for c in ed._wrap_cache)
