"""Provider registry — Successor's chat backend abstraction.

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
  AnthropicClient  — Anthropic Messages API client (api.anthropic.com,
                      z.ai, and other Anthropic-protocol endpoints)
  make_provider    — factory: build a provider from a JSON-style dict
  PROVIDER_REGISTRY — type name → constructor mapping
  PROVIDER_PRESETS  — friendly service name → provider config templates
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
from .anthropic import AnthropicClient
from .factory import PROVIDER_REGISTRY, make_provider
from .llama import LlamaCppClient
from .openai_compat import OpenAICompatClient
from .presets import PROVIDER_PRESETS

__all__ = [
    "AnthropicClient",
    "ChatProvider",
    "ChatStream",
    "ContentChunk",
    "LlamaCppClient",
    "OpenAICompatClient",
    "PROVIDER_PRESETS",
    "PROVIDER_REGISTRY",
    "ReasoningChunk",
    "StreamEnded",
    "StreamError",
    "StreamEvent",
    "StreamStarted",
    "make_provider",
]
