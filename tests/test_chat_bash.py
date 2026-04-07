"""Tests for the chat ↔ bash integration.

Two layers:
  1. /bash slash command in _submit dispatches dispatch_bash and
     appends a tool message
  2. _build_message_lines + _paint_chat_row pre-paint tool cards
     into _RenderedRows with prepainted_cells

Hermetic via temp_config_dir; uses real subprocess.run() against
shell builtins (echo, true, pwd) so no mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from successor.bash import ToolCard, dispatch_bash, preview_bash
from successor.chat import SuccessorChat, _Message
from successor.render.cells import Grid
from successor.snapshot import render_grid_to_plain


# ─── /bash slash command ───


def test_bash_command_appends_tool_message(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash echo hello"
    chat._submit()

    # Should have appended: synthetic user echo + tool message
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    card = tool_msgs[0].tool_card
    assert card.verb == "print-text"
    assert card.exit_code == 0
    assert "hello" in card.output


def test_bash_command_no_args_shows_usage(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash"
    chat._submit()
    # No tool card — just a usage hint
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 0
    assert any("usage:" in m.raw_text for m in chat.messages)


def test_bash_command_blank_arg_shows_usage(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash   "
    chat._submit()
    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 0


def test_bash_dangerous_command_appends_refused_card(temp_config_dir: Path) -> None:
    """A dangerous command appends the REFUSED card so the user can
    see what was blocked, plus a synthetic explanation message."""
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash sudo rm -rf /"
    chat._submit()

    tool_msgs = [m for m in chat.messages if m.tool_card is not None]
    assert len(tool_msgs) == 1
    card = tool_msgs[0].tool_card
    assert card.risk == "dangerous"
    # NOT executed because it was refused
    assert not card.executed
    # Refusal message follows
    assert any("refused" in m.raw_text for m in chat.messages)


def test_bash_command_does_not_send_to_model(temp_config_dir: Path) -> None:
    """Tool messages must be marked synthetic so they're never sent
    to the model in the conversation history."""
    chat = SuccessorChat()
    chat.messages = []
    chat.input_buffer = "/bash echo only_for_testing"
    chat._submit()
    for msg in chat.messages:
        if msg.tool_card is not None:
            assert msg.synthetic, "tool messages must be synthetic"


def test_tool_card_message_construction_forces_synthetic() -> None:
    """Constructing a _Message with tool_card auto-sets synthetic=True."""
    card = preview_bash("ls")
    msg = _Message("tool", "", tool_card=card)
    assert msg.synthetic
    assert msg.tool_card is card


# ─── Pre-painted row pipeline ───


def test_tool_card_renders_in_chat_grid(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    chat.messages.append(_Message("user", "show pwd", synthetic=True))
    chat.messages.append(_Message(
        "tool", "", tool_card=dispatch_bash("pwd"),
    ))

    g = Grid(30, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)

    # Card structure visible
    assert "working-directory" in plain
    assert "$ pwd" in plain
    assert "exit 0" in plain


def test_tool_card_row_prepainted_cells_present(temp_config_dir: Path) -> None:
    """The row builder must produce rows with prepainted_cells set
    for tool messages."""
    chat = SuccessorChat()
    chat.messages = []
    chat.messages.append(_Message(
        "tool", "", tool_card=dispatch_bash("echo testing"),
    ))
    rows = chat._build_message_lines(80, chat._current_variant())
    tool_rows = [r for r in rows if r.line_tag == "tool_card"]
    assert len(tool_rows) > 0
    for r in tool_rows:
        assert len(r.prepainted_cells) > 0


def test_multiple_tool_cards_stack(temp_config_dir: Path) -> None:
    """Multiple tool messages render stacked vertically without overlap."""
    chat = SuccessorChat()
    chat.messages = []
    chat.messages.append(_Message("tool", "", tool_card=dispatch_bash("echo first")))
    chat.messages.append(_Message("tool", "", tool_card=dispatch_bash("echo second")))

    g = Grid(40, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)

    assert "first" in plain
    assert "second" in plain
    # Both cards' raw commands visible on their bottom borders
    assert "$ echo first" in plain
    assert "$ echo second" in plain


def test_tool_card_in_session_with_regular_messages(temp_config_dir: Path) -> None:
    """Mix tool cards with regular markdown messages — both render."""
    chat = SuccessorChat()
    chat.messages = []
    chat.messages.append(_Message("user", "what's the cwd?", synthetic=True))
    chat.messages.append(_Message(
        "tool", "", tool_card=dispatch_bash("pwd"),
    ))
    chat.messages.append(_Message(
        "successor", "that's the project root.", synthetic=True,
    ))

    g = Grid(40, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)

    assert "what's the cwd?" in plain
    assert "$ pwd" in plain
    assert "project root" in plain


def test_failed_tool_card_renders_failure_glyph(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    chat.messages.append(_Message(
        "tool", "", tool_card=dispatch_bash("false"),
    ))
    g = Grid(20, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)
    assert "exit 1" in plain
    assert "✗" in plain


def test_unknown_command_renders_with_question_badge(temp_config_dir: Path) -> None:
    """Commands without a registered parser render as the generic
    'bash ?' card and still execute."""
    chat = SuccessorChat()
    chat.messages = []
    chat.messages.append(_Message(
        "tool", "", tool_card=dispatch_bash("wc -l README.md"),
    ))
    g = Grid(20, 100)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)
    # The generic bash card has the verb "bash"
    assert "bash" in plain
    assert "$ wc -l README.md" in plain
    # And the question badge
    assert "?" in plain
