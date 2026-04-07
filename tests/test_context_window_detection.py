"""Tests for provider-driven context window detection.

Real upstream gap surfaced during OpenRouter testing: the chat was
hardcoding the context window to whatever the profile JSON said
(default 262_144), so a profile pointed at a 64K model would have
its compaction thresholds set against the wrong number — autocompact
never proactively fires, the user hits the model's real ceiling, and
gets cryptic 'context length exceeded' from the server.

The fix is to detect from the provider:
  - LlamaCppClient.detect_context_window() probes /props
  - OpenAICompatClient.detect_context_window() probes /v1/models

Both return None gracefully on failure (timeout, missing field,
unknown model). The chat consults them via _resolve_context_window()
with precedence: profile override → detected → CONTEXT_MAX default.

These tests fake the HTTP layer so they're hermetic — the live E2E
against OpenRouter is in scripts/, not pytest.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from successor.providers.llama import LlamaCppClient
from successor.providers.openai_compat import OpenAICompatClient


# ─── llama.cpp /props detection ───


class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def read(self) -> bytes:
        return self._payload


def _fake_urlopen(payload: bytes, status: int = 200):
    """Return a urlopen replacement that always responds with the payload."""
    def _open(req, timeout=None):
        return _FakeResponse(payload, status)
    return _open


def test_llama_detect_returns_n_ctx_from_props() -> None:
    client = LlamaCppClient(base_url="http://localhost:8080")
    payload = b'{"default_generation_settings": {"n_ctx": 262144}}'
    with patch(
        "successor.providers.llama.urllib.request.urlopen",
        _fake_urlopen(payload),
    ):
        assert client.detect_context_window() == 262144


def test_llama_detect_caches_result() -> None:
    client = LlamaCppClient(base_url="http://localhost:8080")
    payload = b'{"default_generation_settings": {"n_ctx": 32768}}'
    call_count = {"n": 0}

    def counting_open(req, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(payload)

    with patch(
        "successor.providers.llama.urllib.request.urlopen",
        counting_open,
    ):
        assert client.detect_context_window() == 32768
        assert client.detect_context_window() == 32768
        assert client.detect_context_window() == 32768
    assert call_count["n"] == 1


def test_llama_detect_returns_none_when_unreachable() -> None:
    client = LlamaCppClient(base_url="http://localhost:8080")

    def raise_open(req, timeout=None):
        raise OSError("connection refused")

    with patch(
        "successor.providers.llama.urllib.request.urlopen",
        raise_open,
    ):
        assert client.detect_context_window() is None


def test_llama_detect_returns_none_when_field_missing() -> None:
    """Some llama.cpp builds may not expose n_ctx in /props. Graceful None."""
    client = LlamaCppClient(base_url="http://localhost:8080")
    payload = b'{"default_generation_settings": {}}'
    with patch(
        "successor.providers.llama.urllib.request.urlopen",
        _fake_urlopen(payload),
    ):
        assert client.detect_context_window() is None


def test_llama_detect_strips_v1_from_base_url() -> None:
    """The /props endpoint lives at the server root, not under /v1.
    A user who configures base_url with /v1 (for parity with hosted
    services) should still get a successful detection."""
    client = LlamaCppClient(base_url="http://localhost:8080/v1")
    captured_url = {"u": ""}

    def capture_open(req, timeout=None):
        captured_url["u"] = req.full_url
        return _FakeResponse(b'{"default_generation_settings": {"n_ctx": 8192}}')

    with patch(
        "successor.providers.llama.urllib.request.urlopen",
        capture_open,
    ):
        assert client.detect_context_window() == 8192
    assert captured_url["u"] == "http://localhost:8080/props"


# ─── OpenAI-compat /v1/models detection ───


def test_openai_compat_detect_finds_model_context_length() -> None:
    """OpenRouter convention: per-model context_length at the top level."""
    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-oss-20b:free",
        api_key="sk-or-test",
    )
    payload = (
        b'{"data": ['
        b'  {"id": "other/model", "context_length": 8192},'
        b'  {"id": "openai/gpt-oss-20b:free", "context_length": 131072},'
        b'  {"id": "third/one", "context_length": 4096}'
        b']}'
    )
    with patch(
        "successor.providers.openai_compat.urllib.request.urlopen",
        _fake_urlopen(payload),
    ):
        assert client.detect_context_window() == 131072


def test_openai_compat_detect_falls_back_to_top_provider_field() -> None:
    """Some entries put context_length only under top_provider."""
    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        model="anthropic/claude-haiku-4-5",
    )
    payload = (
        b'{"data": [{'
        b'  "id": "anthropic/claude-haiku-4-5",'
        b'  "top_provider": {"context_length": 200000}'
        b'}]}'
    )
    with patch(
        "successor.providers.openai_compat.urllib.request.urlopen",
        _fake_urlopen(payload),
    ):
        assert client.detect_context_window() == 200000


def test_openai_compat_detect_returns_none_for_unknown_model() -> None:
    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        model="nonexistent/model",
    )
    payload = b'{"data": [{"id": "other/model", "context_length": 8192}]}'
    with patch(
        "successor.providers.openai_compat.urllib.request.urlopen",
        _fake_urlopen(payload),
    ):
        assert client.detect_context_window() is None


def test_openai_compat_falls_back_to_static_table_for_openai_models() -> None:
    """OpenAI's official endpoint doesn't expose context_length in /v1/models.
    The client should fall back to the hardcoded model→window table after
    the live probe returns no context_length."""
    client = OpenAICompatClient(
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
    )
    payload = b'{"data": [{"id": "gpt-4o-mini", "object": "model", "owned_by": "openai"}]}'
    with patch(
        "successor.providers.openai_compat.urllib.request.urlopen",
        _fake_urlopen(payload),
    ):
        assert client.detect_context_window() == 128_000


def test_openai_compat_static_table_prefix_matches_dated_suffix() -> None:
    """gpt-4o-2024-11-20 should resolve to the gpt-4o entry (128k) via
    prefix match, not 262144 fallback."""
    client = OpenAICompatClient(
        base_url="https://api.openai.com/v1",
        model="gpt-4o-2024-11-20",
    )
    payload = b'{"data": [{"id": "gpt-4o-2024-11-20", "object": "model"}]}'
    with patch(
        "successor.providers.openai_compat.urllib.request.urlopen",
        _fake_urlopen(payload),
    ):
        assert client.detect_context_window() == 128_000


def test_openai_compat_static_table_prioritizes_more_specific_prefix() -> None:
    """gpt-4-turbo (128k) must NOT be shadowed by gpt-4 (8k). The table
    is searched in declaration order, so the more specific entries
    must come first — this test locks that ordering in."""
    client = OpenAICompatClient(
        base_url="https://api.openai.com/v1",
        model="gpt-4-turbo",
    )
    payload = b'{"data": [{"id": "gpt-4-turbo", "object": "model"}]}'
    with patch(
        "successor.providers.openai_compat.urllib.request.urlopen",
        _fake_urlopen(payload),
    ):
        assert client.detect_context_window() == 128_000


def test_openai_compat_static_table_misses_unknown_model() -> None:
    """A non-OpenAI model name that's not in the table should return None
    so the chat falls through to the profile override or 262K default."""
    client = OpenAICompatClient(
        base_url="https://api.openai.com/v1",
        model="some/random-model",
    )
    payload = b'{"data": [{"id": "some/random-model", "object": "model"}]}'
    with patch(
        "successor.providers.openai_compat.urllib.request.urlopen",
        _fake_urlopen(payload),
    ):
        assert client.detect_context_window() is None


def test_openai_compat_live_context_length_wins_over_static_table() -> None:
    """If a hosted endpoint DOES expose context_length (e.g. OpenRouter
    proxying an OpenAI model), the live value wins over the table — the
    fallback only fires when the live probe came up empty."""
    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        model="gpt-4o-mini",
    )
    payload = b'{"data": [{"id": "gpt-4o-mini", "context_length": 999999}]}'
    with patch(
        "successor.providers.openai_compat.urllib.request.urlopen",
        _fake_urlopen(payload),
    ):
        assert client.detect_context_window() == 999999


def test_openai_compat_detect_caches_result() -> None:
    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-oss-20b:free",
    )
    payload = b'{"data": [{"id": "openai/gpt-oss-20b:free", "context_length": 131072}]}'
    call_count = {"n": 0}

    def counting_open(req, timeout=None):
        call_count["n"] += 1
        return _FakeResponse(payload)

    with patch(
        "successor.providers.openai_compat.urllib.request.urlopen",
        counting_open,
    ):
        assert client.detect_context_window() == 131072
        assert client.detect_context_window() == 131072
        assert client.detect_context_window() == 131072
    assert call_count["n"] == 1


# ─── Chat-level resolution precedence ───


def test_chat_resolve_window_uses_profile_override(temp_config_dir: Path) -> None:
    """Profile-supplied context_window wins over any auto-detect."""
    from successor.chat import SuccessorChat
    chat = SuccessorChat()
    chat.profile.provider["context_window"] = 50_000

    class _NeverDetect:
        def detect_context_window(self):  # pragma: no cover
            raise AssertionError("should not be called when override is set")

    chat.client = _NeverDetect()  # type: ignore[assignment]
    # Reset cached value because we just changed the profile after
    # the chat constructed itself.
    if hasattr(chat, "_cached_resolved_window"):
        delattr(chat, "_cached_resolved_window")
    assert chat._resolve_context_window() == 50_000


def test_chat_resolve_window_uses_provider_detection(temp_config_dir: Path) -> None:
    """No profile override → provider detection wins over the default."""
    from successor.chat import CONTEXT_MAX, SuccessorChat
    chat = SuccessorChat()
    chat.profile.provider.pop("context_window", None)

    class _Detect:
        def detect_context_window(self):
            return 65_536

    chat.client = _Detect()  # type: ignore[assignment]
    if hasattr(chat, "_cached_resolved_window"):
        delattr(chat, "_cached_resolved_window")
    assert chat._resolve_context_window() == 65_536
    assert 65_536 != CONTEXT_MAX  # sanity: detection != fallback


def test_chat_resolve_window_falls_back_to_default(temp_config_dir: Path) -> None:
    """No override AND no detection → CONTEXT_MAX (262_144)."""
    from successor.chat import CONTEXT_MAX, SuccessorChat
    chat = SuccessorChat()
    chat.profile.provider.pop("context_window", None)

    class _NoDetect:
        def detect_context_window(self):
            return None

    chat.client = _NoDetect()  # type: ignore[assignment]
    if hasattr(chat, "_cached_resolved_window"):
        delattr(chat, "_cached_resolved_window")
    assert chat._resolve_context_window() == CONTEXT_MAX


def test_chat_resolve_window_caches(temp_config_dir: Path) -> None:
    """Resolution is cached on the chat — repeated calls don't re-probe."""
    from successor.chat import SuccessorChat
    chat = SuccessorChat()
    chat.profile.provider.pop("context_window", None)
    call_count = {"n": 0}

    class _CountDetect:
        def detect_context_window(self):
            call_count["n"] += 1
            return 8192

    chat.client = _CountDetect()  # type: ignore[assignment]
    if hasattr(chat, "_cached_resolved_window"):
        delattr(chat, "_cached_resolved_window")
    for _ in range(10):
        assert chat._resolve_context_window() == 8192
    assert call_count["n"] == 1


def test_chat_agent_budget_uses_resolved_window(temp_config_dir: Path) -> None:
    """ContextBudget construction reflects the resolved window, not the
    hardcoded default."""
    from successor.chat import SuccessorChat
    chat = SuccessorChat()
    chat.profile.provider.pop("context_window", None)

    class _Detect:
        def detect_context_window(self):
            return 100_000

    chat.client = _Detect()  # type: ignore[assignment]
    if hasattr(chat, "_cached_resolved_window"):
        delattr(chat, "_cached_resolved_window")
    budget = chat._agent_budget()
    assert budget.window == 100_000
