"""Chat integration coverage for holonet, browser, and vision native tools."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from successor.chat import SuccessorChat
from successor.profiles import Profile
from successor.providers.llama import StreamEnded
from successor.tool_runner import ToolExecutionResult
from successor.web.browser import BrowserRuntimeStatus
from successor.web.vision import VisionRuntimeStatus


class _StaticStream:
    def __init__(self, events: list[object]) -> None:
        self._events = list(events)

    def drain(self) -> list[object]:
        if not self._events:
            return []
        events = list(self._events)
        self._events.clear()
        return events

    def close(self) -> None:
        return


@dataclass
class _MockClient:
    base_url: str = "http://mock"
    model: str = "mock-model"

    def stream_chat(self, messages, *, max_tokens=None, temperature=None, timeout=None, extra=None, tools=None):  # noqa: ARG002
        return _StaticStream([])


class _CapturingClient:
    def __init__(self, streams: list[_StaticStream]) -> None:
        self._streams = list(streams)
        self.calls: list[dict[str, object]] = []
        self.base_url = "http://mock"
        self.model = "mock-model"

    def stream_chat(self, messages, *, max_tokens=None, temperature=None, timeout=None, extra=None, tools=None):  # noqa: ARG002
        self.calls.append({
            "messages": list(messages),
            "tools": tools,
        })
        if not self._streams:
            raise RuntimeError("capturing client exhausted")
        return self._streams.pop(0)


def _pump_until_idle(chat: SuccessorChat, *, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        chat._pump_stream()
        chat._pump_running_tools()
        if chat._stream is None and not chat._running_tools:
            return
        time.sleep(0.02)
    raise AssertionError("chat did not settle")


def _trace_events(root: Path) -> list[dict]:
    trace_files = sorted((root / "logs").glob("*.jsonl"))
    assert trace_files, "expected trace files"
    return [
        json.loads(line)
        for line in trace_files[-1].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_native_holonet_tool_call_dispatches(monkeypatch, temp_config_dir: Path) -> None:
    monkeypatch.setattr(
        "successor.chat.run_holonet",
        lambda route, cfg, progress: ToolExecutionResult(  # noqa: ARG005
            output="Goal completed via Europe PMC without opening a browser window.",
            exit_code=0,
        ),
    )
    chat = SuccessorChat(
        profile=Profile(name="web-chat", tools=("holonet",)),
        client=_MockClient(),
    )
    chat.messages = []
    chat._stream = _StaticStream([
        StreamEnded(
            finish_reason="tool_calls",
            usage=None,
            timings=None,
            full_reasoning="",
            full_content="",
            tool_calls=({
                "id": "call_holo_1",
                "name": "holonet",
                "arguments": {"provider": "europe_pmc", "query": "semaglutide obesity"},
                "raw_arguments": '{"provider":"europe_pmc","query":"semaglutide obesity"}',
            },),
        ),
    ])

    chat._pump_stream()
    _pump_until_idle(chat)

    cards = [m.tool_card for m in chat.messages if m.tool_card is not None]
    assert len(cards) == 1
    assert cards[0].tool_name == "holonet"
    assert cards[0].tool_call_id == "call_holo_1"
    assert "Europe PMC" in cards[0].output
    assert any(
        msg.synthetic and msg.raw_text.startswith("progress: ")
        for msg in chat.messages
        if msg.role == "successor"
    )


def test_browser_tool_filtered_when_playwright_missing(temp_config_dir: Path) -> None:
    from unittest.mock import patch

    chat = SuccessorChat(
        profile=Profile(name="browser-chat", tools=("bash", "browser")),
        client=_MockClient(),
    )
    with patch(
        "successor.chat.browser_runtime_status",
        return_value=BrowserRuntimeStatus(
            package_available=False,
            python_executable="/usr/bin/python3",
            using_external_runtime=False,
            channel="chrome",
            executable_path="",
            user_data_dir="/tmp/browser",
        ),
    ):
        assert chat._enabled_tools_for_turn() == ["bash", "task", "verify", "runbook"]


def test_vision_tool_filtered_when_runtime_missing(temp_config_dir: Path) -> None:
    from unittest.mock import patch

    chat = SuccessorChat(
        profile=Profile(name="vision-chat", tools=("bash", "vision")),
        client=_MockClient(),
    )
    with patch(
        "successor.chat.vision_runtime_status",
        return_value=VisionRuntimeStatus(
            tool_available=False,
            mode="inherit",
            provider_type="llamacpp",
            base_url="http://localhost:8080",
            model="local",
            reason="llama.cpp endpoint reports vision=false",
        ),
    ):
        assert chat._enabled_tools_for_turn() == ["bash", "task", "verify", "runbook"]


def test_skill_tool_enabled_when_profile_has_usable_skills(temp_config_dir: Path) -> None:
    from unittest.mock import patch

    chat = SuccessorChat(
        profile=Profile(
            name="browser-chat",
            tools=("browser",),
            skills=("browser-operator",),
        ),
        client=_MockClient(),
    )
    with patch(
        "successor.chat.browser_runtime_status",
        return_value=BrowserRuntimeStatus(
            package_available=True,
            python_executable="/usr/bin/python3",
            using_external_runtime=False,
            channel="chrome",
            executable_path="",
            user_data_dir="/tmp/browser",
        ),
    ):
        assert chat._enabled_tools_for_turn() == ["browser", "task", "verify", "runbook", "skill"]


def test_skill_tool_enabled_when_profile_has_vision_skill(temp_config_dir: Path) -> None:
    from unittest.mock import patch

    chat = SuccessorChat(
        profile=Profile(
            name="vision-chat",
            tools=("vision",),
            skills=("vision-inspector",),
        ),
        client=_MockClient(),
    )
    with patch(
        "successor.chat.vision_runtime_status",
        return_value=VisionRuntimeStatus(
            tool_available=True,
            mode="endpoint",
            provider_type="openai_compat",
            base_url="http://127.0.0.1:8090",
            model="vision-local",
            reason="ready",
        ),
    ):
        assert chat._enabled_tools_for_turn() == ["vision", "task", "verify", "runbook", "skill"]


def test_skill_tool_filtered_when_required_tool_missing(temp_config_dir: Path) -> None:
    from unittest.mock import patch

    chat = SuccessorChat(
        profile=Profile(
            name="browser-chat",
            tools=("bash", "browser"),
            skills=("browser-operator",),
        ),
        client=_MockClient(),
    )
    with patch(
        "successor.chat.browser_runtime_status",
        return_value=BrowserRuntimeStatus(
            package_available=False,
            python_executable="/usr/bin/python3",
            using_external_runtime=False,
            channel="chrome",
            executable_path="",
            user_data_dir="/tmp/browser",
        ),
    ):
        assert chat._enabled_tools_for_turn() == ["bash", "task", "verify", "runbook"]


def test_native_browser_tool_call_dispatches(monkeypatch, temp_config_dir: Path) -> None:
    monkeypatch.setattr(
        "successor.chat.browser_runtime_status",
        lambda *_args, **_kwargs: BrowserRuntimeStatus(
            package_available=True,
            python_executable="/usr/bin/python3",
            using_external_runtime=False,
            channel="chrome",
            executable_path="",
            user_data_dir="/tmp/browser",
        ),
    )
    monkeypatch.setattr(
        "successor.chat.run_browser_action",
        lambda arguments, manager, progress: ToolExecutionResult(  # noqa: ARG005
            output="Opened page.\nURL: file:///tmp/demo.html",
            exit_code=0,
        ),
    )
    chat = SuccessorChat(
        profile=Profile(name="browser-chat", tools=("browser",)),
        client=_MockClient(),
    )
    chat.messages = []
    chat._stream = _StaticStream([
        StreamEnded(
            finish_reason="tool_calls",
            usage=None,
            timings=None,
            full_reasoning="",
            full_content="",
            tool_calls=({
                "id": "call_browser_1",
                "name": "browser",
                "arguments": {"action": "open", "url": "file:///tmp/demo.html"},
                "raw_arguments": '{"action":"open","url":"file:///tmp/demo.html"}',
            },),
        ),
    ])

    chat._pump_stream()
    _pump_until_idle(chat)

    cards = [m.tool_card for m in chat.messages if m.tool_card is not None]
    assert len(cards) == 1
    assert cards[0].tool_name == "browser"
    assert cards[0].tool_call_id == "call_browser_1"
    assert "Opened page." in cards[0].output
    assert any(
        msg.synthetic and "progress: opened file:///tmp/demo.html" in msg.raw_text
        for msg in chat.messages
        if msg.role == "successor"
    )


def test_browser_verification_intervention_becomes_continuation_nudge(
    monkeypatch,
    temp_config_dir: Path,
) -> None:
    monkeypatch.setattr(
        "successor.chat.browser_runtime_status",
        lambda *_args, **_kwargs: BrowserRuntimeStatus(
            package_available=True,
            python_executable="/usr/bin/python3",
            using_external_runtime=False,
            channel="chrome",
            executable_path="",
            user_data_dir="/tmp/browser",
        ),
    )
    monkeypatch.setattr(
        "successor.chat.run_browser_action",
        lambda arguments, manager, progress: ToolExecutionResult(  # noqa: ARG005
            output="Clicked target.\n\nProgress note: page state has not meaningfully changed across the last 3 browser actions.",
            exit_code=0,
            metadata={
                "state_hash": "steady",
                "controls_summary": "Visible controls:\n- button: \"Add Issue\"; selector=#add-issue",
                "verification_intervention": {
                    "kind": "stagnant_state",
                    "recommended_action": "inspect",
                    "controls_summary": "Visible controls:\n- button: \"Add Issue\"; selector=#add-issue",
                },
            },
        ),
    )
    client = _CapturingClient([
        _StaticStream([
            StreamEnded(
                finish_reason="tool_calls",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="",
                tool_calls=({
                    "id": "call_browser_1",
                    "name": "browser",
                    "arguments": {"action": "click", "target": "Open"},
                    "raw_arguments": '{"action":"click","target":"Open"}',
                },),
            ),
        ]),
        _StaticStream([
            StreamEnded(
                finish_reason="stop",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="Done.",
                tool_calls=(),
            ),
        ]),
    ])
    chat = SuccessorChat(
        profile=Profile(
            name="browser-chat",
            tools=("browser",),
            skills=("browser-verifier",),
        ),
        client=client,
    )
    chat.messages = []

    chat.input_buffer = "inspect it like a human and verify the UI"
    chat._submit()
    _pump_until_idle(chat)
    chat._trace.close()

    assert len(client.calls) == 2
    second_sys = client.calls[1]["messages"][0]
    assert second_sys["role"] == "system"
    assert "Browser Verification Reminder" in second_sys["content"]
    assert "Visible controls:" in second_sys["content"]
    assert any(
        msg.synthetic and "browser actions stalled on the same page state" in msg.raw_text
        for msg in chat.messages
        if msg.role == "successor"
    )
    events = _trace_events(temp_config_dir)
    assert any(event["type"] == "browser_verification_intervention" for event in events)
    assert any(event["type"] == "progress_summary_emitted" for event in events)


def test_native_vision_tool_call_dispatches(monkeypatch, temp_config_dir: Path) -> None:
    monkeypatch.setattr(
        "successor.chat.vision_runtime_status",
        lambda *_args, **_kwargs: VisionRuntimeStatus(
            tool_available=True,
            mode="endpoint",
            provider_type="openai_compat",
            base_url="http://127.0.0.1:8090",
            model="vision-local",
            reason="ready",
        ),
    )
    monkeypatch.setattr(
        "successor.chat.run_vision_analysis",
        lambda arguments, cfg, client=None, progress=None: ToolExecutionResult(  # noqa: ARG005
            output=f"Vision analysis completed.\nPath: {arguments['path']}\n\nThe CTA is clipped.",
            exit_code=0,
        ),
    )
    chat = SuccessorChat(
        profile=Profile(
            name="vision-chat",
            tools=("vision",),
        ),
        client=_MockClient(),
    )
    chat.messages = []
    chat._stream = _StaticStream([
        StreamEnded(
            finish_reason="tool_calls",
            usage=None,
            timings=None,
            full_reasoning="",
            full_content="",
            tool_calls=({
                "id": "call_vision_1",
                "name": "vision",
                "arguments": {"path": "/tmp/ui.png", "prompt": "Find the main issue."},
                "raw_arguments": '{"path":"/tmp/ui.png","prompt":"Find the main issue."}',
            },),
        ),
    ])

    chat._pump_stream()
    _pump_until_idle(chat)

    cards = [m.tool_card for m in chat.messages if m.tool_card is not None]
    assert len(cards) == 1
    assert cards[0].tool_name == "vision"
    assert cards[0].tool_call_id == "call_vision_1"
    assert "CTA is clipped" in cards[0].output


def test_native_skill_tool_call_dispatches(monkeypatch, temp_config_dir: Path) -> None:
    monkeypatch.setattr(
        "successor.chat.browser_runtime_status",
        lambda *_args, **_kwargs: BrowserRuntimeStatus(
            package_available=True,
            python_executable="/usr/bin/python3",
            using_external_runtime=False,
            channel="chrome",
            executable_path="",
            user_data_dir="/tmp/browser",
        ),
    )
    chat = SuccessorChat(
        profile=Profile(
            name="browser-chat",
            tools=("browser",),
            skills=("browser-operator",),
        ),
        client=_MockClient(),
    )
    chat.messages = []
    chat._stream = _StaticStream([
        StreamEnded(
            finish_reason="tool_calls",
            usage=None,
            timings=None,
            full_reasoning="",
            full_content="",
            tool_calls=({
                "id": "call_skill_1",
                "name": "skill",
                "arguments": {
                    "skill": "browser-operator",
                    "task": "Open the page and inspect the CTA.",
                },
                "raw_arguments": '{"skill":"browser-operator","task":"Open the page and inspect the CTA."}',
            },),
        ),
    ])

    chat._pump_stream()
    _pump_until_idle(chat)

    cards = [m.tool_card for m in chat.messages if m.tool_card is not None]
    assert len(cards) == 1
    assert cards[0].tool_name == "skill"
    assert cards[0].tool_call_id == "call_skill_1"
    assert "Loaded skill `browser-operator`." in cards[0].output

    api_messages = chat._build_api_messages_native("system prompt")
    assert api_messages[-1]["role"] == "tool"
    assert "<skill-loaded>" in api_messages[-1]["content"]
    assert "browser-operator" in api_messages[-1]["content"]
