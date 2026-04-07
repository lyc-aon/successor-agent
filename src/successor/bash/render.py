"""ToolCard renderer — pure paint function that draws a parsed bash
command card into a Grid.

This module is the bash subsystem's renderer. It lives here (not in
`render/paint.py`) so the renderer's primitives stay generic — paint.py
knows nothing about bash. The bash package imports paint primitives,
not the other way around.

Layout:

    ╭─ read-file ──────────────────────────────────────╮
    │  path     README.md                              │
    │  bytes    1247                                   │
    ╰─ $ cat README.md ────────────────────────────────╯
       # Successor Agent

       An omni-agent harness for locally-run...
       ↳ exit 0 in 12.4ms

The top section is the parsed verb + params table inside a rounded
box. The bottom border carries the raw command verbatim (so the user
can spot parser misses) prefixed with `$ `. Below the box, the
command's output streams as code-tinted text. A trailing status line
shows exit code + duration.

Risk-tinted border:
    safe        theme.accent       (subtle, ambient)
    mutating    theme.accent_warm  (warm, attention)
    dangerous   theme.accent_warn  (red, urgent) + ⚠ glyph in header

Confidence < 0.7 adds a `?` badge after the verb so the user knows
the parser was unsure.
"""

from __future__ import annotations

from ..render.cells import (
    ATTR_BOLD,
    ATTR_DIM,
    ATTR_ITALIC,
    Cell,
    Grid,
    Style,
)
from ..render.paint import (
    BOX_ROUND,
    fill_region,
    paint_box,
    paint_text,
)
from ..render.theme import ThemeVariant
from .cards import Risk, ToolCard
from .verbclass import VerbClass, glyph_for_class, verb_class_for


# ─── Constants ───

# How many output lines we show inline before "more lines" hint kicks in.
DEFAULT_MAX_OUTPUT_LINES = 12

# How wide the param label column gets, max. Wider labels wrap to
# next line for readability.
MAX_LABEL_WIDTH = 16

# Visual padding inside the card body
CARD_INNER_PAD_X = 2

# Output indent (matches box's left edge + 3 cells for visual flow)
OUTPUT_INDENT = 3


# ─── Risk → border color ───


def _border_color(risk: Risk, theme: ThemeVariant) -> int:
    if risk == "dangerous":
        return theme.accent_warn
    if risk == "mutating":
        return theme.accent_warm
    return theme.accent


def _verb_glyph_for_card(card: ToolCard) -> str:
    """Glyph that prefixes the verb in the card header.

    Verb-class-aware: READ cards show ◲, SEARCH cards ⌕, LIST cards ☰,
    MUTATE cards ✎, etc. DANGER (risk-escalated) always gets the ⚠
    glyph regardless of verb. This is the user's primary peripheral-
    vision cue — scrolling through a long chat, the glyphs alone
    make the card kind recognizable without reading verb text.
    """
    cls = verb_class_for(card.verb, card.risk)
    return glyph_for_class(cls) + " "


# ─── Height computation ───


def measure_tool_card_height(
    card: ToolCard,
    *,
    width: int,
    show_output: bool = True,
    max_output_lines: int = DEFAULT_MAX_OUTPUT_LINES,
) -> int:
    """Compute the total height a ToolCard would consume at this width.

    Used by callers that need to lay out the card *before* painting
    (e.g., the chat painter computing scroll geometry). Pure function
    of the card data + width.
    """
    if width < 20:
        return 0

    # Box: top border + N param rows + bottom border
    n_params = max(1, len(card.params)) if card.params else 1
    box_h = 2 + n_params  # top + params + bottom

    if not show_output:
        return box_h

    # Output rows + status line
    out_lines = _wrap_output(card, width=width, max_lines=max_output_lines)
    return box_h + len(out_lines) + 1  # +1 for the trailing status line


# ─── Public paint entry point ───


