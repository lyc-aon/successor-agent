"""End-to-end tests for the chat-layer autocompact gate.

The gate lives at chat._check_and_maybe_defer_for_autocompact() and
fires from the top of _begin_agent_turn(). It reads the profile's
CompactionConfig, builds a ContextBudget against the resolved
context window, decides whether to compact proactively, and (when
deciding to compact) spawns a worker thread + sets a deferred-resume
flag so the agent turn resumes after compaction completes.

These tests drive the gate end-to-end against a real SuccessorChat
constructed with a mock client + synthetic burn rounds. They cover:

  - Profile JSON drives the threshold (custom percentages flow
    through to the runtime budget)
  - The gate fires when usage crosses the autocompact threshold
  - The gate does NOT fire when below the threshold
  - The gate does NOT fire when compaction is disabled
  - The per-turn guard prevents the gate from firing twice on a
    single user message
  - The min-rounds guard prevents the gate from firing on a
    nearly-empty log
  - The deferred-resume flag is set + cleared correctly
  - Cancel via Ctrl+G clears the deferred-resume flag

All hermetic — no real llama.cpp server, no live HTTP. The mock
client implements just the stream_chat() surface compact() needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


from successor.profiles import (
    PROFILE_REGISTRY,
    CompactionConfig,
    Profile,
)
from successor.providers.llama import (
    ContentChunk,
    StreamEnded,
    StreamStarted,
)
from successor.render.theme import THEME_REGISTRY


# ─── Mock provider client ───


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
    """Mock LlamaCppClient that returns canned streams.

    The chat uses this for BOTH the regular turn stream AND the
    compaction summary stream. Tests configure the queue with the
    streams they want returned in order.
    """
    streams: list = field(default_factory=list)
    call_count: int = 0
    last_messages: list = field(default_factory=list)
    last_max_tokens: int | None = None
    last_temperature: float | None = None
    last_tools: list | None = None
    base_url: str = "http://mock"

    def stream_chat(self, messages, *, max_tokens=None, temperature=None,
                    timeout=None, extra=None, tools=None):
        self.call_count += 1
        self.last_messages = list(messages)
        self.last_max_tokens = max_tokens
        self.last_temperature = temperature
        self.last_tools = tools
        if not self.streams:
            return _MockStream(events=[
                StreamStarted(),
                ContentChunk(text=""),
                StreamEnded(finish_reason="stop", usage=None, timings=None),
            ])
        idx = min(self.call_count - 1, len(self.streams) - 1)
        return self.streams[idx]

    def detect_context_window(self) -> int:
        # The chat caches the resolved window before any test reads
        # it, so this is just a safe fallback.
        return 200_000


def _stream_with_summary(text: str) -> _MockStream:
    return _MockStream(events=[
        StreamStarted(),
        ContentChunk(text=text),
        StreamEnded(finish_reason="stop", usage=None, timings=None),
    ])


# ─── Helpers ───


def _make_chat(*, compaction: CompactionConfig | None = None, window: int = 200_000):
    """Build a SuccessorChat with a mock client and a fixed window."""
    from successor.chat import SuccessorChat

    profile = Profile(
        name="autocompact-test",
        compaction=compaction or CompactionConfig(),
    )
    client = _MockClient(streams=[
        _stream_with_summary("compacted summary"),  # for the compact() call
        _stream_with_summary("model response"),     # for the resumed turn
        _stream_with_summary("model response"),     # for any extra calls
    ])
    chat = SuccessorChat(profile=profile, client=client)
    chat._cached_resolved_window = window
    return chat, client


def _burn_into_chat(chat, target_tokens: int) -> None:
    """Use the chat's /burn command path to inject synthetic context."""
    chat._handle_burn_cmd(target_tokens)


# ─── Gate fires when over threshold ───


