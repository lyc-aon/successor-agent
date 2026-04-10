"""Chat-loop integration tests for the internal task ledger tool."""

from __future__ import annotations

import json
import time
from pathlib import Path

from successor.chat import SuccessorChat
from successor.profiles import Profile
from successor.providers.llama import ContentChunk, StreamEnded


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


class _CapturingClient:
    def __init__(self, streams: list[_StaticStream]) -> None:
        self._streams = list(streams)
        self.call_count = 0
        self.calls: list[dict[str, object]] = []
        self.base_url = "http://mock"
        self.model = "mock-model"

    def stream_chat(self, messages, **kwargs):
        self.call_count += 1
        self.calls.append({
            "messages": list(messages),
            "tools": kwargs.get("tools"),
        })
        if not self._streams:
            raise RuntimeError("capturing client exhausted")
        return self._streams.pop(0)


def _pump_until_idle(chat: SuccessorChat, *, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        chat._pump_stream()
        chat._pump_running_tools()
        if chat._stream is None and chat._agent_turn == 0 and not chat._running_tools:
            return
        time.sleep(0.01)
    raise AssertionError("chat did not settle")


def _trace_events(root: Path) -> list[dict]:
    trace_files = sorted((root / "logs").glob("*.jsonl"))
    assert trace_files, "expected trace files"
    return [
        json.loads(line)
        for line in trace_files[-1].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_task_tool_dispatch_updates_session_ledger(temp_config_dir: Path) -> None:
    chat = SuccessorChat(
        profile=Profile(name="agent", tools=("bash",)),
        client=_CapturingClient([]),
    )
    chat.messages = []

    assert chat._spawn_task_runner(
        {
            "items": [
                {
                    "content": "Implement task ledger",
                    "active_form": "implementing task ledger",
                    "status": "in_progress",
                }
            ]
        },
        tool_call_id="call_task_1",
    )

    assert chat._task_ledger.has_in_progress() is True
    cards = [m.tool_card for m in chat.messages if m.tool_card is not None]
    assert len(cards) == 1
    assert cards[0].tool_name == "task"
    assert cards[0].tool_call_id == "call_task_1"
    assert "Updated the session task ledger." in cards[0].output
    assert "<task-ledger>" in (cards[0].api_content_override or "")


def test_task_tool_is_enabled_for_agentic_turns(temp_config_dir: Path) -> None:
    client = _CapturingClient([_StaticStream([])])
    chat = SuccessorChat(
        profile=Profile(name="agent", tools=("bash",)),
        client=client,
    )

    chat.input_buffer = "hello"
    chat._submit()

    tools = client.calls[0]["tools"]
    assert isinstance(tools, list)
    names = [entry["function"]["name"] for entry in tools]
    assert names == ["bash", "task", "verify", "runbook"]


def test_long_horizon_request_gets_planning_reminder_before_work(
    temp_config_dir: Path,
) -> None:
    client = _CapturingClient([
        _StaticStream([
            ContentChunk(text="Starting with a plan."),
            StreamEnded(
                finish_reason="stop",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="Starting with a plan.",
                tool_calls=(),
            ),
        ]),
    ])
    chat = SuccessorChat(
        profile=Profile(name="agent", tools=("bash",)),
        client=client,
    )

    chat.input_buffer = (
        "Build a browser bullet-hell typing game, verify the runtime "
        "honestly, iterate for several turns, and record the session."
    )
    chat._submit()
    _pump_until_idle(chat)
    chat._trace.close()

    assert client.call_count == 1
    first_sys = client.calls[0]["messages"][0]
    assert first_sys["role"] == "system"
    assert "Planning Reminder" in first_sys["content"]
    assert "call `task` with 3-6 coarse steps" in first_sys["content"]

    events = _trace_events(temp_config_dir)
    assert any(event["type"] == "task_adoption_nudge" for event in events)


def test_in_progress_task_triggers_single_continuation_nudge(
    temp_config_dir: Path,
) -> None:
    client = _CapturingClient([
        _StaticStream([
            StreamEnded(
                finish_reason="tool_calls",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="",
                tool_calls=({
                    "id": "task_1",
                    "name": "task",
                    "arguments": {
                        "items": [
                            {
                                "content": "Implement task ledger",
                                "active_form": "implementing task ledger",
                                "status": "in_progress",
                            }
                        ]
                    },
                    "raw_arguments": '{"items":[{"content":"Implement task ledger","active_form":"implementing task ledger","status":"in_progress"}]}',
                },),
            ),
        ]),
        _StaticStream([
            ContentChunk(text="I made the first change."),
            StreamEnded(
                finish_reason="stop",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="I made the first change.",
                tool_calls=(),
            ),
        ]),
        _StaticStream([
            StreamEnded(
                finish_reason="tool_calls",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="",
                tool_calls=({
                    "id": "task_2",
                    "name": "task",
                    "arguments": {
                        "items": [
                            {
                                "content": "Implement task ledger",
                                "active_form": "implementing task ledger",
                                "status": "completed",
                            }
                        ]
                    },
                    "raw_arguments": '{"items":[{"content":"Implement task ledger","active_form":"implementing task ledger","status":"completed"}]}',
                },),
            ),
        ]),
        _StaticStream([
            ContentChunk(text="All done."),
            StreamEnded(
                finish_reason="stop",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="All done.",
                tool_calls=(),
            ),
        ]),
    ])
    chat = SuccessorChat(
        profile=Profile(name="agent", tools=("bash",)),
        client=client,
    )
    chat.messages = []

    chat.input_buffer = "implement it"
    chat._submit()
    _pump_until_idle(chat)
    chat._trace.close()

    assert client.call_count == 4
    assert chat._agent_turn == 0
    assert any(m.raw_text == "All done." for m in chat.messages if m.role == "successor")
    third_sys = client.calls[2]["messages"][0]
    assert third_sys["role"] == "system"
    assert "Continuation Reminder" in third_sys["content"]
    assert "implementing task ledger" in third_sys["content"]

    events = _trace_events(temp_config_dir)
    assert any(event["type"] == "task_continue_nudge" for event in events)


def test_pending_only_tasks_do_not_force_continuation(temp_config_dir: Path) -> None:
    client = _CapturingClient([
        _StaticStream([
            StreamEnded(
                finish_reason="tool_calls",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="",
                tool_calls=({
                    "id": "task_pending",
                    "name": "task",
                    "arguments": {
                        "items": [
                            {"content": "Wait for user confirmation", "status": "pending"}
                        ]
                    },
                    "raw_arguments": '{"items":[{"content":"Wait for user confirmation","status":"pending"}]}',
                },),
            ),
        ]),
        _StaticStream([
            ContentChunk(text="Need your confirmation before I continue."),
            StreamEnded(
                finish_reason="stop",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="Need your confirmation before I continue.",
                tool_calls=(),
            ),
        ]),
    ])
    chat = SuccessorChat(
        profile=Profile(name="agent", tools=("bash",)),
        client=client,
    )
    chat.messages = []

    chat.input_buffer = "wait"
    chat._submit()
    _pump_until_idle(chat)

    assert client.call_count == 2
    assert chat._agent_turn == 0
