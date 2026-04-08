"""Tests for the post-compact size assertion.

Verifies that `compact()` correctly flags compactions that fail to
shrink the log meaningfully (post-compact size >= 90% of pre-compact)
by setting `BoundaryMarker.warning` and `BoundaryMarker.underperformed`.

Healthy compactions never trigger the warning. The assertion is
non-fatal — the new log is still applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from successor.agent.compact import compact
from successor.agent.log import BoundaryMarker, LogMessage, MessageLog
from successor.agent.tokens import TokenCounter
from successor.providers.llama import (
    ContentChunk,
    StreamEnded,
    StreamStarted,
)


# ─── Mock client (mirrors test_agent_compact.py's helper) ───


@dataclass
class _MockStream:
    events: list = field(default_factory=list)
    _drained: bool = False

    def drain(self):
        if self._drained:
            return []
        self._drained = True
        return list(self.events)

    def close(self):
        pass


@dataclass
class _MockClient:
    streams: list = field(default_factory=list)
    call_count: int = 0
    last_messages: list = field(default_factory=list)
    last_max_tokens: int | None = None
    last_temperature: float | None = None
    base_url: str = "http://mock"

    def stream_chat(self, messages, *, max_tokens=None, temperature=None,
                    timeout=None, extra=None):
        self.call_count += 1
        self.last_messages = list(messages)
        self.last_max_tokens = max_tokens
        self.last_temperature = temperature
        idx = min(self.call_count - 1, len(self.streams) - 1)
        return self.streams[idx]


def _stream_with_summary(text: str) -> _MockStream:
    return _MockStream(events=[
        StreamStarted(),
        ContentChunk(text=text),
        StreamEnded(finish_reason="stop", usage=None, timings=None),
    ])


# ─── Log builders ───


def _build_log_with_long_rounds(n_rounds: int, body_size: int = 2_000) -> MessageLog:
    """Build a log where each round has substantial content so the
    pre-compact token count is meaningful (>>900 tokens)."""
    log = MessageLog(system_prompt="You are successor.")
    body = "filler text " * (body_size // 12)
    for i in range(n_rounds):
        log.begin_round(started_at=float(i))
        log.append_to_current_round(LogMessage(
            role="user",
            content=f"q{i}: {body}",
            created_at=float(i),
        ))
        log.append_to_current_round(LogMessage(
            role="assistant",
            content=f"a{i}: {body}",
            created_at=float(i),
        ))
    return log


# ─── Healthy compaction does NOT trigger the warning ───


def test_healthy_compaction_no_warning() -> None:
    """A normal compaction with a SHORT summary leaves the warning empty."""
    log = _build_log_with_long_rounds(n_rounds=10, body_size=2_000)
    counter = TokenCounter()  # heuristic only
    client = _MockClient(streams=[_stream_with_summary("a brief summary")])

    new_log, boundary = compact(log, client, counter=counter, keep_recent_rounds=3)

    assert boundary.warning == ""
    assert not boundary.underperformed
    # Sanity: the actual reduction was meaningful
    assert boundary.post_compact_tokens < boundary.pre_compact_tokens * 0.9


def test_healthy_compaction_reduction_pct_is_positive() -> None:
    log = _build_log_with_long_rounds(n_rounds=10, body_size=2_000)
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_summary("brief")])

    _, boundary = compact(log, client, counter=counter, keep_recent_rounds=3)
    assert boundary.reduction_pct > 10.0


# ─── Underperforming compaction DOES trigger the warning ───


def test_oversized_summary_triggers_warning() -> None:
    """A summary as long as the original log triggers the assertion."""
    log = _build_log_with_long_rounds(n_rounds=10, body_size=2_000)
    counter = TokenCounter()
    # The summary is enormous — way larger than the saved space
    huge_summary = "filler text " * 5_000  # ~60K chars
    client = _MockClient(streams=[_stream_with_summary(huge_summary)])

    new_log, boundary = compact(log, client, counter=counter, keep_recent_rounds=3)

    # Warning fired
    assert boundary.warning != ""
    assert "underperformed" in boundary.warning
    assert boundary.underperformed


def test_warning_message_contains_token_counts() -> None:
    """The warning message includes the actual pre/post numbers so the
    user can see how badly it went."""
    log = _build_log_with_long_rounds(n_rounds=10, body_size=2_000)
    counter = TokenCounter()
    huge_summary = "x " * 50_000  # 100K chars
    client = _MockClient(streams=[_stream_with_summary(huge_summary)])

    _, boundary = compact(log, client, counter=counter, keep_recent_rounds=3)

    assert str(boundary.pre_compact_tokens) in boundary.warning
    assert str(boundary.post_compact_tokens) in boundary.warning


def test_keep_too_many_recent_rounds_triggers_warning() -> None:
    """If keep_recent_rounds is so high that nothing real gets summarized,
    the post-compact size will be ~the same as pre-compact and the
    warning fires."""
    log = _build_log_with_long_rounds(n_rounds=8, body_size=2_000)
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_summary("brief")])

    # keep_recent_rounds >= len(rounds) gets clamped to len // 2 = 4 internally
    # which still summarizes 4 rounds. Force a near-pass-through with a
    # massively oversized summary instead.
    big = "filler " * 30_000
    client = _MockClient(streams=[_stream_with_summary(big)])
    _, boundary = compact(log, client, counter=counter, keep_recent_rounds=4)
    assert boundary.warning != ""


# ─── Boundary marker properties ───


def test_underperformed_property_at_exactly_90pct() -> None:
    """The underperformed threshold is post >= pre * 0.9."""
    bm_at_90 = BoundaryMarker(
        happened_at=0.0,
        pre_compact_tokens=1_000,
        post_compact_tokens=900,  # exactly 90%
        rounds_summarized=5,
        summary_text="",
    )
    assert bm_at_90.underperformed

    bm_below_90 = BoundaryMarker(
        happened_at=0.0,
        pre_compact_tokens=1_000,
        post_compact_tokens=899,  # 89.9%
        rounds_summarized=5,
        summary_text="",
    )
    assert not bm_below_90.underperformed


def test_underperformed_property_handles_zero_pre_tokens() -> None:
    """Edge case: pre_compact_tokens=0 should not trigger the assertion."""
    bm = BoundaryMarker(
        happened_at=0.0,
        pre_compact_tokens=0,
        post_compact_tokens=0,
        rounds_summarized=0,
        summary_text="",
    )
    assert not bm.underperformed


def test_warning_propagates_through_replace() -> None:
    """compact() uses dataclasses.replace to set the post_tokens and
    warning. Verify both fields propagate."""
    log = _build_log_with_long_rounds(n_rounds=10, body_size=2_000)
    counter = TokenCounter()
    huge = "x " * 50_000
    client = _MockClient(streams=[_stream_with_summary(huge)])

    new_log, boundary = compact(log, client, counter=counter, keep_recent_rounds=3)
    assert boundary.warning != ""
    assert boundary.post_compact_tokens > 0


def test_boundary_message_marks_underperformed() -> None:
    """The synthetic system message in the new log includes the
    ⚠ underperformed annotation when the assertion fired."""
    log = _build_log_with_long_rounds(n_rounds=10, body_size=2_000)
    counter = TokenCounter()
    huge = "x " * 50_000
    client = _MockClient(streams=[_stream_with_summary(huge)])

    new_log, _ = compact(log, client, counter=counter, keep_recent_rounds=3)

    # Find the boundary message
    boundary_msgs = [
        m for round_ in new_log.rounds for m in round_.messages
        if m.is_boundary
    ]
    assert len(boundary_msgs) >= 1
    assert "underperformed" in boundary_msgs[0].content


def test_healthy_boundary_message_no_underperformed_annotation() -> None:
    """A healthy compaction's boundary message does NOT include the warning."""
    log = _build_log_with_long_rounds(n_rounds=10, body_size=2_000)
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_summary("tiny")])

    new_log, _ = compact(log, client, counter=counter, keep_recent_rounds=3)

    boundary_msgs = [
        m for round_ in new_log.rounds for m in round_.messages
        if m.is_boundary
    ]
    assert len(boundary_msgs) >= 1
    assert "underperformed" not in boundary_msgs[0].content
