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
    """The bundled `successor` name resolves via the
    intros/<name>/hero.txt convention. hero.txt holds the dedicated
    oracle portrait used by the chat empty state; legacy
    fallback was 10-title.txt before hero.txt landed."""
    art = load_intro_art("successor")
    assert art is not None
    # Source is loaded — the parsed dot grid should have content
    assert art.dot_h > 0
    assert art.dot_w > 0


def test_load_intro_art_prefers_hero_over_title_when_both_exist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """If a builtin intro directory ships both hero.txt and 10-title.txt,
    the loader picks hero.txt. The fallback path only kicks in for
    legacy intros that haven't added hero.txt yet."""
    from successor.render import intro_art as ia_mod

    # Build a fake builtin intros directory with both files. hero.txt
    # is one row of '⠿' (filled braille blocks), 10-title.txt is one
    # row of '⠁' (single-dot blocks). The loaded BrailleArt's dot
    # density tells us which file was read.
    fake_builtin = tmp_path / "builtin"
    intros_dir = fake_builtin / "intros" / "fake-anim"
    intros_dir.mkdir(parents=True)
    (intros_dir / "hero.txt").write_text("⠿⠿⠿⠿\n⠿⠿⠿⠿\n")
    (intros_dir / "10-title.txt").write_text("⠁⠁⠁⠁\n⠁⠁⠁⠁\n")

    # Patch builtin_root to point at our fake tree
    monkeypatch.setattr(
        "successor.loader.builtin_root",
        lambda: fake_builtin,
    )

    art = ia_mod.load_intro_art("fake-anim")
    assert art is not None
    # hero.txt is dense (8 dots per cell, '⠿'), title is sparse
    # (1 dot per cell, '⠁'). Count the on-bits in the parsed dot
    # bitmap to confirm we got the dense file.
    on_bits = sum(1 for row in art.dots for px in row if px)
    # 4 cells × 2 rows × 8 dots/cell = 64 dots if dense, 8 if sparse
    assert on_bits > 32, f"expected dense hero.txt, got {on_bits} on-bits"


