from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from successor.chat import SuccessorChat, _Message
from successor.file_tools import (
    FILE_UNCHANGED_STUB,
    FileReadTracker,
    FileReadStateEntry,
    FileToolError,
    edit_file_preview_card,
    note_non_read_tool_call,
    read_file_preview_card,
    run_edit_file,
    run_read_file,
    run_write_file,
    write_file_preview_card,
)
from successor.profiles import Profile


@dataclass
class _MockClient:
    base_url: str = "http://mock"
    model: str = "mock-model"

    def stream_chat(self, messages, *, max_tokens=None, temperature=None, timeout=None, extra=None, tools=None):  # noqa: ARG002
        raise AssertionError("stream_chat should not be called in this test")


def _pump_until_idle(chat: SuccessorChat, *, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        chat._pump_running_tools()
        if not chat._running_tools:
            return
        time.sleep(0.02)
    raise AssertionError("chat did not settle")


def test_read_file_updates_state_and_formats_line_numbers(tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    preview = read_file_preview_card({"file_path": str(target)}, tool_call_id="call_read_1")
    state: dict[str, FileReadStateEntry] = {}

    result = run_read_file(
        {"file_path": str(target)},
        preview=preview,
        read_state=state,
        working_directory=str(tmp_path),
    )

    card = result.final_card
    assert card is not None
    assert card.tool_name == "read_file"
    assert "File: " in card.output
    assert "1 | alpha" in card.output
    assert "3 | gamma" in card.output
    assert state[str(target)].partial is False
    assert state[str(target)].content == "alpha\nbeta\ngamma\n"


def test_read_file_partial_marks_state_partial(tmp_path: Path) -> None:
    target = tmp_path / "chunked.txt"
    target.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    preview = read_file_preview_card(
        {"file_path": str(target), "offset": 2, "limit": 2},
        tool_call_id="call_read_2",
    )
    state: dict[str, FileReadStateEntry] = {}

    result = run_read_file(
        {"file_path": str(target), "offset": 2, "limit": 2},
        preview=preview,
        read_state=state,
        working_directory=str(tmp_path),
    )

    assert result.metadata is not None
    assert result.metadata["partial"] is True
    assert "View: partial" in result.output
    assert state[str(target)].partial is True


def test_read_file_returns_stub_for_unchanged_duplicate_full_read(tmp_path: Path) -> None:
    target = tmp_path / "stable.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    preview = read_file_preview_card({"file_path": str(target)}, tool_call_id="call_read_dup")
    state: dict[str, FileReadStateEntry] = {}
    tracker = FileReadTracker()

    first = run_read_file(
        {"file_path": str(target)},
        preview=preview,
        read_state=state,
        read_tracker=tracker,
        working_directory=str(tmp_path),
    )
    second = run_read_file(
        {"file_path": str(target)},
        preview=preview,
        read_state=state,
        read_tracker=tracker,
        working_directory=str(tmp_path),
    )

    assert "1 | one" in first.output
    assert second.output == FILE_UNCHANGED_STUB
    assert second.metadata is not None
    assert second.metadata["unchanged"] is True


def test_read_file_warns_then_blocks_after_repeated_identical_reads(tmp_path: Path) -> None:
    target = tmp_path / "loop.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    preview = read_file_preview_card({"file_path": str(target)}, tool_call_id="call_loop")
    state: dict[str, FileReadStateEntry] = {}
    tracker = FileReadTracker()

    run_read_file(
        {"file_path": str(target)},
        preview=preview,
        read_state=state,
        read_tracker=tracker,
        working_directory=str(tmp_path),
    )
    run_read_file(
        {"file_path": str(target)},
        preview=preview,
        read_state=state,
        read_tracker=tracker,
        working_directory=str(tmp_path),
    )
    warned = run_read_file(
        {"file_path": str(target)},
        preview=preview,
        read_state=state,
        read_tracker=tracker,
        working_directory=str(tmp_path),
    )

    assert warned.metadata is not None
    assert warned.metadata["repeated_read_count"] == 3
    assert warned.output.startswith("Warning: this is the third consecutive identical read")

    with pytest.raises(FileToolError, match="4 times consecutively"):
        run_read_file(
            {"file_path": str(target)},
            preview=preview,
            read_state=state,
            read_tracker=tracker,
            working_directory=str(tmp_path),
        )


def test_non_read_tool_call_resets_repeated_read_tracker(tmp_path: Path) -> None:
    target = tmp_path / "reset.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    preview = read_file_preview_card({"file_path": str(target)}, tool_call_id="call_reset")
    state: dict[str, FileReadStateEntry] = {}
    tracker = FileReadTracker()

    run_read_file(
        {"file_path": str(target)},
        preview=preview,
        read_state=state,
        read_tracker=tracker,
        working_directory=str(tmp_path),
    )
    run_read_file(
        {"file_path": str(target)},
        preview=preview,
        read_state=state,
        read_tracker=tracker,
        working_directory=str(tmp_path),
    )
    note_non_read_tool_call(tracker)
    result = run_read_file(
        {"file_path": str(target)},
        preview=preview,
        read_state=state,
        read_tracker=tracker,
        working_directory=str(tmp_path),
    )

    assert result.metadata is not None
    assert result.metadata["repeated_read_count"] == 1
    assert not result.output.startswith("Warning:")


def test_write_file_creates_new_file_without_prior_read(tmp_path: Path) -> None:
    target = tmp_path / "fresh.txt"
    preview = write_file_preview_card({"file_path": str(target), "content": "hello"}, tool_call_id="call_write_new")
    state: dict[str, FileReadStateEntry] = {}

    result = run_write_file(
        {"file_path": str(target), "content": "hello"},
        preview=preview,
        read_state=state,
        working_directory=str(tmp_path),
    )

    assert target.read_text(encoding="utf-8") == "hello"
    assert result.final_card is not None
    assert result.final_card.change_artifact is not None
    assert state[str(target)].partial is False


def test_write_file_requires_prior_full_read_for_existing_files(tmp_path: Path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("before", encoding="utf-8")
    preview = write_file_preview_card({"file_path": str(target), "content": "after"}, tool_call_id="call_write_existing")

    with pytest.raises(FileToolError, match="Read it first"):
        run_write_file(
            {"file_path": str(target), "content": "after"},
            preview=preview,
            read_state={},
            working_directory=str(tmp_path),
        )


def test_write_file_rejects_stale_file(tmp_path: Path) -> None:
    target = tmp_path / "stale.txt"
    target.write_text("before", encoding="utf-8")
    preview = read_file_preview_card({"file_path": str(target)}, tool_call_id="call_seed")
    state: dict[str, FileReadStateEntry] = {}
    run_read_file(
        {"file_path": str(target)},
        preview=preview,
        read_state=state,
        working_directory=str(tmp_path),
    )
    target.write_text("changed elsewhere", encoding="utf-8")

    with pytest.raises(FileToolError, match="modified since it was read"):
        run_write_file(
            {"file_path": str(target), "content": "after"},
            preview=write_file_preview_card(
                {"file_path": str(target), "content": "after"},
                tool_call_id="call_write_stale",
            ),
            read_state=state,
            working_directory=str(tmp_path),
        )


def test_edit_file_rejects_partial_read_and_ambiguous_match(tmp_path: Path) -> None:
    target = tmp_path / "ambiguous.txt"
    target.write_text("two\ntwo\n", encoding="utf-8")
    state = {
        str(target): FileReadStateEntry(
            path=str(target),
            content="two\ntwo\n",
            timestamp=time.time(),
            mtime_ns=target.stat().st_mtime_ns,
            partial=True,
            offset=1,
            limit=1,
        ),
    }
    preview = edit_file_preview_card(
        {"file_path": str(target), "old_string": "two", "new_string": "TWO"},
        tool_call_id="call_edit_partial",
    )

    with pytest.raises(FileToolError, match="only read partially"):
        run_edit_file(
            {"file_path": str(target), "old_string": "two", "new_string": "TWO"},
            preview=preview,
            read_state=state,
            working_directory=str(tmp_path),
        )

    state[str(target)] = FileReadStateEntry(
        path=str(target),
        content="two\ntwo\n",
        timestamp=time.time(),
        mtime_ns=target.stat().st_mtime_ns,
        partial=False,
    )
    with pytest.raises(FileToolError, match="matched 2 locations"):
        run_edit_file(
            {"file_path": str(target), "old_string": "two", "new_string": "TWO"},
            preview=preview,
            read_state=state,
            working_directory=str(tmp_path),
        )


def test_edit_file_preserves_crlf_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "windows.txt"
    target.write_bytes(b"hello\r\nworld\r\n")
    preview = read_file_preview_card({"file_path": str(target)}, tool_call_id="call_seed_crlf")
    state: dict[str, FileReadStateEntry] = {}
    run_read_file(
        {"file_path": str(target)},
        preview=preview,
        read_state=state,
        working_directory=str(tmp_path),
    )

    result = run_edit_file(
        {
            "file_path": str(target),
            "old_string": "hello\nworld",
            "new_string": "HELLO\nWORLD",
        },
        preview=edit_file_preview_card(
            {
                "file_path": str(target),
                "old_string": "hello\nworld",
                "new_string": "HELLO\nWORLD",
            },
            tool_call_id="call_edit_crlf",
        ),
        read_state=state,
        working_directory=str(tmp_path),
    )

    assert target.read_bytes() == b"HELLO\r\nWORLD\r\n"
    assert result.final_card is not None
    assert result.final_card.change_artifact is not None


def test_native_write_file_dispatch_roundtrips_into_api_history(
    temp_config_dir: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "roundtrip.txt"
    target.write_text("before", encoding="utf-8")
    chat = SuccessorChat(
        profile=Profile(name="files", tools=("read_file", "write_file", "edit_file")),
        client=_MockClient(),
    )
    chat.messages = [
        _Message("user", "update the file"),
        _Message("successor", "", display_text=""),
    ]
    chat._file_read_state[str(target)] = FileReadStateEntry(
        path=str(target),
        content="before",
        timestamp=time.time(),
        mtime_ns=target.stat().st_mtime_ns,
        partial=False,
    )

    assert chat._dispatch_native_tool_calls([
        {
            "id": "call_write_1",
            "name": "write_file",
            "arguments": {"file_path": str(target), "content": "after"},
        },
    ])
    _pump_until_idle(chat)

    cards = [m.tool_card for m in chat.messages if m.tool_card is not None]
    assert len(cards) == 1
    card = cards[0]
    assert card.tool_name == "write_file"
    assert card.tool_call_id == "call_write_1"
    assert target.read_text(encoding="utf-8") == "after"

    api_messages = chat._build_api_messages_native("SYS")
    assistant = [m for m in api_messages if m["role"] == "assistant"][-1]
    tool_msg = [m for m in api_messages if m["role"] == "tool"][-1]
    assert assistant["tool_calls"][0]["id"] == "call_write_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "write_file"
    assert tool_msg["tool_call_id"] == "call_write_1"
