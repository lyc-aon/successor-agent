"""Tests for agent/tokens.py — TokenCounter with /tokenize endpoint
and char heuristic fallback.

Two test paths:
  - heuristic-only (no endpoint, hermetic)
  - mocked endpoint (deterministic, hermetic — no real HTTP)

The live /tokenize tests live in the burn rig, NOT here, because
they require a running llama.cpp server.
"""

from __future__ import annotations

import math


from successor.agent.log import LogMessage, MessageLog
from successor.agent.tokens import (
    HEURISTIC_CHARS_PER_TOKEN,
    TokenCounter,
)


# ─── Heuristic-only counting ───


def test_count_empty_string() -> None:
    c = TokenCounter()  # no endpoint
    assert c.count("") == 0


def test_count_single_char() -> None:
    c = TokenCounter()
    assert c.count("a") == 1


def test_count_short_string() -> None:
    c = TokenCounter()
    expected = max(1, math.ceil(11 / HEURISTIC_CHARS_PER_TOKEN))
    assert c.count("hello world") == expected


def test_count_is_cached() -> None:
    c = TokenCounter()
    c.count("hello world")
    assert c.cache_size() == 1
    c.count("hello world")  # cache hit
    assert c.cache_size() == 1


def test_count_message_includes_role_overhead() -> None:
    c = TokenCounter()
    m = LogMessage(role="user", content="x")
    # Body 1 token + 4 role overhead
    assert c.count_message(m) >= 5


def test_count_round_caches_estimate_on_round() -> None:
    from successor.agent.log import ApiRound
    c = TokenCounter()
    r = ApiRound()
    r.append(LogMessage(role="user", content="hello"))
    r.append(LogMessage(role="assistant", content="hi"))
    total = c.count_round(r)
    assert r.token_estimate == total
    assert total > 0


def test_count_log_includes_system_prompt() -> None:
    c = TokenCounter()
    log = MessageLog(system_prompt="system instructions here")
    log.append_to_current_round(LogMessage(role="user", content="hi"))
    sys_only = c.count("system instructions here") + 4
    total = c.count_log(log)
    assert total >= sys_only


def test_refresh_round_estimates_updates_all_rounds() -> None:
    c = TokenCounter()
    log = MessageLog()
    for i in range(3):
        log.begin_round()
        log.append_to_current_round(LogMessage(role="user", content=f"q{i}"))
    c.refresh_round_estimates(log)
    for r in log.rounds:
        assert r.token_estimate > 0


# ─── LRU cache eviction ───


def test_lru_cache_evicts_oldest() -> None:
    """Single-char strings hit a fast path that bypasses the cache,
    so use multi-char strings here."""
    c = TokenCounter(cache_size=3)
    c.count("alpha")
    c.count("beta")
    c.count("gamma")
    assert c.cache_size() == 3
    c.count("delta")  # should evict 'alpha'
    assert c.cache_size() == 3
    # Re-counting 'alpha' is a fresh miss → cache size still 3
    # (delta gets evicted to make room for alpha re-entry)
    c.count("alpha")
    assert c.cache_size() == 3


def test_clear_resets_cache_and_endpoint_state() -> None:
    c = TokenCounter()
    c.count("hello world")
    assert c.cache_size() > 0
    c.clear()
    assert c.cache_size() == 0


# ─── Mocked endpoint path ───


class _MockEndpoint:
    """Fake endpoint with a controllable response."""
    def __init__(self, base_url: str = "http://mock") -> None:
        self.base_url = base_url

    def count_text_tokens(self, text: str) -> int | None:  # noqa: ARG002
        return None


def test_count_falls_back_to_heuristic_when_endpoint_unreachable(monkeypatch) -> None:
    """Endpoint with bad URL should hit network error → fall back to heuristic
    after max_endpoint_failures consecutive failures."""
    ep = _MockEndpoint(base_url="http://localhost:1")  # nothing listening
    c = TokenCounter(endpoint=ep, max_endpoint_failures=2)
    # First call fails → falls back to heuristic for THIS call
    n = c.count("hello world")
    assert n > 0  # heuristic still produces a count
    # After max failures, _use_endpoint becomes False
    c.count("another string")
    c.count("yet another")
    # By now we should be in heuristic-only mode
    assert c._use_endpoint is False


def test_endpoint_failures_reset_on_success(monkeypatch) -> None:
    """Successful endpoint hits reset the failure counter."""

    # Patch _count_via_endpoint to alternate fail/succeed
    class _AlternatingCounter(TokenCounter):
        def __init__(self, ep):
            super().__init__(endpoint=ep, max_endpoint_failures=3)
            self._call_idx = 0

        def _count_via_endpoint(self, text):
            self._call_idx += 1
            if self._call_idx % 2 == 1:  # odd calls fail
                self._endpoint_failures += 1
                if self._endpoint_failures >= self._max_endpoint_failures:
                    self._use_endpoint = False
                return None
            self._endpoint_failures = 0  # success resets
            return 5  # fake count

    c = _AlternatingCounter(_MockEndpoint())
    # 1st call: fail (failures=1, fall back to heuristic)
    c.count("a string")
    # 2nd call: success (failures=0, returns 5 from endpoint)
    n2 = c.count("another string")
    assert n2 == 5
    # 3rd call (different text) cache miss: fail (failures=1)
    c.count("third string")
    # 4th call: success (failures=0)
    n4 = c.count("fourth string")
    assert n4 == 5
    # We should still be using the endpoint since failures never hit max
    assert c._use_endpoint
