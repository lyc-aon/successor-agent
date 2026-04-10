"""ChatProvider protocol — the seam between Successor and any model backend.

Every provider returns the SAME stream type (the typed events from
`llama.py`: `StreamStarted`, `ReasoningChunk`, `ContentChunk`,
`StreamEnded`, `StreamError`) so the chat App's `_pump_stream` doesn't
care which backend is producing them. Adding a new provider is "implement
the protocol with these three methods + four attributes."

The protocol is structural, not nominal — any class that exposes
`base_url`, `model`, `stream_chat()`, and `health()` is a ChatProvider
by Python's typing rules. `LlamaCppClient` already conforms without
inheriting from anything.

This keeps phase 2 a pure refactor: nothing changes for callers,
nothing changes for the existing client, but a factory function
becomes available that profiles will use in phase 3.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

# Re-export the existing event types so callers can `from
# successor.providers import ChatStream, StreamEnded, ...` regardless of
# which provider produced the stream. The actual ChatStream class
# lives in llama.py because that's where it was first written; future
# providers can either reuse it (most will) or implement a duck-typed
# equivalent that exposes `done`, `drain()`, `close()`, and the
# `reasoning_so_far` / `content_so_far` accumulators.
from .llama import (
    ChatStream,
    ContentChunk,
    ReasoningChunk,
    StreamEnded,
    StreamError,
    StreamEvent,
    StreamStarted,
)

__all__ = [
    "ChatProvider",
    "ChatStream",
    "ContentChunk",
    "ReasoningChunk",
    "StreamEnded",
    "StreamError",
    "StreamEvent",
    "StreamStarted",
]


@runtime_checkable
class ChatProvider(Protocol):
    """The shape every chat backend must satisfy.

    Four attributes + two methods. Anything more is provider-specific
    and lives on the concrete class. The chat App only ever touches
    this surface.

    Attributes:
        base_url:  the network endpoint root (e.g. http://localhost:8080)
        model:     the model identifier shown to the user in the ctx bar
                   (no semantic meaning beyond display)

    Methods:
        stream_chat(messages, **kwargs) -> ChatStream
            Open a streaming completion. Returns a ChatStream that the
            caller polls via .drain() each frame. Must run the actual
            HTTP work on a worker thread; the main thread polls.

        health() -> bool
            Quick blocking liveness probe. Used at startup to decide
            whether to greet with "the forge is hot" or "the forge is
            cold." Should return False quickly on connection failure
            rather than blocking — the chat startup blocks on this call.
    """

    base_url: str
    model: str

    def stream_chat(
        self,
        messages: Iterable[dict],
        *,
        max_tokens: int | None = ...,
        temperature: float | None = ...,
        timeout: float | None = ...,
        extra: dict | None = ...,
        tools: list[dict] | None = ...,
    ) -> ChatStream:
        ...

    def health(self) -> bool:
        ...
