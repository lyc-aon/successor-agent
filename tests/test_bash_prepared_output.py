"""Tests for bash/prepared_output.py — verb-class-aware output parsing.

Three layers of coverage:

  1. Structural parsers — grep, ls, git-status output is parsed into
     the correct spans with the right kinds (chrome/dim/match/plain).
  2. Pretext-shaped caching — layout(width) returns the same object
     on repeat calls; different widths invalidate.
  3. Integration — real dispatch_bash runs hit the right prep path
     and render highlighted output at paint time.
"""

from __future__ import annotations

import pytest

from successor.bash import (
    ToolCard,
    dispatch_bash,
    paint_tool_card,
    parse_bash,
    preview_bash,
)
from successor.bash.prepared_output import (
    OutputLine,
    OutputSpan,
    PreparedToolOutput,
    _parse_grep_line,
    _parse_ls_line,
    _parse_git_status_line,
    _split_match_spans,
)
from successor.render.cells import Grid
from successor.render.theme import find_theme_or_fallback
from successor.snapshot import render_grid_to_plain


THEME = find_theme_or_fallback("steel").variant("dark")


# ─── grep parser ───


def test_parse_grep_line_splits_file_lineno_content() -> None:
    line = "src/foo.py:42:    return TODO"
    p = _parse_grep_line(line, query="TODO")
    # file chrome, :42: dim, content with match highlight
    kinds = [s.kind for s in p.spans]
    assert "chrome" in kinds  # filename
    assert "dim" in kinds  # :42:
    assert "match" in kinds  # TODO highlight
    assert p.kind == "match"


def test_parse_grep_line_without_query_no_match_span() -> None:
    p = _parse_grep_line("src/foo.py:1:hello", query=None)
    assert all(s.kind != "match" for s in p.spans)


def test_parse_grep_line_non_matching_falls_through() -> None:
    p = _parse_grep_line("no colons here", query="x")
    assert len(p.spans) == 1
    assert p.spans[0].kind == "plain"
    assert p.kind == "stdout"


def test_split_match_spans_case_insensitive() -> None:
    spans = _split_match_spans("hello World hello", "world")
    texts = [s.text for s in spans]
    kinds = [s.kind for s in spans]
    assert texts == ["hello ", "World", " hello"]
    assert kinds == ["plain", "match", "plain"]


def test_split_match_spans_multiple_hits() -> None:
    spans = _split_match_spans("TODO fix TODO later TODO", "TODO")
    # 3 matches + interleaving plains → 3 match spans
    match_count = sum(1 for s in spans if s.kind == "match")
    assert match_count == 3


def test_split_match_spans_no_query_returns_plain() -> None:
    spans = _split_match_spans("anything", "")
    assert len(spans) == 1
    assert spans[0].kind == "plain"


# ─── ls -l parser ───


def test_parse_ls_line_directory() -> None:
    line = "drwxr-xr-x  2 lycaon lycaon  4096 Apr 10 12:00 src"
    p = _parse_ls_line(line)
    # Last span should be the name "src"
    assert p.spans[-1].text == "src"
    # Dir marker should appear as a preceding span
    assert "▸" in "".join(s.text for s in p.spans)


def test_parse_ls_line_regular_file() -> None:
    line = "-rw-r--r--  1 lycaon lycaon   100 Apr 10 12:00 README.md"
    p = _parse_ls_line(line)
    assert p.spans[-1].text == "README.md"
    # Plain file gets a dot marker, not triangle
    assert "▸" not in "".join(s.text for s in p.spans[:-1])
    assert "·" in "".join(s.text for s in p.spans)


def test_parse_ls_line_symlink() -> None:
    line = "lrwxrwxrwx  1 lycaon lycaon    10 Apr 10 12:00 link"
    p = _parse_ls_line(line)
    assert p.spans[-1].text == "link"
    assert "↗" in "".join(s.text for s in p.spans)


