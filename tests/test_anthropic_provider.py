"""Tests for the AnthropicClient provider and its SSE parsing.

Covers:
  1. Protocol conformance (ChatProvider structural typing)
  2. Construction and attribute access
  3. Factory dispatch for "anthropic" and "claude" alias
  4. System message extraction from message list
  5. Tool definition conversion (OpenAI format → Anthropic format)
  6. SSE parsing for text content, tool calls, and errors
  7. Context window detection via hardcoded table
"""

from __future__ import annotations

import json
import io

from successor.providers import (
    ChatProvider,
    make_provider,
    PROVIDER_REGISTRY,
)
from successor.providers.anthropic import (
    AnthropicClient,
    _AnthropicChatStream,
    _lookup_anthropic_fallback,
)
from successor.providers.llama import (
    ContentChunk,
    StreamEnded,
    StreamStarted,
)


# ─── Protocol conformance ───


def test_anthropic_conforms_to_chat_provider() -> None:
    client = AnthropicClient()
    assert isinstance(client, ChatProvider)


def test_anthropic_has_required_attributes() -> None:
    client = AnthropicClient(
        base_url="https://api.z.ai/api/anthropic",
        model="glm-5.1",
        api_key="test-key",
    )
    assert client.base_url == "https://api.z.ai/api/anthropic"
    assert client.model == "glm-5.1"
    assert client.api_key == "test-key"
    assert callable(client.stream_chat)
    assert callable(client.health)


def test_anthropic_provider_type() -> None:
    assert AnthropicClient.provider_type == "anthropic"


# ─── Factory dispatch ───


def test_factory_dispatches_anthropic() -> None:
    config = {
        "type": "anthropic",
        "base_url": "https://api.z.ai/api/anthropic",
        "model": "glm-5.1",
        "api_key": "test-key",
    }
    provider = make_provider(config)
    assert isinstance(provider, AnthropicClient)
    assert provider.base_url == "https://api.z.ai/api/anthropic"
    assert provider.model == "glm-5.1"


def test_factory_alias_claude() -> None:
    provider = make_provider({"type": "claude", "api_key": "k"})
    assert isinstance(provider, AnthropicClient)


def test_anthropic_in_registry() -> None:
    assert "anthropic" in PROVIDER_REGISTRY
    assert "claude" in PROVIDER_REGISTRY


# ─── System message extraction ───


def test_system_messages_extracted() -> None:
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ]
    system_text, anthropic_msgs = AnthropicClient._convert_messages(messages)
    assert system_text == "You are helpful."
    assert len(anthropic_msgs) == 1
    assert anthropic_msgs[0] == {"role": "user", "content": "Hello"}


def test_multiple_system_messages_joined() -> None:
    messages = [
        {"role": "system", "content": "Part one."},
        {"role": "system", "content": "Part two."},
        {"role": "user", "content": "Hi"},
    ]
    system_text, anthropic_msgs = AnthropicClient._convert_messages(messages)
    assert system_text == "Part one.\n\nPart two."
    assert len(anthropic_msgs) == 1


def test_no_system_messages() -> None:
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
    ]
    system_text, anthropic_msgs = AnthropicClient._convert_messages(messages)
    assert system_text == ""
    assert len(anthropic_msgs) == 2


# ─── Tool definition conversion ───


def test_tool_conversion_openai_to_anthropic() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Get the weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ]
    result = AnthropicClient._convert_tools(tools)
    assert len(result) == 1
    assert result[0]["name"] == "get_weather"
    assert result[0]["description"] == "Get the weather"
    assert "input_schema" in result[0]
    assert result[0]["input_schema"]["properties"]["city"]["type"] == "string"


# ─── Assistant tool_calls conversion ───


def test_assistant_tool_calls_converted() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_123",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city": "SF"}',
                    },
                }
            ],
        }
    ]
    _, anthropic_msgs = AnthropicClient._convert_messages(messages)
    assert len(anthropic_msgs) == 1
    content = anthropic_msgs[0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "tool_use"
    assert content[0]["id"] == "call_123"
    assert content[0]["name"] == "get_weather"
    assert content[0]["input"] == {"city": "SF"}


def test_assistant_tool_calls_with_text() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "Let me check.",
            "tool_calls": [
                {
                    "id": "call_456",
                    "function": {
                        "name": "search",
                        "arguments": '{"q": "test"}',
                    },
                }
            ],
        }
    ]
    _, anthropic_msgs = AnthropicClient._convert_messages(messages)
    content = anthropic_msgs[0]["content"]
    assert len(content) == 2
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "Let me check."
    assert content[1]["type"] == "tool_use"


