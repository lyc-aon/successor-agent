from __future__ import annotations

from pathlib import Path

from successor.chat import SuccessorChat
from successor.render.cells import Grid
from successor.snapshot import render_grid_to_plain
from successor.streaming_tool_preview import build_streaming_tool_preview


class _PreviewStream:
    def __init__(self, tool_calls: list[dict], *, reasoning_so_far: str = "") -> None:
        self.reasoning_so_far = reasoning_so_far
        self.tool_calls_so_far = list(tool_calls)

    def drain(self) -> list[object]:
        return []


def test_pending_name_preview_is_explicit() -> None:
    preview = build_streaming_tool_preview(
        name="",
        raw_arguments='{"file_path":"/tmp/demo.css"}',
    )
    assert preview.state == "pending_name"
    assert preview.glyph == "◌"
    assert preview.label == "pending tool"
    assert "resolving tool name" in preview.status


def test_write_file_preview_uses_native_glyph_and_path_hint() -> None:
    preview = build_streaming_tool_preview(
        name="write_file",
        raw_arguments='{"file_path":"/tmp/demo.css","content":"body { color:',
    )
    assert preview.state == "known"
    assert preview.glyph == "✎"
    assert preview.label == "write-file"
    assert preview.hint == "path: /tmp/demo.css"


def test_unknown_tool_preview_is_explicit() -> None:
    preview = build_streaming_tool_preview(
        name="vector_magic",
        raw_arguments='{"shape":"triangle"}',
    )
    assert preview.state == "unsupported"
    assert preview.glyph == "?"
    assert preview.label == "vector_magic"
    assert preview.status == "no preview adapter"


def test_holonet_provider_only_prefers_provider_hint() -> None:
    preview = build_streaming_tool_preview(
        name="holonet",
        raw_arguments='{"provider":"brave_search"}',
    )
    assert preview.label == "web-search"
    assert preview.hint == "provider: brave_search"


def test_bash_preview_keeps_prior_semantics_when_partial_regresses() -> None:
    semantic = build_streaming_tool_preview(
        name="bash",
        raw_arguments='{"command":"git status"}',
    )
    assert semantic.sticky is True
    assert semantic.label != "bash"

    regressed = build_streaming_tool_preview(
        name="bash",
        raw_arguments='{"command":"git sta',
        prior=semantic,
    )
    assert regressed.label == semantic.label
    assert regressed.glyph == semantic.glyph
    assert regressed.display_text.startswith("git sta")


def test_streaming_render_pending_name_is_not_fake_bash(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat._stream = _PreviewStream([
        {
            "index": 0,
            "id": "call_1",
            "name": "",
            "raw_arguments": '{"file_path":"/tmp/demo.css"}',
        }
    ])
    grid = Grid(18, 100)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    assert "pending tool" in plain
    assert "resolving tool name" in plain
    assert "bash" not in plain


def test_streaming_render_write_file_header_is_semantic(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat._stream = _PreviewStream([
        {
            "index": 0,
            "id": "call_1",
            "name": "write_file",
            "raw_arguments": '{"file_path":"/tmp/demo.css","content":"body { color:',
        }
    ])
    grid = Grid(18, 100)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    assert "write-file" in plain
    assert "path: /tmp/demo.css" in plain
    assert "receiving arguments" not in plain


def test_streaming_render_unknown_tool_reports_missing_adapter(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat._stream = _PreviewStream([
        {
            "index": 0,
            "id": "call_1",
            "name": "vector_magic",
            "raw_arguments": '{"shape":"triangle"}',
        }
    ])
    grid = Grid(18, 100)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    assert "vector_magic" in plain
    assert "no preview adapter" in plain
    assert "bash" not in plain


def test_streaming_render_browser_preview_prefers_target_hint(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat._stream = _PreviewStream([
        {
            "index": 0,
            "id": "call_1",
            "name": "browser",
            "raw_arguments": '{"action":"click","target":"button Start"}',
        }
    ])
    grid = Grid(18, 100)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    assert "browser-click" in plain
    assert "target: button Start" in plain
    assert "action: click" not in plain


def test_streaming_tool_preview_attaches_directly_below_stream_text(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat._stream = _PreviewStream([
        {
            "index": 0,
            "id": "call_1",
            "name": "write_file",
            "raw_arguments": '{"file_path":"/tmp/demo.css","content":"body { color:',
        }
    ])
    chat._stream_content = ["I'll create the file now.\n"]
    grid = Grid(18, 100)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)

    assert "I'll create the file now.▌\n         ↳" in plain
    assert "\n       ▌\n" not in plain
