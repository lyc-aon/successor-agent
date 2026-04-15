"""Provider factory — config dict to ChatProvider instance.

Profiles store provider configuration as a JSON dict like:

    {
      "type": "llamacpp",
      "base_url": "http://localhost:8080",
      "model": "local",
      "max_tokens": 32768,
      "temperature": 0.7
    }

`make_provider(config)` reads that dict and returns the appropriate
ChatProvider instance. The `type` field is the only required key —
everything else is provider-specific and gets passed through to the
constructor with a small renaming pass (e.g. `max_tokens` →
`default_max_tokens`).

This file is the place where new provider types get registered. Adding
a third backend is two lines: import the class, add an entry to
`PROVIDER_REGISTRY`. The factory itself doesn't need changes.
"""

from __future__ import annotations

from typing import Any, Callable

from .anthropic import AnthropicClient
from .llama import LlamaCppClient
from .openai_compat import OpenAICompatClient


# Mapping from `type` field to constructor. Constructors must accept
# only keyword arguments and tolerate unknown keys (we filter to the
# constructor's known parameters before calling).
ProviderFactory = Callable[..., Any]

PROVIDER_REGISTRY: dict[str, ProviderFactory] = {
    "llamacpp": LlamaCppClient,
    "openai_compat": OpenAICompatClient,
    "anthropic": AnthropicClient,
    # Aliases for ergonomics — the canonical names above are what
    # `provider_type` returns, but users will type whatever feels
    # right in their JSON files.
    "llama": LlamaCppClient,
    "llama.cpp": LlamaCppClient,
    "openai": OpenAICompatClient,
    "openai-compat": OpenAICompatClient,
    "claude": AnthropicClient,
    "kimi": OpenAICompatClient,
    "kimi-code": OpenAICompatClient,
}


# Mapping from JSON-friendly key names to constructor parameter names.
# Profiles use short, idiomatic keys (max_tokens); constructors use
# the more explicit default_max_tokens. The factory translates.
_KEY_TRANSLATIONS: dict[str, str] = {
    "max_tokens": "default_max_tokens",
    "temperature": "default_temperature",
    "timeout": "default_timeout",
}


def make_provider(config: dict[str, Any]) -> Any:
    """Construct a ChatProvider from a profile's provider config dict.

    Required key: "type" — must match a name in PROVIDER_REGISTRY.
    Other keys are passed to the constructor after key translation.
    Unknown keys are silently dropped (forward-compat with future
    provider parameters that this Successor version doesn't know about).

    Raises:
        ValueError: if `type` is missing or unknown
    """
    if "type" not in config:
        raise ValueError(
            "provider config missing required 'type' field. "
            f"available types: {sorted(set(PROVIDER_REGISTRY.keys()))}"
        )

    provider_type = config["type"]
    if not isinstance(provider_type, str):
        raise ValueError(f"provider 'type' must be a string, got {type(provider_type).__name__}")

    factory = PROVIDER_REGISTRY.get(provider_type.lower())
    if factory is None:
        available = sorted(set(PROVIDER_REGISTRY.keys()))
        raise ValueError(
            f"unknown provider type '{provider_type}'. "
            f"available: {', '.join(available)}"
        )

    # Build kwargs for the constructor by translating keys and dropping
    # the `type` field. We don't validate parameter names here — if a
    # user supplies an unknown key, the constructor will raise TypeError
    # which percolates up as a clear error.
    #
    # However, we DO filter to known constructor parameters because
    # forward-compat profiles may carry extra fields that this Successor
    # version's constructor doesn't understand. The intent is "old
    # Successor reads new profile, ignores unknown fields, runs anyway."
    import inspect

    valid_params = set(inspect.signature(factory).parameters.keys())
    kwargs: dict[str, Any] = {}
    for key, value in config.items():
        if key == "type":
            continue
        translated = _KEY_TRANSLATIONS.get(key, key)
        if translated in valid_params:
            kwargs[translated] = value
        # else silently drop — forward-compat

    return factory(**kwargs)
