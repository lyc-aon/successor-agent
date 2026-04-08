"""Chat integration coverage for holonet and browser native tools."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from successor.chat import SuccessorChat
from successor.profiles import Profile
from successor.providers.llama import StreamEnded
from successor.tool_runner import ToolExecutionResult
from successor.web.browser import BrowserRuntimeStatus


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


def _pump_until_idle(chat: SuccessorChat, *, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        chat._pump_stream()
        chat._pump_running_tools()
        if chat._stream is None and not chat._running_tools:
            return
        time.sleep(0.02)
    raise AssertionError("chat did not settle")


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
        assert chat._enabled_tools_for_turn() == ["bash"]


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
        assert chat._enabled_tools_for_turn() == ["browser", "skill"]


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
        assert chat._enabled_tools_for_turn() == ["bash"]


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
