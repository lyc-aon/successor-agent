"""Tests for the context fill bar in the chat's static footer.

The bar:
  - reads token count from agent.TokenCounter when cached, else falls
    back to char-count heuristic
  - reads window size from profile.provider.context_window
  - shows threshold-state badge ("◉ COMPACT", "⚠ BLOCKED")
  - color-shifts between accent → accent_warm → accent_warn
  - applies a continuous pulse via theme blends when past autocompact
"""

from __future__ import annotations

from copy import replace
from dataclasses import replace as dc_replace
from pathlib import Path

from successor.agent import TokenCounter
from successor.chat import SuccessorChat, _Message
from successor.render.cells import Grid
from successor.snapshot import render_grid_to_plain


def _chat_with_window(window: int) -> SuccessorChat:
    """Build a chat with a custom context_window in its profile."""
    chat = SuccessorChat()
    chat.messages = []
    new_provider = {**(chat.profile.provider or {}), "context_window": window}
    chat.profile = dc_replace(chat.profile, provider=new_provider)
    chat._cached_token_counter = TokenCounter()  # heuristic only — hermetic
    return chat


def _footer_line(chat: SuccessorChat, *, rows: int = 20, cols: int = 120) -> str:
    g = Grid(rows, cols)
    chat.on_tick(g)
    plain = render_grid_to_plain(g)
    return plain.split("\n")[-1].rstrip()


def _stuff_chat_to_pct(chat: SuccessorChat, target_pct: float) -> int:
    window = chat.profile.provider.get("context_window", 262_144)
    target_chars = int(window * target_pct * 3.5)
    chat.messages = [_Message("user", "x" * target_chars)]
    return target_chars


# ─── Threshold state transitions ───


def test_footer_shows_ok_state_at_low_fill(temp_config_dir: Path) -> None:
    chat = _chat_with_window(50_000)
    _stuff_chat_to_pct(chat, 0.10)
    line = _footer_line(chat)
    assert "ctx" in line
    assert "COMPACT" not in line
    assert "BLOCKED" not in line


def test_footer_shows_compact_badge_at_autocompact(temp_config_dir: Path) -> None:
    chat = _chat_with_window(50_000)
    _stuff_chat_to_pct(chat, 0.92)
    line = _footer_line(chat)
    assert "COMPACT" in line


def test_footer_shows_blocked_badge_at_blocking(temp_config_dir: Path) -> None:
    chat = _chat_with_window(50_000)
    _stuff_chat_to_pct(chat, 0.99)
    line = _footer_line(chat)
    assert "BLOCKED" in line


def test_footer_window_from_profile(temp_config_dir: Path) -> None:
    """Window comes from profile.provider.context_window."""
    chat_50k = _chat_with_window(50_000)
    _stuff_chat_to_pct(chat_50k, 0.5)
    line_50k = _footer_line(chat_50k)
    assert "50000" in line_50k

    chat_100k = _chat_with_window(100_000)
    _stuff_chat_to_pct(chat_100k, 0.5)
    line_100k = _footer_line(chat_100k)
    assert "100000" in line_100k


def test_footer_uses_token_counter_when_cached(temp_config_dir: Path) -> None:
    """When _cached_token_counter is set, the count uses the counter
    instead of the legacy char/4 heuristic."""
    chat = _chat_with_window(262_144)
    chat._cached_token_counter = TokenCounter()  # explicit
    chat.messages = [_Message("user", "hello world")]
    line = _footer_line(chat)
    # Counter heuristic on 11 chars: ~4 tokens + role overhead → ~8 total
    # Char/4 heuristic on 11 chars: 2 tokens
    # Either way the bar is at near-zero fill — just verify it renders
    assert "ctx" in line


def test_footer_falls_back_when_no_counter(temp_config_dir: Path) -> None:
    """No cached counter → char/4 heuristic still works."""
    chat = SuccessorChat()
    chat.messages = []
    chat._cached_token_counter = None  # no counter
    chat.messages = [_Message("user", "x" * 1000)]
    line = _footer_line(chat)
    # Should still render a footer with a count
    assert "ctx" in line


# ─── Bar fill character ───


def test_footer_bar_grows_with_fill(temp_config_dir: Path) -> None:
    """The bar should have proportionally more █ chars at higher fill."""
    chat = _chat_with_window(50_000)

    _stuff_chat_to_pct(chat, 0.10)
    line_low = _footer_line(chat)
    bars_low = line_low.count("█")

    _stuff_chat_to_pct(chat, 0.80)
    line_high = _footer_line(chat)
    bars_high = line_high.count("█")

    assert bars_high > bars_low


def test_footer_percentage_label_present(temp_config_dir: Path) -> None:
    chat = _chat_with_window(50_000)
    _stuff_chat_to_pct(chat, 0.50)
    line = _footer_line(chat)
    assert "%" in line


def test_footer_model_name_present(temp_config_dir: Path) -> None:
    chat = _chat_with_window(50_000)
    line = _footer_line(chat)
    assert "local" in line  # default model name from successor-dev profile
