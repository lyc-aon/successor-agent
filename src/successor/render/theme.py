"""Theme system — semantic color palettes for the chat UI.

A **Theme** is an identity (name + icon + description) plus two
**ThemeVariant**s — a `dark` and a `light` palette that share the same
visual character. The chat App holds the Theme + a separate `display_mode`
("dark" or "light") and resolves to the appropriate variant per frame.

This split exists because dark and light are not themes — they are
*modes* of any theme. Every real design system (Material, Tailwind,
Apple HIG, ComPress's own oklch palette) treats palette identity and
mode as orthogonal axes. The previous shape — flat Theme objects called
DARK_THEME / LIGHT_THEME / FORGE_THEME — conflated them, which meant
"toggle dark/light" couldn't preserve theme identity and "switch theme"
couldn't preserve mode preference.

Two color formats are supported in JSON theme files:

  hex string:    "#10070A" or "10070A"
  oklch tuple:   "oklch(0.06, 0.008, 260)" or [0.06, 0.008, 260]

Hex is convenient for hand-tuning. oklch is mathematically clean: when
you take a color and lower its lightness L by 0.1, the result LOOKS
exactly 0.1 darker to a human. That makes the relationship between
dark and light variants of a theme rigorous — they share chroma + hue
and only differ in lightness. Try doing that with HSL or RGB and the
relative darkness varies wildly across hues.

Public surface:
    ThemeVariant       9 semantic color slots (one mode of a theme)
    Theme              identity + dark variant + light variant
    blend_variants     lerp every slot for smooth transitions
    oklch_to_rgb       the oklch → 24-bit packed RGB conversion helper
    parse_color        accept hex string OR oklch tuple/string
    parse_theme_file   JSON file → Theme (used by the loader Registry)
    THEME_REGISTRY     the Registry[Theme] singleton
    get_theme(name)    convenience wrapper around THEME_REGISTRY.get
    next_theme(t)      cycle to the next theme in registry order
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from ..loader import Registry
from .text import lerp_rgb


# ─── Color parsing ───


def oklch_to_rgb(L: float, C: float, h: float) -> int:
    """Convert oklch (lightness, chroma, hue) → 24-bit packed RGB int.

    L is 0..1 lightness, C is chroma, h is hue in degrees. Uses Björn
    Ottosson's OKLab → linear sRGB matrix and standard sRGB gamma
    encoding. Out-of-gamut values are clamped to [0, 255].

    Reference: https://bottosson.github.io/posts/oklab/
    """
    h_rad = math.radians(h)
    a = C * math.cos(h_rad)
    b = C * math.sin(h_rad)

    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b

    l = l_ ** 3
    m = m_ ** 3
    s = s_ ** 3

    r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b_lin = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s

    def gamma_encode(x: float) -> float:
        x = max(0.0, min(1.0, x))
        if x <= 0.0031308:
            return 12.92 * x
        return 1.055 * (x ** (1.0 / 2.4)) - 0.055

    r_byte = int(round(gamma_encode(r) * 255))
    g_byte = int(round(gamma_encode(g) * 255))
    b_byte = int(round(gamma_encode(b_lin) * 255))
    return (r_byte << 16) | (g_byte << 8) | b_byte


_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_OKLCH_STR_RE = re.compile(
    r"^oklch\(\s*([0-9.+-]+)\s*[, ]\s*([0-9.+-]+)\s*[, ]\s*([0-9.+-]+)\s*\)$",
    re.IGNORECASE,
)


def parse_color(value: object) -> int:
    """Parse a color from JSON into a 24-bit packed RGB int.

    Accepts:
      "#10070A"                              hex with leading #
      "10070A"                               hex without #
      "oklch(0.06, 0.008, 260)"              oklch CSS-style string
      [0.06, 0.008, 260]                     oklch as 3-element list
      0xXXXXXX (already-packed int)          pass-through (for tests)

    Raises ValueError on anything else. The error message names the
    bad value so the loader's stderr warning is actionable.
    """
    if isinstance(value, int):
        if 0 <= value <= 0xFFFFFF:
            return value
        raise ValueError(f"int color out of 24-bit range: {value}")

    if isinstance(value, (list, tuple)):
        if len(value) != 3:
            raise ValueError(
                f"oklch list must have 3 elements (L, C, h); got {len(value)}"
            )
        try:
            L, C, h = float(value[0]), float(value[1]), float(value[2])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"oklch list contains non-numeric values: {value}") from exc
        return oklch_to_rgb(L, C, h)

    if isinstance(value, str):
        s = value.strip()
        m = _HEX_RE.match(s)
        if m:
            return int(m.group(1), 16)
        m = _OKLCH_STR_RE.match(s)
        if m:
            L = float(m.group(1))
            C = float(m.group(2))
            h = float(m.group(3))
            return oklch_to_rgb(L, C, h)
        raise ValueError(
            f"unrecognized color format: {value!r} "
            f"(expected hex like '#10070A' or oklch like 'oklch(0.06, 0.008, 260)')"
        )

    raise ValueError(f"color must be a string, list, or int; got {type(value).__name__}")


# ─── ThemeVariant ───


@dataclass(frozen=True, slots=True)
class ThemeVariant:
    """One palette — either the dark or light expression of a theme.

    Nine semantic slots that every painted region maps into. The slot
    names describe how the color is used, not the color itself — that's
    what makes themes hot-swappable across both axes (theme + mode).
    """

    # Backgrounds — three depths so we can layer regions visually
    bg: int          # main background (chat area, title row)
    bg_input: int    # input area background
    bg_footer: int   # static footer background

    # Foreground text — three weights of contrast
    fg: int          # primary text (default messages, title)
    fg_dim: int      # dim text (synthetic messages, hints, dim labels)
    fg_subtle: int   # very dim (fade-in start, empty progress, separators)

    # Accents — primary brand color + warm and warning variants
    accent: int      # successor messages, prompt indicator, progress fill
    accent_warm: int # secondary accent (mid progress, scroll indicator)
    accent_warn: int # warning (high progress, errors)


_VARIANT_SLOTS: tuple[str, ...] = (
    "bg",
    "bg_input",
    "bg_footer",
    "fg",
    "fg_dim",
    "fg_subtle",
    "accent",
    "accent_warm",
    "accent_warn",
)


def parse_variant(data: object, *, where: str) -> ThemeVariant:
    """Parse a JSON dict into a ThemeVariant.

    `where` is a human-readable label like "steel.dark" used in error
    messages so a missing slot in a multi-theme load is easy to find.
    """
    if not isinstance(data, dict):
        raise ValueError(f"{where}: variant must be a dict, got {type(data).__name__}")
    missing = [slot for slot in _VARIANT_SLOTS if slot not in data]
    if missing:
        raise ValueError(f"{where}: missing slots: {', '.join(missing)}")
    kwargs = {}
    for slot in _VARIANT_SLOTS:
        try:
            kwargs[slot] = parse_color(data[slot])
        except ValueError as exc:
            raise ValueError(f"{where}.{slot}: {exc}") from exc
    return ThemeVariant(**kwargs)


# ─── Theme bundle ───


@dataclass(frozen=True, slots=True)
class Theme:
    """A theme — identity + both display-mode variants.

    The chat App holds Theme + display_mode separately so toggling
    display mode preserves theme identity and switching themes
    preserves the user's mode preference. Use `.variant(mode)` to
    resolve to the right `ThemeVariant` for a given frame.
    """

    name: str
    icon: str
    description: str
    dark: ThemeVariant
    light: ThemeVariant

    def variant(self, display_mode: str) -> ThemeVariant:
        """Return the dark or light variant for the given display mode.

        Anything other than "light" returns dark, on the principle that
        dark is the canonical mode and any unknown value should fall
        back to it (loud failure on color picking is worse than silent
        defaulting).
        """
        return self.light if display_mode == "light" else self.dark


# ─── JSON file parser (used by the Registry) ───


def parse_theme_file(path: Path) -> Theme | None:
    """Parse a theme JSON file into a Theme.

    Returns None for files that aren't themes (e.g. a README in the
    themes/ directory). Raises ValueError for files that look like
    themes but are malformed — the loader catches the exception and
    emits a stderr warning naming the file.

    Required JSON shape:
        {
          "name": "steel",
          "icon": "◆",
          "description": "instrument-panel oklch — cool blue accents",
          "dark": { 9 slots },
          "light": { 9 slots }
        }

    Both `dark` and `light` are required — themes that look identical
    in both modes still need to declare both, because the loader can't
    invent a sensible inverse for an arbitrary palette.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"read failed: {exc}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc.msg} at line {exc.lineno}") from exc

    if not isinstance(data, dict):
        raise ValueError("top-level JSON must be an object")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("missing or empty 'name' field")
    icon = data.get("icon", "◆")
    if not isinstance(icon, str):
        raise ValueError("'icon' must be a string")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ValueError("'description' must be a string")

    if "dark" not in data:
        raise ValueError("missing required 'dark' variant")
    if "light" not in data:
        raise ValueError("missing required 'light' variant")

    dark = parse_variant(data["dark"], where=f"{name}.dark")
    light = parse_variant(data["light"], where=f"{name}.light")

    return Theme(
        name=name.strip().lower(),
        icon=icon,
        description=description,
        dark=dark,
        light=light,
    )


