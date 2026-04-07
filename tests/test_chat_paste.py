"""Tests for the chat input's paste handling.

Bracketed paste content arrives as a single coalesced KeyEvent (the
KeyDecoder buffers everything between CSI 200~ and CSI 201~ into one
event). The chat's _handle_key_event normalizes that content before
appending it to input_buffer:

  - \\r\\n / lone \\r → \\n  (Windows / classic-Mac line endings)
  - \\t              → 4 spaces (most pasted code uses 4-space indent)
  - orphan focus tails ([I, [O) leaked by some terminals → stripped
  - other control chars below 0x20 → dropped

The painter shows a "↑ N more lines" badge on the topmost visible
input row when a long paste exceeds the input box's row cap.
"""

from __future__ import annotations

from pathlib import Path

from successor.chat import INPUT_MAX_ROWS, SuccessorChat
from successor.input.keys import Key, KeyEvent
from successor.render.cells import Grid
from successor.snapshot import render_grid_to_plain


def _paste(chat: SuccessorChat, text: str) -> None:
    """Drive a full paste through _handle_key_event the same way the
    KeyDecoder would: PASTE_START, then one char-bearing event with
    the coalesced content, then PASTE_END."""
    chat._handle_key_event(KeyEvent(key=Key.PASTE_START))
    chat._handle_key_event(KeyEvent(char=text))
    chat._handle_key_event(KeyEvent(key=Key.PASTE_END))


def test_paste_normalizes_crlf(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.input_buffer = ""
    _paste(chat, "line1\r\nline2\r\nline3")
    assert chat.input_buffer == "line1\nline2\nline3"


def test_paste_normalizes_lone_cr(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.input_buffer = ""
    _paste(chat, "old\rmac\rstyle")
    assert chat.input_buffer == "old\nmac\nstyle"


def test_paste_expands_tabs_to_four_spaces(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.input_buffer = ""
    _paste(chat, "def f():\n\treturn 42")
    # Tab → 4 spaces; newline preserved.
    assert chat.input_buffer == "def f():\n    return 42"


def test_paste_strips_orphan_focus_tail(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.input_buffer = ""
    # Some terminals leak focus events ([I focus-in / [O focus-out)
    # right before the paste-end marker. The KeyDecoder won't pull them
    # out because they're inside the paste body. Strip in the chat.
    _paste(chat, "hello world\x1b[O")
    assert chat.input_buffer == "hello world"

    chat.input_buffer = ""
    _paste(chat, "again\x1b[I")
    assert chat.input_buffer == "again"


def test_paste_drops_other_control_chars(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.input_buffer = ""
    _paste(chat, "ok\x07bell\x00null\x08bs")
    assert chat.input_buffer == "okbellnullbs"


def test_paste_preserves_unicode(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.input_buffer = ""
    _paste(chat, "héllo · wörld 🐺")
    assert chat.input_buffer == "héllo · wörld 🐺"


def test_paste_enter_inside_paste_is_literal_newline(temp_config_dir: Path) -> None:
    """During a paste the Enter key must NOT submit. The chat tracks
    _in_paste between PASTE_START and PASTE_END so multi-line pastes
    don't fire submit on the first newline."""
    chat = SuccessorChat()
    chat.input_buffer = ""
    chat._handle_key_event(KeyEvent(key=Key.PASTE_START))
    chat._handle_key_event(KeyEvent(char="line one"))
    chat._handle_key_event(KeyEvent(key=Key.ENTER))  # mid-paste enter
    chat._handle_key_event(KeyEvent(char="line two"))
    chat._handle_key_event(KeyEvent(key=Key.PASTE_END))
    assert chat.input_buffer == "line one\nline two"


def test_paste_overflow_badge_appears(temp_config_dir: Path) -> None:
    """Pasting more lines than the input cap shows a '↑ N more lines'
    badge on the topmost visible row of the input area."""
    chat = SuccessorChat()
    chat.input_buffer = ""
    # 20 lines well exceeds the 8-row INPUT_MAX_ROWS cap.
    _paste(chat, "\n".join(f"line{i}" for i in range(20)))
    assert chat.input_buffer.count("\n") == 19

    grid = Grid(rows=24, cols=80)
    chat._app_size = (80, 24)  # type: ignore[attr-defined]
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    assert "more lines" in plain
    # 20 wrapped rows minus 8 visible = 12 hidden
    assert "↑ 12 more lines" in plain


def test_paste_no_badge_when_inside_cap(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.input_buffer = ""
    _paste(chat, "one\ntwo\nthree")
    grid = Grid(rows=24, cols=80)
    chat._app_size = (80, 24)  # type: ignore[attr-defined]
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    assert "more lines" not in plain


def test_paste_overflow_singular_label(temp_config_dir: Path) -> None:
    """Exactly 1 hidden line should say 'line' not 'lines'."""
    chat = SuccessorChat()
    chat.input_buffer = ""
    # INPUT_MAX_ROWS + 1 = 9 lines means exactly 1 hidden
    _paste(chat, "\n".join(f"l{i}" for i in range(INPUT_MAX_ROWS + 1)))
    grid = Grid(rows=24, cols=80)
    chat._app_size = (80, 24)  # type: ignore[attr-defined]
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    assert "↑ 1 more line" in plain
    assert "↑ 1 more lines" not in plain
