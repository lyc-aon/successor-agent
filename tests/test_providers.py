"""Tests for the ChatProvider protocol and the make_provider factory.

Covers:
  1. Both LlamaCppClient and OpenAICompatClient conform structurally
     to the ChatProvider protocol.
  2. make_provider correctly dispatches by type, including aliases.
  3. Key translation (max_tokens → default_max_tokens etc.).
  4. Forward-compat: unknown keys are silently dropped.
  5. Error paths: missing type, unknown type, wrong type for type field.
  6. The constructed provider has the expected base_url, model, and
     default fields populated from the config dict.

These tests don't actually open network connections — they verify
construction shape only. Real-network integration tests would belong
to a separate file that's gated on the local llama.cpp server being up.
"""

from __future__ import annotations

import pytest

from successor.providers import (
    ChatProvider,
    LlamaCppClient,
    OpenAICompatClient,
    PROVIDER_REGISTRY,
    make_provider,
)
from successor.providers import llama as llama_module


# ─── Protocol conformance ───


def test_llamacpp_conforms_to_chat_provider() -> None:
    """LlamaCppClient is a ChatProvider by structural typing."""
    client = LlamaCppClient()
    assert isinstance(client, ChatProvider)


def test_openai_compat_conforms_to_chat_provider() -> None:
    client = OpenAICompatClient()
    assert isinstance(client, ChatProvider)


def test_llamacpp_has_required_attributes() -> None:
    client = LlamaCppClient(base_url="http://example.com:8080", model="test")
    assert client.base_url == "http://example.com:8080"
    assert client.model == "test"
    assert hasattr(client, "stream_chat")
    assert callable(client.stream_chat)
    assert hasattr(client, "health")
    assert callable(client.health)


def test_openai_compat_has_required_attributes() -> None:
    client = OpenAICompatClient(
        base_url="http://example.com:1234",
        model="gpt-4",
        api_key="sk-test",
    )
    assert client.base_url == "http://example.com:1234"
    assert client.model == "gpt-4"
    assert client.api_key == "sk-test"
    assert callable(client.stream_chat)
    assert callable(client.health)


def test_base_url_trailing_slash_stripped() -> None:
    """Trailing slashes are normalized away so URL building works."""
    a = LlamaCppClient(base_url="http://localhost:8080/")
    b = LlamaCppClient(base_url="http://localhost:8080")
    assert a.base_url == b.base_url == "http://localhost:8080"


# ─── make_provider factory ───


def test_factory_dispatches_llamacpp() -> None:
    config = {
        "type": "llamacpp",
        "base_url": "http://localhost:8080",
        "model": "local",
    }
    provider = make_provider(config)
    assert isinstance(provider, LlamaCppClient)
    assert provider.base_url == "http://localhost:8080"
    assert provider.model == "local"


def test_factory_dispatches_openai_compat() -> None:
    config = {
        "type": "openai_compat",
        "base_url": "http://localhost:1234",
        "model": "local-model",
        "api_key": "sk-test",
    }
    provider = make_provider(config)
    assert isinstance(provider, OpenAICompatClient)
    assert provider.base_url == "http://localhost:1234"
    assert provider.api_key == "sk-test"


def test_factory_aliases_work() -> None:
    """The aliases ('llama', 'openai-compat', etc.) all resolve."""
    a = make_provider({"type": "llama"})
    assert isinstance(a, LlamaCppClient)

    b = make_provider({"type": "llama.cpp"})
    assert isinstance(b, LlamaCppClient)

    c = make_provider({"type": "openai"})
    assert isinstance(c, OpenAICompatClient)

    d = make_provider({"type": "openai-compat"})
    assert isinstance(d, OpenAICompatClient)


def test_factory_type_is_case_insensitive() -> None:
    p = make_provider({"type": "LLAMACPP"})
    assert isinstance(p, LlamaCppClient)


