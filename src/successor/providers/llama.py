"""llama.cpp streaming client (pure stdlib).

llama.cpp ships an OpenAI-compatible HTTP API at /v1/chat/completions.
This module wraps it as a streaming client that pushes typed events
onto a thread-safe queue, so the renderer's main thread can poll for
new content each frame without blocking.

The client is **pure stdlib** (urllib + threading + queue + json),
zero deps. The HTTP request runs on a worker thread; the chat App's
main thread polls `ChatStream.poll()` each tick.

Two output channels per stream:
  - reasoning_content    the model's internal thinking (Qwen3.5 thinking
                         models emit this; can be hundreds of deltas
                         before any user-visible content begins)
  - content              the actual user-visible answer

Events emitted:
  StreamStarted()                       — first delta arrived
  ReasoningChunk(text)                  — a piece of reasoning_content
  ContentChunk(text)                    — a piece of content
  StreamEnded(usage, finish_reason, …)  — stream finished cleanly
  StreamError(message)                  — stream failed (network/HTTP)

Use:
    client = LlamaCppClient()
    stream = client.stream_chat(messages=[...], max_tokens=32768)
    while not stream.done:
        for event in stream.drain():
            handle(event)
        time.sleep(0.01)

Or, plumbed into Successor's chat App: each `on_tick` calls `stream.drain()`
and feeds events into the renderer state.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable


# `0` means "auto-budget against the detected local context window".
# This keeps local llama.cpp sessions from inheriting an arbitrary old
# generation ceiling when the server was launched with a much larger `-c`.
AUTO_MAX_TOKENS = 0
AUTO_MAX_TOKENS_FALLBACK = 262_144


# ─── Event types ───


@dataclass(slots=True, frozen=True)
class StreamStarted:
    """Emitted as soon as the first delta arrives."""
    pass


@dataclass(slots=True, frozen=True)
class ReasoningChunk:
    """A delta of `reasoning_content` from a thinking model."""
    text: str


@dataclass(slots=True, frozen=True)
class ContentChunk:
    """A delta of user-visible `content`."""
    text: str


@dataclass(slots=True, frozen=True)
class StreamEnded:
    """The stream finished cleanly. Includes usage info if the server
    returned any.

    `tool_calls` is the structured list of native tool calls the model
    emitted during this stream, accumulated from `delta.tool_calls`
    chunks. Each entry is a dict shaped:
        {"id": str, "name": str, "arguments": dict, "raw_arguments": str}
    where `arguments` is the parsed JSON object and `raw_arguments`
    is the unparsed source string (kept for diagnostics + retries).
    Empty list when the model emitted text-only.
    """
    finish_reason: str
    finish_reason_reported: bool = True
    usage: dict | None = None
    timings: dict | None = None
    full_reasoning: str = ""
    full_content: str = ""
    tool_calls: tuple = ()


@dataclass(slots=True, frozen=True)
class StreamError:
    """The stream failed. The accumulated text so far is included so
    the chat App can decide whether to commit a partial response."""
    message: str
    full_reasoning: str = ""
    full_content: str = ""


StreamEvent = StreamStarted | ReasoningChunk | ContentChunk | StreamEnded | StreamError


@dataclass(slots=True, frozen=True)
class LlamaCppRuntimeCapabilities:
    """Runtime capabilities surfaced by llama.cpp's /props endpoint."""

    context_window: int | None = None
    total_slots: int | None = None
    endpoint_slots: bool = False
    supports_parallel_tool_calls: bool = False
    supports_typed_content: bool = False
    supports_vision: bool = False

    @property
    def usable_background_slots(self) -> int:
        """Background lanes after reserving one slot for the parent chat."""
        if not self.endpoint_slots:
            return 1
        if not isinstance(self.total_slots, int) or self.total_slots < 2:
            return 1
        return max(1, self.total_slots - 1)


# ─── ChatStream — one in-flight request ───


