"""Tests for the dedicated prompt-history browser."""

from __future__ import annotations

from pathlib import Path

from successor.chat import INPUT_HISTORY_MAX, SuccessorChat, _Message
from successor.input.keys import Key, KeyEvent, MOD_CTRL
from successor.profiles import Profile
from successor.render.cells import Grid
from successor.snapshot import render_grid_to_plain


def _new_chat(temp_config_dir: Path) -> SuccessorChat:
    return SuccessorChat(profile=Profile(name="history-test"))


def test_history_add_basic(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("hello")
    chat._history_add("world")
    assert chat._input_history == ["hello", "world"]


def test_history_add_skips_empty(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("")
    chat._history_add("   ")
    chat._history_add("real")
    assert chat._input_history == ["real"]


def test_history_add_dedupes_consecutive_duplicates(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("same")
    chat._history_add("same")
    chat._history_add("same")
    assert chat._input_history == ["same"]


def test_history_add_keeps_non_consecutive_duplicates(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("first")
    chat._history_add("second")
    chat._history_add("first")
    assert chat._input_history == ["first", "second", "first"]


def test_history_add_caps_at_max(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    for i in range(INPUT_HISTORY_MAX + 50):
        chat._history_add(f"entry-{i}")
    assert len(chat._input_history) == INPUT_HISTORY_MAX
    assert chat._input_history[-1] == f"entry-{INPUT_HISTORY_MAX + 50 - 1}"
    assert chat._input_history[0] == "entry-50"


def test_ctrl_r_opens_history_overlay(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("oldest")
    chat._history_add("newest")

    chat._handle_key_event(KeyEvent(char="r", mods=MOD_CTRL))

    assert chat._history_overlay_open is True
    assert chat._history_overlay_selected == 0
    assert chat._history_overlay_entries() == ["newest", "oldest"]


def test_ctrl_r_with_empty_history_shows_hint(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)

    chat._handle_key_event(KeyEvent(char="r", mods=MOD_CTRL))

    assert chat._history_overlay_open is False
    assert chat.messages[-1].role == "successor"
    assert "history is empty" in (chat.messages[-1].raw_text or "")


def test_up_and_down_scroll_chat_instead_of_recalling_history(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("prior prompt")
    chat.messages = [_Message("user", f"line {i}") for i in range(40)]

    grid = Grid(12, 80)
    chat.on_tick(grid)
    assert chat._max_scroll() > 0
    assert chat.scroll_offset == 0

    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.scroll_offset == 1
    assert chat.input_buffer == ""
    assert chat._history_overlay_open is False

    chat._handle_key_event(KeyEvent(key=Key.DOWN))
    assert chat.scroll_offset == 0


def test_history_overlay_enter_loads_selected_entry(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("first")
    chat._history_add("second")
    chat._history_add("third")

    chat._handle_key_event(KeyEvent(char="r", mods=MOD_CTRL))
    chat._handle_key_event(KeyEvent(key=Key.DOWN))
    chat._handle_key_event(KeyEvent(key=Key.ENTER))

    assert chat._history_overlay_open is False
    assert chat.input_buffer == "second"


def test_history_overlay_esc_restores_draft(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("build the app")
    chat.input_buffer = "draft in progress"

    chat._handle_key_event(KeyEvent(char="r", mods=MOD_CTRL))
    assert chat._history_overlay_open is True

    chat._handle_key_event(KeyEvent(key=Key.ESC))

    assert chat._history_overlay_open is False
    assert chat.input_buffer == "draft in progress"


def test_history_overlay_filtering_updates_selection_and_accepts(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("npm install")
    chat._history_add("pytest -q")
    chat._history_add("python -m http.server")

    chat._handle_key_event(KeyEvent(char="r", mods=MOD_CTRL))
    for ch in "pytest":
        chat._handle_key_event(KeyEvent(char=ch))
    chat._handle_key_event(KeyEvent(key=Key.ENTER))

    assert chat.input_buffer == "pytest -q"
    assert chat._history_overlay_open is False


def test_slash_history_opens_overlay_with_initial_query(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("git status")
    chat._history_add("git commit -m test")
    chat.input_buffer = "/history commit"

    chat._submit()

    assert chat._history_overlay_open is True
    assert chat._history_overlay_query == "commit"
    assert chat._history_overlay_entries() == ["git commit -m test"]


def test_history_overlay_renders_to_grid(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("pnpm test")
    chat._history_add("python -m http.server 9987")
    chat.input_buffer = "draft for later"
    chat._handle_key_event(KeyEvent(char="r", mods=MOD_CTRL))

    grid = Grid(28, 120)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)

    assert "history browser" in plain
    assert "python -m http.server 9987" in plain
    assert "draft for later" in plain


def test_submit_adds_to_history(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat.input_buffer = "the message"
    chat._submit()
    assert "the message" in chat._input_history


def test_submit_dedupes_against_recent(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat.input_buffer = "same"
    chat._submit()
    chat.input_buffer = "same"
    chat._submit()
    assert chat._input_history == ["same"]