def paint_tool_card(
    grid: Grid,
    card: ToolCard,
    *,
    x: int,
    y: int,
    w: int,
    theme: ThemeVariant,
    show_output: bool = True,
    max_output_lines: int = DEFAULT_MAX_OUTPUT_LINES,
) -> int:
    """Paint `card` at (x, y) with width `w`. Returns rows consumed.

    The painter is fully self-contained — pass a card and a region
    and it draws everything (box, params, raw command, output, status).
    Returns the actual height drawn so callers can stack cards.

    All painting goes through render/paint.py primitives — no direct
    grid.set() calls outside the box header overlay.
    """
    if w < 20 or y >= grid.rows:
        return 0

    border = _border_color(card.risk, theme)

    # ─── Compute box dimensions ───
    n_params = max(1, len(card.params)) if card.params else 1
    box_h = 2 + n_params
    box_w = w

    # ─── Box border + interior fill ───
    border_style = Style(fg=border, bg=theme.bg, attrs=ATTR_BOLD)
    inner_style = Style(fg=theme.fg, bg=theme.bg_input)
    paint_box(
        grid, x, y, box_w, box_h,
        style=border_style,
        fill_style=inner_style,
        chars=BOX_ROUND,
    )

    # ─── Header pill — verb + class glyph + confidence badge ───
    verb_text = card.verb
    glyph = _verb_glyph_for_card(card)
    confidence_badge = " ?" if card.confidence < 0.7 else ""
    header_text = f" {glyph}{verb_text}{confidence_badge} "
    # Truncate if it would overflow the top border
    max_header_w = box_w - 4
    if len(header_text) > max_header_w:
        header_text = header_text[: max(0, max_header_w - 1)] + "…"
    header_x = x + 3
    if 0 <= y < grid.rows and header_x < x + box_w - 1:
        paint_text(
            grid, header_text, header_x, y,
            style=Style(fg=theme.bg, bg=border, attrs=ATTR_BOLD),
        )

    # ─── Param rows inside the box ───
    label_w = min(
        MAX_LABEL_WIDTH,
        max((len(k) for k, _ in card.params), default=0),
    )
    body_x = x + CARD_INNER_PAD_X
    body_y = y + 1
    body_w = box_w - 2 * CARD_INNER_PAD_X

    if not card.params:
        # Empty param row — show "(no parameters)" so the box doesn't look empty
        if body_y < grid.rows:
            paint_text(
                grid, "(no parameters)", body_x + 1, body_y,
                style=Style(
                    fg=theme.fg_subtle, bg=theme.bg_input,
                    attrs=ATTR_DIM | ATTR_ITALIC,
                ),
            )
    else:
        for i, (key, value) in enumerate(card.params):
            row_y = body_y + i
            if row_y >= y + box_h - 1 or row_y >= grid.rows:
                break
            label = key.rjust(label_w)
            paint_text(
                grid, label, body_x + 1, row_y,
                style=Style(fg=theme.fg_dim, bg=theme.bg_input, attrs=ATTR_DIM),
            )
            value_x = body_x + 1 + label_w + 2
            value_max = body_w - (value_x - body_x) - 1
            value_text = str(value)
            if len(value_text) > value_max:
                value_text = value_text[: max(0, value_max - 1)] + "…"
            paint_text(
                grid, value_text, value_x, row_y,
                style=Style(fg=theme.fg, bg=theme.bg_input, attrs=ATTR_BOLD),
            )

    # ─── Raw command on the bottom border ───
    raw = card.raw_command
    raw_label = f" $ {raw} "
    max_raw = box_w - 4
    if len(raw_label) > max_raw:
        raw_label = raw_label[: max(0, max_raw - 1)] + "…"
    bot_y = y + box_h - 1
    raw_x = x + 3
    if 0 <= bot_y < grid.rows and raw_x < x + box_w - 1:
        paint_text(
            grid, raw_label, raw_x, bot_y,
            style=Style(
                fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_DIM | ATTR_ITALIC,
            ),
        )

    cur_y = y + box_h

    # ─── Output below the box ───
    if not show_output:
        return cur_y - y

    # No output yet (parse-only / preview) — skip output entirely
    if not card.executed:
        return cur_y - y

    out_lines = _wrap_output(card, width=w, max_lines=max_output_lines)
    out_x = x + OUTPUT_INDENT
    for line_text, line_style_kind in out_lines:
        if cur_y >= grid.rows:
            break
        # Tinted background bar across the output region
        fill_region(
            grid, x + 1, cur_y, w - 2, 1,
            style=Style(bg=theme.bg_input),
        )
        if line_style_kind == "stderr":
            line_style = Style(
                fg=theme.accent_warn, bg=theme.bg_input, attrs=ATTR_DIM,
            )
        elif line_style_kind == "truncated":
            line_style = Style(
                fg=theme.fg_subtle, bg=theme.bg_input,
                attrs=ATTR_DIM | ATTR_ITALIC,
            )
        else:
            line_style = Style(fg=theme.fg, bg=theme.bg_input)
        paint_text(grid, line_text, out_x, cur_y, style=line_style)
        cur_y += 1

    # ─── Status footer — exit code + duration ───
    if cur_y < grid.rows:
        status_glyph = "✓" if card.succeeded else "✗"
        status_color = theme.accent if card.succeeded else theme.accent_warn
        dur_ms = card.duration_ms or 0.0
        if dur_ms < 1000:
            dur_text = f"{dur_ms:.0f}ms"
        else:
            dur_text = f"{dur_ms / 1000:.1f}s"
        status_text = f"  ↳ {status_glyph} exit {card.exit_code} in {dur_text}"
        if card.truncated:
            status_text += "  · output truncated"
        paint_text(
            grid, status_text, x + OUTPUT_INDENT, cur_y,
            style=Style(
                fg=status_color, bg=theme.bg, attrs=ATTR_DIM | ATTR_BOLD,
            ),
        )
        cur_y += 1

    return cur_y - y


# ─── Output wrapping ───


def _wrap_output(
    card: ToolCard,
    *,
    width: int,
    max_lines: int,
) -> list[tuple[str, str]]:
    """Convert card.output and card.stderr into a list of (text, kind) rows.

    kind is one of "stdout", "stderr", "truncated". Hard-wraps at the
    available width, drops empty trailing lines, and truncates the
    list at max_lines, leaving a "(N more lines)" trailer.
    """
    avail = max(20, width - OUTPUT_INDENT - 2)
    rows: list[tuple[str, str]] = []

    def _push_text(text: str, kind: str) -> None:
        for line in text.split("\n"):
            line = line.rstrip("\r")
            if not line:
                rows.append(("", kind))
                continue
            # Hard-wrap long lines
            while len(line) > avail:
                rows.append((line[:avail], kind))
                line = line[avail:]
            rows.append((line, kind))

    if card.output:
        _push_text(card.output, "stdout")
    if card.stderr:
        _push_text(card.stderr, "stderr")

    # Drop trailing blanks
    while rows and rows[-1][0] == "":
        rows.pop()

    if not rows:
        return [("(no output)", "truncated")]

    if len(rows) > max_lines:
        hidden = len(rows) - max_lines + 1
        rows = rows[: max_lines - 1]
        rows.append((f"⋯ {hidden} more line{'s' if hidden != 1 else ''} ⋯", "truncated"))

    return rows
