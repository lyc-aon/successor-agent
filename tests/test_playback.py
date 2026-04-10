"""Tests for recording bundles and the shared playback viewer."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from successor.chat import SuccessorChat, find_slash_command
from successor.cli import build_parser, cmd_playback, cmd_record
from successor.config import load_chat_config, save_chat_config
from successor.playback import (
    RecordingBundle,
    build_recordings_library_payload,
    ensure_bundle_is_gitignored,
    recordings_library_path,
    write_playback_html,
    write_recordings_html,
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
    assert "<title>Playback Escape</title>" in html
    assert "Wheel to zoom. Drag to pan." in html


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
    assert "<title>Bundle Demo</title>" in html
    assert "Artifacts" in html


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


def test_recording_bundle_writes_assertions_artifact_from_verification_trace(tmp_path: Path) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        "\n".join([
            json.dumps({"t": 0.0, "type": "session_start"}),
            json.dumps({
                "t": 0.5,
                "type": "verification_contract_updated",
                "tool_call_id": "call_verify_1",
                "items": [
                    {
                        "claim": "Game responds to typed command input",
                        "evidence": "player script issues a valid command and score increments",
                        "status": "passed",
                        "observed": "score increased after typed command",
                    }
                ],
            }),
        ])
        + "\n",
        encoding="utf-8",
    )
    chat = _DummyChat()
    bundle_root = tmp_path / "bundle-proof"

    with RecordingBundle(
        bundle_root,
        title="Verified Playback",
        description="bundle regression with proof state",
        frame_interval_s=0.01,
    ) as bundle:
        bundle.capture_frame(_grid_with_text("verified"), chat=chat, force=True)
    summary = bundle.finalize(trace_path=trace_path)

    assert bundle.assertions_path.exists()
    assertions = json.loads(bundle.assertions_path.read_text(encoding="utf-8"))
    assert assertions["status"] == "passed"
    assert assertions["passed"] == 1
    assert summary["verification"]["status"] == "passed"
    assert summary["assertions_path"] == str(bundle.assertions_path)


def test_recording_bundle_writes_runbook_and_experiment_artifacts_from_trace(
    tmp_path: Path,
) -> None:
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        "\n".join([
            json.dumps({"t": 0.0, "type": "session_start"}),
            json.dumps({
                "t": 0.4,
                "type": "runbook_updated",
                "tool_call_id": "call_runbook_1",
                "objective": "Ship a stable typing game loop",
                "status": "running",
                "baseline_status": "captured",
                "attempt_count": 1,
                "runbook": {
                    "objective": "Ship a stable typing game loop",
                    "success_definition": "Scripted player and browser verification both pass",
                    "scope": ["src/game", "src/ui"],
                    "protected_surfaces": [],
                    "baseline_status": "captured",
                    "baseline_summary": "Build opens but the player stalls after wave one",
                    "active_hypothesis": "Input focus is being lost after transitions",
                    "evaluator": [
                        {
                            "id": "build",
                            "kind": "command",
                            "spec": "npm run build",
                            "pass_condition": "exit 0",
                        }
                    ],
                    "decision_policy": "Keep only attempts that pass evaluator and verification",
                    "status": "running",
                },
                "artifact": {
                    "configured": True,
                    "objective": "Ship a stable typing game loop",
                    "success_definition": "Scripted player and browser verification both pass",
                    "scope": ["src/game", "src/ui"],
                    "protected_surfaces": [],
                    "baseline_status": "captured",
                    "baseline_summary": "Build opens but the player stalls after wave one",
                    "active_hypothesis": "Input focus is being lost after transitions",
                    "status": "running",
                    "decision_policy": "Keep only attempts that pass evaluator and verification",
                    "evaluator": [
                        {
                            "id": "build",
                            "kind": "command",
                            "spec": "npm run build",
                            "pass_condition": "exit 0",
                        }
                    ],
                    "attempt_count": 1,
                    "last_attempt": None,
                },
            }),
            json.dumps({
                "t": 0.8,
                "type": "experiment_attempt_recorded",
                "tool_call_id": "call_runbook_1",
                "objective": "Ship a stable typing game loop",
                "baseline_status": "captured",
                "attempt": {
                    "attempt_id": 1,
                    "hypothesis": "Locking focus after transitions fixes the stall",
                    "summary": "Build passed and scripted player reached wave three",
                    "decision": "kept",
                    "files_touched": ["src/game/input.ts"],
                    "evaluator_summary": "build and player script passed",
                    "verification_summary": "browser HUD updated correctly",
                    "artifact_refs": ["logs/player.log"],
                },
            }),
        ])
        + "\n",
        encoding="utf-8",
    )
    chat = _DummyChat()
    bundle_root = tmp_path / "bundle-runbook"

    with RecordingBundle(
        bundle_root,
        title="Runbook Playback",
        description="bundle regression with runbook state",
        frame_interval_s=0.01,
    ) as bundle:
        bundle.capture_frame(_grid_with_text("runbook"), chat=chat, force=True)
    summary = bundle.finalize(trace_path=trace_path)

    assert bundle.runbook_path.exists()
    assert bundle.experiments_path.exists()
    runbook = json.loads(bundle.runbook_path.read_text(encoding="utf-8"))
    assert runbook["configured"] is True
    assert runbook["objective"] == "Ship a stable typing game loop"
    experiment_lines = bundle.experiments_path.read_text(encoding="utf-8").splitlines()
    assert any(json.loads(line)["kind"] == "attempt" for line in experiment_lines if line.strip())
    assert summary["runbook"]["configured"] is True
    assert summary["experiments"][0]["kind"] in {"baseline", "attempt"}


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


def test_playback_slash_command_is_registered() -> None:
    assert find_slash_command("playback") is not None
    assert find_slash_command("review") is not None


def test_write_recordings_html_builds_library_from_existing_bundles(tmp_path: Path) -> None:
    root = tmp_path / "recordings"
    first = root / "20260409-010101"
    second = root / "20260409-020202"
    for bundle, title in ((first, "alpha run"), (second, "beta run")):
        bundle.mkdir(parents=True)
        (bundle / "summary.json").write_text(
            json.dumps(
                {
                    "title": title,
                    "description": "demo bundle",
                    "frame_count": 3,
                    "trace_event_count": 2,
                    "duration_s": 12.5,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (bundle / "timeline.json").write_text(
            json.dumps(
                [
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
                        "plain": "hello world",
                    }
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (bundle / "session_trace.json").write_text(
            json.dumps([{"t": 0.1, "type": "browser_open"}]) + "\n",
            encoding="utf-8",
        )
        (bundle / "playback.html").write_text("stale", encoding="utf-8")

    payload = build_recordings_library_payload(root)
    assert payload["kind"] == "library"
    assert len(payload["sessions"]) == 2

    html_path = write_recordings_html(root)
    html = html_path.read_text(encoding="utf-8")
    assert html_path == recordings_library_path(root)
    assert "<title>Successor recordings</title>" in html
    assert "alpha run" in html
    assert "beta run" in html
    refreshed = (first / "playback.html").read_text(encoding="utf-8")
    assert "<title>alpha run</title>" in refreshed
    assert "alpha run" in refreshed


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


def test_chat_playback_recordings_opens_library(
    temp_config_dir: Path,
    monkeypatch,
) -> None:
    save_chat_config({"autorecord": False})
    recordings_root = temp_config_dir / "recordings"
    monkeypatch.setenv("SUCCESSOR_RECORDINGS_DIR", str(recordings_root))
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda uri: opened.append(uri) or True)

    chat = SuccessorChat()
    chat.input_buffer = "/playback recordings"
    chat._submit()

    assert (recordings_root / "recordings.html").exists()
    assert opened and opened[0].endswith("/recordings.html")
    assert "opened recordings manager" in chat.messages[-1].raw_text


def test_chat_playback_current_uses_live_bundle(
    temp_config_dir: Path,
    monkeypatch,
) -> None:
    save_chat_config({"autorecord": False})
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda uri: opened.append(uri) or True)

    class _FakeLiveBundle:
        def __init__(self, root: Path) -> None:
            self.root = root
            self.viewer_path = root / "playback.html"
            self.refresh_calls = 0
            self.capture_calls = 0

        def capture_frame(self, _grid, *, chat, force: bool = False) -> None:
            self.capture_calls += 1

        def refresh_viewer(self, *, trace_path) -> dict[str, object]:
            self.refresh_calls += 1
            self.viewer_path.parent.mkdir(parents=True, exist_ok=True)
            self.viewer_path.write_text("<html>live</html>\n", encoding="utf-8")
            return {"viewer_path": str(self.viewer_path), "trace_path": str(trace_path)}

    chat = SuccessorChat()
    fake = _FakeLiveBundle(temp_config_dir / "live-bundle")
    chat._recorder = fake
    chat._front = _grid_with_text("live frame")
    chat.input_buffer = "/playback"
    chat._submit()

    assert fake.capture_calls == 1
    assert fake.refresh_calls == 1
    assert fake.viewer_path.exists()
    assert opened and opened[0].endswith("/playback.html")
    assert "opened current session reviewer" in chat.messages[-1].raw_text
