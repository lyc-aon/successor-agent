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


# ─── Multi-command blocks ───


def test_multi_command_block() -> None:
    d = BashStreamDetector()
    out = d.feed("```bash\ncd /tmp\nls -la\npwd\n```")
    out += d.flush()
    assert out == ["cd /tmp", "ls -la", "pwd"]


def test_block_with_comments() -> None:
    """Comments are stripped, real commands are kept."""
    d = BashStreamDetector()
    out = d.feed("```bash\n# this is a comment\nls\n# another\necho hi\n```")
    out += d.flush()
    assert out == ["ls", "echo hi"]


def test_block_with_blank_lines() -> None:
    d = BashStreamDetector()
    out = d.feed("```bash\nls\n\necho hi\n\n```")
    out += d.flush()
    assert out == ["ls", "echo hi"]


def test_backslash_continuation() -> None:
    d = BashStreamDetector()
    out = d.feed("```bash\nfind /tmp \\\n  -name foo\n```")
    out += d.flush()
    assert out == ["find /tmp -name foo"]


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
