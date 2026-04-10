"""Token counting with provider-tokenizer support and heuristic fallback.

Preferred path: ask the provider for exact text-token counts when it
offers that capability (llama.cpp does via `/tokenize`).

Fallback path: if the provider cannot count text exactly, or the exact
path fails repeatedly, use a conservative chars/token heuristic. The
TokenCounter caches per-string counts in an LRU so the loop can ask
freely. Across model swaps, the cache should be cleared (`counter.clear()`).

The heuristic is calibrated against Qwen3.5's actual tokenization:
~3.5 chars/token for English code-mixed content. We use 3.5
conservatively to slightly OVERESTIMATE so the budget tracker
triggers compaction sooner rather than too late.
"""

from __future__ import annotations

import math
import urllib.error
from collections import OrderedDict
from typing import Protocol

from .log import ApiRound, LogMessage, MessageLog


# ─── Constants ───

# Conservative heuristic — Qwen3.5 averages ~3.7 chars/token on mixed
# English+code+JSON content (measured against the /tokenize endpoint).
# We use 3.5 to slightly overestimate so the budget tracker fires
# autocompact a touch early rather than late.
HEURISTIC_CHARS_PER_TOKEN: float = 3.5

# LRU cache size — number of distinct strings to remember.
# Tuned to comfortably hold a 200K-token conversation (~5000 distinct
# message bodies) without thrashing the eviction. Each entry is a
# small int + ref to the source string, so even at 16K entries the
# total memory is well under 1MB.
DEFAULT_CACHE_SIZE: int = 16_384

# ─── Provider protocol ───
#
# We don't depend directly on LlamaCppClient — accept anything that
# offers a compatible counting hook. Lets tests substitute a fake.


class TokenizerEndpoint(Protocol):
    """Optional provider-side tokenizer capability."""
    base_url: str

    def count_text_tokens(self, text: str) -> int | None:
        ...


# ─── The counter ───


class TokenCounter:
    """Counts tokens for strings, messages, rounds, and full message logs.

    Two paths:
      1. provider tokenizer when the provider exposes one (accurate)
      2. char heuristic when no exact tokenizer or tokenizer failure

    Per-string LRU cache so the loop can call this freely without
    re-paying the HTTP round-trip on every iteration.
    """

    __slots__ = (
        "endpoint", "_cache", "_cache_size", "_use_endpoint",
        "_endpoint_failures", "_max_endpoint_failures",
    )

    def __init__(
        self,
        endpoint: TokenizerEndpoint | None = None,
        *,
        cache_size: int = DEFAULT_CACHE_SIZE,
        max_endpoint_failures: int = 3,
    ) -> None:
        """
        endpoint:               provider-side tokenizer capability. Pass
                                None to force heuristic-only counting.
        cache_size:             LRU cache size for per-string counts.
        max_endpoint_failures:  after this many consecutive HTTP errors,
                                stop trying the endpoint and fall back
                                to heuristic permanently (until clear()).
        """
        self.endpoint = endpoint
        self._cache: OrderedDict[str, int] = OrderedDict()
        self._cache_size = cache_size
        self._use_endpoint = self._endpoint_available(endpoint)
        self._endpoint_failures = 0
        self._max_endpoint_failures = max_endpoint_failures

    # ─── Public counting API ───

    def count(self, text: str) -> int:
        """Count tokens for one string. Cached.

        Empty strings cost 0. Single-char strings cost 1. Otherwise
        try the endpoint, fall back to heuristic on failure.
        """
        if not text:
            return 0
        if len(text) == 1:
            return 1

        # Cache hit
        cached = self._cache.get(text)
        if cached is not None:
            self._cache.move_to_end(text)
            return cached

        # Cache miss — actually count
        if self._use_endpoint and self.endpoint is not None:
            count = self._count_via_endpoint(text)
            if count is None:
                count = self._count_via_heuristic(text)
        else:
            count = self._count_via_heuristic(text)

        # Insert with LRU eviction
        self._cache[text] = count
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return count

    def count_message(self, msg: LogMessage) -> int:
        """Count tokens for a single LogMessage, including the role overhead.

        Empirically each message has ~4 tokens of role/separator overhead
        in OpenAI-format chat completions. We add a flat 4 to match.
        """
        api = msg.to_api_dict()
        body_tokens = self.count(api.get("content", ""))
        return body_tokens + 4

    def count_round(self, round: ApiRound) -> int:
        """Count tokens for an entire round and CACHE on the round.

        Side effect: writes the result to round.token_estimate so the
        budget tracker can read it without another tokenizer call.
        """
        total = sum(self.count_message(m) for m in round.messages)
        round.token_estimate = total
        return total

    def count_log(self, log: MessageLog) -> int:
        """Count tokens for the full log including system prompt.

        Refreshes per-round cached estimates as a side effect.
        """
        sys_tokens = self.count(log.system_prompt) + 4 if log.system_prompt else 0
        round_tokens = sum(self.count_round(r) for r in log.rounds)
        return sys_tokens + round_tokens

    def refresh_round_estimates(self, log: MessageLog) -> None:
        """Walk the log and update each round's cached token_estimate.

        Cheap because of the per-string cache — strings that haven't
        changed since the last refresh hit the LRU. Call this once
        per loop iteration before the budget check.
        """
        for r in log.rounds:
            self.count_round(r)

    # ─── Cache management ───

    def clear(self) -> None:
        """Drop the cache. Required after model swap because token IDs
        differ across tokenizers."""
        self._cache.clear()
        self._endpoint_failures = 0
        self._use_endpoint = self._endpoint_available(self.endpoint)

    def cache_size(self) -> int:
        return len(self._cache)

    def counting_method(self) -> str:
        if self._use_endpoint and self.endpoint is not None:
            return "provider_tokenizer"
        return "heuristic"

    # ─── Internal counting paths ───

    def _count_via_endpoint(self, text: str) -> int | None:
        """Ask the provider to count the text, or None on failure."""
        if self.endpoint is None:
            return None
        try:
            counted = self.endpoint.count_text_tokens(text)
            if not isinstance(counted, int) or counted < 0:
                self._endpoint_failures += 1
                if self._endpoint_failures >= self._max_endpoint_failures:
                    self._use_endpoint = False
                return None
            self._endpoint_failures = 0  # success — reset failure counter
            return counted
        except (AttributeError, urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
            # Network error, server down, malformed response — bail to heuristic
            self._endpoint_failures += 1
            if self._endpoint_failures >= self._max_endpoint_failures:
                self._use_endpoint = False  # give up until clear()
            return None

    @staticmethod
    def _count_via_heuristic(text: str) -> int:
        """Char-based fallback. Conservative — slightly overestimates."""
        return max(1, math.ceil(len(text) / HEURISTIC_CHARS_PER_TOKEN))

    @staticmethod
    def _endpoint_available(endpoint: TokenizerEndpoint | None) -> bool:
        if endpoint is None or not callable(getattr(endpoint, "count_text_tokens", None)):
            return False
        advertised = getattr(endpoint, "supports_tokenize_endpoint", None)
        if advertised is None:
            return True
        return bool(advertised)
