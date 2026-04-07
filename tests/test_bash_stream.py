"""Tests for agent/bash_stream.py — streaming bash block detector.

Verifies that the state machine correctly handles fenced ```bash
blocks arriving in arbitrary chunk fragmentations, including:
  - whole-block-in-one-chunk
  - fence split across chunks
  - content split across chunks
  - one-character-at-a-time drip
  - non-bash language blocks (must be ignored)
  - inline backticks (must be ignored)
  - multi-command blocks
  - comments and continuations
"""

from __future__ import annotations

from successor.agent.bash_stream import BashStreamDetector


# ─── Single block, intact chunk ───


def test_simple_block() -> None:
    d = BashStreamDetector()
    out = d.feed("```bash\nls -la\n```")
    out += d.flush()
    assert out == ["ls -la"]


def test_block_with_surrounding_text() -> None:
    d = BashStreamDetector()
    out = d.feed("Let me check.\n\n```bash\npwd\n```\n\nDone.")
    out += d.flush()
    assert out == ["pwd"]


def test_empty_block() -> None:
    """An empty bash block produces no commands."""
    d = BashStreamDetector()
    out = d.feed("```bash\n```")
    out += d.flush()
    assert out == []


# ─── Fragmentation ───


def test_split_mid_open_fence() -> None:
    d = BashStreamDetector()
    out = []
    out += d.feed("``")
    out += d.feed("`bash\nls\n```")
    out += d.flush()
    assert out == ["ls"]


def test_split_mid_lang_tag() -> None:
    d = BashStreamDetector()
    out = []
    out += d.feed("```ba")
    out += d.feed("sh\nls\n```")
    out += d.flush()
    assert out == ["ls"]


def test_split_mid_content() -> None:
    d = BashStreamDetector()
    out = []
    out += d.feed("```bash\ncat ")
    out += d.feed("README")
    out += d.feed(".md\n```")
    out += d.flush()
    assert out == ["cat README.md"]


def test_split_mid_close_fence() -> None:
    d = BashStreamDetector()
    out = []
    out += d.feed("```bash\nls\n``")
    out += d.feed("`")
    out += d.flush()
    assert out == ["ls"]


def test_one_char_at_a_time() -> None:
    d = BashStreamDetector()
    out = []
    text = "```bash\necho hi\n```"
    for ch in text:
        out += d.feed(ch)
    out += d.flush()
    assert out == ["echo hi"]


# ─── Edge cases ───


def test_no_trailing_newline_on_close() -> None:
    """Models often emit ``` as the absolute last token of a message,
    with no trailing newline."""
    d = BashStreamDetector()
    out = d.feed("```bash\nls\n```")
    out += d.flush()
    assert out == ["ls"]


def test_multiple_blocks() -> None:
    d = BashStreamDetector()
    out = d.feed("```bash\nfirst\n```\nbetween\n```bash\nsecond\n```")
    out += d.flush()
    assert out == ["first", "second"]


def test_inline_backticks_ignored() -> None:
    """Single-line ` ``` ` style is inline code, not a block."""
    d = BashStreamDetector()
    out = d.feed("Run `ls` first.")
    out += d.flush()
    assert out == []


def test_python_block_ignored() -> None:
    d = BashStreamDetector()
    out = d.feed("```python\nprint(1)\n```")
    out += d.flush()
    assert out == []


def test_unlanguaged_block_ignored() -> None:
    """``` with no language tag is plain code, not bash."""
    d = BashStreamDetector()
    out = d.feed("```\nls\n```")
    out += d.flush()
    assert out == []


def test_sh_alias_recognized() -> None:
    d = BashStreamDetector()
    out = d.feed("```sh\nls\n```")
    out += d.flush()
    assert out == ["ls"]


def test_shell_alias_recognized() -> None:
    d = BashStreamDetector()
    out = d.feed("```shell\nls\n```")
    out += d.flush()
    assert out == ["ls"]


# ─── Multi-line blocks — yielded as ONE command ───
#
# The detector used to split on newlines and yield N commands from
# a block containing N lines. That heuristic broke heredocs, quoted
# multi-line strings, functions, and every other bash construct
# that spans lines. The fix: yield the whole block as one command
# and let bash parse it via subprocess(shell=True).


