"""Edge-case E2E tests for the autocompact gate.

Covers:
  - Tiny context windows where the floor system kicks in
  - Huge context windows where percentages dominate
  - Disabled compaction (proactive autocompact off)
  - Invalid profile JSON loading (lenient policy clamps to defaults)
  - The post-compact assertion surfacing through the chat's compaction
    worker result handler

Hermetic via temp_config_dir + a mock client. No live llama.cpp.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


from successor.profiles import (
    PROFILE_REGISTRY,
    CompactionConfig,
    Profile,
    parse_profile_file,
)
from successor.providers.llama import (
    ContentChunk,
    StreamEnded,
    StreamStarted,
)
from successor.render.theme import THEME_REGISTRY


# ─── Mock client (mirrors test_chat_autocompact_gate.py) ───


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
    base_url: str = "http://mock"

    def stream_chat(self, messages, *, max_tokens=None, temperature=None,
                    timeout=None, extra=None, tools=None):
        self.call_count += 1
        if not self.streams:
            return _MockStream(events=[
                StreamStarted(),
                ContentChunk(text=""),
                StreamEnded(finish_reason="stop", usage=None, timings=None),
            ])
        idx = min(self.call_count - 1, len(self.streams) - 1)
        return self.streams[idx]

    def detect_context_window(self) -> int:
        return 200_000


def _stream_with_summary(text: str) -> _MockStream:
    return _MockStream(events=[
        StreamStarted(),
        ContentChunk(text=text),
        StreamEnded(finish_reason="stop", usage=None, timings=None),
    ])


def _make_chat(*, profile: Profile, window: int):
    from successor.chat import SuccessorChat
    client = _MockClient(streams=[
        _stream_with_summary("compacted summary"),
        _stream_with_summary("model response"),
    ])
    chat = SuccessorChat(profile=profile, client=client)
    chat._cached_resolved_window = window
    return chat, client


# ─── Tiny window — floors take over ───


def test_tiny_8k_window_floors_drive_thresholds(temp_config_dir: Path) -> None:
    """At 8K window, percentages would be tiny (12.5% of 8K = 1000)
    but floors enforce a usable minimum."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(profile=Profile(name="tiny"), window=8_000)
    budget = chat._agent_budget()

    # All floors are active
    assert budget.warning_buffer == 8_000
    assert budget.autocompact_buffer == 4_000
    assert budget.blocking_buffer == 1_000

    # Threshold positions reflect the floor-driven buffers
    assert budget.warning_at == 0      # warning at fill = 0%
    assert budget.autocompact_at == 4_000   # at fill ≈ 50%
    assert budget.blocking_at == 7_000      # at fill ≈ 87.5%


def test_tiny_window_custom_lower_floors(temp_config_dir: Path) -> None:
    """A profile can lower the floors so a tiny window has more
    usable headroom."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile = Profile(
        name="tiny-custom",
        compaction=CompactionConfig(
            warning_pct=0.20,
            autocompact_pct=0.10,
            blocking_pct=0.05,
            warning_floor=400,
            autocompact_floor=200,
            blocking_floor=100,
        ),
    )
    chat, _client = _make_chat(profile=profile, window=8_000)
    budget = chat._agent_budget()

    # Percentages dominate the lowered floors
    assert budget.warning_buffer == 1_600   # 20% of 8000
    assert budget.autocompact_buffer == 800   # 10% of 8000
    assert budget.blocking_buffer == 400      # 5% of 8000


# ─── Huge window — percentages dominate ───


def test_huge_1m_window_percentages_dominate(temp_config_dir: Path) -> None:
    """At 1M window, the percentages produce buffers far above the floors."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(profile=Profile(name="huge"), window=1_000_000)
    budget = chat._agent_budget()

    assert budget.warning_buffer == 125_000
    assert budget.autocompact_buffer == 62_500
    assert budget.blocking_buffer == 15_625


