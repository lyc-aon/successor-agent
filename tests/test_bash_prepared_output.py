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


from successor.bash import (
    ToolCard,
    dispatch_bash,
    paint_tool_card,
)
from successor.bash.prepared_output import (
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
    assert any(line.kind == "stderr" for line in lines)


# ─── No line cap: every wrapped output row is returned ───


def test_layout_returns_all_lines_when_max_lines_none() -> None:
    """PreparedToolOutput.layout returns every wrapped line when
    called without a max_lines cap. Tests and callers that want
    the raw body still use this form.
    """
    card = dispatch_bash("for i in $(seq 1 20); do echo line$i; done")
    prep = PreparedToolOutput(card)
    lines = prep.layout(80)  # max_lines=None by default
    assert not any(line.kind == "truncated" for line in lines)
    plain_concat = "\n".join(line.plain for line in lines)
    for i in range(1, 21):
        assert f"line{i}" in plain_concat


def test_layout_max_lines_caps_head_and_adds_overflow_marker() -> None:
    """When max_lines is set, long outputs clip to a head window
    plus a single "⋯ +N more lines ⋯" marker row. The clipped
    result has EXACTLY max_lines rows — (max_lines - 1) content
    rows + 1 marker row.
    """
    card = dispatch_bash("for i in $(seq 1 20); do echo line$i; done")
    prep = PreparedToolOutput(card)
    clipped = prep.layout(80, max_lines=5)
    assert len(clipped) == 5
    # First 4 rows are the head — line1..line4
    for i in range(1, 5):
        assert f"line{i}" in clipped[i - 1].plain
    # line5..line20 are NOT in the head
    head_text = "\n".join(line.plain for line in clipped[:-1])
    for i in range(5, 21):
        assert f"line{i}" not in head_text
    # Last row is the overflow marker mentioning 16 hidden lines
    assert clipped[-1].kind == "truncated"
    assert "16 more" in clipped[-1].plain


def test_layout_max_lines_unchanged_when_output_fits() -> None:
    """Short outputs that fit inside max_lines pass through
    unchanged — no marker added, no content clipped.
    """
    card = dispatch_bash("echo one; echo two; echo three")
    prep = PreparedToolOutput(card)
    clipped = prep.layout(80, max_lines=5)
    # 3 lines of output, no marker
    assert len(clipped) == 3
    assert not any(line.kind == "truncated" for line in clipped)


def test_task_ledger_output_uses_semantic_rows_without_clipping() -> None:
    card = ToolCard(
        verb="task-ledger",
        params=(("tasks", "6"),),
        risk="safe",
        raw_command="update 6 tasks",
        confidence=1.0,
        parser_name="native-task",
        tool_name="task",
        tool_arguments={
            "items": [
                {"content": f"Task {idx}", "status": "pending"}
                for idx in range(1, 7)
            ]
        },
        output="Updated the session task ledger.",
        exit_code=0,
        duration_ms=0.0,
    )
    prep = PreparedToolOutput(card)

    assert prep.preferred_max_lines is None
    lines = prep.layout(80, max_lines=prep.preferred_max_lines)
    plain = "\n".join(line.plain for line in lines)

    assert not any(line.kind == "truncated" for line in lines)
    assert "tasks  " in lines[0].plain
    assert "0 completed" in plain
    assert "6 pending" in plain
    assert "Task 6" in plain
    assert "[pending]" not in plain
    assert any(line.kind == "artifact_pending" for line in lines)


def test_verification_output_uses_semantic_rows_without_clipping() -> None:
    card = ToolCard(
        verb="verification",
        params=(("assertions", "4"),),
        risk="safe",
        raw_command="update 4 assertions",
        confidence=1.0,
        parser_name="native-verify",
        tool_name="verify",
        tool_arguments={
            "items": [
                {
                    "claim": "Hero CTA opens modal",
                    "evidence": "browser click changes dialog state",
                    "status": "passed",
                    "observed": "dialog rendered",
                },
                {
                    "claim": "No console errors",
                    "evidence": "console remains clean during playthrough",
                    "status": "in_progress",
                    "observed": "",
                },
                {
                    "claim": "Score increments on hit",
                    "evidence": "HUD score changes after scripted hit",
                    "status": "pending",
                    "observed": "",
                },
                {
                    "claim": "Failure path blocks invalid input",
                    "evidence": "bad command leaves state unchanged",
                    "status": "failed",
                    "observed": "invalid input still advanced the timer",
                },
            ]
        },
        output="Updated the session verification contract.",
        exit_code=0,
        duration_ms=0.0,
    )
    prep = PreparedToolOutput(card)

    assert prep.preferred_max_lines is None
    lines = prep.layout(80, max_lines=prep.preferred_max_lines)
    plain = "\n".join(line.plain for line in lines)

    assert not any(line.kind == "truncated" for line in lines)
    assert "proof  " in lines[0].plain
    assert "1 passed" in plain
    assert "1 failed" in plain
    assert "1 running" in plain
    assert "1 pending" in plain
    assert "evidence" in plain
    assert "observed" in plain
    assert "[passed]" not in plain
    assert any(line.kind == "artifact_done" for line in lines)
    assert any(line.kind == "artifact_failed" for line in lines)
    assert any(line.kind == "artifact_active" for line in lines)
    assert any(line.kind == "artifact_pending" for line in lines)


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
    match_rows = [line for line in lines if line.kind == "match"]
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
    plain = "\n".join(line.plain for line in lines)
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
