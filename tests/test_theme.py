"""Tests for theme parsing and the Theme/ThemeVariant data model.

Covers the JSON parser (parse_theme_file), color format parsing (hex
and oklch), the Theme bundle's variant() resolver, blend_variants()
math, and the THEME_REGISTRY auto-loading the built-in steel theme.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from successor.render.theme import (
    THEME_REGISTRY,
    Theme,
    ThemeVariant,
    all_themes,
    blend_variants,
    find_theme_or_fallback,
    get_theme,
    next_theme,
    normalize_display_mode,
    oklch_to_rgb,
    parse_color,
    parse_theme_file,
    parse_variant,
    toggle_display_mode,
)


# ─── parse_color ───


def test_parse_hex_with_hash() -> None:
    assert parse_color("#10070A") == 0x10070A
    assert parse_color("#FFFFFF") == 0xFFFFFF
    assert parse_color("#000000") == 0x000000


def test_parse_hex_without_hash() -> None:
    assert parse_color("10070A") == 0x10070A
    assert parse_color("ff6347") == 0xFF6347


def test_parse_oklch_string() -> None:
    """oklch CSS-style strings parse via the math conversion."""
    rgb = parse_color("oklch(0.06, 0.008, 260)")
    # The exact value depends on the matrix, but the result must be a
    # valid 24-bit int and dark (low lightness → small components).
    assert isinstance(rgb, int)
    assert 0 <= rgb <= 0xFFFFFF
    # Lightness 0.06 → very dark; each component should be small.
    r = (rgb >> 16) & 0xFF
    g = (rgb >> 8) & 0xFF
    b = rgb & 0xFF
    assert r < 30
    assert g < 30
    assert b < 30


def test_parse_oklch_string_alternate_separator() -> None:
    """Both 'oklch(L, C, h)' and 'oklch(L C h)' work."""
    a = parse_color("oklch(0.5, 0.1, 200)")
    b = parse_color("oklch(0.5 0.1 200)")
    assert a == b


def test_parse_oklch_list() -> None:
    """A 3-element list/tuple parses identically to the string form."""
    a = parse_color([0.5, 0.1, 200])
    b = parse_color("oklch(0.5, 0.1, 200)")
    assert a == b


def test_parse_color_int_passthrough() -> None:
    """A pre-packed int passes through unchanged."""
    assert parse_color(0xABCDEF) == 0xABCDEF


def test_parse_color_int_out_of_range() -> None:
    with pytest.raises(ValueError, match="out of 24-bit range"):
        parse_color(0x1000000)
    with pytest.raises(ValueError, match="out of 24-bit range"):
        parse_color(-1)


def test_parse_color_unknown_string_raises() -> None:
    with pytest.raises(ValueError, match="unrecognized"):
        parse_color("not-a-color")


def test_parse_color_unknown_type_raises() -> None:
    with pytest.raises(ValueError, match="must be a string"):
        parse_color({"r": 1})  # type: ignore[arg-type]


def test_parse_color_oklch_list_wrong_length() -> None:
    with pytest.raises(ValueError, match="3 elements"):
        parse_color([0.5, 0.1])


def test_parse_color_oklch_list_non_numeric() -> None:
    with pytest.raises(ValueError, match="non-numeric"):
        parse_color([0.5, "abc", 200])


# ─── oklch_to_rgb sanity checks ───


def test_oklch_dark_returns_dark_rgb() -> None:
    """L close to 0 → all components small."""
    rgb = oklch_to_rgb(0.05, 0.01, 260)
    r = (rgb >> 16) & 0xFF
    g = (rgb >> 8) & 0xFF
    b = rgb & 0xFF
    assert max(r, g, b) < 30


def test_oklch_light_returns_light_rgb() -> None:
    """L close to 1 → all components large."""
    rgb = oklch_to_rgb(0.95, 0.005, 260)
    r = (rgb >> 16) & 0xFF
    g = (rgb >> 8) & 0xFF
    b = rgb & 0xFF
    assert min(r, g, b) > 200


def test_oklch_clamps_out_of_gamut() -> None:
    """Out-of-gamut values clamp to [0, 255], no overflow or crash."""
    rgb = oklch_to_rgb(2.0, 5.0, 0)  # silly values, gamut overflow
    r = (rgb >> 16) & 0xFF
    g = (rgb >> 8) & 0xFF
    b = rgb & 0xFF
    for c in (r, g, b):
        assert 0 <= c <= 255


# ─── parse_variant ───


_GOOD_VARIANT = {
    "bg": "#000000",
    "bg_input": "#111111",
    "bg_footer": "#222222",
    "fg": "#FFFFFF",
    "fg_dim": "#CCCCCC",
    "fg_subtle": "#888888",
    "accent": "#3366FF",
    "accent_warm": "#FFAA00",
    "accent_warn": "#FF3300",
}


def test_parse_variant_happy_path() -> None:
    v = parse_variant(_GOOD_VARIANT, where="test")
    assert isinstance(v, ThemeVariant)
    assert v.bg == 0x000000
    assert v.fg == 0xFFFFFF
    assert v.accent == 0x3366FF


def test_parse_variant_missing_slot_raises() -> None:
    incomplete = dict(_GOOD_VARIANT)
    del incomplete["accent_warn"]
    with pytest.raises(ValueError, match="missing slots"):
        parse_variant(incomplete, where="test")


def test_parse_variant_bad_color_raises_with_slot_path() -> None:
    bad = dict(_GOOD_VARIANT)
    bad["accent"] = "not-a-color"
    with pytest.raises(ValueError, match=r"test\.accent"):
        parse_variant(bad, where="test")


def test_parse_variant_non_dict_raises() -> None:
    with pytest.raises(ValueError, match="must be a dict"):
        parse_variant(["a", "b"], where="test")


# ─── parse_theme_file ───


def test_parse_theme_file_happy_path(tmp_path: Path) -> None:
    theme_data = {
        "name": "TestTheme",
        "icon": "*",
        "description": "for tests",
        "dark": _GOOD_VARIANT,
        "light": _GOOD_VARIANT,
    }
    p = tmp_path / "test.json"
    p.write_text(json.dumps(theme_data))

    theme = parse_theme_file(p)
    assert theme is not None
    assert theme.name == "testtheme"  # lowercased
    assert theme.icon == "*"
    assert theme.description == "for tests"
    assert isinstance(theme.dark, ThemeVariant)
    assert isinstance(theme.light, ThemeVariant)


def test_parse_theme_file_missing_dark_raises(tmp_path: Path) -> None:
    p = tmp_path / "test.json"
    p.write_text(json.dumps({
        "name": "x",
        "light": _GOOD_VARIANT,
    }))
    with pytest.raises(ValueError, match="missing required 'dark'"):
        parse_theme_file(p)


def test_parse_theme_file_missing_light_raises(tmp_path: Path) -> None:
    p = tmp_path / "test.json"
    p.write_text(json.dumps({
        "name": "x",
        "dark": _GOOD_VARIANT,
    }))
    with pytest.raises(ValueError, match="missing required 'light'"):
        parse_theme_file(p)


def test_parse_theme_file_missing_name_raises(tmp_path: Path) -> None:
    p = tmp_path / "test.json"
    p.write_text(json.dumps({
        "dark": _GOOD_VARIANT,
        "light": _GOOD_VARIANT,
    }))
    with pytest.raises(ValueError, match="missing or empty 'name'"):
        parse_theme_file(p)


def test_parse_theme_file_invalid_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "test.json"
    p.write_text("{ this is not json")
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_theme_file(p)


def test_parse_theme_file_top_level_array_raises(tmp_path: Path) -> None:
    p = tmp_path / "test.json"
    p.write_text(json.dumps([]))
    with pytest.raises(ValueError, match="must be an object"):
        parse_theme_file(p)


# ─── Theme.variant resolver ───


def _make_theme(name: str = "x") -> Theme:
    dark_v = parse_variant(_GOOD_VARIANT, where=f"{name}.dark")
    light_v = parse_variant({**_GOOD_VARIANT, "bg": "#FAFAFA"}, where=f"{name}.light")
    return Theme(name=name, icon="*", description="", dark=dark_v, light=light_v)


def test_variant_returns_dark_for_dark() -> None:
    theme = _make_theme()
    v = theme.variant("dark")
    assert v.bg == 0x000000


def test_variant_returns_light_for_light() -> None:
    theme = _make_theme()
    v = theme.variant("light")
    assert v.bg == 0xFAFAFA


def test_variant_unknown_falls_back_to_dark() -> None:
    """Anything other than 'light' resolves to dark — defensive default."""
    theme = _make_theme()
    assert theme.variant("system").bg == 0x000000
    assert theme.variant("").bg == 0x000000
    assert theme.variant("Dark").bg == 0x000000  # case-sensitive


# ─── normalize_display_mode / toggle_display_mode ───


def test_normalize_accepts_dark_and_light() -> None:
    assert normalize_display_mode("dark") == "dark"
    assert normalize_display_mode("light") == "light"


def test_normalize_strips_and_lowercases() -> None:
    assert normalize_display_mode("  LIGHT  ") == "light"
    assert normalize_display_mode("Dark") == "dark"


def test_normalize_unknown_falls_back_to_dark() -> None:
    assert normalize_display_mode("system") == "dark"
    assert normalize_display_mode("") == "dark"
    assert normalize_display_mode(None) == "dark"
    assert normalize_display_mode(42) == "dark"  # type: ignore[arg-type]


def test_toggle_flips_modes() -> None:
    assert toggle_display_mode("dark") == "light"
    assert toggle_display_mode("light") == "dark"


# ─── blend_variants ───


def test_blend_at_zero_returns_a() -> None:
    a = parse_variant(_GOOD_VARIANT, where="a")
    b = parse_variant({**_GOOD_VARIANT, "bg": "#FFFFFF"}, where="b")
    assert blend_variants(a, b, 0.0) is a


def test_blend_at_one_returns_b() -> None:
    a = parse_variant(_GOOD_VARIANT, where="a")
    b = parse_variant({**_GOOD_VARIANT, "bg": "#FFFFFF"}, where="b")
    assert blend_variants(a, b, 1.0) is b


def test_blend_midpoint_is_between() -> None:
    """At t=0.5, every channel of every slot is between a and b."""
    a = parse_variant(_GOOD_VARIANT, where="a")
    b_data = dict(_GOOD_VARIANT)
    b_data["bg"] = "#FFFFFF"
    b = parse_variant(b_data, where="b")
    mid = blend_variants(a, b, 0.5)
    # bg went from #000000 to #FFFFFF; at 0.5, each channel ~127
    r = (mid.bg >> 16) & 0xFF
    g = (mid.bg >> 8) & 0xFF
    bb = mid.bg & 0xFF
    assert 100 <= r <= 155
    assert 100 <= g <= 155
    assert 100 <= bb <= 155


def test_blend_clamps_t_below_zero() -> None:
    a = parse_variant(_GOOD_VARIANT, where="a")
    b = parse_variant({**_GOOD_VARIANT, "bg": "#FFFFFF"}, where="b")
    assert blend_variants(a, b, -0.5) is a


def test_blend_clamps_t_above_one() -> None:
    a = parse_variant(_GOOD_VARIANT, where="a")
    b = parse_variant({**_GOOD_VARIANT, "bg": "#FFFFFF"}, where="b")
    assert blend_variants(a, b, 2.0) is b


# ─── Built-in registry ───


def test_steel_builtin_loads() -> None:
    """The bundled steel theme is in the registry on first access."""
    THEME_REGISTRY.reload()
    steel = get_theme("steel")
    assert steel is not None
    assert steel.name == "steel"
    # Both variants present and parsed.
    assert isinstance(steel.dark, ThemeVariant)
    assert isinstance(steel.light, ThemeVariant)
    # Source label says it came from the package.
    assert THEME_REGISTRY.source_of("steel") == "builtin"


def test_user_theme_overrides_builtin(temp_config_dir: Path) -> None:
    """A user theme with the same name as a builtin wins."""
    user_themes = temp_config_dir / "themes"
    user_themes.mkdir()

    override = {
        "name": "steel",
        "icon": "X",
        "description": "user override",
        "dark": _GOOD_VARIANT,
        "light": _GOOD_VARIANT,
    }
    (user_themes / "steel.json").write_text(json.dumps(override))

    THEME_REGISTRY.reload()
    steel = get_theme("steel")
    assert steel is not None
    assert steel.icon == "X"
    assert steel.description == "user override"
    assert THEME_REGISTRY.source_of("steel") == "user"


def test_user_theme_loads_alongside_builtin(temp_config_dir: Path) -> None:
    """User-only theme names appear in the registry alongside builtins."""
    user_themes = temp_config_dir / "themes"
    user_themes.mkdir()

    sakura = {
        "name": "sakura",
        "icon": "✿",
        "description": "test cherry blossom",
        "dark": _GOOD_VARIANT,
        "light": _GOOD_VARIANT,
    }
    (user_themes / "sakura.json").write_text(json.dumps(sakura))

    THEME_REGISTRY.reload()
    names = THEME_REGISTRY.names()
    assert "steel" in names  # builtin
    assert "sakura" in names  # user


def test_broken_user_theme_doesnt_block_builtin(
    temp_config_dir: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A malformed user theme is skipped; the builtin steel still loads."""
    user_themes = temp_config_dir / "themes"
    user_themes.mkdir()
    (user_themes / "broken.json").write_text("{ not json")

    THEME_REGISTRY.reload()
    assert get_theme("steel") is not None
    assert get_theme("broken") is None
    captured = capsys.readouterr()
    assert "broken.json" in captured.err