# ─── Tool result conversion ───


def test_tool_result_converted() -> None:
    messages = [
        {
            "role": "tool",
            "tool_call_id": "call_123",
            "name": "get_weather",
            "content": "Sunny, 72F",
        }
    ]
    _, anthropic_msgs = AnthropicClient._convert_messages(messages)
    assert len(anthropic_msgs) == 1
    assert anthropic_msgs[0]["role"] == "user"
    content = anthropic_msgs[0]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "tool_result"
    assert content[0]["tool_use_id"] == "call_123"
    assert content[0]["content"] == "Sunny, 72F"


def test_consecutive_tool_results_batched_into_one_user_message() -> None:
    """Multiple tool_results from one assistant turn must be coalesced
    into a single user message with N tool_result blocks, per Anthropic
    spec. Previously each tool_result became its own user message, which
    capable models (GLM 5.1, Claude) handle poorly — it looks like the
    harness is issuing three separate user turns."""
    messages = [
        {"role": "user", "content": "do three things"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_a", "type": "function",
                 "function": {"name": "browser", "arguments": '{"action":"open"}'}},
                {"id": "call_b", "type": "function",
                 "function": {"name": "vision", "arguments": '{"path":"/tmp/x.png"}'}},
                {"id": "call_c", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"file_path":"/tmp/y"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "page opened"},
        {"role": "tool", "tool_call_id": "call_b", "content": "image described"},
        {"role": "tool", "tool_call_id": "call_c", "content": "file contents"},
        {"role": "user", "content": "next"},
    ]
    _, anthropic_msgs = AnthropicClient._convert_messages(messages)
    # Expect: user(text), assistant(3 tool_use), user(3 tool_result), user(text)
    assert len(anthropic_msgs) == 4
    assert anthropic_msgs[0] == {"role": "user", "content": "do three things"}
    assert anthropic_msgs[1]["role"] == "assistant"
    assert len(anthropic_msgs[1]["content"]) == 3
    # The batched tool_result message:
    batched = anthropic_msgs[2]
    assert batched["role"] == "user"
    assert isinstance(batched["content"], list)
    assert len(batched["content"]) == 3
    assert [b["type"] for b in batched["content"]] == [
        "tool_result", "tool_result", "tool_result"
    ]
    assert [b["tool_use_id"] for b in batched["content"]] == [
        "call_a", "call_b", "call_c"
    ]
    assert [b["content"] for b in batched["content"]] == [
        "page opened", "image described", "file contents"
    ]
    # Trailing user message preserved, not merged with the batch
    assert anthropic_msgs[3] == {"role": "user", "content": "next"}


def test_trailing_tool_results_flushed() -> None:
    """Messages ending with tool_results (no subsequent non-tool message)
    still get flushed as a single user message — covers the snapshot
    case where we serialize mid-turn."""
    messages = [
        {"role": "user", "content": "ping"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_x", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_x", "content": "contents"},
    ]
    _, anthropic_msgs = AnthropicClient._convert_messages(messages)
    assert len(anthropic_msgs) == 3
    assert anthropic_msgs[2]["role"] == "user"
    assert len(anthropic_msgs[2]["content"]) == 1
    assert anthropic_msgs[2]["content"][0]["tool_use_id"] == "call_x"


# ─── Context window detection ───


def test_detect_context_window_claude() -> None:
    client = AnthropicClient(model="claude-sonnet-4-6-20250514")
    assert client.detect_context_window() == 200_000


def test_detect_context_window_glm() -> None:
    client = AnthropicClient(model="glm-5.1")
    assert client.detect_context_window() == 128_000


def test_detect_context_window_unknown() -> None:
    client = AnthropicClient(model="unknown-model-xyz")
    assert client.detect_context_window() is None


def test_detect_context_window_cached() -> None:
    client = AnthropicClient(model="glm-5.1")
    result1 = client.detect_context_window()
    result2 = client.detect_context_window()
    assert result1 == result2 == 128_000


def test_lookup_anthropic_fallback_prefix_match() -> None:
    assert _lookup_anthropic_fallback("glm-5.1-some-suffix") == 128_000


