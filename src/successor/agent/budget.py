"""Context budget tracking + circuit breaker + recompact-chain detection.

Three pieces of state the loop needs around compaction:

  1. ContextBudget — given a window size and a token count, decide
     whether to fire autocompact, show a warning, or refuse to call
     the API entirely. Pure data; no I/O. Mirrors free-code's
     thresholds (`AUTOCOMPACT_BUFFER_TOKENS = 13000` etc.) but with
     numbers tuned for llama.cpp's much larger windows.

  2. CircuitBreaker — after N consecutive compaction failures, stop
     trying so an unrecoverable failure mode (model returning empty
     summaries, network down, etc.) doesn't infinite-loop the agent.

  3. RecompactChain — detect that we just compacted and another
     compaction is being requested suspiciously soon. Mirrors
     free-code's check at `query.ts:632-635` that prevents oscillation.

All three are pure data + a small bit of state. Tested standalone
without any I/O.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal


# ─── ContextBudget ───
#
# Threshold semantics (used > window − buffer means "fire X"):
#
#   window         total context size from the model (e.g., 50_000 for the
#                  burn-test A3B setup, 262_144 for production qwopus)
#   warning_buf    when used > (window - warning_buf), the title bar pill
#                  goes accent_warm and shows the fill %
#   autocompact_buf when used > (window - autocompact_buf), proactive
#                  autocompact fires before the next API call
#   blocking_buf   when used > (window - blocking_buf), the loop refuses
#                  to call the API entirely and yields BlockingLimitReached
#
# Invariant: warning_buf > autocompact_buf > blocking_buf (we want each
# threshold to trip at progressively more "used" tokens).
#
# Default numbers are scaled from free-code's (13K autocompact buffer
# at ~200K window) by the same ratio for our 262K qwopus default,
# then adjusted down a bit because llama.cpp is local and cheap so
# we can compact more aggressively.

ThresholdState = Literal["ok", "warning", "autocompact", "blocking"]


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Configuration + threshold-checking helpers.

    Frozen because the budget is set once per session (from the
    profile) and never mutated. Mutable state lives in BudgetTracker.
    """

    window: int = 262_144
    warning_buffer: int = 32_000
    autocompact_buffer: int = 16_000
    blocking_buffer: int = 4_000

    def __post_init__(self) -> None:
        # Sanity check the invariant — fail loud at construction time
        # rather than hide a misconfiguration.
        if not (self.warning_buffer > self.autocompact_buffer > self.blocking_buffer >= 0):
            raise ValueError(
                f"budget thresholds out of order: "
                f"warning={self.warning_buffer} > "
                f"autocompact={self.autocompact_buffer} > "
                f"blocking={self.blocking_buffer} >= 0"
            )

    @property
    def warning_at(self) -> int:
        return self.window - self.warning_buffer

    @property
    def autocompact_at(self) -> int:
        return self.window - self.autocompact_buffer

    @property
    def blocking_at(self) -> int:
        return self.window - self.blocking_buffer

    def fill_pct(self, used: int) -> float:
        """Return the fill percentage (0.0–1.0)."""
        if self.window <= 0:
            return 0.0
        return min(1.0, max(0.0, used / self.window))

    def state(self, used: int) -> ThresholdState:
        """Return the current threshold state for `used` tokens."""
        if used >= self.blocking_at:
            return "blocking"
        if used >= self.autocompact_at:
            return "autocompact"
        if used >= self.warning_at:
            return "warning"
        return "ok"

    def should_autocompact(self, used: int) -> bool:
        return used >= self.autocompact_at

    def over_blocking_limit(self, used: int) -> bool:
        return used >= self.blocking_at

    def in_warning_zone(self, used: int) -> bool:
        return used >= self.warning_at

    def headroom(self, used: int) -> int:
        """Tokens still available before hitting the blocking limit."""
        return max(0, self.blocking_at - used)


# ─── CircuitBreaker ───


@dataclass
class CircuitBreaker:
    """Trips after N consecutive failures.

    Used by the loop to stop retrying compaction when something is
    fundamentally broken (model returning garbage summaries, network
    permanently down, etc.). The user has to manually reset to retry.
    """

    max_failures: int = 3
    consecutive_failures: int = 0

    @property
    def tripped(self) -> bool:
        return self.consecutive_failures >= self.max_failures

    def success(self) -> None:
        """Compaction succeeded. Reset the failure counter."""
        self.consecutive_failures = 0

    def fail(self) -> None:
        """Compaction failed. Increment the counter."""
        self.consecutive_failures += 1

    def reset(self) -> None:
        """Manual reset — clears the trip state. User-invokable."""
        self.consecutive_failures = 0


# ─── RecompactChain ───


@dataclass
class RecompactChain:
    """Detects two compactions firing too close together.

    If the loop just compacted and another compaction is being
    requested within `min_interval_s` AND `min_turns_apart` turns,
    something is wrong (probably the summary itself is huge or the
    model isn't actually shrinking the context). We refuse the
    second compaction and let the loop fall through to the
    blocking-limit path instead, which is more useful diagnostically.
    """

    min_interval_s: float = 30.0
    min_turns_apart: int = 3
    last_compact_at: float = 0.0
    last_compact_turn: int = -1

    def record(self, turn: int, *, at: float | None = None) -> None:
        """Note that a compaction just happened."""
        self.last_compact_at = at if at is not None else time.monotonic()
        self.last_compact_turn = turn

    def is_chained(self, turn: int, *, at: float | None = None) -> bool:
        """Would another compaction right now constitute a chain?"""
        if self.last_compact_turn < 0:
            return False
        now = at if at is not None else time.monotonic()
        time_close = (now - self.last_compact_at) < self.min_interval_s
        turns_close = (turn - self.last_compact_turn) < self.min_turns_apart
        return time_close and turns_close


# ─── BudgetTracker — the per-loop bundle ───


@dataclass
class BudgetTracker:
    """Bundle of (ContextBudget, CircuitBreaker, RecompactChain) +
    runtime stats. Owned by the loop, one per session.

    The loop reads `state(used)` to decide whether to compact, then
    notes the result via `compact_started/succeeded/failed`. Tests
    drive the methods directly with mocked token counts.
    """

    budget: ContextBudget = field(default_factory=ContextBudget)
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    recompact: RecompactChain = field(default_factory=RecompactChain)

    # Per-session stats
    compactions_total: int = 0
    compactions_failed: int = 0
    last_observed_tokens: int = 0
    peak_tokens: int = 0

    def observe(self, used: int) -> None:
        """Loop calls this each iteration to record current usage."""
        self.last_observed_tokens = used
        if used > self.peak_tokens:
            self.peak_tokens = used

    def should_attempt_compaction(self, used: int, turn: int) -> tuple[bool, str]:
        """Decide whether to fire compaction now.

        Returns (decision, reason). reason is "ok" if compaction
        should run, otherwise a human-readable refusal.
        """
        if not self.budget.should_autocompact(used):
            return (False, "under threshold")
        if self.circuit_breaker.tripped:
            return (False, "circuit breaker tripped")
        if self.recompact.is_chained(turn):
            return (False, "recompact chain detected")
        return (True, "ok")

    def note_compaction_success(self, turn: int) -> None:
        self.compactions_total += 1
        self.circuit_breaker.success()
        self.recompact.record(turn)

    def note_compaction_failure(self, turn: int) -> None:
        self.compactions_total += 1
        self.compactions_failed += 1
        self.circuit_breaker.fail()
        # Do NOT record the chain on failure — a failed compaction
        # didn't actually shrink anything, so we shouldn't refuse a
        # subsequent retry on chain grounds.