# ─── Registry ───


THEME_REGISTRY: Registry[Theme] = Registry[Theme](
    kind="themes",
    file_glob="*.json",
    parser=parse_theme_file,
    description="theme",
)


SUPPORTED_THEME_NAMES: tuple[str, ...] = ("steel", "paper")
_LEGACY_THEME_ALIASES: dict[str, str] = {
    "dark": "steel",
    "light": "steel",
    "forge": "paper",
    "cobalt": "steel",
}


def normalize_theme_name(name: object) -> str | None:
    """Resolve a saved/requested theme name to a supported theme.

    Successor now only exposes the built-in `steel` and `paper` themes.
    Old built-in names (`forge`, `cobalt`) are mapped forward so saved
    configs and profiles continue to land on a sensible palette.
    """
    if not isinstance(name, str):
        return None
    normalized = name.strip().lower()
    if not normalized:
        return None
    normalized = _LEGACY_THEME_ALIASES.get(normalized, normalized)
    if normalized in SUPPORTED_THEME_NAMES:
        return normalized
    return None


def get_theme(name: str) -> Theme | None:
    """Look up a theme by name, mapping legacy built-ins forward.

    The public catalog is restricted to paper/steel, but explicit user
    theme names still resolve so local overrides remain possible.
    """
    normalized = normalize_theme_name(name)
    if normalized is not None:
        return THEME_REGISTRY.get(normalized)
    if not isinstance(name, str):
        return None
    requested = name.strip().lower()
    if not requested:
        return None
    return THEME_REGISTRY.get(requested)