def test_multi_command_block_yields_one_command() -> None:
    """A block with several commands is passed to bash as a single
    script. bash runs them in sequence; the tool card shows the
    raw multi-line script on its bottom border and the combined
    output above."""
    d = BashStreamDetector()
    out = d.feed("```bash\ncd /tmp\nls -la\npwd\n```")
    out += d.flush()
    assert out == ["cd /tmp\nls -la\npwd"]


def test_block_with_comments_yields_one_command() -> None:
    """Comments are preserved — bash ignores them natively."""
    d = BashStreamDetector()
    out = d.feed("```bash\n# this is a comment\nls\n# another\necho hi\n```")
    out += d.flush()
    assert out == ["# this is a comment\nls\n# another\necho hi"]


def test_block_with_blank_lines_yields_one_command() -> None:
    """Blank lines don't split; they're just part of the script."""
    d = BashStreamDetector()
    out = d.feed("```bash\nls\n\necho hi\n\n```")
    out += d.flush()
    # Trailing blank lines are stripped by .strip() but internal
    # blank lines stay intact
    assert out == ["ls\n\necho hi"]


def test_backslash_continuation_yields_one_command() -> None:
    """Backslash continuations are preserved — bash joins them."""
    d = BashStreamDetector()
    out = d.feed("```bash\nfind /tmp \\\n  -name foo\n```")
    out += d.flush()
    assert out == ["find /tmp \\\n  -name foo"]


def test_heredoc_block_yields_one_command() -> None:
    """REGRESSION: a heredoc writing HTML to a file used to explode
    into one command per heredoc line, each failing with
    'command not found' on the HTML tag lines. After the fix, the
    whole heredoc is one command passed straight to bash."""
    d = BashStreamDetector()
    sample = (
        "```bash\n"
        "cat > /tmp/foo.html <<'EOF'\n"
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head><title>X</title></head>\n"
        "<body><h1>Hi</h1></body>\n"
        "</html>\n"
        "EOF\n"
        "```"
    )
    out = d.feed(sample)
    out += d.flush()
    assert len(out) == 1
    assert "cat > /tmp/foo.html <<'EOF'" in out[0]
    assert "<!DOCTYPE html>" in out[0]
    assert out[0].endswith("EOF")


def test_bash_function_definition_yields_one_command() -> None:
    """Bash function definitions span lines and can't be split
    line-by-line — they'd fail at `() {` alone."""
    d = BashStreamDetector()
    sample = (
        "```bash\n"
        "greet() {\n"
        "  echo hello\n"
        "  echo world\n"
        "}\n"
        "greet\n"
        "```"
    )
    out = d.feed(sample)
    out += d.flush()
    assert len(out) == 1
    assert "greet()" in out[0]
    assert "greet" in out[0]


def test_if_then_fi_block_yields_one_command() -> None:
    """Same deal for if/then/fi — the splitter would dispatch
    `if [ -f foo ]; then` as a standalone 'command'."""
    d = BashStreamDetector()
    sample = (
        "```bash\n"
        "if [ -f README.md ]; then\n"
        "  echo exists\n"
        "else\n"
        "  echo missing\n"
        "fi\n"
        "```"
    )
    out = d.feed(sample)
    out += d.flush()
    assert len(out) == 1
    assert "if [ -f README.md ]; then" in out[0]
    assert "fi" in out[0]


# ─── State management ───


def test_completed_accumulates_across_calls() -> None:
    d = BashStreamDetector()
    d.feed("```bash\nfirst\n```")
    d.feed("```bash\nsecond\n```")
    assert d.completed() == ["first", "second"]


def test_reset_clears_state() -> None:
    d = BashStreamDetector()
    d.feed("```bash\nls\n```")
    assert len(d.completed()) > 0
    d.reset()
    assert len(d.completed()) == 0
    assert not d.is_inside_block()


def test_is_inside_block_during_streaming() -> None:
    d = BashStreamDetector()
    d.feed("```bash\nls")
    assert d.is_inside_block()
    d.feed("\n```")
    assert not d.is_inside_block()