def test_huge_window_state_transitions(temp_config_dir: Path) -> None:
    """At 1M window, the state machine transitions at the expected
    token counts derived from the default percentages."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(profile=Profile(name="huge"), window=1_000_000)
    budget = chat._agent_budget()

    # warning_at = 1_000_000 - 125_000 = 875_000
    # autocompact_at = 1_000_000 - 62_500 = 937_500
    # blocking_at = 1_000_000 - 15_625 = 984_375
    assert budget.state(500_000) == "ok"
    assert budget.state(874_999) == "ok"
    assert budget.state(875_000) == "warning"
    assert budget.state(937_500) == "autocompact"
    assert budget.state(984_375) == "blocking"


# ─── Disabled compaction ───


def test_disabled_compaction_gate_never_fires(temp_config_dir: Path) -> None:
    """profile.compaction.enabled=False → autocompact gate is a no-op."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(
        profile=Profile(
            name="disabled",
            compaction=CompactionConfig(enabled=False),
        ),
        window=200_000,
    )

    # Burn so far past the threshold there's no ambiguity
    chat._handle_burn_cmd(195_000)

    # Gate is a no-op
    deferred = chat._check_and_maybe_defer_for_autocompact()
    assert deferred is False
    assert chat._compaction_worker is None


def test_disabled_compaction_blocking_buffer_still_applies(
    temp_config_dir: Path,
) -> None:
    """Disabling autocompact does NOT disable the blocking limit. The
    chat will still refuse to send a request that exceeds the blocking
    threshold — that's a separate, harder safety check."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(
        profile=Profile(
            name="disabled",
            compaction=CompactionConfig(enabled=False),
        ),
        window=200_000,
    )
    budget = chat._agent_budget()

    # Blocking still calculated
    assert budget.blocking_buffer > 0
    assert budget.over_blocking_limit(199_000) is True
    assert budget.over_blocking_limit(50_000) is False


def test_disabled_compaction_manual_compact_still_works(
    temp_config_dir: Path,
) -> None:
    """The /compact slash command path is independent of the
    autocompact enabled flag — disabling autocompact does NOT
    block manual compaction."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(
        profile=Profile(
            name="disabled",
            compaction=CompactionConfig(enabled=False),
        ),
        window=200_000,
    )

    chat._handle_burn_cmd(190_000)
    # Manual /compact should still spawn a worker
    chat._handle_compact_cmd()
    assert chat._compaction_worker is not None
    # Manual compaction does NOT set the deferred-resume flag
    assert chat._pending_agent_turn_after_compact is False


def test_manual_compact_allows_two_large_rounds_without_burn(
    temp_config_dir: Path,
) -> None:
    """A large two-round conversation is already compactable.

    Manual /compact should not require synthetic burn rounds just
    because the chat history is structurally small.
    """
    from successor.chat import _Message

    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    chat, _client = _make_chat(profile=Profile(name="manual-two-round"), window=200_000)
    chat.messages = [
        _Message("user", "q0 " + ("alpha " * 8000)),
        _Message("successor", "a0 " + ("beta " * 8000)),
        _Message("user", "q1 " + ("gamma " * 8000)),
        _Message("successor", "a1 " + ("delta " * 8000)),
    ]

    chat._handle_compact_cmd()

    assert chat._compaction_worker is not None


# ─── Invalid profile JSON ───


def test_invalid_profile_json_uses_safe_defaults(temp_config_dir: Path) -> None:
    """A profile JSON with broken compaction values still LOADS — it
    just uses safe defaults instead of rejecting at load time. This is
    the lenient-load policy the rest of the parser uses."""
    p = temp_config_dir / "broken.json"
    p.write_text(json.dumps({
        "name": "broken",
        "compaction": {
            "warning_pct": "not a number",
            "autocompact_pct": -1.5,
            "blocking_pct": 5.0,
        },
    }))

    profile = parse_profile_file(p)
    assert profile is not None
    # Compaction fell back to defaults
    assert profile.compaction == CompactionConfig()


