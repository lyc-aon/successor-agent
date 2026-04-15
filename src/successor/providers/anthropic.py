"""Anthropic Messages API streaming client.

The Anthropic Messages API is the protocol spoken by api.anthropic.com
and by compatible wrappers like z.ai. It differs from the OpenAI
/v1/chat/completions surface in several important ways:

  - Endpoint: POST /v1/messages (not /v1/chat/completions)
  - Auth: x-api-key header + anthropic-version header
  - System prompt: top-level ``system`` field, not a message with
    role "system" in the messages array
  - SSE: Named events (event: content_block_delta) instead of
    unnamed data: lines
  - Content: Array of typed blocks (text, tool_use) instead of a
    plain string
  - Tool calls: Content blocks with incremental input_json_delta
    fragments instead of delta.tool_calls

This client translates between Successor's internal message format
(which follows OpenAI conventions) and the Anthropic wire format,
so the rest of the harness doesn't need to know which backend is
in use.

Emits the same typed events as the other providers (StreamStarted,
ReasoningChunk, ContentChunk, StreamEnded, StreamError) so the
chat App's _pump_stream doesn't care which backend produced them.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Iterable

from .llama import (
    ChatStream,
    ContentChunk,
    ReasoningChunk,
    StreamEnded,
    StreamError,
    StreamStarted,
)


# ─── Context window fallback table ───

# Anthropic and GLM models whose context windows are known. Prefix-matched
# in declaration order so specific entries win over general ones.
_ANTHROPIC_MODEL_WINDOWS: tuple[tuple[str, int], ...] = (
    # Claude 4.6 family (200K)
    ("claude-opus-4-6", 200_000),
    ("claude-sonnet-4-6", 200_000),
    # Claude 4.5 family (200K)
    ("claude-haiku-4-5", 200_000),
    # GLM family via z.ai (128K)
    ("glm-5.1", 128_000),
    ("glm-5-turbo", 128_000),
    ("glm-5", 128_000),
    ("glm-4.7", 128_000),
    ("glm-4.6", 128_000),
    ("glm-4.5", 128_000),
)


def _lookup_anthropic_fallback(model_id: str) -> int | None:
    """Return a hardcoded context window for an Anthropic/GLM model, or None."""
    if not model_id:
        return None
    lowered = model_id.lower()
    for prefix, window in _ANTHROPIC_MODEL_WINDOWS:
        if lowered.startswith(prefix):
            return window
    return None


# ─── AnthropicClient ───


class AnthropicClient:
    """Configured Anthropic Messages API endpoint factory.

    base_url accepts the server root without /v1 — the client appends
    /v1/messages itself. Examples:

      - https://api.anthropic.com
      - https://api.z.ai/api/anthropic

    Auth uses the x-api-key header (Anthropic convention) rather than
    Authorization: Bearer (OpenAI convention).

    Conforms structurally to `providers.base.ChatProvider`.
    """

    provider_type = "anthropic"
    supports_tokenize_endpoint = False

    def __init__(
        self,
        *,
        base_url: str = "https://api.anthropic.com",
        model: str = "claude-sonnet-4-6-20250514",
        api_key: str | None = None,
        default_max_tokens: int = 32768,
        default_temperature: float = 0.7,
        default_timeout: float = 600.0,
        connect_timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.default_max_tokens = default_max_tokens
        self.default_temperature = default_temperature
        self.default_timeout = default_timeout
        self.connect_timeout = connect_timeout

    def count_text_tokens(self, text: str) -> int | None:
        """Anthropic does not expose a universal tokenizer API."""
        return None

    def stream_chat(
        self,
        messages: Iterable[dict],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        extra: dict | None = None,
        tools: list[dict] | None = None,
    ) -> ChatStream:
        """Open a streaming Anthropic Messages completion.

        Translates Successor's OpenAI-convention messages into Anthropic
        format and returns a ChatStream that emits the standard events.
        """
        system_text, anthropic_messages = self._convert_messages(messages)
        anthropic_tools = self._convert_tools(tools) if tools else None

        body: dict = {
            "model": self.model,
            "stream": True,
            "max_tokens": max_tokens if max_tokens is not None else self.default_max_tokens,
            "temperature": (
                temperature if temperature is not None else self.default_temperature
            ),
            "messages": anthropic_messages,
        }
        if system_text:
            body["system"] = system_text
        if anthropic_tools:
            body["tools"] = anthropic_tools
        if extra:
            body.update(extra)

        url = f"{self.base_url}/v1/messages"

        return _AnthropicChatStream(
            url=url,
            body=body,
            timeout=timeout if timeout is not None else self.default_timeout,
            connect_timeout=self.connect_timeout,
            api_key=self.api_key or "",
        )

    def health(self) -> bool:
        """Quick blocking health check.

        Sends a minimal messages request and checks for a non-error
        response. Returns True if the server responds with 200 (or a
        streamable content type), False otherwise.
        """
        url = f"{self.base_url}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        body = json.dumps({
            "model": self.model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        }).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                return resp.status == 200
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            return False

    def detect_context_window(self) -> int | None:
        """Return a known context window for the configured model.

        Anthropic does not expose a /v1/models endpoint with context
        window info, so we rely on the hardcoded fallback table.
        Result is cached after the first call.
        """
        if hasattr(self, "_cached_context_window"):
            return self._cached_context_window
        result = _lookup_anthropic_fallback(self.model)
        self._cached_context_window = result
        return result

    # ─── Message format conversion ───

    @staticmethod
    def _convert_messages(messages: Iterable[dict]) -> tuple[str, list[dict]]:
        """Convert Successor's OpenAI-style messages to Anthropic format.

        Returns (system_text, anthropic_messages) where system_text is
        the concatenation of all system messages (Anthropic puts system
        at the top level, not in the messages array).
        """
        system_parts: list[str] = []
        anthropic_msgs: list[dict] = []

        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "") or ""

            if role == "system":
                system_parts.append(content)
                continue

            if role == "assistant":
                # Check for tool_calls in OpenAI format
                tool_calls = m.get("tool_calls")
                if tool_calls:
                    blocks: list[dict] = []
                    # If there's text content alongside tool calls
                    if content:
                        blocks.append({"type": "text", "text": content})
                    for tc in tool_calls:
                        fn = tc.get("function") or {}
                        raw_args = fn.get("arguments", "{}")
                        if isinstance(raw_args, str):
                            try:
                                parsed_args = json.loads(raw_args)
                            except json.JSONDecodeError:
                                parsed_args = {}
                        else:
                            parsed_args = raw_args
                        blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "input": parsed_args,
                        })
                    anthropic_msgs.append({"role": "assistant", "content": blocks})
                    continue
                anthropic_msgs.append({"role": "assistant", "content": content})
                continue

            if role == "tool":
                # Tool result — convert to Anthropic tool_result content block
                tool_call_id = m.get("tool_call_id", "")
                name = m.get("name", "")
                anthropic_msgs.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_call_id,
                        "content": content,
                    }],
                })
                continue

            # user messages — pass through as-is (string content)
            anthropic_msgs.append({"role": role, "content": content})

        return "\n\n".join(system_parts), anthropic_msgs

    @staticmethod
    def _convert_tools(tools: list[dict]) -> list[dict]:
        """Convert OpenAI-format tool definitions to Anthropic format.

        OpenAI: {"type": "function", "function": {"name": ..., "parameters": ...}}
        Anthropic: {"name": ..., "description": ..., "input_schema": ...}
        """
        anthropic_tools: list[dict] = []
        for t in tools:
            if t.get("type") == "function":
                fn = t.get("function") or {}
                at: dict = {
                    "name": fn.get("name", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                }
                if fn.get("description"):
                    at["description"] = fn["description"]
                anthropic_tools.append(at)
            else:
                # Pass through as-is for non-function tools
                anthropic_tools.append(t)
        return anthropic_tools


# ─── Anthropic SSE stream ───


class _AnthropicChatStream(ChatStream):
    """ChatStream variant that speaks Anthropic's SSE protocol.

    Overrides _run to inject Anthropic-specific headers and _read_sse
    to parse named-event SSE instead of the OpenAI unnamed format.
    """

    def __init__(
        self,
        *,
        url: str,
        body: dict,
        timeout: float,
        connect_timeout: float,
        api_key: str,
    ) -> None:
        self._api_key = api_key
        # Must call super().__init__ LAST because it starts the thread
        super().__init__(
            url=url,
            body=body,
            timeout=timeout,
            connect_timeout=connect_timeout,
        )

    def _run(self, url: str, body: dict, timeout: float, connect_timeout: float) -> None:
        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            socket_timeout = max(timeout, connect_timeout)
            with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
                self._queue.put(StreamStarted())
                self._read_anthropic_sse(resp, timeout)
        except urllib.error.HTTPError as e:
            # Try to read the error body for a more helpful message
            error_detail = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")
                error_json = json.loads(error_body)
                error_detail = error_json.get("error", {}).get("message", "")
            except Exception:
                pass
            msg = f"HTTP {e.code}: {e.reason}"
            if error_detail:
                msg = f"{msg} — {error_detail}"
            self._emit_error(msg)
        except urllib.error.URLError as e:
            self._emit_error(f"connection failed: {e.reason}")
        except TimeoutError:
            self._emit_error("connection timed out")
        except Exception as e:
            self._emit_error(f"{type(e).__name__}: {e}")
        finally:
            self._done.set()

    def _read_anthropic_sse(self, resp, timeout: float) -> None:
        """Parse the Anthropic SSE stream.

        Anthropic's SSE format uses named events:
          event: message_start
          data: {"type":"message_start","message":{...}}

          event: content_block_start
          data: {"type":"content_block_start","index":0,"content_block":{...}}

          event: content_block_delta
          data: {"type":"content_block_delta","index":0,"delta":{...}}

          event: content_block_stop
          data: {"type":"content_block_stop","index":0}

          event: message_delta
          data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},...}

          event: message_stop
          data: {"type":"message_stop"}

        Tool calls arrive as content blocks with type "tool_use" and
        incremental input_json_delta fragments.
        """
        finish_reason = "stop"
        usage: dict | None = None
        # Tool call accumulators keyed by block index.
        # Each: {"id": str, "name": str, "args_buf": [str, ...]}
        pending_tool_calls: dict[int, dict] = {}

        deadline = time.monotonic() + timeout
        current_event = ""
        # Track the type of the current content block (text vs tool_use)
        active_blocks: dict[int, str] = {}

        while True:
            if self._stop.is_set():
                self._emit_end(
                    finish_reason="cancelled",
                    finish_reason_reported=True,
                    usage=usage,
                    timings=None,
                    tool_calls=self._finalize_tool_calls(pending_tool_calls),
                )
                return
            if time.monotonic() > deadline:
                self._emit_error("stream timed out")
                return

            line = resp.readline()
            if not line:
                break

            line = line.strip()
            if not line:
                continue

            # Parse event: name lines
            if line.startswith(b"event:"):
                current_event = line[6:].strip().decode("utf-8", errors="replace")
                continue

            # Parse data: payload lines
            if not line.startswith(b"data:"):
                continue

            payload = line[5:].lstrip()
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue

            event_type = chunk.get("type", current_event)

            if event_type == "message_start":
                # Extract any metadata from the message object
                pass

            elif event_type == "content_block_start":
                idx = chunk.get("index", 0)
                block = chunk.get("content_block") or {}
                block_type = block.get("type", "text")
                active_blocks[idx] = block_type
                if block_type == "tool_use":
                    pending_tool_calls[idx] = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "args_buf": [],
                    }

            elif event_type == "content_block_delta":
                idx = chunk.get("index", 0)
                delta = chunk.get("delta") or {}

                # Text delta
                if delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    if text:
                        with self._content_lock:
                            self._content_buf.append(text)
                        self._queue.put(ContentChunk(text))

                # Thinking/reasoning delta (some Anthropic models)
                elif delta.get("type") == "thinking_delta":
                    text = delta.get("thinking", "")
                    if text:
                        with self._reasoning_lock:
                            self._reasoning_buf.append(text)
                        self._queue.put(ReasoningChunk(text))

                # Tool input JSON delta
                elif delta.get("type") == "input_json_delta":
                    partial = delta.get("partial_json", "")
                    if idx in pending_tool_calls:
                        pending_tool_calls[idx]["args_buf"].append(partial)
                    self._publish_tool_calls_snapshot(pending_tool_calls)

            elif event_type == "content_block_stop":
                idx = chunk.get("index", 0)
                # Tool call block is complete — snapshot already updated

            elif event_type == "message_delta":
                delta = chunk.get("delta") or {}
                sr = delta.get("stop_reason")
                if sr:
                    # Map Anthropic stop reasons to OpenAI equivalents
                    reason_map = {
                        "end_turn": "stop",
                        "tool_use": "tool_calls",
                        "max_tokens": "length",
                        "stop_sequence": "stop",
                        "model_context_window_exceeded": "length",
                    }
                    finish_reason = reason_map.get(sr, sr)
                usage_delta = chunk.get("usage")
                if usage_delta:
                    usage = usage_delta

            elif event_type == "message_stop":
                break

            elif event_type == "ping":
                pass

            elif event_type == "error":
                error_data = chunk.get("error") or {}
                self._emit_error(error_data.get("message", "unknown error"))
                return

        self._emit_end(
            finish_reason=finish_reason,
            finish_reason_reported=True,
            usage=usage,
            timings=None,
            tool_calls=self._finalize_tool_calls(pending_tool_calls),
        )

    # Reuse the parent class's _emit_end, _emit_error, _finalize_tool_calls,
    # and _publish_tool_calls_snapshot — they operate on the same instance
    # state (queue, buffers, locks).