def all_themes() -> list[Theme]:
    """Return the supported theme catalog in stable product order."""
    loaded = {theme.name: theme for theme in THEME_REGISTRY.all()}
    return [loaded[name] for name in SUPPORTED_THEME_NAMES if name in loaded]


def next_theme(current: Theme | None) -> Theme:
    """Cycle to the next theme in load order. Wraps around at the end.

    If `current` is None or not in the supported catalog, returns the
    first supported theme.
    Falls back to a hardcoded steel-equivalent if the registry is empty
    (shouldn't happen — the package always ships at least one builtin).
    """
    themes = all_themes()
    if not themes:
        # Pathological — registry is empty. Return a minimal hardcoded
        # theme so the renderer doesn't crash. This should be unreachable
        # in any normal install but is the safest possible fallback.
        return _FALLBACK_THEME
    if current is None:
        return themes[0]
    try:
        idx = themes.index(current)
    except ValueError:
        # current isn't in the supported catalog
        return themes[0]
    return themes[(idx + 1) % len(themes)]


def find_theme_or_fallback(name: str | None) -> Theme:
    """Resolve a theme name with a guaranteed return.

    Used by chat App initialization where we need a Theme even if the
    saved name doesn't exist anymore. Tries: requested name → first
    loaded theme → hardcoded fallback. Always returns a valid Theme.
    """
    if name:
        theme = get_theme(name)
        if theme is not None:
            return theme
    themes = all_themes()
    if themes:
        return themes[0]
    return _FALLBACK_THEME


