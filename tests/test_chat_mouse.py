"""Tests for mouse default / migration behavior in SuccessorChat."""

from __future__ import annotations

import json
from pathlib import Path

from successor.chat import SuccessorChat, _Message, find_density
from successor.input.keys import Key, KeyEvent, MouseButton, MouseEvent
from successor.profiles import Profile
from successor.render.cells import Grid


class _IdleStream:
    def __init__(self) -> None:
        self.reasoning_so_far = ""
        self.tool_calls_so_far: list[dict] = []

    def drain(self) -> list[object]:
        return []


def test_chat_defaults_mouse_off_when_config_missing(temp_config_dir: Path) -> None:
    chat = SuccessorChat(profile=Profile(name="mouse-test"))
    assert chat._mouse_enabled is False
    assert chat.term.mouse_reporting is False


def test_chat_preserves_v2_mouse_false(temp_config_dir: Path) -> None:
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "theme": "steel",
        "display_mode": "light",
        "density": "normal",
        "mouse": False,
    }))

    chat = SuccessorChat(profile=Profile(name="mouse-test"))
    assert chat._mouse_enabled is False
    assert chat.term.mouse_reporting is False


def test_chat_preserves_v2_mouse_true(temp_config_dir: Path) -> None:
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "theme": "steel",
        "display_mode": "light",
        "density": "normal",
        "mouse": True,
    }))

    chat = SuccessorChat(profile=Profile(name="mouse-test"))
    assert chat._mouse_enabled is True
    assert chat.term.mouse_reporting is True


def test_wheel_events_scroll_chat_history(temp_config_dir: Path) -> None:
    chat = SuccessorChat(profile=Profile(name="mouse-test"))
    chat.messages = [
        _Message("user", f"line {i}")
        for i in range(40)
    ]

    grid = Grid(12, 80)
    chat.on_tick(grid)
    assert chat._max_scroll() > 0
    assert chat.scroll_offset == 0

    chat._handle_mouse_event(MouseEvent(
        button=MouseButton.WHEEL_UP,
        col=0,
        row=0,
        pressed=True,
    ))
    assert chat.scroll_offset > 0

    chat._handle_mouse_event(MouseEvent(
        button=MouseButton.WHEEL_DOWN,
        col=0,
        row=0,
        pressed=True,
    ))
    assert chat.scroll_offset == 0


def test_streaming_rows_expand_scroll_headroom(temp_config_dir: Path) -> None:
    chat = SuccessorChat(profile=Profile(name="mouse-test"))
    compact = find_density("compact")
    assert compact is not None
    chat.density = compact
    chat.messages = [
        _Message("user", f"line {i}")
        for i in range(4)
    ]
    chat._stream = _IdleStream()  # type: ignore[assignment]
    chat._stream_reasoning_chars = 42

    grid = Grid(8, 80)
    chat.on_tick(grid)

    assert chat._stream is not None
    assert chat._max_scroll() > 0
    assert chat.scroll_offset == 0

    chat._handle_key_event(KeyEvent(key=Key.UP))

    assert chat.scroll_offset > 0