def test_lookup_anthropic_fallback_empty() -> None:
    assert _lookup_anthropic_fallback("") is None


# ─── SSE parsing ───


def _make_anthropic_sse(events: list[tuple[str, dict]]) -> bytes:
    """Build a fake Anthropic SSE response from event tuples."""
    lines = []
    for event_name, data in events:
        lines.append(f"event: {event_name}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")
    return "\n".join(lines).encode("utf-8")


class _FakeResponse:
    """Fake urllib response that yields lines from a bytes buffer."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)

    def readline(self):
        return self._buf.readline()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_sse_text_content(monkeypatch) -> None:
    """Parse a simple text-only Anthropic SSE stream."""
    sse_data = _make_anthropic_sse([
        ("message_start", {"type": "message_start", "message": {"id": "msg_1"}}),
        ("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        ("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "Hello"},
        }),
        ("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": " world"},
        }),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        }),
        ("message_stop", {"type": "message_stop"}),
    ])

    stream = _AnthropicChatStream(
        url="https://test",
        body={},
        timeout=30.0,
        connect_timeout=5.0,
        api_key="test",
    )
    # Monkeypatch the SSE reading to use our fake response
    fake_resp = _FakeResponse(sse_data)
    stream._queue.put(StreamStarted())
    stream._read_anthropic_sse(fake_resp, 30.0)
    stream._done.set()

    events = stream.drain()
    # Filter out the extra StreamStarted we manually put
    content_events = [e for e in events if isinstance(e, ContentChunk)]
    assert len(content_events) == 2
    assert content_events[0].text == "Hello"
    assert content_events[1].text == " world"

    end_events = [e for e in events if isinstance(e, StreamEnded)]
    assert len(end_events) == 1
    assert end_events[0].finish_reason == "stop"
    assert end_events[0].usage == {"output_tokens": 5}


def test_sse_tool_calls(monkeypatch) -> None:
    """Parse an Anthropic SSE stream with tool_use content blocks."""
    sse_data = _make_anthropic_sse([
        ("message_start", {"type": "message_start", "message": {"id": "msg_2"}}),
        ("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        ("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "Let me check."},
        }),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("content_block_start", {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_123", "name": "get_weather", "input": {}},
        }),
        ("content_block_delta", {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": "{\"city\":"},
        }),
        ("content_block_delta", {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": "\"SF\"}"},
        }),
        ("content_block_stop", {"type": "content_block_stop", "index": 1}),
        ("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 20},
        }),
        ("message_stop", {"type": "message_stop"}),
    ])

    stream = _AnthropicChatStream(
        url="https://test",
        body={},
        timeout=30.0,
        connect_timeout=5.0,
        api_key="test",
    )
    fake_resp = _FakeResponse(sse_data)
    stream._queue.put(StreamStarted())
    stream._read_anthropic_sse(fake_resp, 30.0)
    stream._done.set()

    events = stream.drain()
    end_events = [e for e in events if isinstance(e, StreamEnded)]
    assert len(end_events) == 1
    assert end_events[0].finish_reason == "tool_calls"
    assert len(end_events[0].tool_calls) == 1
    tc = end_events[0].tool_calls[0]
    assert tc["name"] == "get_weather"
    assert tc["arguments"] == {"city": "SF"}


def test_sse_context_window_exceeded_stop_reason() -> None:
    """model_context_window_exceeded maps to finish_reason='length'."""
    sse_data = _make_anthropic_sse([
        ("message_start", {"type": "message_start", "message": {"id": "msg_ctx"}}),
        ("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }),
        ("content_block_delta", {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "partial"},
        }),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "model_context_window_exceeded"},
            "usage": {"output_tokens": 1},
        }),
        ("message_stop", {"type": "message_stop"}),
    ])

    stream = _AnthropicChatStream(
        url="https://test",
        body={},
        timeout=30.0,
        connect_timeout=5.0,
        api_key="test",
    )
    fake_resp = _FakeResponse(sse_data)
    stream._queue.put(StreamStarted())
    stream._read_anthropic_sse(fake_resp, 30.0)
    stream._done.set()

    events = stream.drain()
    end_events = [e for e in events if isinstance(e, StreamEnded)]
    assert len(end_events) == 1
    assert end_events[0].finish_reason == "length"
