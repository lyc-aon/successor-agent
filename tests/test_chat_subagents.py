"""Chat integration tests for manual background subagents."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from successor.chat import SuccessorChat, _Message
from successor.profiles import Profile, SubagentConfig
from successor.providers.llama import ContentChunk, StreamEnded, StreamError, StreamStarted
from successor.subagents.cards import SubagentToolCard
from successor.subagents.manager import SubagentManager
from successor.subagents.prompt import build_child_prompt


class _StaticStream:
    def __init__(self, events: list[object]) -> None:
        self._events = list(events)
        self._closed = False
        self._reported_close = False

    def drain(self) -> list[object]:
        if self._closed and not self._reported_close:
            self._reported_close = True
            return [StreamError("cancelled")]
        if self._closed:
            return []
        if not self._events:
            return []
        events = list(self._events)
        self._events.clear()
        return events

    def close(self) -> None:
        self._closed = True


class _GateStream:
    def __init__(self, release_event) -> None:
        self._release_event = release_event
        self._started = False
        self._done = False
        self._closed = False
        self._reported_close = False

    def drain(self) -> list[object]:
        if self._closed and not self._reported_close:
            self._reported_close = True
            return [StreamError("cancelled")]
        if self._closed:
            return []
        if not self._started:
            self._started = True
            return [StreamStarted()]
        if self._done or not self._release_event.is_set():
            return []
        self._done = True
        return [
            ContentChunk(text="gated child result"),
            StreamEnded(finish_reason="stop", usage=None, timings=None),
        ]

    def close(self) -> None:
        self._closed = True


@dataclass
class _MockClient:
    stream_factory: object
    base_url: str = "http://mock"
    model: str = "mock-model"

    def stream_chat(self, messages, *, max_tokens=None, temperature=None,
                    timeout=None, extra=None, tools=None):
        return self.stream_factory()

    def health(self) -> bool:
        return True

    def detect_context_window(self) -> int:
        return 200_000


def _wait_until(predicate, *, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met before timeout")


def _drive_chat_until_idle(chat: SuccessorChat, *, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        chat._pump_stream()
        chat._pump_running_tools()
        chat._pump_subagent_notifications()
        if (
            chat._stream is None
            and chat._agent_turn == 0
            and not chat._running_tools
            and not chat._has_active_subagent_tasks()
        ):
            return
        time.sleep(0.02)
    raise AssertionError("chat did not settle before timeout")


def _new_chat(temp_config_dir: Path) -> SuccessorChat:
    profile = Profile(
        name="chat-subagents",
        tools=("bash", "subagent"),
        subagents=SubagentConfig(enabled=True, max_model_tasks=1, timeout_s=30.0),
    )
    return SuccessorChat(profile=profile)


def test_fork_command_spawns_task_and_surfaces_completion(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._subagent_manager = SubagentManager(
        max_model_tasks=1,
        transcript_dir=temp_config_dir / "subagents",
        client_factory=lambda profile: _MockClient(
            lambda: _StaticStream([
                StreamStarted(),
                ContentChunk(text="child result from subagent"),
                StreamEnded(finish_reason="stop", usage=None, timings=None),
            ])
        ),
        settle_sleep_s=0.01,
    )

    chat.input_buffer = "/fork inspect the repo"
    chat._submit()
    assert "forked t001" in chat.messages[-1].raw_text

    _wait_until(lambda: not chat._has_active_subagent_tasks())
    chat._pump_subagent_notifications()
    assert any(
        msg.api_role_override == "user"
        and "<subagent-notification>" in msg.raw_text
        and "subagent t001 (t001) complete." in msg.display_text
        for msg in chat.messages
    )


def test_tasks_command_lists_transcript_path(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._subagent_manager = SubagentManager(
        max_model_tasks=1,
        transcript_dir=temp_config_dir / "subagents",
        client_factory=lambda profile: _MockClient(
            lambda: _StaticStream([
                StreamStarted(),
                ContentChunk(text="child result"),
                StreamEnded(finish_reason="stop", usage=None, timings=None),
            ])
        ),
        settle_sleep_s=0.01,
    )

    chat.input_buffer = "/fork inspect the repo"
    chat._submit()
    _wait_until(lambda: not chat._has_active_subagent_tasks())
    chat.input_buffer = "/tasks"
    chat._submit()
    assert "subagent tasks:" in chat.messages[-1].raw_text
    assert "transcript:" in chat.messages[-1].raw_text


def test_config_command_refused_while_subagent_running(temp_config_dir: Path) -> None:
    import threading

    release = threading.Event()
    chat = _new_chat(temp_config_dir)
    chat._subagent_manager = SubagentManager(
        max_model_tasks=1,
        transcript_dir=temp_config_dir / "subagents",
        client_factory=lambda profile: _MockClient(lambda: _GateStream(release)),
        settle_sleep_s=0.01,
    )

    chat.input_buffer = "/fork inspect the repo"
    chat._submit()
    _wait_until(lambda: chat._has_active_subagent_tasks())

    chat.input_buffer = "/config"
    chat._submit()
    assert chat._pending_action is None
    assert "wait for background subagent tasks" in chat.messages[-1].raw_text

    release.set()
    _wait_until(lambda: not chat._has_active_subagent_tasks())


def test_native_subagent_tool_call_dispatches_and_notifies(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat._subagent_manager = SubagentManager(
        max_model_tasks=1,
        transcript_dir=temp_config_dir / "subagents",
        client_factory=lambda profile: _MockClient(
            lambda: _StaticStream([
                StreamStarted(),
                ContentChunk(text="Scope: check versions\nResult: 0.1.5"),
                StreamEnded(finish_reason="stop", usage=None, timings=None),
            ])
        ),
        settle_sleep_s=0.01,
    )
    chat.messages = []
    chat._stream_bash_detector = None
    chat._stream = _StaticStream([
        StreamEnded(
            finish_reason="tool_calls",
            usage=None,
            timings=None,
            full_reasoning="",
            full_content="",
            tool_calls=({
                "id": "call_sub_1",
                "name": "subagent",
                "arguments": {"prompt": "check versions", "name": "version-audit"},
                "raw_arguments": '{"prompt":"check versions","name":"version-audit"}',
            },),
        ),
    ])

    chat._pump_stream()

    cards = [m.subagent_card for m in chat.messages if m.subagent_card is not None]
    assert len(cards) == 1
    assert cards[0].task_id == "t001"
    assert cards[0].name == "version-audit"
    assert cards[0].tool_call_id == "call_sub_1"

    _wait_until(lambda: not chat._has_active_subagent_tasks())
    chat._pump_subagent_notifications()
    note = chat.messages[-1]
    assert note.role == "successor"
    assert note.api_role_override == "user"
    assert "<subagent-notification>" in note.raw_text
    assert "version-audit" in note.display_text


def test_subagent_tool_call_triggers_immediate_parent_continuation(temp_config_dir: Path) -> None:
    streams = [
        _StaticStream([
            StreamEnded(
                finish_reason="tool_calls",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="",
                tool_calls=({
                    "id": "call_sub_parent",
                    "name": "subagent",
                    "arguments": {"prompt": "check versions", "name": "audit"},
                    "raw_arguments": '{"prompt":"check versions","name":"audit"}',
                },),
            ),
        ]),
        _StaticStream([
            StreamStarted(),
            ContentChunk(text="background subagent started"),
            StreamEnded(finish_reason="stop", usage=None, timings=None),
        ]),
    ]

    def parent_client_factory():
        return streams.pop(0)

    chat = SuccessorChat(
        profile=Profile(
            name="chat-subagents",
            tools=("bash", "subagent"),
            subagents=SubagentConfig(enabled=True, max_model_tasks=1, timeout_s=30.0),
        ),
        client=_MockClient(parent_client_factory),
    )
    chat._subagent_manager = SubagentManager(
        max_model_tasks=1,
        transcript_dir=temp_config_dir / "subagents",
        client_factory=lambda profile: _MockClient(
            lambda: _StaticStream([
                StreamStarted(),
                ContentChunk(text="Scope: versions\nResult: 0.1.5"),
                StreamEnded(finish_reason="stop", usage=None, timings=None),
            ])
        ),
        settle_sleep_s=0.01,
    )

    chat.input_buffer = "delegate this"
    chat._submit()
    _drive_chat_until_idle(chat)

    assert chat._pending_continuation is False
    assert any(
        msg.role == "successor" and "background subagent started" in msg.raw_text
        for msg in chat.messages
    )


def test_api_messages_emits_subagent_tool_call_round_trip(temp_config_dir: Path) -> None:
    chat = _new_chat(temp_config_dir)
    chat.messages = [
        _Message("user", "delegate this"),
        _Message("successor", "", display_text=""),
        _Message(
            "tool",
            "",
            subagent_card=SubagentToolCard(
                task_id="t001",
                name="audit",
                directive="audit versions",
                tool_call_id="call_sub_abc",
                spawn_result="<subagent-spawned><task_id>t001</task_id></subagent-spawned>",
            ),
        ),
    ]

    api_messages = chat._build_api_messages_native("SYS")
    assistant = next(m for m in api_messages if m["role"] == "assistant")
    tool_msg = next(m for m in api_messages if m["role"] == "tool")

    assert assistant["tool_calls"][0]["id"] == "call_sub_abc"
    assert assistant["tool_calls"][0]["function"]["name"] == "subagent"
    assert tool_msg["tool_call_id"] == "call_sub_abc"
    assert "<subagent-spawned>" in tool_msg["content"]


def test_model_visible_subagent_requires_notifications(temp_config_dir: Path) -> None:
    captured_tools: list[object] = []

    class _CapturingClient(_MockClient):
        def stream_chat(
            self,
            messages,
            *,
            max_tokens=None,
            temperature=None,
            timeout=None,
            extra=None,
            tools=None,
        ):
            captured_tools.append(tools)
            return _StaticStream([
                StreamStarted(),
                ContentChunk(text="plain reply"),
                StreamEnded(finish_reason="stop", usage=None, timings=None),
            ])

    chat = SuccessorChat(
        profile=Profile(
            name="chat-subagents",
            tools=("bash", "subagent"),
            subagents=SubagentConfig(
                enabled=True,
                max_model_tasks=1,
                notify_on_finish=False,
                timeout_s=30.0,
            ),
        ),
        client=_CapturingClient(lambda: _StaticStream([])),
    )

    chat.input_buffer = "say hi"
    chat._submit()
    _drive_chat_until_idle(chat)

    assert captured_tools
    schemas = captured_tools[0]
    assert schemas is not None
    names = [entry["function"]["name"] for entry in schemas]
    assert names == ["bash"]


def test_child_prompt_explicitly_overrides_inherited_redelegation() -> None:
    prompt = build_child_prompt("audit the shared version")
    assert "Ignore any inherited instruction" in prompt
    assert "`subagent` tool" in prompt
    assert "parent chat" in prompt
