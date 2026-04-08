"""Tests for input history recall (Up/Down arrow shell-style).

The state machine is:

    Normal mode → empty buffer + Up → Recall mode (loads most recent)
    Recall mode + Up → older entry (no-op at oldest)
    Recall mode + Down → newer entry, then back to draft past newest
    Recall mode + any editing key → exit recall, keep buffer
    Recall mode + Esc → exit recall, restore the saved draft
    Submit → exit recall, add buffer to history (deduped)

These tests drive the chat's _handle_key_event handler with synthetic
KeyEvents and assert the input_buffer + recall index after each
transition. Hermetic via temp_config_dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from successor.chat import INPUT_HISTORY_MAX, SuccessorChat
from successor.input.keys import Key, KeyEvent
from successor.profiles import Profile


# ─── Fixture helper ───


def _new_chat(temp_config_dir: Path) -> SuccessorChat:
    """Build a fresh chat with a stub profile.

    The profile bypasses the registry resolution so the test does
    not depend on whichever default profile happens to be installed.
    """
    return SuccessorChat(profile=Profile(name="history-test"))


# ─── _history_add: ring buffer behavior ───


def test_history_add_basic(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("hello")
    chat._history_add("world")
    assert chat._input_history == ["hello", "world"]


def test_history_add_skips_empty(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("")
    chat._history_add("   ")  # whitespace-only also skipped (rstripped)
    chat._history_add("real")
    assert chat._input_history == ["real"]


def test_history_add_dedupes_consecutive_duplicates(temp_config_dir: Path) -> None:
    """Hitting Enter on the same text twice should not double the entry."""
    chat = _new_chat(temp_config_dir)
    chat._history_add("same")
    chat._history_add("same")
    chat._history_add("same")
    assert chat._input_history == ["same"]


def test_history_add_keeps_non_consecutive_duplicates(temp_config_dir: Path) -> None:
    """A repeat after some other entry is preserved as a separate entry."""
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
    # Oldest entries dropped, newest preserved
    assert chat._input_history[-1] == f"entry-{INPUT_HISTORY_MAX + 50 - 1}"
    assert chat._input_history[0] == f"entry-{50}"


# ─── Up arrow with empty buffer enters recall ───


def test_up_with_empty_buffer_enters_recall(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("first")
    chat._history_add("second")
    chat._history_add("third")
    chat.input_buffer = ""

    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.input_buffer == "third"  # most recent
    assert chat._input_history_idx == 2
    assert chat._history_in_recall_mode()


def test_up_with_non_empty_buffer_scrolls_chat(temp_config_dir: Path) -> None:
    """A user mid-draft should not lose their text on accidental Up."""
    chat = _new_chat(temp_config_dir)
    chat._history_add("first")
    chat.input_buffer = "draft in progress"

    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.input_buffer == "draft in progress"  # untouched
    assert chat._input_history_idx is None
    assert not chat._history_in_recall_mode()


def test_up_with_empty_history_does_nothing(temp_config_dir: Path) -> None:
    """Empty history + Up should be a clean no-op (chat scrolls instead)."""
    chat = _new_chat(temp_config_dir)
    assert chat._input_history == []
    chat.input_buffer = ""

    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.input_buffer == ""
    assert chat._input_history_idx is None


# ─── Up arrow navigation in recall mode ───


def test_up_in_recall_walks_older(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("oldest")
    chat._history_add("middle")
    chat._history_add("newest")
    chat.input_buffer = ""

    # First Up → newest
    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.input_buffer == "newest"
    # Second Up → middle
    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.input_buffer == "middle"
    # Third Up → oldest
    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.input_buffer == "oldest"
    assert chat._input_history_idx == 0


def test_up_at_oldest_is_noop(temp_config_dir: Path) -> None:
    """Pressing Up at the oldest entry should not crash or wrap."""
    chat = _new_chat(temp_config_dir)
    chat._history_add("only")
    chat.input_buffer = ""

    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.input_buffer == "only"
    assert chat._input_history_idx == 0

    # Already at oldest. Up should be a no-op.
    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.input_buffer == "only"
    assert chat._input_history_idx == 0


# ─── Down arrow navigation in recall mode ───


def test_down_in_recall_walks_newer(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("oldest")
    chat._history_add("middle")
    chat._history_add("newest")
    chat.input_buffer = ""

    # Walk to oldest
    chat._handle_key_event(KeyEvent(key=Key.UP))  # newest
    chat._handle_key_event(KeyEvent(key=Key.UP))  # middle
    chat._handle_key_event(KeyEvent(key=Key.UP))  # oldest
    assert chat.input_buffer == "oldest"

    # Down → middle
    chat._handle_key_event(KeyEvent(key=Key.DOWN))
    assert chat.input_buffer == "middle"
    # Down → newest
    chat._handle_key_event(KeyEvent(key=Key.DOWN))
    assert chat.input_buffer == "newest"


def test_down_past_newest_restores_draft(temp_config_dir: Path) -> None:
    """Down past the newest entry should restore the draft and exit recall."""
    chat = _new_chat(temp_config_dir)
    chat._history_add("entry-1")
    chat._history_add("entry-2")
    chat.input_buffer = "in-progress draft"

    # Save the draft by going into recall mode (need empty buffer first)
    chat.input_buffer = ""
    # Set the draft directly to simulate "user had typed something but cleared it"
    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.input_buffer == "entry-2"

    # Down past newest should exit recall AND restore draft (empty in this case)
    chat._handle_key_event(KeyEvent(key=Key.DOWN))
    assert chat.input_buffer == ""
    assert chat._input_history_idx is None
    assert not chat._history_in_recall_mode()


def test_down_outside_recall_scrolls_chat(temp_config_dir: Path) -> None:
    """Down arrow when not in recall mode should not touch the input buffer."""
    chat = _new_chat(temp_config_dir)
    chat._history_add("foo")
    chat.input_buffer = "typing"

    chat._handle_key_event(KeyEvent(key=Key.DOWN))
    assert chat.input_buffer == "typing"
    assert chat._input_history_idx is None


# ─── Editing keys in recall mode ───


def test_typing_in_recall_exits_and_appends(temp_config_dir: Path) -> None:
    """A printable keypress in recall mode should drop the recall flag
    and append the new char to the recalled text."""
    chat = _new_chat(temp_config_dir)
    chat._history_add("hello")
    chat.input_buffer = ""

    chat._handle_key_event(KeyEvent(key=Key.UP))  # enters recall, buffer = "hello"
    assert chat._history_in_recall_mode()

    # Type a space + extra char
    chat._handle_key_event(KeyEvent(char=" "))
    chat._handle_key_event(KeyEvent(char="!"))
    assert chat.input_buffer == "hello !"
    assert not chat._history_in_recall_mode()


def test_backspace_in_recall_exits_and_deletes(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("hello")
    chat.input_buffer = ""

    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.input_buffer == "hello"

    chat._handle_key_event(KeyEvent(key=Key.BACKSPACE))
    assert chat.input_buffer == "hell"
    assert not chat._history_in_recall_mode()


def test_esc_in_recall_restores_draft(temp_config_dir: Path) -> None:
    """Esc is the 'I changed my mind' escape hatch. Buffer reverts."""
    chat = _new_chat(temp_config_dir)
    chat._history_add("from history")
    # Simulate the user typing something, then clearing it, then Up
    chat.input_buffer = ""
    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.input_buffer == "from history"

    # Esc should restore the draft (empty in this case)
    chat._handle_key_event(KeyEvent(key=Key.ESC))
    assert chat.input_buffer == ""
    assert not chat._history_in_recall_mode()


def test_esc_with_in_progress_draft_restores_it(temp_config_dir: Path) -> None:
    """If the user had typed something then went into recall, Esc
    should bring back the typed text."""
    chat = _new_chat(temp_config_dir)
    chat._history_add("recalled")
    # User started typing something, then cleared it (empty buffer for Up)
    # but we want to verify draft restoration with a non-empty draft.
    # Set the draft directly (the recall enter helper saves whatever is
    # currently in the buffer).
    chat.input_buffer = "WIP: my real draft"
    # Manually save draft and enter recall (the public path requires
    # empty buffer for Up to trigger; this tests the draft-save mechanism)
    chat._input_history_draft = chat.input_buffer
    chat._input_history_idx = 0
    chat.input_buffer = chat._input_history[0]
    assert chat.input_buffer == "recalled"

    # Esc should restore the original draft
    chat._handle_key_event(KeyEvent(key=Key.ESC))
    assert chat.input_buffer == "WIP: my real draft"
    assert not chat._history_in_recall_mode()


# ─── Submit interactions ───


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


def test_submit_clears_recall_mode(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._history_add("old")
    chat.input_buffer = ""
    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat._history_in_recall_mode()

    chat._submit()
    assert not chat._history_in_recall_mode()
    assert chat._input_history_idx is None


def test_submit_recalled_text_unchanged_does_not_double_history(
    temp_config_dir: Path,
) -> None:
    """Recalling a previous entry and submitting it as-is should not
    add a duplicate (the dedupe check blocks the consecutive repeat)."""
    chat = _new_chat(temp_config_dir)
    chat._history_add("repeat me")
    chat.input_buffer = ""
    chat._handle_key_event(KeyEvent(key=Key.UP))
    assert chat.input_buffer == "repeat me"

    chat._submit()
    assert chat._input_history == ["repeat me"]