# ─── Animated transitions ───


def blend_variants(a: ThemeVariant, b: ThemeVariant, t: float) -> ThemeVariant:
    """Linearly interpolate every color slot between two variants.

    Used during theme transitions and display-mode flips. t is clamped
    to [0, 1]. At t=0 returns a, at t=1 returns b. The interpolation
    happens in sRGB space (lerp_rgb), not perceptual space — close
    enough at the speeds we transition at, and avoids needing oklch
    round-trips on every frame.
    """
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    return ThemeVariant(
        bg=lerp_rgb(a.bg, b.bg, t),
        bg_input=lerp_rgb(a.bg_input, b.bg_input, t),
        bg_footer=lerp_rgb(a.bg_footer, b.bg_footer, t),
        fg=lerp_rgb(a.fg, b.fg, t),
        fg_dim=lerp_rgb(a.fg_dim, b.fg_dim, t),
        fg_subtle=lerp_rgb(a.fg_subtle, b.fg_subtle, t),
        accent=lerp_rgb(a.accent, b.accent, t),
        accent_warm=lerp_rgb(a.accent_warm, b.accent_warm, t),
        accent_warn=lerp_rgb(a.accent_warn, b.accent_warn, t),
    )


# ─── Hardcoded fallback ───
#
# If the registry fails to load any theme (corrupt builtin dir, custom
# Python install with stripped data files), we still need *something*
# the renderer can paint with. This is the absolute floor — a working
# but minimal Steel-shaped palette so `successor chat` never crashes on a
# bad install.

_FALLBACK_VARIANT_DARK = ThemeVariant(
    bg=oklch_to_rgb(0.06, 0.008, 260),
    bg_input=oklch_to_rgb(0.09, 0.008, 260),
    bg_footer=oklch_to_rgb(0.14, 0.008, 260),
    fg=oklch_to_rgb(0.92, 0.005, 260),
    fg_dim=oklch_to_rgb(0.68, 0.005, 260),
    fg_subtle=oklch_to_rgb(0.50, 0.008, 260),
    accent=oklch_to_rgb(0.68, 0.18, 260),
    accent_warm=oklch_to_rgb(0.72, 0.16, 85),
    accent_warn=oklch_to_rgb(0.62, 0.20, 25),
)

_FALLBACK_VARIANT_LIGHT = ThemeVariant(
    bg=oklch_to_rgb(0.97, 0.003, 260),
    bg_input=oklch_to_rgb(0.99, 0.002, 260),
    bg_footer=oklch_to_rgb(1.00, 0.000, 0),
    fg=oklch_to_rgb(0.15, 0.01, 260),
    fg_dim=oklch_to_rgb(0.40, 0.01, 260),
    fg_subtle=oklch_to_rgb(0.58, 0.008, 260),
    accent=oklch_to_rgb(0.50, 0.20, 260),
    accent_warm=oklch_to_rgb(0.50, 0.16, 85),
    accent_warn=oklch_to_rgb(0.48, 0.20, 25),
)

_FALLBACK_THEME = Theme(
    name="steel",
    icon="\u25c6",  # ◆
    description="fallback steel — registry unavailable",
    dark=_FALLBACK_VARIANT_DARK,
    light=_FALLBACK_VARIANT_LIGHT,
)


# ─── Display mode helpers ───


VALID_DISPLAY_MODES: tuple[str, ...] = ("dark", "light")


def normalize_display_mode(mode: object) -> str:
    """Coerce an arbitrary value into a valid display mode string.

    Used by config loading where the saved value could be anything.
    Anything other than "light" returns "dark" — same fallback rule
    as Theme.variant().
    """
    if isinstance(mode, str) and mode.strip().lower() == "light":
        return "light"
    return "dark"


def toggle_display_mode(mode: str) -> str:
    """Flip between dark and light."""
    return "light" if mode == "dark" else "dark"
