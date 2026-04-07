"""Tests for agent/budget.py — ContextBudget, CircuitBreaker, RecompactChain, BudgetTracker."""

from __future__ import annotations

import pytest

from successor.agent.budget import (
    BudgetTracker,
    CircuitBreaker,
    ContextBudget,
    RecompactChain,
)


# ─── ContextBudget ───


def test_context_budget_defaults_are_consistent() -> None:
    b = ContextBudget()
    assert b.warning_buffer > b.autocompact_buffer > b.blocking_buffer >= 0
    assert b.warning_at < b.autocompact_at < b.blocking_at < b.window


def test_context_budget_invalid_thresholds_raise() -> None:
    with pytest.raises(ValueError):
        ContextBudget(warning_buffer=10, autocompact_buffer=20, blocking_buffer=30)


def test_context_budget_state_transitions() -> None:
    b = ContextBudget(window=100, warning_buffer=30, autocompact_buffer=20, blocking_buffer=10)
    # warning_at=70, autocompact_at=80, blocking_at=90
    assert b.state(0) == "ok"
    assert b.state(50) == "ok"
    assert b.state(70) == "warning"
    assert b.state(75) == "warning"
    assert b.state(80) == "autocompact"
    assert b.state(85) == "autocompact"
    assert b.state(90) == "blocking"
    assert b.state(99) == "blocking"


def test_context_budget_fill_pct() -> None:
    b = ContextBudget(window=100, warning_buffer=30, autocompact_buffer=20, blocking_buffer=10)
    assert b.fill_pct(0) == 0.0
    assert b.fill_pct(50) == 0.5
    assert b.fill_pct(100) == 1.0
    assert b.fill_pct(150) == 1.0  # capped


def test_context_budget_headroom() -> None:
    b = ContextBudget(window=100, warning_buffer=30, autocompact_buffer=20, blocking_buffer=10)
    # blocking_at = 90
    assert b.headroom(0) == 90
    assert b.headroom(80) == 10
    assert b.headroom(90) == 0
    assert b.headroom(95) == 0  # never negative


def test_context_budget_predicates() -> None:
    b = ContextBudget(window=100, warning_buffer=30, autocompact_buffer=20, blocking_buffer=10)
    assert not b.should_autocompact(70)
    assert not b.should_autocompact(79)
    assert b.should_autocompact(80)
    assert not b.over_blocking_limit(80)
    assert b.over_blocking_limit(90)
    assert b.in_warning_zone(70)
    assert not b.in_warning_zone(69)


# ─── CircuitBreaker ───


def test_circuit_breaker_starts_untripped() -> None:
    cb = CircuitBreaker()
    assert not cb.tripped
    assert cb.consecutive_failures == 0


def test_circuit_breaker_trips_at_max_failures() -> None:
    cb = CircuitBreaker(max_failures=3)
    cb.fail()
    assert not cb.tripped
    cb.fail()
    assert not cb.tripped
    cb.fail()
    assert cb.tripped


def test_circuit_breaker_success_resets_counter() -> None:
    cb = CircuitBreaker(max_failures=3)
    cb.fail()
    cb.fail()
    cb.success()
    assert cb.consecutive_failures == 0
    cb.fail()
    cb.fail()
    cb.fail()
    assert cb.tripped


def test_circuit_breaker_manual_reset() -> None:
    cb = CircuitBreaker(max_failures=2)
    cb.fail()
    cb.fail()
    assert cb.tripped
    cb.reset()
    assert not cb.tripped
    assert cb.consecutive_failures == 0


# ─── RecompactChain ───


def test_recompact_chain_no_history_not_chained() -> None:
    rc = RecompactChain()
    assert not rc.is_chained(turn=10, at=100.0)


def test_recompact_chain_close_in_time_and_turns_is_chained() -> None:
    rc = RecompactChain(min_interval_s=30.0, min_turns_apart=3)
    rc.record(turn=10, at=100.0)
    # Same turn, 5s later: chained (both close)
    assert rc.is_chained(turn=10, at=105.0)
    # Same turn, 31s later: not chained (time elapsed)
    assert not rc.is_chained(turn=10, at=131.0)
    # 3 turns later, 5s later: not chained (turns elapsed)
    assert not rc.is_chained(turn=13, at=105.0)


def test_recompact_chain_record_updates_state() -> None:
    rc = RecompactChain()
    assert rc.last_compact_turn == -1
    rc.record(turn=5, at=100.0)
    assert rc.last_compact_turn == 5
    assert rc.last_compact_at == 100.0


# ─── BudgetTracker ───


def test_budget_tracker_observe_records_peak() -> None:
    t = BudgetTracker()
    t.observe(1000)
    t.observe(500)
    t.observe(2000)
    assert t.peak_tokens == 2000
    assert t.last_observed_tokens == 2000


def test_budget_tracker_should_compact_under_threshold() -> None:
    t = BudgetTracker(budget=ContextBudget(
        window=100, warning_buffer=30, autocompact_buffer=20, blocking_buffer=10,
    ))
    decision, reason = t.should_attempt_compaction(used=70, turn=1)
    assert not decision
    assert "under threshold" in reason


def test_budget_tracker_should_compact_at_threshold() -> None:
    t = BudgetTracker(budget=ContextBudget(
        window=100, warning_buffer=30, autocompact_buffer=20, blocking_buffer=10,
    ))
    decision, reason = t.should_attempt_compaction(used=80, turn=1)
    assert decision


def test_budget_tracker_should_compact_blocked_by_circuit() -> None:
    t = BudgetTracker(budget=ContextBudget(
        window=100, warning_buffer=30, autocompact_buffer=20, blocking_buffer=10,
    ))
    t.circuit_breaker.fail()
    t.circuit_breaker.fail()
    t.circuit_breaker.fail()
    decision, reason = t.should_attempt_compaction(used=90, turn=1)
    assert not decision
    assert "circuit breaker" in reason


def test_budget_tracker_should_compact_blocked_by_chain() -> None:
    t = BudgetTracker(budget=ContextBudget(
        window=100, warning_buffer=30, autocompact_buffer=20, blocking_buffer=10,
    ))
    t.recompact.record(turn=5)
    decision, reason = t.should_attempt_compaction(used=90, turn=5)
    assert not decision
    assert "chain" in reason


def test_budget_tracker_compaction_success_records_chain() -> None:
    t = BudgetTracker()
    t.note_compaction_success(turn=10)
    assert t.compactions_total == 1
    assert t.recompact.last_compact_turn == 10
    assert not t.circuit_breaker.tripped


def test_budget_tracker_compaction_failure_does_not_record_chain() -> None:
    """Failed compactions DON'T trip the chain detector — otherwise
    the user would be unable to retry after a transient error."""
    t = BudgetTracker()
    t.note_compaction_failure(turn=10)
    assert t.compactions_failed == 1
    assert t.recompact.last_compact_turn == -1  # NOT recorded
