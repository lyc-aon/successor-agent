"""Theme system — semantic color palettes for the chat UI.

Colors are computed at import time from oklch (perceptual) values
ported from Lycaon's ComPress design system (`packages/frontend/src/app.css`,
"Instrument Panel" admin theme, hue 260). The same dataclass holds
both light and dark variants so demos and apps can swap them at any
time without rebuilding the renderer.

Why oklch: it's a perceptually uniform color space, so when you take
a color and lower its lightness L by 0.1, the result LOOKS exactly
0.1 darker to a human. This makes the relationship between dark and
light themes mathematically clean — they share chroma + hue and only
differ in lightness. Try doing that with HSL or RGB and the relative
darkness varies wildly across hues.

Why we ported the values: ComPress uses the same palette across its
admin and storefront. Reusing it in Ronin's terminal chat means the
same design language extends from "browse the storefront on mobile"
to "talk to the agent in a terminal" without color drift.

Public surface:
    Theme              dataclass with 9 semantic color slots
    DARK_THEME         dark instance (the default)
    LIGHT_THEME        light instance
    THEMES             tuple of (DARK_THEME, LIGHT_THEME)
    next_theme(t)      cycle to the next theme
    find_theme(name)   look up a theme by name
    blend_themes(a, b, t)  lerp every slot for smooth transitions
    oklch_to_rgb(L, C, h)  the conversion helper itself
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .text import lerp_rgb


# ─── oklch → sRGB conversion ───


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


# ─── Theme dataclass ───


@dataclass(frozen=True, slots=True)
class Theme:
    """A complete color palette for the chat UI.

    Nine semantic slots that every painted region maps into. The slot
    names describe how the color is used, not the color itself — that's
    what makes themes hot-swappable.
    """

    name: str
    icon: str  # one-character glyph for the toggle widget

    # Backgrounds — three depths so we can layer regions visually
    bg: int           # main background (chat area, title row)
    bg_input: int     # input area background
    bg_footer: int    # static footer background

    # Foreground text — three weights of contrast
    fg: int           # primary text (default messages, title)
    fg_dim: int       # dim text (synthetic messages, hints, dim labels)
    fg_subtle: int    # very dim (fade-in start, empty progress, separators)

    # Accents — primary brand color + warm and warning variants
    accent: int       # ronin messages, prompt indicator, progress fill
    accent_warm: int  # secondary accent (mid progress, scroll indicator)
    accent_warn: int  # warning (high progress, errors)


# ─── ComPress oklch palette translation ───


DARK_THEME: Theme = Theme(
    name="dark",
    icon="\u263e",  # ☾
    bg=oklch_to_rgb(0.06, 0.008, 260),       # admin-bg
    bg_input=oklch_to_rgb(0.09, 0.008, 260), # admin-surface
    bg_footer=oklch_to_rgb(0.14, 0.008, 260),# admin-elevated
    fg=oklch_to_rgb(0.92, 0.005, 260),       # admin-ink
    fg_dim=oklch_to_rgb(0.68, 0.005, 260),   # admin-dim
    fg_subtle=oklch_to_rgb(0.50, 0.008, 260),# admin-muted
    accent=oklch_to_rgb(0.68, 0.18, 260),    # admin-accent
    accent_warm=oklch_to_rgb(0.72, 0.16, 85),# admin-warning (amber)
    accent_warn=oklch_to_rgb(0.62, 0.20, 25),# admin-error (orange-red)
)


LIGHT_THEME: Theme = Theme(
    name="light",
    icon="\u2600",  # ☀
    bg=oklch_to_rgb(0.97, 0.003, 260),       # admin-bg
    bg_input=oklch_to_rgb(0.99, 0.002, 260), # admin-surface
    bg_footer=oklch_to_rgb(1.00, 0.000, 0),  # admin-elevated
    fg=oklch_to_rgb(0.15, 0.01, 260),        # admin-ink
    fg_dim=oklch_to_rgb(0.40, 0.01, 260),    # admin-dim
    fg_subtle=oklch_to_rgb(0.58, 0.008, 260),# admin-muted
    accent=oklch_to_rgb(0.50, 0.20, 260),    # admin-accent (saturated)
    accent_warm=oklch_to_rgb(0.50, 0.16, 85),
    accent_warn=oklch_to_rgb(0.48, 0.20, 25),
)


# Registry — order matters for cycling.
THEMES: tuple[Theme, ...] = (DARK_THEME, LIGHT_THEME)


def next_theme(current: Theme) -> Theme:
    """Cycle to the next theme in the registry."""
    try:
        idx = THEMES.index(current)
    except ValueError:
        return THEMES[0]
    return THEMES[(idx + 1) % len(THEMES)]


def find_theme(name: str) -> Theme | None:
    """Look up a theme by name (case-insensitive). Returns None if missing."""
    n = name.strip().lower()
    for t in THEMES:
        if t.name == n:
            return t
    return None


# ─── Animated transitions ───


def blend_themes(a: Theme, b: Theme, t: float) -> Theme:
    """Linearly interpolate every color slot between two themes.

    Used for smooth transitions when the user switches themes mid-frame.
    name and icon are taken from the destination theme so the indicator
    shows what we're heading toward, not what we came from.

    t is clamped to [0, 1].
    """
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    return Theme(
        name=b.name,
        icon=b.icon,
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
