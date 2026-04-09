"""Tests for chat._agent_budget percentage scaling.

Verifies that chat.SuccessorChat._agent_budget() correctly derives
ContextBudget buffer values from the active profile's CompactionConfig
across the full range of plausible context window sizes:

  8K     pathological floor case (every floor activates)
  50K    test scaffold size
  128K   GPT-4 / Claude Sonnet
  200K   Claude Opus
  262K   default qwen3.5 mid-grade
  1M     Claude 1M context
  2M     hypothetical future model

The chat exposes _cached_resolved_window so tests can short-circuit
the provider detection step and pin the resolved window directly.
This is the same seam the production chat uses for caching.

These tests run against a real SuccessorChat constructed with a
custom Profile — no mocks, no monkeypatching. The chat doesn't need
a live client for budget construction.
"""

from __future__ import annotations

from pathlib import Path


from successor.profiles import (
    CompactionConfig,
    PROFILE_REGISTRY,
    Profile,
)
from successor.render.theme import THEME_REGISTRY


def _make_chat_with_window(profile: Profile, window: int):
    """Construct a SuccessorChat with a fixed resolved context window.

    Bypasses provider detection by pre-populating the cache. The
    resulting chat is otherwise normal — same _agent_budget() path
    as production.
    """
    from successor.chat import SuccessorChat

    chat = SuccessorChat(profile=profile)
    chat._cached_resolved_window = window
    return chat


# ─── Default profile, varying window sizes ───


def test_agent_budget_at_8k_window(temp_config_dir: Path) -> None:
    """8K window — every floor activates."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile = Profile(name="test")  # default compaction
    chat = _make_chat_with_window(profile, 8_000)
    budget = chat._agent_budget()

    assert budget.window == 8_000
    # All floors active at this size
    assert budget.warning_buffer == 8_000
    assert budget.autocompact_buffer == 4_000
    assert budget.blocking_buffer == 1_000
    # Invariant: floors are spread apart by 1+ tokens
    assert budget.warning_buffer > budget.autocompact_buffer > budget.blocking_buffer


def test_agent_budget_at_50k_window(temp_config_dir: Path) -> None:
    """50K window — floors still dominate (12.5% of 50K = 6250 < 8000 floor)."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile = Profile(name="test")
    chat = _make_chat_with_window(profile, 50_000)
    budget = chat._agent_budget()

    assert budget.window == 50_000
    assert budget.warning_buffer == 8_000   # floor
    assert budget.autocompact_buffer == 4_000  # floor
    assert budget.blocking_buffer == 1_000  # floor


def test_agent_budget_at_128k_window(temp_config_dir: Path) -> None:
    """128K window — percentages start to dominate."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile = Profile(name="test")
    chat = _make_chat_with_window(profile, 128_000)
    budget = chat._agent_budget()

    assert budget.window == 128_000
    # 12.5% of 128000 = 16000 (above floor of 8000)
    assert budget.warning_buffer == 16_000
    # 6.25% of 128000 = 8000 (above floor of 4000)
    assert budget.autocompact_buffer == 8_000
    # 1.5625% of 128000 = 2000 (above floor of 1000)
    assert budget.blocking_buffer == 2_000


def test_agent_budget_at_200k_window(temp_config_dir: Path) -> None:
    """200K window — Claude Opus standard."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile = Profile(name="test")
    chat = _make_chat_with_window(profile, 200_000)
    budget = chat._agent_budget()

    assert budget.window == 200_000
    assert budget.warning_buffer == 25_000
    assert budget.autocompact_buffer == 12_500
    assert budget.blocking_buffer == 3_125


def test_agent_budget_at_262k_window(temp_config_dir: Path) -> None:
    """262K window — qwen3.5 mid-grade default."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile = Profile(name="test")
    chat = _make_chat_with_window(profile, 262_144)
    budget = chat._agent_budget()

    assert budget.window == 262_144
    assert budget.warning_buffer == 32_768
    assert budget.autocompact_buffer == 16_384
    assert budget.blocking_buffer == 4_096


def test_agent_budget_at_1m_window(temp_config_dir: Path) -> None:
    """1M window — Claude 1M context."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile = Profile(name="test")
    chat = _make_chat_with_window(profile, 1_000_000)
    budget = chat._agent_budget()

    assert budget.window == 1_000_000
    assert budget.warning_buffer == 125_000
    assert budget.autocompact_buffer == 62_500
    assert budget.blocking_buffer == 15_625
    # Floors are inactive — values came from percentages
    assert budget.warning_buffer > 8_000
    assert budget.autocompact_buffer > 4_000
    assert budget.blocking_buffer > 1_000