# ─── find_theme_or_fallback / next_theme ───


def test_find_theme_or_fallback_returns_named() -> None:
    THEME_REGISTRY.reload()
    theme = find_theme_or_fallback("steel")
    assert theme.name == "steel"


def test_find_theme_or_fallback_unknown_name_returns_first() -> None:
    THEME_REGISTRY.reload()
    theme = find_theme_or_fallback("nonexistent")
    # Should be a real loaded theme, not the hardcoded fallback's identity.
    assert theme in all_themes()


def test_find_theme_or_fallback_none_returns_first() -> None:
    THEME_REGISTRY.reload()
    theme = find_theme_or_fallback(None)
    assert theme in all_themes()


def test_next_theme_cycles(temp_config_dir: Path) -> None:
    """next_theme walks the registry in order and wraps."""
    # Add two user themes alongside the builtin so we have a stable
    # cycle to test (sorted order: forge, sakura, steel — or whichever
    # the loader produces).
    user_themes = temp_config_dir / "themes"
    user_themes.mkdir()
    for name in ("alpha", "beta"):
        (user_themes / f"{name}.json").write_text(json.dumps({
            "name": name,
            "icon": "*",
            "description": "",
            "dark": _GOOD_VARIANT,
            "light": _GOOD_VARIANT,
        }))

    THEME_REGISTRY.reload()
    themes = all_themes()
    assert len(themes) >= 3  # steel + alpha + beta

    # Cycle through every theme exactly once back to the start.
    seen = []
    current = themes[0]
    for _ in range(len(themes) + 1):
        seen.append(current.name)
        current = next_theme(current)

    # First and last should match (full cycle wrapped).
    assert seen[0] == seen[-1]
    # Every theme appeared at some point.
    assert set(seen) >= {t.name for t in themes}


def test_next_theme_with_none_returns_first(temp_config_dir: Path) -> None:
    THEME_REGISTRY.reload()
    first = all_themes()[0]
    assert next_theme(None).name == first.name