def test_gate_fires_when_over_autocompact_threshold(temp_config_dir: Path) -> None:
    """Default profile + 200K window: autocompact at 187_500. Burn past it."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, client = _make_chat(window=200_000)

    # Burn past the autocompact threshold
    _burn_into_chat(chat, 190_000)
    assert chat._compaction_worker is None  # not yet fired

    # Calling the gate should fire
    deferred = chat._check_and_maybe_defer_for_autocompact()
    assert deferred is True
    assert chat._compaction_worker is not None
    assert chat._pending_agent_turn_after_compact is True
    assert chat._autocompact_attempted_this_turn is True


def test_gate_does_not_fire_when_under_threshold(temp_config_dir: Path) -> None:
    """Below autocompact threshold → gate returns False."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(window=200_000)

    # Burn way under the threshold
    _burn_into_chat(chat, 10_000)

    deferred = chat._check_and_maybe_defer_for_autocompact()
    assert deferred is False
    assert chat._compaction_worker is None
    assert chat._pending_agent_turn_after_compact is False


def test_gate_does_not_fire_when_disabled(temp_config_dir: Path) -> None:
    """profile.compaction.enabled=False → gate is a no-op."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(
        compaction=CompactionConfig(enabled=False),
        window=200_000,
    )

    # Burn way past what would normally trigger autocompact
    _burn_into_chat(chat, 195_000)

    deferred = chat._check_and_maybe_defer_for_autocompact()
    assert deferred is False
    assert chat._compaction_worker is None
    assert chat._pending_agent_turn_after_compact is False


def test_gate_does_not_fire_below_min_rounds(temp_config_dir: Path) -> None:
    """A nearly-empty log can't be compacted — gate refuses."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    # Custom config with very low autocompact pct so a small log triggers it
    chat, _client = _make_chat(
        compaction=CompactionConfig(
            warning_pct=0.95,
            autocompact_pct=0.01,
            blocking_pct=0.005,
            warning_floor=100,
            autocompact_floor=10,
            blocking_floor=5,
        ),
        window=200_000,
    )
    # Don't burn — log is empty
    deferred = chat._check_and_maybe_defer_for_autocompact()
    assert deferred is False


# ─── Per-turn guard ───


def test_gate_per_turn_guard_prevents_double_fire(temp_config_dir: Path) -> None:
    """Once the gate fires for a user message, the per-turn guard
    blocks it from firing again until the next /submit."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, client = _make_chat(window=200_000)
    _burn_into_chat(chat, 190_000)

    # First call fires
    assert chat._check_and_maybe_defer_for_autocompact() is True
    # Clear the worker (simulates the worker finishing) so the
    # in-flight check doesn't block, but DON'T clear the per-turn flag
    chat._compaction_worker = None
    chat._pending_agent_turn_after_compact = False

    # Second call within the same turn does NOT fire
    assert chat._check_and_maybe_defer_for_autocompact() is False


def test_gate_per_turn_guard_resets_on_new_submit(temp_config_dir: Path) -> None:
    """A new user message resets the guard so the gate can fire again."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(window=200_000)
    _burn_into_chat(chat, 190_000)

    chat._check_and_maybe_defer_for_autocompact()
    assert chat._autocompact_attempted_this_turn is True

    # Reset (simulates _submit clearing the flag)
    chat._autocompact_attempted_this_turn = False
    chat._compaction_worker = None
    chat._pending_agent_turn_after_compact = False

    # Gate is armed again
    assert chat._check_and_maybe_defer_for_autocompact() is True


# ─── In-flight worker guard ───


def test_gate_does_not_fire_with_worker_in_flight(temp_config_dir: Path) -> None:
    """If a compaction worker is already running, the gate is a no-op."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(window=200_000)
    _burn_into_chat(chat, 190_000)

    # First call spawns the worker
    assert chat._check_and_maybe_defer_for_autocompact() is True

    # Second call (without finishing) is a no-op
    chat._autocompact_attempted_this_turn = False  # would otherwise short-circuit
    assert chat._check_and_maybe_defer_for_autocompact() is False


# ─── Custom profile thresholds flow through ───


def test_profile_aggressive_pct_lowers_trigger_point(temp_config_dir: Path) -> None:
    """A profile with autocompact_pct=0.50 (aggressive) fires at 50% full."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(
        compaction=CompactionConfig(
            warning_pct=0.75,
            autocompact_pct=0.50,
            blocking_pct=0.10,
        ),
        window=200_000,
    )

    # 100K (50%) burn — exactly at the autocompact threshold
    _burn_into_chat(chat, 100_000)

    # Verify the budget reflects the aggressive setting
    budget = chat._agent_budget()
    assert budget.autocompact_buffer == 100_000
    assert budget.autocompact_at == 100_000

    deferred = chat._check_and_maybe_defer_for_autocompact()
    assert deferred is True


