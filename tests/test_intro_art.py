"""Tests for the chat empty-state hero panel + intro art loader.

Three layers under test:

  1. The loader (`render/intro_art.py`) — resolves a name or path
     into a BrailleArt instance, with graceful None on miss.
  2. The chat's `_is_empty_chat` predicate — only fires the hero
     panel when there's no real content (tool cards, boundaries,
     summaries, and committed messages all suppress it).
  3. The painter (`_paint_empty_state` via the chat surface) —
     renders the info panel with profile/provider/tools/appearance
     sections plus the bottom hint, and gracefully degrades on
     narrow terminals.
"""

from __future__ import annotations

import json
from pathlib import Path

from successor.bash.cards import ToolCard
from successor.chat import SuccessorChat, _Message
from successor.profiles import PROFILE_REGISTRY
from successor.render.cells import Grid
from successor.render.intro_art import load_intro_art
from successor.snapshot import render_grid_to_plain


# ─── load_intro_art ───


def test_load_intro_art_resolves_bundled_successor() -> None:
    """The bundled `successor` name resolves to the title portrait
    via the intros/<name>/10-title.txt convention."""
    art = load_intro_art("successor")
    assert art is not None
    # Source is loaded — the parsed dot grid should have content
    assert art.dot_h > 0
    assert art.dot_w > 0


def test_load_intro_art_returns_none_for_unknown_name() -> None:
    art = load_intro_art("nonexistent-art-name-xyz")
    assert art is None


def test_load_intro_art_returns_none_for_none_input() -> None:
    assert load_intro_art(None) is None
    assert load_intro_art("") is None
    assert load_intro_art("   ") is None


def test_load_intro_art_resolves_user_dir(tmp_path: Path, monkeypatch) -> None:
    """A braille frame at ~/.config/successor/art/<name>.txt resolves
    by name from the user dir."""
    cfg = tmp_path / "successor"
    art_dir = cfg / "art"
    art_dir.mkdir(parents=True)
    # Minimal valid braille frame — one row of plain braille blanks
    (art_dir / "myart.txt").write_text("⠀⠀⠀⠀⠀⠀⠀⠀\n⠀⠀⠀⠀⠀⠀⠀⠀\n")
    monkeypatch.setenv("SUCCESSOR_CONFIG_DIR", str(cfg))
    art = load_intro_art("myart")
    assert art is not None
    assert art.dot_w > 0


def test_load_intro_art_resolves_absolute_path(tmp_path: Path) -> None:
    """An absolute path bypasses name resolution and loads directly."""
    p = tmp_path / "custom.txt"
    p.write_text("⠿⠿⠿⠿\n⠿⠿⠿⠿\n")
    art = load_intro_art(str(p))
    assert art is not None
    assert art.dot_w > 0


def test_load_intro_art_resolves_tilde_path(tmp_path: Path, monkeypatch) -> None:
    """A path starting with ~ expands via Path.expanduser."""
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / "tilde-art.txt"
    p.write_text("⠿⠿\n⠿⠿\n")
    art = load_intro_art("~/tilde-art.txt")
    assert art is not None


# ─── _is_empty_chat predicate ───


