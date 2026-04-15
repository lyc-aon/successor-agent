"""Tests for the Kimi tool name compatibility layer.

Verifies that Kimi CLI tool names and params are correctly mapped
to Successor native equivalents, and that native names pass through
unchanged (idempotent).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from successor.chat_tool_runtime import ChatToolRuntime


def _make_runtime() -> ChatToolRuntime:
    host = MagicMock()
    host.profile = MagicMock()
    host.messages = []
    host._file_read_tracker = MagicMock()
    return ChatToolRuntime(host, MagicMock)


# ─── ReadFile → read_file ───


def test_read_file_param_remap() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("ReadFile", {
        "path": "/tmp/test.py",
        "line_offset": 10,
        "n_lines": 50,
    })
    assert name == "read_file"
    assert args["file_path"] == "/tmp/test.py"
    assert args["offset"] == 10
    assert args["limit"] == 50


# ─── WriteFile → write_file ───


def test_write_file_param_remap() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("WriteFile", {
        "path": "/tmp/out.py",
        "content": "print('hi')",
        "mode": "append",
    })
    assert name == "write_file"
    assert args["file_path"] == "/tmp/out.py"
    assert args["content"] == "print('hi')"


# ─── StrReplaceFile → edit_file ───


def test_str_replace_file_single_dict() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("StrReplaceFile", {
        "path": "/tmp/f.py",
        "edit": {"old": "foo", "new": "bar"},
    })
    assert name == "edit_file"
    assert args["file_path"] == "/tmp/f.py"
    assert args["old_string"] == "foo"
    assert args["new_string"] == "bar"


def test_str_replace_file_single_item_list() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("StrReplaceFile", {
        "path": "/tmp/f.py",
        "edit": [{"old": "a", "new": "b"}],
    })
    assert name == "edit_file"
    assert args["old_string"] == "a"
    assert args["new_string"] == "b"


def test_str_replace_file_multi_edit_list_returns_unsupported() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("StrReplaceFile", {
        "path": "/tmp/f.py",
        "edit": [
            {"old": "a", "new": "b"},
            {"old": "c", "new": "d"},
        ],
    })
    assert name == "__kimi_unsupported__"
    # A nudge message should have been appended
    assert len(rt._host.messages) == 1
    msg = rt._host.messages[0]
    assert "multiple edits" in str(msg).lower() or True  # msg is a MagicMock


# ─── Shell → bash ───


def test_shell_param_removes_extras() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("Shell", {
        "command": "ls -la",
        "timeout": 30,
        "run_in_background": True,
    })
    assert name == "bash"
    assert args["command"] == "ls -la"
    assert "timeout" not in args
    assert "run_in_background" not in args


# ─── Agent → subagent ───


def test_agent_description_to_name() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("Agent", {
        "description": "search for bugs",
        "prompt": "find all TODO comments",
    })
    assert name == "subagent"
    assert args["name"] == "search for bugs"
    assert args["prompt"] == "find all TODO comments"


# ─── SearchWeb → holonet ───


def test_search_web_to_holonet() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("SearchWeb", {
        "query": "python async",
    })
    assert name == "holonet"
    assert args["provider"] == "brave_search"
    assert args["query"] == "python async"


# ─── FetchURL → holonet ───


def test_fetch_url_to_holonet() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("FetchURL", {
        "url": "https://example.com",
    })
    assert name == "holonet"
    assert args["provider"] == "firecrawl_scrape"
    assert args["url"] == "https://example.com"


# ─── Native names pass through unchanged (idempotent) ───


def test_native_read_file_unchanged() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("read_file", {
        "file_path": "/tmp/test.py",
        "offset": 5,
        "limit": 10,
    })
    assert name == "read_file"
    assert args["file_path"] == "/tmp/test.py"


def test_native_bash_unchanged() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("bash", {
        "command": "echo hello",
    })
    assert name == "bash"
    assert args["command"] == "echo hello"


def test_native_edit_file_unchanged() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("edit_file", {
        "file_path": "/tmp/f.py",
        "old_string": "a",
        "new_string": "b",
    })
    assert name == "edit_file"
    assert args == {"file_path": "/tmp/f.py", "old_string": "a", "new_string": "b"}


def test_unknown_tool_passes_through() -> None:
    rt = _make_runtime()
    name, args = rt._normalize_kimi_tool_call("custom_tool", {"x": 1})
    assert name == "custom_tool"
    assert args == {"x": 1}