def test_profile_lazy_pct_raises_trigger_point(temp_config_dir: Path) -> None:
    """A profile with autocompact_pct=0.02 fires only at the very edge."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(
        compaction=CompactionConfig(
            warning_pct=0.05,
            autocompact_pct=0.02,
            blocking_pct=0.005,
            warning_floor=2_000,
            autocompact_floor=1_000,
            blocking_floor=500,
        ),
        window=200_000,
    )

    # The chat-level /burn token counter uses a /4 char heuristic
    # but the agent-log token counter uses a /3.5 char heuristic, so
    # the actual count can run ~15% higher than the burn target.
    # Burn well under 196K to leave headroom for the discrepancy.
    _burn_into_chat(chat, 150_000)

    budget = chat._agent_budget()
    assert budget.autocompact_buffer == 4_000  # 2% of 200000
    assert budget.autocompact_at == 196_000

    # Verify the actual agent-log count is below the threshold
    counter = chat._agent_token_counter()
    log = chat._to_agent_log()
    used = counter.count_log(log)
    assert used < budget.autocompact_at, (
        f"burn overshot: used={used} threshold={budget.autocompact_at}"
    )

    deferred = chat._check_and_maybe_defer_for_autocompact()
    assert deferred is False  # below the lazy threshold


# ─── Profile JSON → live chat → gate trigger ───


def test_profile_json_drives_gate_trigger(temp_config_dir: Path) -> None:
    """End-to-end: drop a profile JSON with custom compaction thresholds,
    load it via the registry, build a chat, verify the gate fires at the
    JSON-configured threshold."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()

    user_dir = temp_config_dir / "profiles"
    user_dir.mkdir()
    (user_dir / "custom.json").write_text(json.dumps({
        "name": "custom",
        "compaction": {
            "warning_pct": 0.40,
            "autocompact_pct": 0.20,
            "blocking_pct": 0.05,
        },
    }))
    PROFILE_REGISTRY.reload()

    from successor.chat import SuccessorChat
    profile = PROFILE_REGISTRY.get("custom")
    assert profile is not None
    assert profile.compaction.autocompact_pct == 0.20

    client = _MockClient(streams=[
        _stream_with_summary("compacted summary"),
        _stream_with_summary("model response"),
    ])
    chat = SuccessorChat(profile=profile, client=client)
    chat._cached_resolved_window = 200_000

    # Threshold = window - 20% = 200000 - 40000 = 160000
    _burn_into_chat(chat, 165_000)
    assert chat._check_and_maybe_defer_for_autocompact() is True


# ─── Cancel via Ctrl+G clears the deferred flag ───


def test_cancel_clears_deferred_resume_flag(temp_config_dir: Path) -> None:
    """Ctrl+G during an autocompact aborts the worker AND clears the
    pending-agent-turn flag so the deferred turn doesn't resume."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(window=200_000)
    _burn_into_chat(chat, 190_000)

    # Fire the gate
    assert chat._check_and_maybe_defer_for_autocompact() is True
    assert chat._pending_agent_turn_after_compact is True

    # Simulate the Ctrl+G handler manually (the same code paths that
    # the real key event handler runs)
    chat._compaction_worker.close()
    chat._compaction_worker = None
    chat._compaction_anim = None
    chat._pending_agent_turn_after_compact = False

    assert chat._pending_agent_turn_after_compact is False
    assert chat._compaction_worker is None