def test_agent_budget_at_2m_window(temp_config_dir: Path) -> None:
    """2M window — hypothetical future model. Just makes sure scaling
    keeps working at larger sizes (no overflow, no surprises)."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile = Profile(name="test")
    chat = _make_chat_with_window(profile, 2_000_000)
    budget = chat._agent_budget()

    assert budget.window == 2_000_000
    assert budget.warning_buffer == 250_000
    assert budget.autocompact_buffer == 125_000
    assert budget.blocking_buffer == 31_250


# ─── Custom profile percentages ───


def test_agent_budget_with_custom_aggressive_pcts(temp_config_dir: Path) -> None:
    """A profile that compacts aggressively at 25% / 50% / 75% headroom."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile = Profile(
        name="aggressive",
        compaction=CompactionConfig(
            warning_pct=0.50,
            autocompact_pct=0.25,
            blocking_pct=0.10,
        ),
    )
    chat = _make_chat_with_window(profile, 200_000)
    budget = chat._agent_budget()

    assert budget.warning_buffer == 100_000
    assert budget.autocompact_buffer == 50_000
    assert budget.blocking_buffer == 20_000
    # Triggers EARLIER than the default
    assert budget.autocompact_at < 200_000 * 0.95


def test_agent_budget_with_custom_lazy_pcts(temp_config_dir: Path) -> None:
    """A profile that defers compaction as long as possible."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile = Profile(
        name="lazy",
        compaction=CompactionConfig(
            warning_pct=0.05,
            autocompact_pct=0.02,
            blocking_pct=0.005,
            warning_floor=2_000,
            autocompact_floor=1_000,
            blocking_floor=500,
        ),
    )
    chat = _make_chat_with_window(profile, 200_000)
    budget = chat._agent_budget()

    assert budget.warning_buffer == 10_000
    assert budget.autocompact_buffer == 4_000
    assert budget.blocking_buffer == 1_000
    # Triggers MUCH later than default
    assert budget.autocompact_at > 200_000 * 0.95


# ─── State transitions across the new percentage layout ───


def test_agent_budget_state_transitions_at_200k(temp_config_dir: Path) -> None:
    """Default profile @ 200K → verify the state machine fires at the
    expected token counts derived from the percentages."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile = Profile(name="test")
    chat = _make_chat_with_window(profile, 200_000)
    budget = chat._agent_budget()

    # warning_at = 200000 - 25000 = 175000
    # autocompact_at = 200000 - 12500 = 187500
    # blocking_at = 200000 - 3125 = 196875
    assert budget.warning_at == 175_000
    assert budget.autocompact_at == 187_500
    assert budget.blocking_at == 196_875

    assert budget.state(100_000) == "ok"
    assert budget.state(174_999) == "ok"
    assert budget.state(175_000) == "warning"
    assert budget.state(187_500) == "autocompact"
    assert budget.state(196_875) == "blocking"
    assert budget.state(199_999) == "blocking"


def test_agent_budget_changing_profile_re_resolves(temp_config_dir: Path) -> None:
    """Changing self.profile and rebuilding the budget picks up the
    new compaction config."""
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile_a = Profile(name="a")  # default compaction
    chat = _make_chat_with_window(profile_a, 200_000)
    budget_a = chat._agent_budget()
    assert budget_a.autocompact_buffer == 12_500

    # Swap to a profile with different settings
    chat.profile = Profile(
        name="b",
        compaction=CompactionConfig(
            warning_pct=0.40,
            autocompact_pct=0.20,
            blocking_pct=0.05,
        ),
    )
    budget_b = chat._agent_budget()
    assert budget_b.autocompact_buffer == 40_000
    # Sanity: the new budget actually uses the new profile
    assert budget_b.autocompact_buffer != budget_a.autocompact_buffer


# ─── Disabled compaction still produces a valid budget ───


def test_agent_budget_disabled_still_yields_valid_thresholds(
    temp_config_dir: Path,
) -> None:
    """profile.compaction.enabled=False doesn't affect the BUDGET math.

    The enabled flag gates whether autocompact FIRES proactively, not
    what the threshold values are. The blocking buffer still applies
    so the chat doesn't try to send a request that exceeds the API
    limit even with autocompact off.
    """
    PROFILE_REGISTRY.reload()
    THEME_REGISTRY.reload()
    profile = Profile(
        name="disabled",
        compaction=CompactionConfig(enabled=False),
    )
    chat = _make_chat_with_window(profile, 200_000)
    budget = chat._agent_budget()

    # Same numbers as the enabled-default case at the same window
    assert budget.warning_buffer == 25_000
    assert budget.autocompact_buffer == 12_500
    assert budget.blocking_buffer == 3_125
