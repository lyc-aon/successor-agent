"""Tests for CompactionConfig — the per-profile autocompactor knobs.

Three layers of coverage:

  1. Pure dataclass tests — defaults, validation, ordering, ranges,
     buffers_for_window math, the floor edge case at tiny windows.

  2. JSON round-trip tests — parse_profile_file with no compaction
     field, partial compaction field, full compaction field, and
     malformed values that should fall back to defaults.

  3. Profile integration — Profile dataclass picks up the field
     correctly, frozen-dataclass behavior works, equality holds.

Hermetic via temp_config_dir; no I/O outside the temp dir.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from successor.profiles import (
    PROFILE_REGISTRY,
    CompactionConfig,
    Profile,
    parse_profile_file,
)


# ─── CompactionConfig defaults + validation ───


def test_default_compaction_config_is_valid() -> None:
    c = CompactionConfig()
    # Defaults satisfy ordering invariants
    assert c.warning_pct > c.autocompact_pct > c.blocking_pct >= 0
    assert c.warning_floor >= c.autocompact_floor >= c.blocking_floor >= 0
    # Defaults are sensible
    assert c.enabled is True
    assert c.keep_recent_rounds >= 1
    assert c.summary_max_tokens >= 256


def test_compaction_config_rejects_warning_below_autocompact() -> None:
    with pytest.raises(ValueError, match="thresholds out of order"):
        CompactionConfig(warning_pct=0.05, autocompact_pct=0.10)


def test_compaction_config_rejects_negative_pct() -> None:
    with pytest.raises(ValueError, match="warning_pct must be in"):
        CompactionConfig(warning_pct=-0.01)
    with pytest.raises(ValueError, match="autocompact_pct must be in"):
        CompactionConfig(autocompact_pct=-0.01)
    with pytest.raises(ValueError, match="blocking_pct must be in"):
        CompactionConfig(blocking_pct=-0.01)


def test_compaction_config_rejects_pct_above_one() -> None:
    with pytest.raises(ValueError, match="warning_pct must be in"):
        CompactionConfig(warning_pct=1.01)


def test_compaction_config_rejects_floors_out_of_order() -> None:
    with pytest.raises(ValueError, match="floors out of order"):
        CompactionConfig(
            warning_floor=1_000,
            autocompact_floor=2_000,  # bigger than warning — invalid
            blocking_floor=500,
        )


def test_compaction_config_rejects_negative_floor() -> None:
    with pytest.raises(ValueError, match="warning_floor must be"):
        CompactionConfig(warning_floor=-1)


def test_compaction_config_rejects_keep_recent_rounds_zero() -> None:
    with pytest.raises(ValueError, match="keep_recent_rounds"):
        CompactionConfig(keep_recent_rounds=0)


def test_compaction_config_rejects_tiny_summary_max_tokens() -> None:
    with pytest.raises(ValueError, match="summary_max_tokens"):
        CompactionConfig(summary_max_tokens=100)


# ─── buffers_for_window — the percentage scaling math ───


def test_buffers_for_window_at_default_window() -> None:
    """At 262K window, defaults produce predictable buffer sizes."""
    c = CompactionConfig()
    warn, auto, block = c.buffers_for_window(262_144)
    # 12.5% of 262144 = 32768
    assert warn == 32_768
    # 6.25% of 262144 = 16384
    assert auto == 16_384
    # 1.5625% of 262144 = 4096
    assert block == 4_096


def test_buffers_for_window_at_huge_window() -> None:
    """At 1M window, percentages dominate (no floor activation)."""
    c = CompactionConfig()
    warn, auto, block = c.buffers_for_window(1_000_000)
    # All values from percentages, none from floors
    assert warn == 125_000  # 12.5%
    assert auto == 62_500   # 6.25%
    assert block == 15_625  # 1.5625%
    # Floors are inactive
    assert warn > c.warning_floor
    assert auto > c.autocompact_floor
    assert block > c.blocking_floor


def test_buffers_for_window_at_tiny_window_floors_kick_in() -> None:
    """At 8K window, percentages would be too small — floors take over."""
    c = CompactionConfig()
    warn, auto, block = c.buffers_for_window(8_000)
    # 12.5% of 8000 = 1000, but floor is 8000 → use 8000
    assert warn == 8_000
    # 6.25% of 8000 = 500, but floor is 4000 → use 4000
    assert auto == 4_000
    # 1.5625% of 8000 = 125, but floor is 1000 → use 1000
    assert block == 1_000


def test_buffers_for_window_at_50k_test_window() -> None:
    """50K test scaffold (same as loop.py docstring example)."""
    c = CompactionConfig()
    warn, auto, block = c.buffers_for_window(50_000)
    # 12.5% of 50000 = 6250 → warning floor 8000 wins
    assert warn == 8_000
    # 6.25% of 50000 = 3125 → autocompact floor 4000 wins
    assert auto == 4_000
    # 1.5625% of 50000 = 781 → blocking floor 1000 wins
    assert block == 1_000


def test_buffers_for_window_with_custom_pcts() -> None:
    """Custom percentages flow through to buffer math."""
    c = CompactionConfig(
        warning_pct=0.20,
        autocompact_pct=0.10,
        blocking_pct=0.05,
        warning_floor=1_000,
        autocompact_floor=500,
        blocking_floor=100,
    )
    warn, auto, block = c.buffers_for_window(100_000)
    assert warn == 20_000
    assert auto == 10_000
    assert block == 5_000


def test_buffers_satisfy_invariant_at_pathological_window() -> None:
    """At a window so small the floors collapse, the spread guarantee
    keeps the invariant valid so ContextBudget(...) won't reject."""
    c = CompactionConfig(
        warning_floor=1_000,
        autocompact_floor=1_000,  # equal — would fail invariant naively
        blocking_floor=1_000,
    )
    warn, auto, block = c.buffers_for_window(2_000)
    # The spread guarantee bumps them apart
    assert warn > auto > block


