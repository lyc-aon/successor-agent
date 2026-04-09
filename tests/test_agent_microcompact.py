"""Tests for agent/microcompact.py — time-based stale tool result clearing."""

from __future__ import annotations

from successor.agent.log import LogMessage, MessageLog
from successor.agent.microcompact import (
    CLEARED_PLACEHOLDER,
    microcompact,
)
from successor.bash import dispatch_bash


def _build_log_with_n_tools(n: int) -> MessageLog:
    log = MessageLog()
    for i in range(n):
        log.begin_round()
        log.append_to_current_round(LogMessage(
            role="user", content=f"q{i}", created_at=float(i),
        ))
        log.append_to_current_round(LogMessage(
            role="tool", content="", tool_card=dispatch_bash(f"echo r{i}"),
            created_at=float(i),
        ))
    return log


# ─── Empty / no-op cases ───


def test_microcompact_empty_log() -> None:
    log = MessageLog()
    new_log, n_cleared = microcompact(log)
    assert n_cleared == 0
    assert new_log.is_empty()


def test_microcompact_no_tool_messages() -> None:
    log = MessageLog()
    log.append_to_current_round(LogMessage(role="user", content="hi"))
    log.append_to_current_round(LogMessage(role="assistant", content="hello"))
    new_log, n_cleared = microcompact(log)
    assert n_cleared == 0


def test_microcompact_under_keep_threshold_does_nothing() -> None:
    log = _build_log_with_n_tools(5)
    new_log, n_cleared = microcompact(log, keep_recent=10, idle_threshold_s=None)
    assert n_cleared == 0


# ─── Count-based clearing ───


def test_microcompact_clears_oldest_beyond_keep() -> None:
    log = _build_log_with_n_tools(12)
    new_log, n_cleared = microcompact(log, keep_recent=4, idle_threshold_s=None)
    assert n_cleared == 8

    # Verify the OLDEST 8 are cleared and the YOUNGEST 4 are intact
    tool_messages = [m for m in new_log.iter_messages() if m.tool_card]
    cleared = [m for m in tool_messages if m.tool_card.output == CLEARED_PLACEHOLDER]
    intact = [m for m in tool_messages if m.tool_card.output != CLEARED_PLACEHOLDER]
    assert len(cleared) == 8
    assert len(intact) == 4
    # The intact ones are the most recent 4 (echo r8, r9, r10, r11)
    intact_cmds = [m.tool_card.raw_command for m in intact]
    assert "echo r11" in intact_cmds
    assert "echo r8" in intact_cmds


def test_microcompact_does_not_re_clear_existing_placeholders() -> None:
    """Running microcompact twice in a row shouldn't double-count
    already-cleared messages."""
    log = _build_log_with_n_tools(12)
    new_log1, n1 = microcompact(log, keep_recent=4, idle_threshold_s=None)
    assert n1 == 8
    new_log2, n2 = microcompact(new_log1, keep_recent=4, idle_threshold_s=None)
    assert n2 == 0  # Already cleared — nothing to do


# ─── Time-based clearing ───


def test_microcompact_idle_threshold_clears_all_when_idle() -> None:
    log = _build_log_with_n_tools(3)
    # Set timestamps so the latest is at t=2.0
    # Idle threshold 60s, now=70.0 — definitely past idle
    new_log, n = microcompact(
        log, keep_recent=99,  # count-based won't fire
        idle_threshold_s=60.0, now=70.0,
    )
    assert n == 3  # All 3 cleared by time


def test_microcompact_idle_threshold_does_not_fire_when_recent() -> None:
    log = _build_log_with_n_tools(3)
    # latest at t=2.0, now=10.0 — within 60s
    new_log, n = microcompact(
        log, keep_recent=99, idle_threshold_s=60.0, now=10.0,
    )
    assert n == 0


def test_microcompact_idle_skipped_when_no_timestamps() -> None:
    """Messages with created_at=0.0 should NOT trigger idle clearing
    (defensive against logs without timestamps)."""
    log = MessageLog()
    log.begin_round()
    log.append_to_current_round(LogMessage(
        role="tool", content="", tool_card=dispatch_bash("echo hi"),
        created_at=0.0,
    ))
    new_log, n = microcompact(log, keep_recent=99, idle_threshold_s=60.0, now=99999.0)
    assert n == 0


# ─── Pure function semantics ───


def test_microcompact_does_not_mutate_input() -> None:
    log = _build_log_with_n_tools(10)
    rounds_before = log.round_count
    msgs_before = log.total_messages()
    microcompact(log, keep_recent=2, idle_threshold_s=None)
    assert log.round_count == rounds_before
    assert log.total_messages() == msgs_before
    # The original messages still have real output, not placeholders
    for m in log.iter_messages():
        if m.tool_card:
            assert m.tool_card.output != CLEARED_PLACEHOLDER


def test_microcompact_preserves_non_tool_messages_intact() -> None:
    """User and assistant messages must NOT be touched by microcompact."""
    log = MessageLog()
    log.begin_round()
    log.append_to_current_round(LogMessage(role="user", content="user words", created_at=1.0))
    log.append_to_current_round(LogMessage(role="assistant", content="reply", created_at=1.0))
    log.append_to_current_round(LogMessage(
        role="tool", content="", tool_card=dispatch_bash("echo old"),
        created_at=1.0,
    ))
    new_log, _ = microcompact(log, keep_recent=0, idle_threshold_s=None)
    contents = [m.content for m in new_log.iter_messages() if not m.tool_card]
    assert "user words" in contents
    assert "reply" in contents