class ChatStream:
    """A single in-flight chat completion stream.

    Lifecycle:
      __init__ launches a worker thread that POSTs to llama.cpp and
      parses SSE chunks. Events go into a thread-safe queue.
      The main thread calls `drain()` each frame to consume events.
      `done` becomes True when the worker thread exits.
      `close()` signals the worker to stop early.
    """

    def __init__(
        self,
        url: str,
        body: dict,
        timeout: float,
        connect_timeout: float,
    ) -> None:
        self._queue: queue.Queue[StreamEvent] = queue.Queue()
        self._stop = threading.Event()
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(url, body, timeout, connect_timeout),
            daemon=True,
            name="successor-llama-stream",
        )
        # Accumulators visible to the main thread for partial recovery.
        self._reasoning_buf: list[str] = []
        self._content_buf: list[str] = []
        self._reasoning_lock = threading.Lock()
        self._content_lock = threading.Lock()
        # Live tool-call accumulator snapshot. Updated by the worker
        # every time a delta.tool_calls chunk arrives, read by the
        # chat's tick loop to render a streaming preview card ("the
        # arguments are pouring in RIGHT NOW"). Each entry:
        #   {"index": int, "id": str, "name": str, "raw_arguments": str}
        # where raw_arguments is the concatenated text so far (not yet
        # JSON-parsed — we show it as a schizo-scroll tail).
        self._tool_calls_lock = threading.Lock()
        self._tool_calls_snapshot: list[dict] = []
        self._thread.start()

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def reasoning_so_far(self) -> str:
        with self._reasoning_lock:
            return "".join(self._reasoning_buf)

    @property
    def content_so_far(self) -> str:
        with self._content_lock:
            return "".join(self._content_buf)

    @property
    def tool_calls_so_far(self) -> list[dict]:
        """Thread-safe snapshot of the in-flight tool-call accumulators.

        Returns a list of dicts shaped
          `{"index", "id", "name", "raw_arguments"}`
        sorted by index. Empty until the model starts emitting
        `delta.tool_calls` chunks. The chat reads this every frame
        to paint a streaming preview card showing the arguments
        arriving live.
        """
        with self._tool_calls_lock:
            return [dict(tc) for tc in self._tool_calls_snapshot]

    def drain(self) -> list[StreamEvent]:
        """Pull all currently-available events. Non-blocking."""
        out: list[StreamEvent] = []
        try:
            while True:
                out.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        return out

    def close(self) -> None:
        """Signal the worker to stop. Returns immediately; the thread
        finishes when its current readline() returns."""
        self._stop.set()

    # ─── worker thread ───

    def _run(self, url: str, body: dict, timeout: float, connect_timeout: float) -> None:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                method="POST",
            )
            # NB: urllib's `timeout` parameter governs *all* socket I/O on
            # the connection, not just the initial connect. We use the
            # larger `timeout` value (the streaming deadline) so that
            # long prompt-processing windows on big contexts don't get
            # cut off by the connect deadline. The connect_timeout is
            # kept as a parameter for the future when we move to a real
            # HTTP client that separates connect from read timeouts.
            socket_timeout = max(timeout, connect_timeout)
            with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
                self._queue.put(StreamStarted())
                self._read_sse(resp, timeout)
        except urllib.error.HTTPError as e:
            self._emit_error(f"HTTP {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            self._emit_error(f"connection failed: {e.reason}")
        except TimeoutError:
            self._emit_error("connection timed out")
        except Exception as e:
            self._emit_error(f"{type(e).__name__}: {e}")
        finally:
            self._done.set()

    def _read_sse(self, resp, timeout: float) -> None:
        """Parse the SSE stream from llama.cpp.

        The protocol is:
          data: {...json...}\\n
          \\n              <- empty line separates events
          data: {...}\\n
          \\n
          data: [DONE]\\n

        Tool calls arrive incrementally via `delta.tool_calls`. The
        first chunk for a given index carries `id`, `type`, and
        `function.name`; subsequent chunks deliver `function.arguments`
        as text fragments that we concatenate. The final chunk has
        `finish_reason: "tool_calls"` instead of "stop".
        """
        finish_reason = "stop"
        finish_reason_reported = False
        usage: dict | None = None
        timings: dict | None = None
        # Tool call accumulators keyed by `index` from the streaming
        # protocol. Each entry holds the resolved id+name (set on the
        # first chunk that mentions them) and a list of argument-text
        # fragments that get concatenated and JSON-parsed at end.
        pending_tool_calls: dict[int, dict] = {}

        deadline = time.monotonic() + timeout

        while True:
            if self._stop.is_set():
                self._emit_end(
                    finish_reason="cancelled",
                    finish_reason_reported=True,
                    usage=usage,
                    timings=timings,
                    tool_calls=self._finalize_tool_calls(pending_tool_calls),
                )
                return
            if time.monotonic() > deadline:
                self._emit_error("stream timed out")
                return

            line = resp.readline()
            if not line:
                # EOF — stream ended
                break

            line = line.strip()
            if not line:
                continue

            if not line.startswith(b"data:"):
                continue

            payload = line[5:].lstrip()
            if payload == b"[DONE]":
                break

            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue

            choices = chunk.get("choices") or []
            if not choices:
                # llama.cpp sometimes sends a final chunk with usage but
                # no choices.
                if "usage" in chunk:
                    usage = chunk["usage"]
                if "timings" in chunk:
                    timings = chunk["timings"]
                continue

            choice = choices[0]
            delta = choice.get("delta") or {}

            rc = delta.get("reasoning_content")
            if rc:
                with self._reasoning_lock:
                    self._reasoning_buf.append(rc)
                self._queue.put(ReasoningChunk(rc))

            content = delta.get("content")
            if content:
                with self._content_lock:
                    self._content_buf.append(content)
                self._queue.put(ContentChunk(content))

            tool_calls_delta = delta.get("tool_calls")
            if tool_calls_delta:
                for tc in tool_calls_delta:
                    idx = tc.get("index", 0)
                    slot = pending_tool_calls.setdefault(
                        idx,
                        {"id": "", "name": "", "args_buf": []},
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if "arguments" in fn:
                        slot["args_buf"].append(fn["arguments"])
                # Publish a snapshot so the main thread's render can
                # show the arguments streaming in live — mirrors the
                # reasoning_so_far pattern the thinking spinner uses.
                self._publish_tool_calls_snapshot(pending_tool_calls)

            fr = choice.get("finish_reason")
            if fr:
                finish_reason = fr
                finish_reason_reported = True
                # Some servers include usage in the final chunk.
                if "usage" in chunk:
                    usage = chunk["usage"]
                if "timings" in chunk:
                    timings = chunk["timings"]
                # Don't break — keep reading until [DONE] or EOF.

        self._emit_end(
            finish_reason=finish_reason,
            finish_reason_reported=finish_reason_reported,
            usage=usage,
            timings=timings,
            tool_calls=self._finalize_tool_calls(pending_tool_calls),
        )

    def _publish_tool_calls_snapshot(self, pending: dict[int, dict]) -> None:
        """Copy the per-index accumulator into the thread-safe snapshot
        so the chat's main thread can read it via `tool_calls_so_far`
        without touching the worker-owned `pending_tool_calls` dict.
        Called every time a `delta.tool_calls` chunk arrives.
        """
        snapshot: list[dict] = []
        for idx in sorted(pending.keys()):
            slot = pending[idx]
            snapshot.append({
                "index": idx,
                "id": slot["id"],
                "name": slot["name"],
                "raw_arguments": "".join(slot["args_buf"]),
            })
        with self._tool_calls_lock:
            self._tool_calls_snapshot = snapshot

    @staticmethod
    def _finalize_tool_calls(pending: dict[int, dict]) -> tuple:
        """Convert the per-index accumulator into the final tuple of
        tool-call dicts that StreamEnded carries.

        Each entry is `{"id", "name", "arguments", "raw_arguments"}`.
        Argument JSON is parsed; on parse error, `arguments` is `{}`
        and `raw_arguments` keeps the raw text so the consumer can
        diagnose. Tuples are used so StreamEnded stays frozen.
        """
        out = []
        for idx in sorted(pending.keys()):
            slot = pending[idx]
            raw = "".join(slot["args_buf"])
            parse_error = ""
            parse_error_pos: int | None = None
            try:
                parsed = json.loads(raw) if raw else {}
                if not isinstance(parsed, dict):
                    parsed = {"_value": parsed}
            except json.JSONDecodeError as exc:
                parsed = {}
                parse_error = exc.msg
                parse_error_pos = exc.pos
            out.append({
                "id": slot["id"],
                "name": slot["name"],
                "arguments": parsed,
                "raw_arguments": raw,
                "arguments_parse_error": parse_error,
                "arguments_parse_error_pos": parse_error_pos,
            })
        return tuple(out)

    def _emit_end(
        self,
        *,
        finish_reason: str,
        finish_reason_reported: bool,
        usage: dict | None,
        timings: dict | None,
        tool_calls: tuple = (),
    ) -> None:
        self._queue.put(
            StreamEnded(
                finish_reason=finish_reason,
                finish_reason_reported=finish_reason_reported,
                usage=usage,
                timings=timings,
                full_reasoning=self.reasoning_so_far,
                full_content=self.content_so_far,
                tool_calls=tool_calls,
            )
        )

    def _emit_error(self, message: str) -> None:
        self._queue.put(
            StreamError(
                message=message,
                full_reasoning=self.reasoning_so_far,
                full_content=self.content_so_far,
            )
        )


# ─── LlamaCppClient — config + factory for streams ───


class LlamaCppClient:
    """Configured llama.cpp endpoint factory.

    Default base_url is the standard llama.cpp server port. Defaults
    are intentionally generous because local inference is free; the
    harness is built around mid-grade models running at large context
    windows (typically 32K-256K).

    Conforms structurally to `providers.base.ChatProvider`. The
    `provider_type` class attribute is used by `make_provider` to
    construct an instance from a profile's provider config dict.
    """

    provider_type = "llamacpp"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8080",
        model: str = "local",
        default_max_tokens: int = AUTO_MAX_TOKENS,
        default_temperature: float = 0.7,
        default_timeout: float = 600.0,
        connect_timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.default_max_tokens = default_max_tokens
        self.default_temperature = default_temperature
        self.default_timeout = default_timeout
        self.connect_timeout = connect_timeout

    def _api_root(self) -> str:
        """Return the base URL with `/v1` ensured exactly once.

        Handles both `http://localhost:8080` (llama.cpp default, no
        version path) and `http://localhost:8080/v1` (some setups
        prefix it for parity with hosted services). Avoids producing
        the dreaded `…/v1/v1/chat/completions` 404.
        """
        if self.base_url.endswith("/v1") or "/v1/" in self.base_url:
            return self.base_url
        return f"{self.base_url}/v1"

    def effective_max_tokens(self, requested_max_tokens: int | None = None) -> int:
        """Resolve the actual generation ceiling for this request."""
        resolved = (
            requested_max_tokens
            if requested_max_tokens is not None
            else self.default_max_tokens
        )
        if isinstance(resolved, int) and resolved > 0:
            return resolved
        detected = self.detect_context_window()
        if isinstance(detected, int) and detected > 0:
            return detected
        return AUTO_MAX_TOKENS_FALLBACK

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
        """Open a streaming chat completion.

        messages: iterable of message dicts. Standard fields:
            {"role": "system|user|assistant|tool", "content": str}
          Assistant messages may also carry `tool_calls` (list of
          tool-call dicts in OpenAI format). Tool messages may carry
          `tool_call_id` linking back to a previous assistant call.
          The serializer preserves all of these fields verbatim so
          Qwen's chat template can render them as `<tool_call>` and
          `<tool_response>` blocks.

        tools: optional list of tool definitions in OpenAI format.
          When set, the chat template's "tools" branch fires and
          the model is told what tools are available. Pass None
          for plain chat.

        extra:    optional dict merged into the request body for any
                  llama.cpp-specific knobs (top_p, top_k, repeat_penalty,
                  reasoning_effort, etc.)

        Returns a ChatStream that the caller polls via .drain().
        """
        # Preserve all known fields on each message — flattening to
        # role+content drops `tool_calls` and `tool_call_id`, which
        # breaks the native Qwen tool-call round trip.
        serialized_messages: list[dict] = []
        for m in messages:
            entry: dict = {"role": m["role"], "content": m.get("content", "") or ""}
            if "tool_calls" in m and m["tool_calls"]:
                entry["tool_calls"] = m["tool_calls"]
            if "tool_call_id" in m and m["tool_call_id"]:
                entry["tool_call_id"] = m["tool_call_id"]
            if "name" in m and m["name"]:
                entry["name"] = m["name"]
            serialized_messages.append(entry)

        body: dict = {
            "model": self.model,
            "messages": serialized_messages,
            "stream": True,
            "max_tokens": self.effective_max_tokens(max_tokens),
            "temperature": (
                temperature if temperature is not None else self.default_temperature
            ),
        }
        if tools:
            body["tools"] = tools
        if extra:
            body.update(extra)

        url = f"{self._api_root()}/chat/completions"
        return ChatStream(
            url=url,
            body=body,
            timeout=timeout if timeout is not None else self.default_timeout,
            connect_timeout=self.connect_timeout,
        )

    def health(self) -> bool:
        """Quick blocking health check. Returns True if the server is up."""
        try:
            req = urllib.request.Request(f"{self.base_url}/health")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status != 200:
                    return False
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                return str(data.get("status", "")).lower() == "ok"
        except Exception:
            return False

    def _server_root(self) -> str:
        """Return base_url with any trailing /v1 stripped.

        Used for llama.cpp-specific endpoints that live at the server
        root (like /props, /health, /tokenize) rather than under /v1.
        Mirrors the trailing-/v1 strip the token counter does.
        """
        root = self.base_url
        if root.endswith("/v1"):
            root = root[:-3]
        return root

    def _detect_props(self) -> dict | None:
        """Fetch and cache llama.cpp's /props payload."""
        if hasattr(self, "_cached_props"):
            return self._cached_props
        payload: dict | None = None
        try:
            req = urllib.request.Request(f"{self._server_root()}/props")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    if isinstance(data, dict):
                        payload = data
        except Exception:
            pass
        self._cached_props = payload
        return payload

    def detect_context_window(self) -> int | None:
        """Probe llama.cpp's /props endpoint and return n_ctx.

        llama.cpp exposes the launched server's context size at
        `GET /props` under `default_generation_settings.n_ctx`. This
        is the value the user passed to `-c` when starting llama-server,
        which is the actual ceiling for any conversation against this
        endpoint.

        Result is cached on the instance after the first successful
        probe so the chat doesn't pay the round-trip more than once.
        Returns None if the server is unreachable, the response shape
        is unexpected, or n_ctx is missing — callers fall back to
        the profile override or the hardcoded default.
        """
        props = self._detect_props() or {}
        settings = props.get("default_generation_settings") or {}
        n_ctx = settings.get("n_ctx")
        if isinstance(n_ctx, int) and n_ctx > 0:
            return n_ctx
        return None

    def detect_runtime_capabilities(self) -> LlamaCppRuntimeCapabilities:
        """Return cached llama.cpp runtime capabilities from /props."""
        props = self._detect_props() or {}
        settings = props.get("default_generation_settings") or {}
        chat_caps = props.get("chat_template_caps") or {}

        context_window: int | None = None
        n_ctx = settings.get("n_ctx")
        if isinstance(n_ctx, int) and n_ctx > 0:
            context_window = n_ctx

        total_slots: int | None = None
        raw_total_slots = props.get("total_slots")
        if isinstance(raw_total_slots, int) and raw_total_slots > 0:
            total_slots = raw_total_slots

        return LlamaCppRuntimeCapabilities(
            context_window=context_window,
            total_slots=total_slots,
            endpoint_slots=bool(props.get("endpoint_slots", False)),
            supports_parallel_tool_calls=bool(
                chat_caps.get("supports_parallel_tool_calls", False)
            ),
            supports_typed_content=bool(
                chat_caps.get("supports_typed_content", False)
            ),
            supports_vision=bool(
                (props.get("modalities") or {}).get("vision", False)
            ),
        )