# ─── from_dict — lenient JSON parsing ───


def test_from_dict_empty_returns_defaults() -> None:
    c = CompactionConfig.from_dict({})
    assert c == CompactionConfig()


def test_from_dict_none_returns_defaults() -> None:
    c = CompactionConfig.from_dict(None)
    assert c == CompactionConfig()


def test_from_dict_wrong_type_returns_defaults() -> None:
    c = CompactionConfig.from_dict("not a dict")  # type: ignore
    assert c == CompactionConfig()


def test_from_dict_partial_merges_with_defaults() -> None:
    """Setting one field overrides only that field; rest use defaults."""
    c = CompactionConfig.from_dict({"autocompact_pct": 0.10})
    assert c.autocompact_pct == 0.10
    # Defaults preserved
    assert c.warning_pct == CompactionConfig().warning_pct
    assert c.blocking_pct == CompactionConfig().blocking_pct
    assert c.enabled is True


def test_from_dict_full_round_trip() -> None:
    """A fully-populated dict round-trips to_dict() → from_dict()."""
    original = CompactionConfig(
        warning_pct=0.15,
        autocompact_pct=0.08,
        blocking_pct=0.02,
        warning_floor=10_000,
        autocompact_floor=5_000,
        blocking_floor=1_500,
        enabled=False,
        keep_recent_rounds=10,
        summary_max_tokens=8_000,
    )
    rebuilt = CompactionConfig.from_dict(original.to_dict())
    assert rebuilt == original


def test_from_dict_invalid_pct_falls_back_to_default() -> None:
    """Out-of-range pct values use defaults instead of raising."""
    c = CompactionConfig.from_dict({"warning_pct": 5.0})
    assert c.warning_pct == CompactionConfig().warning_pct


def test_from_dict_invariant_violation_falls_back() -> None:
    """warning_pct < autocompact_pct → invariant violation → safe defaults."""
    # Behavior fields should still be honored if they're valid
    c = CompactionConfig.from_dict({
        "warning_pct": 0.05,
        "autocompact_pct": 0.10,  # bigger than warning — invalid
        "enabled": False,
        "keep_recent_rounds": 8,
    })
    # The threshold pcts fell back to defaults
    assert c.warning_pct == CompactionConfig().warning_pct
    assert c.autocompact_pct == CompactionConfig().autocompact_pct
    # The behavior fields survived because they're independent
    assert c.enabled is False
    assert c.keep_recent_rounds == 8


def test_from_dict_negative_int_field_falls_back() -> None:
    """Negative ints fall back to defaults (range filter)."""
    c = CompactionConfig.from_dict({"keep_recent_rounds": -5})
    assert c.keep_recent_rounds == CompactionConfig().keep_recent_rounds


def test_from_dict_bool_field_must_be_bool() -> None:
    """A truthy int doesn't count as bool — must be a real bool."""
    c = CompactionConfig.from_dict({"enabled": 1})  # int, not bool
    # Fell back to default
    assert c.enabled is True


# ─── Profile integration ───


def test_profile_default_has_compaction_config() -> None:
    """A Profile constructed with no compaction has the default."""
    p = Profile(name="test")
    assert isinstance(p.compaction, CompactionConfig)
    assert p.compaction.enabled is True


def test_profile_with_custom_compaction() -> None:
    """A Profile can be constructed with a custom compaction config.

    Note: any value bigger than the default warning_pct (0.125) needs
    a matching warning_pct override to keep the invariant valid.
    """
    cfg = CompactionConfig(warning_pct=0.30, autocompact_pct=0.20)
    p = Profile(name="test", compaction=cfg)
    assert p.compaction.autocompact_pct == 0.20