def test_partial_invalid_compaction_keeps_valid_fields(temp_config_dir: Path) -> None:
    """When the threshold pcts are invalid but the behavior fields
    are valid, the behavior fields are honored."""
    p = temp_config_dir / "mixed.json"
    p.write_text(json.dumps({
        "name": "mixed",
        "compaction": {
            "warning_pct": 0.01,
            "autocompact_pct": 0.50,
            "enabled": False,
            "keep_recent_rounds": 12,
        },
    }))

    profile = parse_profile_file(p)
    assert profile is not None
    # Threshold pcts fell back
    assert profile.compaction.warning_pct == CompactionConfig().warning_pct
    # Behavior fields survived
    assert profile.compaction.enabled is False
    assert profile.compaction.keep_recent_rounds == 12


def test_invalid_profile_json_chat_construction_succeeds(
    temp_config_dir: Path,
) -> None:
    """A chat constructed with a profile that had invalid compaction
    JSON still builds a working budget (using the safe defaults)."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    user_dir = temp_config_dir / "profiles"
    user_dir.mkdir()
    (user_dir / "broken.json").write_text(json.dumps({
        "name": "broken",
        "compaction": {
            "warning_pct": "garbage",
            "autocompact_pct": -10,
        },
    }))
    PROFILE_REGISTRY.reload()

    profile = PROFILE_REGISTRY.get("broken")
    assert profile is not None

    chat, _client = _make_chat(profile=profile, window=200_000)
    budget = chat._agent_budget()
    # Default-derived buffer values
    assert budget.warning_buffer == 25_000   # 12.5% of 200000
    assert budget.autocompact_buffer == 12_500  # 6.25% of 200000
    assert budget.blocking_buffer == 3_125     # 1.5625% of 200000


# ─── Profile reload picks up changes ───


def test_profile_reload_picks_up_compaction_changes(temp_config_dir: Path) -> None:
    """Editing a profile's compaction JSON and reloading the registry
    produces a new Profile with updated values."""
    PROFILE_REGISTRY.reload()
    user_dir = temp_config_dir / "profiles"
    user_dir.mkdir()

    (user_dir / "evolving.json").write_text(json.dumps({
        "name": "evolving",
        "compaction": {"autocompact_pct": 0.10},
    }))
    PROFILE_REGISTRY.reload()
    p1 = PROFILE_REGISTRY.get("evolving")
    assert p1 is not None
    assert p1.compaction.autocompact_pct == 0.10

    # Edit the file
    (user_dir / "evolving.json").write_text(json.dumps({
        "name": "evolving",
        "compaction": {"autocompact_pct": 0.05, "enabled": False},
    }))
    PROFILE_REGISTRY.reload()
    p2 = PROFILE_REGISTRY.get("evolving")
    assert p2 is not None
    assert p2.compaction.autocompact_pct == 0.05
    assert p2.compaction.enabled is False


# ─── Idempotent JSON round trip via the save flow ───


def test_config_menu_save_round_trip_compaction(temp_config_dir: Path) -> None:
    """The config menu's _profile_to_json_dict produces a JSON file
    that, when re-parsed via parse_profile_file, yields an identical
    CompactionConfig — no information loss in the round trip."""
    from successor.wizard.config import _profile_to_json_dict

    profile = Profile(
        name="roundtrip",
        compaction=CompactionConfig(
            warning_pct=0.20,
            autocompact_pct=0.10,
            blocking_pct=0.04,
            warning_floor=12_000,
            autocompact_floor=6_000,
            blocking_floor=2_000,
            enabled=False,
            keep_recent_rounds=8,
            summary_max_tokens=12_000,
        ),
    )

    serialized = _profile_to_json_dict(profile)
    p = temp_config_dir / "rt.json"
    p.write_text(json.dumps(serialized))

    parsed = parse_profile_file(p)
    assert parsed is not None
    assert parsed.compaction == profile.compaction