def test_parse_ls_line_total_header() -> None:
    p = _parse_ls_line("total 24")
    assert p.kind == "header"
    assert p.spans[0].kind == "dim"


def test_parse_ls_line_short_form_falls_through() -> None:
    # Plain `ls` output (no -l) doesn't match the regex
    p = _parse_ls_line("foo.txt")
    assert p.kind == "stdout"
    assert p.spans[0].text == "foo.txt"


# ─── git status parser ───


def test_parse_git_status_modified() -> None:
    p = _parse_git_status_line(" M src/foo.py")
    assert p.spans[1].text == "src/foo.py"
    # The flag span has chrome kind
    assert p.spans[0].kind == "chrome"


def test_parse_git_status_untracked() -> None:
    p = _parse_git_status_line("?? new-file.txt")
    assert p.spans[0].kind == "dim"


def test_parse_git_status_non_matching_falls_through() -> None:
    p = _parse_git_status_line("not a git status line")
    assert p.spans[0].kind == "plain"


# ─── PreparedToolOutput ───


def test_prepared_tool_output_caches_by_width() -> None:
    card = dispatch_bash("echo hello")
    prep = PreparedToolOutput(card)
    lines_80 = prep.layout(80)
    # Same width → same list instance (cached)
    assert prep.layout(80) is lines_80


def test_prepared_tool_output_invalidates_on_width_change() -> None:
    card = dispatch_bash("echo hello world")
    prep = PreparedToolOutput(card)
    lines_80 = prep.layout(80)
    lines_40 = prep.layout(40)
    assert lines_80 is not lines_40


def test_prepared_tool_output_no_output_placeholder() -> None:
    card = dispatch_bash("true")  # succeeds, no stdout/stderr
    prep = PreparedToolOutput(card)
    lines = prep.layout(80)
    assert len(lines) == 1
    assert lines[0].kind == "truncated"
    assert "no output" in lines[0].plain


def test_prepared_tool_output_stderr_becomes_warn_lines() -> None:
    card = dispatch_bash("echo oops >&2; false")
    prep = PreparedToolOutput(card)
    lines = prep.layout(80)
    # At least one stderr-kind row
    assert any(l.kind == "stderr" for l in lines)


# ─── No line cap: every wrapped output row is returned ───


def test_layout_returns_all_lines_with_no_cap() -> None:
    """PreparedToolOutput.layout no longer truncates at a line count.
    The 8 KiB byte cap at the exec layer is the only ceiling, and the
    full (post-byte-cap) output is wrapped and returned.
    """
    card = dispatch_bash("for i in $(seq 1 20); do echo line$i; done")
    prep = PreparedToolOutput(card)
    lines = prep.layout(80)
    # Every one of the 20 lines is present — no truncation marker
    assert not any(l.kind == "truncated" for l in lines)
    plain_concat = "\n".join(l.plain for l in lines)
    for i in range(1, 21):
        assert f"line{i}" in plain_concat


# ─── Search integration: grep output renders with highlights ───


def test_search_card_highlights_match_in_rendered_output(tmp_path) -> None:
    """End-to-end: a grep card's output has match spans in the
    PreparedToolOutput, and the resulting paint shows the query text
    at its expected position."""
    # Build a search-content card with known output
    card = ToolCard(
        verb="search-content",
        params=(("pattern", "TODO"),),
        risk="safe",
        raw_command="grep TODO foo",
        confidence=0.95,
        parser_name="grep",
        output="src/foo.py:10:    # TODO fix this\nsrc/bar.py:5:    TODO\n",
        stderr="",
        exit_code=0,
        duration_ms=5.0,
    )
    prep = PreparedToolOutput(card)
    lines = prep.layout(80)
    # Should have two match-kind lines (one per grep hit)
    match_rows = [l for l in lines if l.kind == "match"]
    assert len(match_rows) == 2
    # Each row should contain a match-kind span with "TODO"
    for row in match_rows:
        match_spans = [s for s in row.spans if s.kind == "match"]
        assert len(match_spans) == 1
        assert match_spans[0].text == "TODO"