def test_profile_is_still_frozen() -> None:
    """Adding the compaction field didn't break frozen-dataclass."""
    p = Profile(name="test")
    with pytest.raises(Exception):  # FrozenInstanceError
        p.compaction = CompactionConfig()  # type: ignore


# ─── parse_profile_file integration ───


def test_parse_profile_no_compaction_field_uses_defaults(tmp_path: Path) -> None:
    """A profile JSON without a `compaction` key uses default config."""
    p = tmp_path / "minimal.json"
    p.write_text(json.dumps({"name": "minimal"}))

    profile = parse_profile_file(p)
    assert profile is not None
    assert profile.compaction == CompactionConfig()


def test_parse_profile_partial_compaction_merges(tmp_path: Path) -> None:
    """A profile with `compaction.autocompact_pct` only overrides that one.

    The override must satisfy the invariant against the *defaults* it
    merges with — `autocompact_pct=0.10` is fine because the default
    `warning_pct=0.125` is still bigger.
    """
    p = tmp_path / "partial.json"
    p.write_text(json.dumps({
        "name": "partial",
        "compaction": {"autocompact_pct": 0.10},
    }))

    profile = parse_profile_file(p)
    assert profile is not None
    assert profile.compaction.autocompact_pct == 0.10
    # Other fields are defaults
    assert profile.compaction.warning_pct == CompactionConfig().warning_pct
    assert profile.compaction.enabled is True


def test_parse_profile_full_compaction_round_trips(tmp_path: Path) -> None:
    """A profile with every compaction field set parses every field."""
    payload = {
        "name": "full-compact",
        "compaction": {
            "warning_pct": 0.20,
            "autocompact_pct": 0.10,
            "blocking_pct": 0.04,
            "warning_floor": 12_000,
            "autocompact_floor": 6_000,
            "blocking_floor": 2_000,
            "enabled": False,
            "keep_recent_rounds": 8,
            "summary_max_tokens": 12_000,
        },
    }
    p = tmp_path / "full.json"
    p.write_text(json.dumps(payload))

    profile = parse_profile_file(p)
    assert profile is not None
    c = profile.compaction
    assert c.warning_pct == 0.20
    assert c.autocompact_pct == 0.10
    assert c.blocking_pct == 0.04
    assert c.warning_floor == 12_000
    assert c.autocompact_floor == 6_000
    assert c.blocking_floor == 2_000
    assert c.enabled is False
    assert c.keep_recent_rounds == 8
    assert c.summary_max_tokens == 12_000


def test_parse_profile_compaction_wrong_type_uses_defaults(tmp_path: Path) -> None:
    """A `compaction` field that isn't a dict falls back to defaults."""
    p = tmp_path / "bad-compact.json"
    p.write_text(json.dumps({
        "name": "bad",
        "compaction": "not a dict",
    }))

    profile = parse_profile_file(p)
    assert profile is not None
    assert profile.compaction == CompactionConfig()


def test_parse_profile_compaction_invalid_values_use_safe_fallbacks(tmp_path: Path) -> None:
    """Invalid compaction values are silently clamped (lenient policy)."""
    p = tmp_path / "invalid.json"
    p.write_text(json.dumps({
        "name": "invalid",
        "compaction": {
            "warning_pct": 0.01,
            "autocompact_pct": 0.50,  # bigger than warning — invariant violation
        },
    }))

    profile = parse_profile_file(p)
    # Profile loads successfully (lenient policy)
    assert profile is not None
    # Compaction fell back to safe defaults for the threshold pcts
    assert profile.compaction == CompactionConfig()


def test_parse_profile_save_load_idempotent(tmp_path: Path) -> None:
    """Building a profile, serializing it, and re-parsing produces an
    identical compaction config."""
    cfg = CompactionConfig(
        warning_pct=0.18,
        autocompact_pct=0.09,
        keep_recent_rounds=7,
    )
    payload = {
        "name": "roundtrip",
        "compaction": cfg.to_dict(),
    }
    p = tmp_path / "roundtrip.json"
    p.write_text(json.dumps(payload))

    profile = parse_profile_file(p)
    assert profile is not None
    assert profile.compaction == cfg


def test_parse_profile_partial_compaction_invariant_violation(tmp_path: Path) -> None:
    """Setting only autocompact_pct higher than the DEFAULT warning_pct
    triggers the invariant fallback at construction time."""
    p = tmp_path / "partial-bad.json"
    p.write_text(json.dumps({
        "name": "partial-bad",
        "compaction": {"autocompact_pct": 0.30},  # > default warning_pct of 0.125
    }))

    profile = parse_profile_file(p)
    assert profile is not None
    # The construction failed → safe defaults used
    assert profile.compaction == CompactionConfig()
