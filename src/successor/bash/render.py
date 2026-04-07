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
from .prepared_output import OutputLine, OutputSpan, PreparedToolOutput
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


def _span_style(
    span_kind: str, row_kind: str, theme: ThemeVariant,
) -> Style:
    """Resolve a Style for a (span_kind, row_kind) pair.

    Span kinds are per-substring: plain / match / chrome / dim / warn.
    Row kinds are per-line: stdout / stderr / match / truncated /
    header. Row kind informs the base bg/fg tint; span kind overlays
    a more specific treatment (the match highlighter painting a cell
    bg different from its neighbors).

    The mapping below is the single source of truth for how verb-
    class-aware output surfaces translate to renderer styles. When a
    new span/row kind is added, teach this function how to paint it.
    """
    base_bg = theme.bg_input

    # Row-level base: stderr lines get a warn-tinted base fg; header
    # lines (ls "total N") get the subtle treatment; the rest default
    # to the theme's normal fg. Truncated rows mimic the old dim italic.
    if row_kind == "stderr":
        base_fg = theme.accent_warn
        base_attrs = ATTR_DIM
    elif row_kind == "truncated":
        base_fg = theme.fg_subtle
        base_attrs = ATTR_DIM | ATTR_ITALIC
    elif row_kind == "header":
        base_fg = theme.fg_subtle
        base_attrs = ATTR_DIM
    else:
        base_fg = theme.fg
        base_attrs = 0

    # Span-level overlay
    if span_kind == "match":
        # Match spans get a warm-accent background so grep hits pop
        return Style(
            fg=theme.bg, bg=theme.accent_warm,
            attrs=ATTR_BOLD,
        )
    if span_kind == "chrome":
        return Style(
            fg=theme.accent, bg=base_bg, attrs=ATTR_BOLD,
        )
    if span_kind == "dim":
        return Style(
            fg=theme.fg_dim, bg=base_bg, attrs=ATTR_DIM,
        )
    if span_kind == "warn":
        return Style(
            fg=theme.accent_warn, bg=base_bg, attrs=ATTR_BOLD,
        )

    # Plain span inherits the row's base treatment
    return Style(fg=base_fg, bg=base_bg, attrs=base_attrs)


def _paint_output_line(
    grid: Grid,
    line: OutputLine,
    x: int,
    y: int,
    theme: ThemeVariant,
) -> None:
    """Paint one OutputLine to the grid at (x, y).

    Walks the line's spans left-to-right, painting each with the
    style resolved from (span_kind, row_kind). The grid's fill has
    already been applied to the background by the caller.
    """
    cursor = x
    for span in line.spans:
        if not span.text:
            continue
        style = _span_style(span.kind, line.kind, theme)
        paint_text(grid, span.text, cursor, y, style=style)
        cursor += len(span.text)


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
    prepared: PreparedToolOutput | None = None,
) -> int:
    """Compute the total height a ToolCard would consume at this width.

    Used by callers that need to lay out the card *before* painting
    (e.g., the chat painter computing scroll geometry). Pure function
    of the card data + width.

    Pass `prepared` if you already have a PreparedToolOutput for this
    card (e.g. cached on the wrapping chat message); otherwise one is
    constructed inline and thrown away after this call.
    """
    if width < 20:
        return 0

    # Box: top border + N param rows + bottom border
    n_params = max(1, len(card.params)) if card.params else 1
    box_h = 2 + n_params  # top + params + bottom

    if not show_output or not card.executed:
        return box_h

    # Output rows + status line
    prep = prepared if prepared is not None else PreparedToolOutput(card)
    avail = max(20, width - OUTPUT_INDENT - 2)
    out_lines = prep.layout(avail, max_lines=max_output_lines)
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
    prepared: PreparedToolOutput | None = None,
) -> int:
    """Paint `card` at (x, y) with width `w`. Returns rows consumed.

    The painter is fully self-contained — pass a card and a region
    and it draws everything (box, params, raw command, output, status).
    Returns the actual height drawn so callers can stack cards.

    All painting goes through render/paint.py primitives — no direct
    grid.set() calls outside the box header overlay.

    `prepared` is an optional PreparedToolOutput cached on the calling
    side (chat messages hold one per tool card). Passing a cached
    instance skips re-parsing the output every frame. If omitted a
    fresh instance is built for this paint and discarded.
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

    prep = prepared if prepared is not None else PreparedToolOutput(card)
    avail = max(20, w - OUTPUT_INDENT - 2)
    out_lines = prep.layout(avail, max_lines=max_output_lines)
    out_x = x + OUTPUT_INDENT
    for line in out_lines:
        if cur_y >= grid.rows:
            break
        # Tinted background bar across the output region
        fill_region(
            grid, x + 1, cur_y, w - 2, 1,
            style=Style(bg=theme.bg_input),
        )
        _paint_output_line(
            grid, line, out_x, cur_y, theme,
        )
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


# Output wrapping moved to prepared_output.PreparedToolOutput.layout