def test_list_card_produces_entry_markers(tmp_path) -> None:
    """A list-directory card's output is parsed into ls rows with
    file/dir markers."""
    card = ToolCard(
        verb="list-directory",
        params=(("path", "."),),
        risk="safe",
        raw_command="ls -l",
        confidence=0.95,
        parser_name="ls",
        output=(
            "total 8\n"
            "drwxr-xr-x  2 user user 4096 Apr 10 12:00 src\n"
            "-rw-r--r--  1 user user  100 Apr 10 12:00 README.md\n"
        ),
        stderr="",
        exit_code=0,
        duration_ms=5.0,
    )
    prep = PreparedToolOutput(card)
    lines = prep.layout(80)
    plain = "\n".join(l.plain for l in lines)
    assert "total 8" in plain
    assert "src" in plain
    assert "README.md" in plain
    # Dir marker and file marker
    assert "▸" in plain
    assert "·" in plain


# ─── Render integration: the painter actually draws the spans ───


def test_paint_search_card_shows_match_in_grid() -> None:
    """The search card renders the match text to the grid — the span-
    aware painter walks each span, so highlighted matches appear at
    their original positions."""
    card = ToolCard(
        verb="search-content",
        params=(("pattern", "FROBNICATE"),),
        risk="safe",
        raw_command="grep FROBNICATE foo",
        confidence=0.95,
        parser_name="grep",
        output="file.py:99:    do_FROBNICATE(thing)\n",
        stderr="",
        exit_code=0,
        duration_ms=1.0,
    )
    g = Grid(20, 100)
    paint_tool_card(g, card, x=0, y=0, w=100, theme=THEME)
    plain = render_grid_to_plain(g)
    assert "FROBNICATE" in plain
    # Search glyph still present
    assert "⌕" in plain


def test_paint_list_card_shows_markers_in_grid() -> None:
    card = ToolCard(
        verb="list-directory",
        params=(("path", "."),),
        risk="safe",
        raw_command="ls -l",
        confidence=0.95,
        parser_name="ls",
        output=(
            "drwxr-xr-x  2 user user 4096 Apr 10 12:00 tests\n"
            "-rw-r--r--  1 user user  100 Apr 10 12:00 main.py\n"
        ),
        stderr="",
        exit_code=0,
        duration_ms=1.0,
    )
    g = Grid(20, 100)
    paint_tool_card(g, card, x=0, y=0, w=100, theme=THEME)
    plain = render_grid_to_plain(g)
    assert "tests" in plain
    assert "main.py" in plain
    assert "▸" in plain  # dir
    assert "·" in plain  # file


# ─── Cache behaviour at the chat layer ───


def test_message_caches_prepared_output() -> None:
    """The chat _Message caches PreparedToolOutput on first paint and
    reuses it across subsequent renders."""
    from successor.chat import SuccessorChat, _Message
    from successor.render.cells import Grid

    chat = SuccessorChat()
    chat.messages = []
    card = dispatch_bash("echo hello")
    msg = _Message("tool", "", tool_card=card)
    chat.messages.append(msg)
    # Force a paint
    g = Grid(30, 100)
    chat.on_tick(g)
    first_prep = msg._prepared_tool_output
    assert first_prep is not None
    # Second paint reuses the same instance
    chat.on_tick(g)
    assert msg._prepared_tool_output is first_prep


def test_message_caches_rendered_rows_at_stable_width() -> None:
    """With stable width/theme, re-paints hit the row cache."""
    from successor.chat import SuccessorChat, _Message
    from successor.render.cells import Grid

    chat = SuccessorChat()
    chat.messages = []
    card = dispatch_bash("echo world")
    msg = _Message("tool", "", tool_card=card)
    chat.messages.append(msg)
    g = Grid(30, 100)
    chat.on_tick(g)
    first_rows = msg._card_rows_cache
    assert first_rows is not None
    chat.on_tick(g)
    assert msg._card_rows_cache is first_rows
