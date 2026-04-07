"""OpenAI-API-compatible streaming client.

The OpenAI-compatible /v1/chat/completions surface is the lingua franca
of inference servers — LM Studio, Ollama (with the openai-compat
endpoint), vLLM, TGI, OpenRouter, Fireworks, Groq, and many more all
expose it. This client speaks that surface and emits the same typed
events the rest of Ronin already consumes (StreamStarted /
ReasoningChunk / ContentChunk / StreamEnded / StreamError).

Differences from the llama.cpp client:

  - Optional Authorization: Bearer <api_key> header. Local servers
    skip auth; hosted servers require it.
  - The /health endpoint is not part of the OpenAI spec, so we probe
    /v1/models instead — a successful 200 there means "the server is
    up and has at least one model loaded."
  - No dependency on llama.cpp-specific fields like reasoning_content
    in the schema, but we still parse them defensively if a server
    happens to emit them (e.g. Qwen3.5 served via vLLM does).

The class deliberately mirrors LlamaCppClient's constructor signature
so that the factory can swap between them with one config field flip.
Both return ChatStream instances backed by the same SSE worker thread.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterable

from .llama import ChatStream


class OpenAICompatClient:
    """Configured OpenAI-compatible endpoint factory.

    base_url is the server root (NOT including /v1). Auth is optional —
    pass an api_key for hosted servers, leave it None for local servers
    that don't require it.

    Conforms structurally to `providers.base.ChatProvider`.
    """

    provider_type = "openai_compat"

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:1234",
        model: str = "local-model",
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
                  provider-specific knobs (top_p, top_k, frequency_penalty,
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

        # ChatStream doesn't currently take an Authorization header;
        # for OpenAI-compat servers we extend it via a subclass that
        # injects the header before the urlopen call. Done this way
        # so the existing ChatStream worker thread logic stays
        # untouched and shared between providers.
        if self.api_key:
            return _AuthenticatedChatStream(
                url=url,
                body=body,
                timeout=timeout if timeout is not None else self.default_timeout,
                connect_timeout=self.connect_timeout,
                api_key=self.api_key,
            )

        return ChatStream(
            url=url,
            body=body,
            timeout=timeout if timeout is not None else self.default_timeout,
            connect_timeout=self.connect_timeout,
        )

    def health(self) -> bool:
        """Quick blocking health check via /v1/models.

        Returns True iff /v1/models responds 200 with a `data` array.
        Most OpenAI-compat servers expose this endpoint and require no
        auth for it (some require auth — passes the api_key if set).
        """
        url = f"{self.base_url}/v1/models"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status != 200:
                    return False
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                return isinstance(data, dict) and "data" in data
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
            return False


class _AuthenticatedChatStream(ChatStream):
    """ChatStream variant that injects an Authorization header.

    Subclass exists only because the base ChatStream constructs its
    urllib Request with hardcoded headers. Overriding the worker is
    cleaner than threading a header dict through the base class API
    when only one provider needs it.
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
        super().__init__(
            url=url,
            body=body,
            timeout=timeout,
            connect_timeout=connect_timeout,
        )

    def _run(
        self,
        url: str,
        body: dict,
        timeout: float,
        connect_timeout: float,
    ) -> None:
        # Identical to ChatStream._run except for the extra header.
        # Re-importing here to avoid a circular import at module load.
        from .llama import (
            StreamStarted,
        )

        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "Authorization": f"Bearer {self._api_key}",
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