def test_load_intro_art_falls_back_to_title_when_no_hero(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A legacy intro directory that ships only 10-title.txt still
    resolves correctly via the fallback path."""
    from successor.render import intro_art as ia_mod

    fake_builtin = tmp_path / "builtin"
    intros_dir = fake_builtin / "intros" / "legacy-anim"
    intros_dir.mkdir(parents=True)
    # Only ship 10-title.txt, no hero.txt
    (intros_dir / "10-title.txt").write_text("⠿⠿\n⠿⠿\n")

    monkeypatch.setattr(
        "successor.loader.builtin_root",
        lambda: fake_builtin,
    )

    art = ia_mod.load_intro_art("legacy-anim")
    assert art is not None
    on_bits = sum(1 for row in art.dots for px in row if px)
    assert on_bits > 0


def test_bundled_successor_oracle_assets_are_distinct_and_not_solid() -> None:
    """The bundled oracle hero and final held intro frame should be
    distinct assets, and the held frame should not collapse into a
    solid full-screen block."""
    from successor.loader import builtin_root
    from successor.render.braille import BrailleArt, load_frame

    art = load_intro_art("successor")
    assert art is not None
    hero_total_on = sum(1 for row in art.dots for px in row if px)

    base = builtin_root() / "intros" / "successor"
    title_art = BrailleArt(load_frame(base / "10-title.txt"))
    title_total_on = sum(1 for row in title_art.dots for px in row if px)
    title_total_bits = len(title_art.dots) * len(title_art.dots[0])

    assert 0 < hero_total_on < title_total_bits, (
        f"hero should contain a real oracle silhouette, got {hero_total_on} / {title_total_bits}"
    )
    assert 0 < title_total_on < (title_total_bits // 2), (
        f"final intro frame should be a readable oracle hold frame, not a solid block: "
        f"{title_total_on} / {title_total_bits}"
    )

    direct_hero = BrailleArt(load_frame(base / "hero.txt"))
    direct_total_on = sum(1 for row in direct_hero.dots for px in row if px)
    assert hero_total_on == direct_total_on, (
        f"loader should resolve to hero.txt: loader={hero_total_on} direct={direct_total_on}"
    )
    assert hero_total_on != title_total_on, (
        f"hero and final intro frame should stay visually distinct: "
        f"hero={hero_total_on} title={title_total_on}"
    )


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
    """Wide terminal: fitted art on the left + info rail on the right."""
    chat = SuccessorChat()
    grid = Grid(rows=32, cols=130)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    plain_fold = plain.casefold()
    # Section headers
    assert "profile" in plain_fold
    assert "provider" in plain_fold
    assert "tools" in plain_fold
    assert "appearance" in plain_fold
    # The bundled successor portrait — at least some braille content
    assert "⣿" in plain
    # Bottom hint
    assert "type / for commands" in plain
    assert "press ? for help" in plain


def test_empty_state_wide_terminal_leaves_middle_gutter(temp_config_dir: Path) -> None:
    """On a wide terminal the right rail should not sit directly beside the hero."""
    chat = SuccessorChat()
    grid = Grid(rows=32, cols=120)
    chat.on_tick(grid)
    lines = render_grid_to_plain(grid).splitlines()
    profile_x = next(line.casefold().index("profile") for line in lines if "profile" in line.casefold())
    hero_right = max(
        i
        for line in lines
        for i, ch in enumerate(line)
        if 0x2800 < ord(ch) < 0x28FF and ch != "⠀"
    )
    assert profile_x - hero_right >= 12


def test_empty_state_wide_terminal_right_anchors_the_info_rail(
    temp_config_dir: Path,
) -> None:
    """With the hero visible, section text should hug the shell's right side."""
    chat = SuccessorChat()
    grid = Grid(rows=32, cols=120)
    chat.on_tick(grid)
    lines = render_grid_to_plain(grid).splitlines()

    profile_idx = next(i for i, line in enumerate(lines) if "profile" in line.casefold())
    profile_line = lines[profile_idx]
    profile_value_line = lines[profile_idx + 1]

    profile_right_margin = 120 - (profile_line.casefold().index("profile") + len("profile"))
    value_right_margin = 120 - (
        profile_value_line.index(chat.profile.name) + len(chat.profile.name)
    )

    assert profile_right_margin <= 8
    assert value_right_margin <= 8


def test_header_title_clamps_left_when_the_window_gets_tight(
    temp_config_dir: Path,
) -> None:
    """The title should shift left before the right-side pills crowd it."""
    chat = SuccessorChat()
    grid = Grid(rows=24, cols=58)
    chat.on_tick(grid)
    title_row = render_grid_to_plain(grid).splitlines()[0]
    title_x = title_row.index("successor · chat")
    assert title_x <= 6


def test_empty_state_wide_terminal_has_live_oracle_motion(
    temp_config_dir: Path,
    monkeypatch,
) -> None:
    """The empty state should keep the oracle subtly alive over time."""
    chat = SuccessorChat()

    monkeypatch.setattr("successor.chat.time.monotonic", lambda: 100.0)
    grid_a = Grid(rows=32, cols=120)
    chat.on_tick(grid_a)

    monkeypatch.setattr("successor.chat.time.monotonic", lambda: 104.0)
    grid_b = Grid(rows=32, cols=120)
    chat.on_tick(grid_b)

    def oracle_signature(grid: Grid) -> list[tuple[str, int | None, int]]:
        sig: list[tuple[str, int | None, int]] = []
        for r in range(4, 28):
            for c in range(2, 44):
                cell = grid.at(r, c)
                if 0x2800 < ord(cell.char) < 0x28FF and cell.char != "⠀":
                    sig.append((cell.char, cell.style.fg, cell.style.attrs))
        return sig

    oracle_a = oracle_signature(grid_a)
    oracle_b = oracle_signature(grid_b)

    assert oracle_a
    assert oracle_b
    assert oracle_a != oracle_b


def test_empty_state_keeps_art_visible_on_mid_width_terminal(temp_config_dir: Path) -> None:
    """The oracle hero should still render at moderate widths once fitted."""
    chat = SuccessorChat()
    grid = Grid(rows=30, cols=76)
    chat.on_tick(grid)
    plain = render_grid_to_plain(grid)
    assert "profile" in plain.casefold()
    braille_chars = sum(1 for c in plain if 0x2800 < ord(c) < 0x28FF and c != "⠀")
    assert braille_chars > 60


def test_empty_state_hides_art_on_really_narrow_terminal(temp_config_dir: Path) -> None:
    """Very narrow terminals still fall back to info-panel-only."""
    chat = SuccessorChat()
    grid = Grid(rows=30, cols=58)
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
