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
    returned any."""
    finish_reason: str
    usage: dict | None = None
    timings: dict | None = None
    full_reasoning: str = ""
    full_content: str = ""


@dataclass(slots=True, frozen=True)
class StreamError:
    """The stream failed. The accumulated text so far is included so
    the chat App can decide whether to commit a partial response."""
    message: str
    full_reasoning: str = ""
    full_content: str = ""


StreamEvent = StreamStarted | ReasoningChunk | ContentChunk | StreamEnded | StreamError


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
            with urllib.request.urlopen(req, timeout=connect_timeout) as resp:
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
        """
        finish_reason = "stop"
        usage: dict | None = None
        timings: dict | None = None

        deadline = time.monotonic() + timeout

        while True:
            if self._stop.is_set():
                self._emit_end(finish_reason="cancelled", usage=usage, timings=timings)
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

            fr = choice.get("finish_reason")
            if fr:
                finish_reason = fr
                # Some servers include usage in the final chunk.
                if "usage" in chunk:
                    usage = chunk["usage"]
                if "timings" in chunk:
                    timings = chunk["timings"]
                # Don't break — keep reading until [DONE] or EOF.

        self._emit_end(finish_reason=finish_reason, usage=usage, timings=timings)

    def _emit_end(
        self,
        *,
        finish_reason: str,
        usage: dict | None,
        timings: dict | None,
    ) -> None:
        self._queue.put(
            StreamEnded(
                finish_reason=finish_reason,
                usage=usage,
                timings=timings,
                full_reasoning=self.reasoning_so_far,
                full_content=self.content_so_far,
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
    are intentionally generous because Lycaon's local Qwen3.5 setup
    runs at 256K context — never apologize for token cost on local
    inference.

    Conforms structurally to `providers.base.ChatProvider`. The
    `provider_type` class attribute is used by `make_provider` to
    construct an instance from a profile's provider config dict.
    """

    provider_type = "llamacpp"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8080",
        model: str = "qwopus",
        default_max_tokens: int = 32768,
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

    def stream_chat(
        self,
        messages: Iterable[dict],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
        extra: dict | None = None,
    ) -> ChatStream:
        """Open a streaming chat completion.

        messages: iterable of {"role": "system|user|assistant", "content": str}
        extra:    optional dict merged into the request body for any
                  llama.cpp-specific knobs (top_p, top_k, repeat_penalty,
                  reasoning_effort, etc.)

        Returns a ChatStream that the caller polls via .drain().
        """
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": m["role"], "content": m["content"]} for m in messages
            ],
            "stream": True,
            "max_tokens": max_tokens if max_tokens is not None else self.default_max_tokens,
            "temperature": (
                temperature if temperature is not None else self.default_temperature
            ),
        }
        if extra:
            body.update(extra)

        url = f"{self.base_url}/v1/chat/completions"
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
