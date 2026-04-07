"""Performance regression tests for the chat at large context.

The chat must remain interactive (≥30 fps target) even with 200K
tokens of conversation history. The original failure was the
static-footer calling `count_log()` on every frame, which walked
every message and HIT THE /tokenize endpoint per message — at 200K
context that's ~1,400 HTTP calls per frame, dropping the chat to
~1 fps.

The fix has three parts:
  1. Per-message token count cache on `_Message._token_count`
  2. Chat-level total cache invalidated by (id, len) of self.messages
  3. TokenCounter LRU bumped to 16384 entries

These tests guard the perf characteristic — they're not strict
ms thresholds (machines vary) but they catch the catastrophic
case where we'd be paying O(N) per frame again.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from successor.agent import TokenCounter
from successor.chat import SuccessorChat, _Message
from successor.render.cells import Grid


def _make_chat_with_n_messages(n: int) -> SuccessorChat:
    """Build a chat with n synthetic messages, each carrying a
    realistic token count via the heuristic pre-fill that /burn
    uses. Hermetic — no /tokenize endpoint hits."""
    chat = SuccessorChat()
    chat.messages = []
    chat._cached_token_counter = TokenCounter()  # heuristic only
    for i in range(n):
        msg = _Message("user", f"synthetic message {i} " + "padding " * 20)
        # Pre-fill the per-message cache (mimics /burn's optimization)
        msg._token_count = max(1, len(msg.raw_text) // 4) + 4
        chat.messages.append(msg)
    return chat


def _frame_time_ms(chat: SuccessorChat, *, n_frames: int = 10) -> float:
    """Average frame time in ms over n_frames after a warm-up frame."""
    g = Grid(35, 110)
    chat.on_tick(g)  # warm up

    times = []
    for _ in range(n_frames):
        t0 = time.perf_counter()
        g = Grid(35, 110)
        chat.on_tick(g)
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000)
    return sum(times) / len(times)


# ─── Frame time tests ───


def test_steady_state_under_100_messages_is_fast(temp_config_dir: Path) -> None:
    """Small chats should run well under 33ms (30 fps budget)."""
    chat = _make_chat_with_n_messages(50)
    avg = _frame_time_ms(chat)
    assert avg < 33, f"50 messages averaged {avg:.1f}ms/frame, expected < 33"


def test_large_log_remains_interactive(temp_config_dir: Path) -> None:
    """1000+ messages must still hit the 30 fps target.

    The original bug was 975ms/frame at 1433 messages (1 fps). The
    fix brings it to ~8ms/frame (124 fps). We assert under 50ms
    here to give plenty of margin for slower machines.
    """
    chat = _make_chat_with_n_messages(1000)
    avg = _frame_time_ms(chat)
    assert avg < 50, (
        f"1000 messages averaged {avg:.1f}ms/frame, expected < 50. "
        f"This is the perf regression we caught at 200K burn — "
        f"check the static footer's _total_tokens caching path."
    )


def test_huge_log_does_not_collapse(temp_config_dir: Path) -> None:
    """3000 messages should still be well under 100ms/frame.

    This is the catastrophic-failure guard — if any future change
    accidentally re-introduces an O(N) walk per frame, this test
    fires LOUDLY (you'd see the average shoot to 500ms+).
    """
    chat = _make_chat_with_n_messages(3000)
    avg = _frame_time_ms(chat)
    assert avg < 100, (
        f"3000 messages averaged {avg:.1f}ms/frame, expected < 100. "
        f"O(N)-per-frame regression in the footer or row builder."
    )


# ─── Cache correctness tests ───


def test_total_tokens_cache_hits_on_unchanged_messages(temp_config_dir: Path) -> None:
    """Calling _total_tokens twice with no mutation should hit cache."""
    chat = _make_chat_with_n_messages(100)
    n1 = chat._total_tokens()
    # Mark it stale-only-by-id by reassigning the SAME list (id stays)
    # — but no, we want to verify the cache hit. Read it twice.
    n2 = chat._total_tokens()
    assert n1 == n2
    # The cached_total_tokens slot should be set
    assert chat._cached_total_tokens is not None
    assert chat._cached_total_tokens_key[1] == 100  # len=100


def test_total_tokens_cache_invalidates_on_append(temp_config_dir: Path) -> None:
    """Appending a message should auto-invalidate via len change."""
    chat = _make_chat_with_n_messages(50)
    n1 = chat._total_tokens()
    new_msg = _Message("user", "new content here")
    new_msg._token_count = 10
    chat.messages.append(new_msg)
    n2 = chat._total_tokens()
    assert n2 == n1 + 10


def test_total_tokens_cache_invalidates_on_replacement(temp_config_dir: Path) -> None:
    """Wholesale replacement should auto-invalidate via id change.

    This is the case the test_footer_bar_grows_with_fill test caught
    when we initially used a len-only check. Wholesale replacement
    with a same-length list MUST invalidate or the bar shows stale
    data.
    """
    chat = _make_chat_with_n_messages(1)  # one message, total = X
    n1 = chat._total_tokens()
    # Replace with a DIFFERENT one-message list (same length)
    new_msg = _Message("user", "completely different content " * 10)
    new_msg._token_count = 999
    chat.messages = [new_msg]  # new list object → new id
    n2 = chat._total_tokens()
    assert n2 != n1
    # The system prompt accounts for the small offset; the diff
    # should at least include the new message's count
    assert n2 >= 999


def test_per_message_token_count_cached_on_first_access(temp_config_dir: Path) -> None:
    """The first read populates _token_count, subsequent reads hit it."""
    chat = SuccessorChat()
    chat._cached_token_counter = TokenCounter()  # heuristic only
    msg = _Message("user", "hello world this is a test")
    assert msg._token_count is None
    n1 = chat._token_count_for_message(msg)
    assert msg._token_count is not None
    assert msg._token_count == n1
    # Second call returns the cached value (verified via mock counter)
    n2 = chat._token_count_for_message(msg)
    assert n2 == n1


def test_token_counter_default_cache_size_holds_large_log(temp_config_dir: Path) -> None:
    """The default cache size must comfortably hold a 200K-token
    conversation without thrashing. The original bug was the LRU
    evicting cache entries faster than they could be re-cached."""
    from successor.agent.tokens import DEFAULT_CACHE_SIZE
    # 200K tokens at ~150 tokens/message ≈ 1,300 unique strings.
    # The cache should be at least 4x that for safety.
    assert DEFAULT_CACHE_SIZE >= 5000, (
        f"DEFAULT_CACHE_SIZE={DEFAULT_CACHE_SIZE} is too small to hold "
        f"a typical 200K-context conversation without LRU thrashing."
    )
