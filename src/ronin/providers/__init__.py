"""Provider registry — Ronin's chat backend abstraction.

The chat App talks to a single ChatProvider object via a small,
duck-typed surface. Adding a new backend means writing a class that
satisfies the ChatProvider protocol and registering it in factory.py.

Public surface:

  ChatProvider     — the structural protocol every backend conforms to
  ChatStream       — typed event stream returned by stream_chat
  StreamStarted    \\
  ReasoningChunk    > the typed events the chat App consumes per-frame
  ContentChunk     |
  StreamEnded      |
  StreamError      /

  LlamaCppClient   — the original llama.cpp HTTP server client
  OpenAICompatClient — generic OpenAI-API-compatible client (LM Studio,
                       Ollama, vLLM, OpenRouter, hosted servers)
  make_provider    — factory: build a provider from a JSON-style dict
  PROVIDER_REGISTRY — type name → constructor mapping
"""

from .base import (
    ChatProvider,
    ChatStream,
    ContentChunk,
    ReasoningChunk,
    StreamEnded,
    StreamError,
    StreamEvent,
    StreamStarted,
)
from .factory import PROVIDER_REGISTRY, make_provider
from .llama import LlamaCppClient
from .openai_compat import OpenAICompatClient

__all__ = [
    "ChatProvider",
    "ChatStream",
    "ContentChunk",
    "LlamaCppClient",
    "OpenAICompatClient",
    "PROVIDER_REGISTRY",
    "ReasoningChunk",
    "StreamEnded",
    "StreamError",
    "StreamEvent",
    "StreamStarted",
    "make_provider",
]
