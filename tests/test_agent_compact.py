"""Tests for agent/compact.py — full LLM-summarization compaction.

Uses a deterministic mock client + mock ChatStream so tests are
hermetic and don't require a running llama.cpp server. The mock
client lets each test control:
  - what summary text the model "produces"
  - whether the stream errors with prompt-too-long
  - how many calls have been made (for PTL retry verification)
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from successor.agent.compact import (
    CompactionError,
    MIN_ROUNDS_TO_COMPACT,
    compact,
)
from successor.agent.log import LogMessage, MessageLog
from successor.agent.tokens import TokenCounter
from successor.providers.llama import (
    ContentChunk,
    StreamEnded,
    StreamError,
    StreamStarted,
)


# ─── Mock stream + client ───


@dataclass
class _MockStream:
    """Drop-in fake for ChatStream that emits a canned event sequence."""
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
    """Records calls and returns canned streams in sequence."""
    streams: list[_MockStream] = field(default_factory=list)
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
    """Build a stream that emits a single content chunk + StreamEnded."""
    return _MockStream(events=[
        StreamStarted(),
        ContentChunk(text=text),
        StreamEnded(finish_reason="stop", usage=None, timings=None),
    ])


def _stream_with_ptl_error() -> _MockStream:
    return _MockStream(events=[
        StreamStarted(),
        StreamError(message="the request exceeds the available context size — prompt is too long"),
    ])


def _stream_with_other_error(msg: str = "network failure") -> _MockStream:
    return _MockStream(events=[
        StreamStarted(),
        StreamError(message=msg),
    ])


def _stream_with_empty_content() -> _MockStream:
    return _MockStream(events=[
        StreamStarted(),
        StreamEnded(finish_reason="stop", usage=None, timings=None),
    ])


# ─── Builders ───


def _build_log(n_rounds: int) -> MessageLog:
    log = MessageLog(system_prompt="You are successor.")
    for i in range(n_rounds):
        log.begin_round(started_at=float(i))
        log.append_to_current_round(LogMessage(
            role="user", content=f"question {i} with some content",
            created_at=float(i),
        ))
        log.append_to_current_round(LogMessage(
            role="assistant", content=f"answer {i} with substantive content",
            created_at=float(i),
        ))
    return log


# ─── Happy path ───


def test_compact_basic_returns_new_log_and_boundary() -> None:
    log = _build_log(10)
    counter = TokenCounter()  # heuristic only — hermetic
    client = _MockClient(streams=[_stream_with_summary("summary text here")])

    new_log, boundary = compact(log, client, counter=counter, keep_recent_rounds=3)

    assert client.call_count == 1
    assert boundary.summary_text == "summary text here"
    assert boundary.rounds_summarized == 7  # 10 - 3 kept
    # New log: boundary round + summary round + 3 kept rounds + attachment hint
    # (we have no attachments here so attachment hint is skipped)
    assert new_log.round_count == 5  # boundary + summary + 3 kept


def test_compact_preserves_recent_rounds_verbatim() -> None:
    log = _build_log(10)
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_summary("summary")])

    new_log, _ = compact(log, client, counter=counter, keep_recent_rounds=3)

    # The last 3 user messages from the original log should appear
    # verbatim in the new log
    new_user_msgs = [
        m.content for m in new_log.iter_messages()
        if m.role == "user" and not m.is_summary
    ]
    assert "question 7 with some content" in new_user_msgs
    assert "question 8 with some content" in new_user_msgs
    assert "question 9 with some content" in new_user_msgs
    # Earlier ones are GONE
    assert "question 0 with some content" not in new_user_msgs


def test_compact_uses_low_temperature_for_summary() -> None:
    log = _build_log(10)
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_summary("summary")])
    compact(log, client, counter=counter)
    # Should use the SUMMARY_TEMPERATURE constant (0.2), not chat default
    assert client.last_temperature == 0.2


def test_compact_passes_summary_max_tokens() -> None:
    log = _build_log(10)
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_summary("summary")])
    compact(log, client, counter=counter, summary_max_tokens=8192)
    assert client.last_max_tokens == 8192


def test_compact_boundary_carries_correct_metadata() -> None:
    log = _build_log(10)
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_summary("the summary")])

    pre_tokens = counter.count_log(log)
    _, boundary = compact(log, client, counter=counter, keep_recent_rounds=3)

    assert boundary.pre_compact_tokens == pre_tokens
    assert boundary.post_compact_tokens > 0
    assert boundary.post_compact_tokens < pre_tokens  # actually shrank
    assert boundary.rounds_summarized == 7
    assert boundary.summary_text == "the summary"
    assert boundary.reason == "auto"


def test_compact_reason_propagates_to_boundary() -> None:
    log = _build_log(10)
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_summary("s")])
    _, boundary = compact(log, client, counter=counter, reason="manual")
    assert boundary.reason == "manual"


# ─── Refusal cases ───


def test_compact_refuses_too_few_rounds() -> None:
    log = _build_log(1)  # needs one older round to summarize and one to keep
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_summary("s")])
    with pytest.raises(ValueError, match="need at least"):
        compact(log, client, counter=counter)


def test_compact_allows_two_rounds_when_one_can_be_summarized() -> None:
    log = _build_log(MIN_ROUNDS_TO_COMPACT)
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_summary("two-round summary")])

    new_log, boundary = compact(log, client, counter=counter)

    assert boundary.summary_text == "two-round summary"
    assert boundary.rounds_summarized == 1
    assert new_log.round_count == 3  # boundary + summary + kept recent round


def test_compact_refuses_when_keep_recent_eats_everything() -> None:
    log = _build_log(MIN_ROUNDS_TO_COMPACT)  # exactly the minimum
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_summary("s")])
    # keep_recent_rounds == round_count → nothing left to summarize
    # the function silently degrades by halving keep_recent
    new_log, _ = compact(log, client, counter=counter, keep_recent_rounds=MIN_ROUNDS_TO_COMPACT)
    # Did NOT raise — degraded gracefully
    assert new_log.round_count > 0


# ─── Error paths ───


def test_compact_raises_on_empty_summary() -> None:
    log = _build_log(10)
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_empty_content()])
    with pytest.raises(CompactionError, match="empty"):
        compact(log, client, counter=counter)


def test_compact_raises_on_stream_error() -> None:
    log = _build_log(10)
    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_other_error("mock network failure")])
    with pytest.raises(CompactionError, match="stream error"):
        compact(log, client, counter=counter)


# ─── PTL retry path ───


def test_compact_ptl_retry_succeeds_after_truncation() -> None:
    """First call returns prompt-too-long, second succeeds after dropping
    the oldest 3 rounds."""
    log = _build_log(15)
    counter = TokenCounter()
    client = _MockClient(streams=[
        _stream_with_ptl_error(),       # attempt 1: PTL
        _stream_with_summary("recovered"),  # attempt 2: success
    ])

    new_log, boundary = compact(log, client, counter=counter)

    assert client.call_count == 2
    assert boundary.summary_text == "recovered"


def test_compact_ptl_retry_exhausted_raises() -> None:
    """All retries fail with PTL → CompactionError."""
    log = _build_log(20)
    counter = TokenCounter()
    # 4 streams (1 + MAX_PTL_RETRIES), all PTL errors
    client = _MockClient(streams=[
        _stream_with_ptl_error(),
        _stream_with_ptl_error(),
        _stream_with_ptl_error(),
        _stream_with_ptl_error(),
    ])
    with pytest.raises(CompactionError):
        compact(log, client, counter=counter)
    assert client.call_count == 4  # 1 + 3 retries


def test_compact_ptl_retry_drops_oldest_first() -> None:
    """After PTL retry, subsequent prompts should NOT include the
    earliest rounds (they were dropped to fit).

    With the cache-friendly prompt structure, each round becomes its
    own user/assistant message in the API prompt (rather than being
    embedded in one big transcript). After PTL retry drops the oldest
    3 rounds, the question content should still be missing for the
    dropped rounds and present for the rest.
    """
    log = _build_log(15)
    counter = TokenCounter()
    client = _MockClient(streams=[
        _stream_with_ptl_error(),
        _stream_with_summary("recovered"),
    ])
    compact(log, client, counter=counter)

    # Collect the contents of every user message in the second call.
    # Each question is "question N with some content" — use the trailing
    # " with" to disambiguate "question 1" from "question 10".
    user_contents = "\n".join(
        m["content"] for m in client.last_messages if m["role"] == "user"
    )
    # After dropping the oldest 3 rounds, questions 0-2 should be gone
    assert "question 0 with" not in user_contents
    assert "question 1 with" not in user_contents
    assert "question 2 with" not in user_contents
    # But middle and later questions should still be present
    assert "question 3 with" in user_contents
    assert "question 14 with" in user_contents


# ─── Attachment re-injection ───


def test_compact_re_attaches_recently_seen_files() -> None:
    """Files seen via tool cards before compaction get re-attached
    in a hint round after the compacted summary."""
    from successor.bash import dispatch_bash

    log = MessageLog(system_prompt="sp")
    for i in range(8):
        log.begin_round()
        log.append_to_current_round(LogMessage(role="user", content=f"q{i}"))
        # Use a real cat to populate attachments
        log.append_to_current_round(LogMessage(
            role="tool", content="",
            tool_card=dispatch_bash("cat tests/test_agent_log.py"),
        ))

    counter = TokenCounter()
    client = _MockClient(streams=[_stream_with_summary("s")])
    new_log, _ = compact(log, client, counter=counter, keep_recent_rounds=3)

    # The file should appear in the attachment hint round
    has_hint = any(
        "tests/test_agent_log.py" in m.content
        for m in new_log.iter_messages()
        if m.role == "system"
    )
    assert has_hint
