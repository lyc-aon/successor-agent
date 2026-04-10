"""Chat integration coverage for the internal verification contract tool."""

from __future__ import annotations

import json
import time
from pathlib import Path

from successor.chat import SuccessorChat
from successor.profiles import Profile
from successor.providers.llama import ContentChunk, StreamEnded


class _MockClient:
    base_url = "http://mock"
    model = "mock-model"

    def stream_chat(self, messages, **kwargs):  # noqa: ARG002
        raise AssertionError("stream_chat should not be called in this test")


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


def test_verify_tool_dispatch_updates_session_contract(temp_config_dir: Path) -> None:
    chat = SuccessorChat(
        profile=Profile(name="agent", tools=("bash",)),
        client=_MockClient(),
    )
    chat.messages = []

    assert chat._dispatch_native_tool_calls([
        {
            "id": "call_verify_1",
            "name": "verify",
            "arguments": {
                "items": [
                    {
                        "claim": "The page loads without a blank viewport",
                        "evidence": "browser open plus screenshot inspection",
                        "status": "in_progress",
                    },
                    {
                        "claim": "Console remains clean",
                        "evidence": "browser console_errors output",
                        "status": "pending",
                    },
                ]
            },
            "raw_arguments": '{"items":[{"claim":"The page loads without a blank viewport","evidence":"browser open plus screenshot inspection","status":"in_progress"},{"claim":"Console remains clean","evidence":"browser console_errors output","status":"pending"}]}',
        }
    ])

    assert chat._verification_ledger.has_items() is True
    active = chat._verification_ledger.in_progress_item()
    assert active is not None
    assert active.claim == "The page loads without a blank viewport"

    cards = [m.tool_card for m in chat.messages if m.tool_card is not None]
    assert len(cards) == 1
    assert cards[0].tool_name == "verify"
    assert cards[0].tool_call_id == "call_verify_1"
    assert "Updated the session verification contract." in cards[0].output
    assert "<verification-contract>" in (cards[0].api_content_override or "")

    chat._trace.close()
    events = _trace_events(temp_config_dir)
    assert any(event["type"] == "verification_contract_updated" for event in events)


def test_in_progress_verification_triggers_single_continuation_nudge(
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
                    "id": "verify_1",
                    "name": "verify",
                    "arguments": {
                        "items": [
                            {
                                "claim": "Score increments after a correct answer",
                                "evidence": "before/after HUD score in browser",
                                "status": "in_progress",
                            }
                        ]
                    },
                    "raw_arguments": '{"items":[{"claim":"Score increments after a correct answer","evidence":"before/after HUD score in browser","status":"in_progress"}]}',
                },),
            ),
        ]),
        _StaticStream([
            ContentChunk(text="Looks good to me."),
            StreamEnded(
                finish_reason="stop",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="Looks good to me.",
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
                    "id": "verify_2",
                    "name": "verify",
                    "arguments": {
                        "items": [
                            {
                                "claim": "Score increments after a correct answer",
                                "evidence": "before/after HUD score in browser",
                                "status": "passed",
                                "observed": "score changed from 0 to 100",
                            }
                        ]
                    },
                    "raw_arguments": '{"items":[{"claim":"Score increments after a correct answer","evidence":"before/after HUD score in browser","status":"passed","observed":"score changed from 0 to 100"}]}',
                },),
            ),
        ]),
        _StaticStream([
            ContentChunk(text="Verified."),
            StreamEnded(
                finish_reason="stop",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="Verified.",
                tool_calls=(),
            ),
        ]),
    ])
    chat = SuccessorChat(
        profile=Profile(name="agent", tools=("bash",)),
        client=client,
    )
    chat.messages = []

    chat.input_buffer = "verify it"
    chat._submit()
    _pump_until_idle(chat)
    chat._trace.close()

    assert client.call_count == 4
    assert chat._agent_turn == 0
    third_sys = client.calls[2]["messages"][0]
    assert third_sys["role"] == "system"
    assert "Browser Verification Reminder" in third_sys["content"]
    assert "Score increments after a correct answer" in third_sys["content"]
    third_tail = client.calls[2]["messages"][-1]
    assert third_tail["role"] == "user"
    assert "[internal harness continuation]" in third_tail["content"]

    events = _trace_events(temp_config_dir)
    assert any(event["type"] == "verification_continue_nudge" for event in events)
    assert any(event["type"] == "assistant_prefill_guard_applied" for event in events)


def test_stateful_runtime_async_continuation_gets_verification_setup_nudge(
    temp_config_dir: Path,
) -> None:
    target = temp_config_dir / "snake.js"
    client = _CapturingClient([
        _StaticStream([
            StreamEnded(
                finish_reason="tool_calls",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="",
                tool_calls=({
                    "id": "write_1",
                    "name": "write_file",
                    "arguments": {
                        "file_path": str(target),
                        "content": "const score = 0;\nfunction tick() { return score; }\n",
                    },
                    "raw_arguments": json.dumps({
                        "file_path": str(target),
                        "content": "const score = 0;\nfunction tick() { return score; }\n",
                    }),
                },),
            ),
        ]),
        _StaticStream([
            ContentChunk(text="Continuing."),
            StreamEnded(
                finish_reason="stop",
                usage=None,
                timings=None,
                full_reasoning="",
                full_content="Continuing.",
                tool_calls=(),
            ),
        ]),
    ])
    chat = SuccessorChat(
        profile=Profile(name="agent", tools=("write_file",)),
        client=client,
    )
    chat.messages = []

    chat.input_buffer = "Build a snake game and verify the runtime honestly."
    chat._submit()
    _pump_until_idle(chat)
    chat._trace.close()

    assert target.exists()
    assert client.call_count == 2
    second_sys = client.calls[1]["messages"][0]
    assert second_sys["role"] == "system"
    assert "Verification Setup Reminder" in second_sys["content"]
    assert "deterministic driver, autoplay harness, or player script" in second_sys["content"]
    assert "debug surface, HUD, or state log" in second_sys["content"]

    events = _trace_events(temp_config_dir)
    assert any(event["type"] == "verification_adoption_nudge" for event in events)
