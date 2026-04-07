"""OpenAI-API-compatible streaming client.

The OpenAI-compatible /v1/chat/completions surface is the lingua franca
of inference servers — LM Studio, Ollama (with the openai-compat
endpoint), vLLM, TGI, OpenRouter, Fireworks, Groq, and many more all
expose it. This client speaks that surface and emits the same typed
events the rest of Successor already consumes (StreamStarted /
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

    base_url accepts both conventions:
      - with `/v1` already included (the OpenAI SDK / LiteLLM / LM Studio /
        Ollama convention) e.g. `https://openrouter.ai/api/v1`
      - without `/v1` (llama.cpp convention) e.g. `http://localhost:1234`

    The client detects which form was passed and only appends `/v1` when
    it's missing. This makes the harness work with the most common
    hosted endpoints out of the box without users having to remember
    which form a given provider expects.

    Auth is optional — pass an api_key for hosted servers, leave it
    None for local servers that don't require it.

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

    def _api_root(self) -> str:
        """Return the base URL with `/v1` ensured exactly once.

        Handles both `https://openrouter.ai/api/v1` (already includes
        /v1) and `http://localhost:1234` (does not). Avoids producing
        the dreaded `…/v1/v1/chat/completions` 404.
        """
        if self.base_url.endswith("/v1") or "/v1/" in self.base_url:
            return self.base_url
        return f"{self.base_url}/v1"

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

        url = f"{self._api_root()}/chat/completions"

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
        url = f"{self._api_root()}/models"
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

    def detect_context_window(self) -> int | None:
        """Probe /v1/models and return the configured model's context length.

        OpenRouter (and any OpenAI-compat endpoint that follows the
        OpenRouter convention) exposes per-model `context_length` in
        the `/v1/models` listing — either at the top level or under
        `top_provider.context_length`. This method finds the row whose
        `id` matches `self.model` and returns whichever context_length
        field is present.

        Result is cached on the instance after the first probe so the
        chat doesn't pay the round-trip more than once. Returns None
        if the server is unreachable, the model isn't in the listing,
        or the listing has no `context_length` field (e.g. OpenAI's
        official endpoint, which doesn't expose it). Callers fall back
        to the profile override or the hardcoded default.
        """
        if hasattr(self, "_cached_context_window"):
            return self._cached_context_window
        result: int | None = None
        url = f"{self._api_root()}/models"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            req = urllib.request.Request(url, headers=headers)
            # Slightly longer timeout than /health because the listing
            # can be hundreds of KB on OpenRouter.
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    models = data.get("data") if isinstance(data, dict) else None
                    if isinstance(models, list):
                        for m in models:
                            if not isinstance(m, dict):
                                continue
                            if m.get("id") != self.model:
                                continue
                            # Top-level context_length is the OpenRouter
                            # convention; top_provider.context_length is
                            # the same number nested. Some providers
                            # only populate one or the other.
                            ctx = m.get("context_length")
                            if not isinstance(ctx, int) or ctx <= 0:
                                tp = m.get("top_provider") or {}
                                ctx = tp.get("context_length") if isinstance(tp, dict) else None
                            if isinstance(ctx, int) and ctx > 0:
                                result = ctx
                            break
        except Exception:
            pass
        self._cached_context_window = result
        return result


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
