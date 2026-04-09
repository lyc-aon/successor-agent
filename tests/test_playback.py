"""Tests for recording bundles and the shared playback viewer."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from successor.chat import SuccessorChat
from successor.cli import build_parser, cmd_playback, cmd_record
from successor.config import load_chat_config, save_chat_config
from successor.playback import (
    RecordingBundle,
    ensure_bundle_is_gitignored,
    write_playback_html,
)
from successor.render.cells import Cell, Grid


@dataclass
class _FakeMessage:
    role: str


class _DummyChat:
    def __init__(self) -> None:
        self.messages: list[_FakeMessage] = []
        self._stream = None
        self._running_tools: list[object] = []
        self._agent_turn = 0


def _grid_with_text(text: str) -> Grid:
    grid = Grid(2, max(4, len(text)))
    for idx, ch in enumerate(text):
        grid.set(0, idx, Cell(ch))
    return grid


def test_playback_html_escapes_embedded_script_terminators(tmp_path: Path) -> None:
    frames = [
        {
            "index": 1,
            "turn_index": 1,
            "kind": "idle",
            "frame_index": 0,
            "turn_elapsed_s": 0.1,
            "scenario_elapsed_s": 0.1,
            "agent_turn": 1,
            "stream_open": False,
            "running_tools": 0,
            "message_count": 1,
            "plain": "<script src=\"app.js\"></script>\nEOF",
        }
    ]
    trace_events = [
        {
            "t": 0.1,
            "type": "stream_end",
            "assistant_excerpt": "</script><script>alert('x')</script>",
        }
    ]

    html_path = write_playback_html(
        tmp_path / "playback.html",
        title="Playback Escape",
        description="regression",
        frames=frames,
        trace_events=trace_events,
    )
    html = html_path.read_text(encoding="utf-8")

    assert html.count("</script>") == 2
    assert "\\u003c/script\\u003e" in html
    assert "alert('x')" in html
    assert "Keyboard: Space play/pause" in html
    assert "Successor Session Reviewer" in html


def test_playback_html_includes_bundle_artifacts_and_visuals(tmp_path: Path) -> None:
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    (bundle_root / "index.md").write_text("# demo\n", encoding="utf-8")
    (bundle_root / "summary.json").write_text('{"frame_count": 1}\n', encoding="utf-8")
    (bundle_root / "turn_01_plain.txt").write_text("hello\n", encoding="utf-8")
    (bundle_root / "visuals").mkdir()
    image_path = bundle_root / "visuals" / "frame.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
        b"\x0b\xe7\x02\x9d\x00\x00\x00\x00IEND\xaeB`\x82"
    )

    html_path = write_playback_html(
        bundle_root / "playback.html",
        title="Bundle Demo",
        description="artifact pass",
        frames=[],
        trace_events=[],
        bundle_root=bundle_root,
    )
    html = html_path.read_text(encoding="utf-8")

    assert "index.md" in html
    assert "summary.json" in html
    assert "turn_01_plain.txt" in html
    assert "visuals/frame.png" in html
    assert "local-only artifact" in html


def test_recording_bundle_writes_artifacts_and_dedupes_identical_frames(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        json.dumps({"t": 0.0, "type": "session_start"}) + "\n",
        encoding="utf-8",
    )
    chat = _DummyChat()
    bundle_root = tmp_path / "bundle"

    with RecordingBundle(
        bundle_root,
        title="Demo Playback",
        description="bundle regression",
        frame_interval_s=0.01,
    ) as bundle:
        bundle.record_byte(ord("a"))
        bundle.capture_frame(_grid_with_text("alpha"), chat=chat)
        bundle.capture_frame(_grid_with_text("alpha"), chat=chat)
        chat.messages.append(_FakeMessage("user"))
        chat._agent_turn = 1
        bundle.capture_frame(_grid_with_text("beta"), chat=chat, force=True)
    summary = bundle.finalize(trace_path=trace_path)

    assert summary["frame_count"] == 2
    assert bundle.viewer_path.exists()
    assert bundle.timeline_path.exists()
    assert bundle.trace_json_path.exists()
    assert bundle.trace_jsonl_path.exists()
    assert bundle.index_path.exists()
    assert bundle.summary_path.exists()

    timeline = json.loads(bundle.timeline_path.read_text(encoding="utf-8"))
    assert len(timeline) == 2
    assert timeline[-1]["turn_index"] == 1

    trace_events = json.loads(bundle.trace_json_path.read_text(encoding="utf-8"))
    assert trace_events[0]["type"] == "session_start"


class _FakeRecordedChat:
    TRACE_PATH = Path("/tmp/successor-fake-trace.jsonl")

    def __init__(self, recorder=None) -> None:
        self._recorder = recorder
        self.messages = [_FakeMessage("user")]
        self._stream = None
        self._running_tools: list[object] = []
        self._agent_turn = 1
        self.session_trace_path = self.TRACE_PATH

    def run(self) -> None:
        self.session_trace_path.write_text(
            json.dumps({"t": 0.0, "type": "session_start"}) + "\n",
            encoding="utf-8",
        )
        if self._recorder is None or not hasattr(self._recorder, "capture_frame"):
            return
        self._recorder.record_byte(ord("x"))
        self._recorder.capture_frame(_grid_with_text("recorded"), chat=self, force=True)


def test_cmd_record_writes_bundle_and_cmd_playback_regenerates_viewer(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    monkeypatch.setattr("successor.chat.SuccessorChat", _FakeRecordedChat)
    _FakeRecordedChat.TRACE_PATH = tmp_path / "trace.jsonl"
    bundle_root = tmp_path / "recording"

    args = argparse.Namespace(
        output=str(bundle_root),
        input_only=False,
        frame_interval=0.15,
        open=False,
    )
    assert cmd_record(args) == 0
    out = capsys.readouterr().out
    assert "viewer" in out
    assert (bundle_root / "playback.html").exists()


def test_recording_bundle_marks_repo_local_via_git_info_exclude(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bundle_root = repo / "artifacts" / "session-a"

    ensure_bundle_is_gitignored(bundle_root)

    exclude = repo / ".git" / "info" / "exclude"
    text = exclude.read_text(encoding="utf-8")
    assert "artifacts/session-a/" in text


def test_review_alias_parses_to_cmd_playback() -> None:
    parser = build_parser()
    args = parser.parse_args(["review"])
    assert args.func is cmd_playback


def test_chat_autorecord_defaults_on_and_slash_command_can_disable(
    temp_config_dir: Path,
    monkeypatch,
) -> None:
    created: list[Path] = []

    class _FakeBundle:
        def __init__(self, root, **_kwargs) -> None:
            self.root = Path(root)
            created.append(self.root)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def record_byte(self, _b: int) -> None:
            return None

        def capture_frame(self, _grid, *, chat, force: bool = False) -> None:
            return None

        def finalize(self, *, trace_path) -> dict[str, object]:
            return {"trace_path": str(trace_path)}

    monkeypatch.setattr("successor.chat.RecordingBundle", _FakeBundle)
    monkeypatch.setenv("SUCCESSOR_RECORDINGS_DIR", str(temp_config_dir / "recordings"))

    chat = SuccessorChat()
    assert created, "autorecord should attach by default"
    assert chat._owns_recorder is True

    chat.input_buffer = "/recording off"
    chat._submit()
    cfg = load_chat_config()
    assert cfg["autorecord"] is False
    assert chat.messages[-1].raw_text.startswith("auto-record off")


def test_chat_respects_autorecord_off_in_config(
    temp_config_dir: Path,
    monkeypatch,
) -> None:
    save_chat_config({"autorecord": False})

    class _ExplodingBundle:
        def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - should not run
            raise AssertionError("autorecord bundle should not be constructed")

    monkeypatch.setattr("successor.chat.RecordingBundle", _ExplodingBundle)
    chat = SuccessorChat()
    assert chat._recorder is None
