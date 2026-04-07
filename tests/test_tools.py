"""Tests for the @tool decorator and the ToolRegistry import-based loader.

Covers:
  - The @tool decorator captures name/description/schema/func
  - Tool instances are callable (passthrough to the underlying func)
  - The built-in read_file tool loads and is registered
  - User tools are gated by `allow_user_tools` config (default OFF)
  - When the gate is on, user tools load and an audit line goes to stderr
  - User tools override built-ins by name on collision
  - Broken user tool files are skipped with a stderr warning, builtins
    still load
  - Multiple @tool decorations in one file all get registered
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ronin.tools import (
    TOOL_REGISTRY,
    Tool,
    all_tools,
    get_tool,
    tool,
)


# ─── @tool decorator ───


def test_tool_decorator_captures_metadata() -> None:
    @tool(
        name="test_decorator",
        description="for testing",
        schema={"type": "object", "properties": {}},
    )
    def my_tool() -> str:
        return "ok"

    assert isinstance(my_tool, Tool)
    assert my_tool.name == "test_decorator"
    assert my_tool.description == "for testing"
    assert my_tool.schema == {"type": "object", "properties": {}}


def test_tool_instance_is_callable() -> None:
    @tool(name="callable_test")
    def my_tool(x: int) -> int:
        return x * 2

    assert my_tool(5) == 10
    assert my_tool.func(7) == 14  # underlying func also accessible


def test_tool_decorator_default_schema_is_empty() -> None:
    @tool(name="no_schema")
    def my_tool() -> None:
        pass

    assert my_tool.schema == {}


# ─── ToolRegistry — built-in loading ───


def test_builtin_read_file_tool_loads(temp_config_dir: Path) -> None:
    """The bundled read_file tool is registered after a fresh load."""
    TOOL_REGISTRY.reload()
    rf = get_tool("read_file")
    assert rf is not None
    assert rf.name == "read_file"
    assert "filesystem" in rf.description.lower() or "file" in rf.description.lower()
    assert TOOL_REGISTRY.source_of("read_file") == "builtin"


def test_builtin_read_file_actually_works(
    temp_config_dir: Path,
    tmp_path: Path,
) -> None:
    """The registered tool callable reads real files."""
    sample = tmp_path / "sample.txt"
    sample.write_text("hello from a tool", encoding="utf-8")

    TOOL_REGISTRY.reload()
    rf = get_tool("read_file")
    assert rf is not None
    result = rf(path=str(sample))
    assert result == "hello from a tool"


# ─── ToolRegistry — user tool gating ───


def test_user_tools_gated_off_by_default(temp_config_dir: Path) -> None:
    """A user tool file is NOT loaded when allow_user_tools is unset."""
    user_dir = temp_config_dir / "tools"
    user_dir.mkdir()
    (user_dir / "evil.py").write_text(
        "from ronin.tools import tool\n"
        "@tool(name='evil', description='should not load')\n"
        "def evil(): return 'pwned'\n"
    )

    TOOL_REGISTRY.reload()
    assert get_tool("evil") is None


def test_user_tools_load_when_gate_enabled(
    temp_config_dir: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """When allow_user_tools is on, user tools load and stderr audit fires."""
    user_dir = temp_config_dir / "tools"
    user_dir.mkdir()
    (user_dir / "extra.py").write_text(
        "from ronin.tools import tool\n"
        "@tool(name='extra', description='extra tool')\n"
        "def extra(): return 42\n"
    )
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "allow_user_tools": True,
    }))

    TOOL_REGISTRY.reload()
    extra = get_tool("extra")
    assert extra is not None
    assert extra() == 42
    assert TOOL_REGISTRY.source_of("extra") == "user"

    captured = capsys.readouterr()
    assert "loading user tool" in captured.err
    assert "extra.py" in captured.err


def test_user_tool_overrides_builtin(
    temp_config_dir: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A user tool with the same name as a built-in wins."""
    user_dir = temp_config_dir / "tools"
    user_dir.mkdir()
    (user_dir / "read_file.py").write_text(
        "from ronin.tools import tool\n"
        "@tool(name='read_file', description='user-overridden read_file')\n"
        "def read_file(path: str): return f'overridden: {path}'\n"
    )
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "allow_user_tools": True,
    }))

    TOOL_REGISTRY.reload()
    rf = get_tool("read_file")
    assert rf is not None
    assert rf.description == "user-overridden read_file"
    assert TOOL_REGISTRY.source_of("read_file") == "user"
    assert rf(path="/tmp/whatever") == "overridden: /tmp/whatever"


# ─── Error paths ───


def test_broken_user_tool_skipped_with_warning(
    temp_config_dir: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    user_dir = temp_config_dir / "tools"
    user_dir.mkdir()
    (user_dir / "broken.py").write_text("this is { not valid python\n")
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "allow_user_tools": True,
    }))

    TOOL_REGISTRY.reload()
    # The built-in read_file still loaded
    assert get_tool("read_file") is not None
    # The broken file was skipped
    captured = capsys.readouterr()
    assert "skipping" in captured.err
    assert "broken.py" in captured.err


def test_partial_import_doesnt_register_tools(
    temp_config_dir: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A file that registers one tool then crashes mid-import gets rolled back."""
    user_dir = temp_config_dir / "tools"
    user_dir.mkdir()
    (user_dir / "half.py").write_text(
        "from ronin.tools import tool\n"
        "@tool(name='good_one')\n"
        "def good_one(): return 'fine'\n"
        "\n"
        "raise RuntimeError('boom — file is half-broken')\n"
    )
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "allow_user_tools": True,
    }))

    TOOL_REGISTRY.reload()
    # The half-imported tool must NOT be in the registry
    assert get_tool("good_one") is None
    # The error was reported
    captured = capsys.readouterr()
    assert "half.py" in captured.err


# ─── Multi-tool files ───


def test_multiple_tools_in_one_file(
    temp_config_dir: Path,
) -> None:
    """A single .py file with multiple @tool decorators registers all of them."""
    user_dir = temp_config_dir / "tools"
    user_dir.mkdir()
    (user_dir / "many.py").write_text(
        "from ronin.tools import tool\n"
        "\n"
        "@tool(name='tool_a', description='first')\n"
        "def a(): return 'a'\n"
        "\n"
        "@tool(name='tool_b', description='second')\n"
        "def b(): return 'b'\n"
        "\n"
        "@tool(name='tool_c', description='third')\n"
        "def c(): return 'c'\n"
    )
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2,
        "allow_user_tools": True,
    }))

    TOOL_REGISTRY.reload()
    assert get_tool("tool_a") is not None
    assert get_tool("tool_b") is not None
    assert get_tool("tool_c") is not None
    # All three share the same source path
    a = get_tool("tool_a")
    b = get_tool("tool_b")
    assert a is not None and b is not None
    assert a.source_path == b.source_path


# ─── all_tools / iteration ───


def test_all_tools_returns_loaded(temp_config_dir: Path) -> None:
    TOOL_REGISTRY.reload()
    tools = all_tools()
    assert len(tools) >= 1
    names = [t.name for t in tools]
    assert "read_file" in names