def test_empty_chat_is_empty_with_no_messages(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    assert chat._is_empty_chat() is True


def test_empty_chat_is_empty_with_only_synthetic_greeting(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    # Default profile has chat_intro_art set, so the greeting was
    # never added — but synthetic messages still shouldn't count.
    chat.messages = [_Message("successor", "hello", synthetic=True)]
    assert chat._is_empty_chat() is True


def test_empty_chat_is_NOT_empty_with_user_message(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = [_Message("user", "hi")]
    assert chat._is_empty_chat() is False


def test_empty_chat_is_NOT_empty_with_tool_card(temp_config_dir: Path) -> None:
    """Tool cards are synthetic for API serialization but ARE real
    visual content the user expects to see."""
    chat = SuccessorChat()
    card = ToolCard(
        verb="echo", params={}, risk="safe",
        raw_command="echo hi", confidence=1.0,
        parser_name="echo", output="hi\n", exit_code=0,
        duration_ms=5,
    )
    chat.messages = [_Message("tool", "", tool_card=card, synthetic=True)]
    assert chat._is_empty_chat() is False


def test_empty_chat_is_NOT_empty_with_stream_in_flight(temp_config_dir: Path) -> None:
    chat = SuccessorChat()
    chat.messages = []
    class _FakeStream:
        def drain(self): return []
        def close(self): pass
    chat._stream = _FakeStream()
    assert chat._is_empty_chat() is False


# ─── _paint_empty_state via the chat surface ───


def test_empty_state_renders_info_panel_at_normal_width(temp_config_dir: Path) -> None:
    """Wide terminal: art on the left + info panel on the right."""
    chat = SuccessorChat()
    grid = Grid(rows=32, cols=130)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    # Section headers
    assert "profile" in plain
    assert "provider" in plain
    assert "tools" in plain
    assert "appearance" in plain
    # The bundled successor portrait — at least some braille content
    assert "⣿" in plain
    # Bottom hint
    assert "type / for commands" in plain
    assert "press ? for help" in plain


def test_empty_state_hides_art_on_narrow_terminal(temp_config_dir: Path) -> None:
    """Narrow terminal (<80 cols): art hidden, info panel only."""
    chat = SuccessorChat()
    grid = Grid(rows=30, cols=72)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    # Info panel still renders
    assert "profile" in plain
    assert "type / for commands" in plain
    # Art should NOT have rendered (no large braille block)
    # We check by counting filled braille chars — should be ~0
    braille_chars = sum(1 for c in plain if 0x2800 < ord(c) < 0x28FF and c != "⠀")
    assert braille_chars == 0


def test_empty_state_hides_when_user_has_sent_message(temp_config_dir: Path) -> None:
    """Once the user submits, the empty state goes away even though
    chat_intro_art is still set."""
    chat = SuccessorChat()
    chat.messages = [_Message("user", "what's the capital of France?")]
    grid = Grid(rows=28, cols=120)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    # The user message should be visible — the empty state is hidden
    assert "capital of France" in plain
    # The hint should NOT be visible (it's part of the empty state)
    assert "type / for commands" not in plain


def test_empty_state_paints_when_chat_intro_art_unset(temp_config_dir: Path) -> None:
    """A profile with chat_intro_art=None falls back to the synthetic
    greeting — the empty-state painter does NOT fire, the legacy
    greeting message is shown instead."""
    # Build a profile without chat_intro_art
    profiles_dir = temp_config_dir / "profiles"
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / "noart.json").write_text(json.dumps({
        "name": "noart",
        "description": "no hero panel",
        "theme": "steel",
        "display_mode": "dark",
        "density": "normal",
        "system_prompt": "",
        "provider": {
            "type": "llamacpp",
            "base_url": "http://localhost:8080",
            "model": "local",
        },
        "skills": [],
        "tools": [],
        "tool_config": {},
        "intro_animation": None,
        "chat_intro_art": None,
    }))
    (temp_config_dir / "chat.json").write_text(json.dumps({
        "version": 2, "active_profile": "noart",
    }))
    PROFILE_REGISTRY.reload()

    chat = SuccessorChat()
    grid = Grid(rows=28, cols=120)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    # Should fall back to the legacy greeting, NOT show the hint
    assert "type / for commands" not in plain
    assert "I am successor" in plain


def test_empty_state_info_panel_reflects_active_profile(temp_config_dir: Path) -> None:
    """The panel reads from the live chat state, so the profile
    name appears as a value."""
    chat = SuccessorChat()
    grid = Grid(rows=32, cols=130)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    # Profile name from the active profile
    assert chat.profile.name in plain


def test_empty_state_info_panel_shows_resolved_context_window(
    temp_config_dir: Path,
) -> None:
    """The ctx window line uses the chat's resolved value — same
    one that drives compaction thresholds."""
    chat = SuccessorChat()
    grid = Grid(rows=32, cols=130)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    # The default profile resolves to 262144 (no override, llama.cpp
    # detect either succeeds or falls back to CONTEXT_MAX). Either
    # way the line should mention "tokens".
    assert "tokens" in plain