def test_factory_translates_max_tokens_key() -> None:
    """max_tokens (JSON-friendly) → default_max_tokens (constructor)."""
    provider = make_provider({
        "type": "llamacpp",
        "max_tokens": 65536,
    })
    assert provider.default_max_tokens == 65536


def test_llamacpp_auto_max_tokens_uses_detected_context_window(monkeypatch) -> None:
    client = LlamaCppClient(default_max_tokens=0)
    monkeypatch.setattr(client, "detect_context_window", lambda: 131072)
    assert client.effective_max_tokens() == 131072


def test_llamacpp_auto_max_tokens_falls_back_when_detection_misses(monkeypatch) -> None:
    client = LlamaCppClient(default_max_tokens=0)
    monkeypatch.setattr(client, "detect_context_window", lambda: None)
    assert client.effective_max_tokens() == 262144


def test_factory_translates_temperature_key() -> None:
    provider = make_provider({
        "type": "llamacpp",
        "temperature": 0.3,
    })
    assert provider.default_temperature == 0.3


def test_factory_translates_timeout_key() -> None:
    provider = make_provider({
        "type": "llamacpp",
        "timeout": 1200.0,
    })
    assert provider.default_timeout == 1200.0


def test_factory_drops_unknown_keys() -> None:
    """Forward-compat: profiles can carry extra fields without breaking."""
    provider = make_provider({
        "type": "llamacpp",
        "model": "local",
        "future_field": "we don't know what this is yet",
        "another_one": 42,
    })
    # Construction succeeded; the unknown keys were silently ignored.
    assert provider.model == "local"


def test_llamacpp_stream_chat_enables_prompt_cache_by_default(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeChatStream:
        def __init__(self, *, url, body, timeout, connect_timeout) -> None:
            captured["url"] = url
            captured["body"] = body
            captured["timeout"] = timeout
            captured["connect_timeout"] = connect_timeout

    monkeypatch.setattr(llama_module, "ChatStream", _FakeChatStream)

    client = LlamaCppClient(base_url="http://localhost:8080", model="local")
    client.stream_chat(messages=[{"role": "user", "content": "hello"}])

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["cache_prompt"] is True
    assert "id_slot" not in body


def test_llamacpp_stream_chat_includes_preferred_slot_id(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeChatStream:
        def __init__(self, *, url, body, timeout, connect_timeout) -> None:
            captured["body"] = body

    monkeypatch.setattr(llama_module, "ChatStream", _FakeChatStream)

    client = LlamaCppClient(base_url="http://localhost:8080", model="local")
    client.preferred_slot_id = 2
    client.stream_chat(messages=[{"role": "user", "content": "hello"}])

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["cache_prompt"] is True
    assert body["id_slot"] == 2


# ─── Error paths ───


def test_factory_missing_type_raises() -> None:
    with pytest.raises(ValueError, match="missing required 'type'"):
        make_provider({"model": "local"})


def test_factory_unknown_type_raises() -> None:
    with pytest.raises(ValueError, match="unknown provider type"):
        make_provider({"type": "made_up_provider"})


def test_factory_unknown_type_lists_available() -> None:
    """The error message lists every registered provider type."""
    with pytest.raises(ValueError) as excinfo:
        make_provider({"type": "nope"})
    msg = str(excinfo.value)
    assert "llamacpp" in msg
    assert "openai_compat" in msg


def test_factory_non_string_type_raises() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        make_provider({"type": 42})


# ─── Registry contents ───


def test_registry_contains_canonical_names() -> None:
    assert "llamacpp" in PROVIDER_REGISTRY
    assert "openai_compat" in PROVIDER_REGISTRY


def test_registry_canonical_names_match_provider_type_attribute() -> None:
    """Each registered constructor's `provider_type` matches its key."""
    assert PROVIDER_REGISTRY["llamacpp"].provider_type == "llamacpp"
    assert PROVIDER_REGISTRY["openai_compat"].provider_type == "openai_compat"
