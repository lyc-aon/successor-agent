"""Row primitives and row painters for the chat scene."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from .cells import ATTR_BOLD, Cell, Grid, Style
from .markdown import LaidOutLine, LaidOutSpan
from .paint import fill_region, paint_horizontal_divider, paint_text
from .text import lerp_rgb
from .theme import ThemeVariant
from ..bash import measure_tool_card_height, paint_tool_card
from ..bash.prepared_output import PreparedToolOutput
from ..bash.render import (
    measure_tool_card_running_height,
    paint_tool_card_running,
)
from ..subagents.render import (
    measure_subagent_card_height,
    paint_subagent_card,
)


@dataclass(slots=True)
class RenderedRow:
    leading_text: str = ""
    leading_attrs: int = 0
    leading_color_kind: str = "accent"
    body_spans: tuple = ()
    base_color: int = 0
    line_tag: str = ""
    body_indent: int = 0
    prepainted_cells: tuple = ()
    is_boundary: bool = False
    boundary_meta: object | None = None
    materialize_t: float = 1.0
    is_summary: bool = False
    fade_alpha: float = 1.0


def fade_prepainted_rows(
    rows: list[RenderedRow],
    bg_color: int,
    toward_bg_amount: float,
) -> list[RenderedRow]:
    if toward_bg_amount <= 0:
        return rows
    out: list[RenderedRow] = []
    for row in rows:
        if not row.prepainted_cells:
            out.append(row)
            continue
        new_cells = tuple(
            Cell(
                cell.char,
                Style(
                    fg=lerp_rgb(cell.style.fg, bg_color, toward_bg_amount),
                    bg=lerp_rgb(cell.style.bg, bg_color, toward_bg_amount),
                    attrs=cell.style.attrs,
                ),
                wide_tail=cell.wide_tail,
            )
            for cell in row.prepainted_cells
        )
        out.append(
            RenderedRow(
                leading_text=row.leading_text,
                leading_attrs=row.leading_attrs,
                leading_color_kind=row.leading_color_kind,
                body_spans=row.body_spans,
                base_color=row.base_color,
                line_tag=row.line_tag,
                body_indent=row.body_indent,
                prepainted_cells=new_cells,
                is_boundary=row.is_boundary,
                boundary_meta=row.boundary_meta,
                materialize_t=row.materialize_t,
                is_summary=row.is_summary,
                fade_alpha=row.fade_alpha,
            )
        )
    return out


def render_tool_card_rows(
    msg: Any,
    body_width: int,
    theme: ThemeVariant,
) -> list[RenderedRow]:
    if getattr(msg, "subagent_card", None) is not None:
        return render_subagent_card_rows(msg, body_width, theme)

    card = msg.tool_card
    if card is None:
        return []

    runner = msg.running_tool
    if runner is not None:
        return render_running_tool_card_rows(msg, body_width, theme, runner)

    if msg._prepared_tool_output is None:
        msg._prepared_tool_output = PreparedToolOutput(card)
    prepared = msg._prepared_tool_output

    cache_key = (body_width, id(theme))
    if (
        msg._card_rows_cache_key == cache_key
        and msg._card_rows_cache is not None
    ):
        return msg._card_rows_cache

    height = measure_tool_card_height(
        card, width=body_width, show_output=card.executed,
        prepared=prepared,
    )
    if height <= 0:
        return []

    sub = Grid(height, body_width)
    paint_tool_card(
        sub, card, x=0, y=0, w=body_width, theme=theme,
        prepared=prepared,
    )

    rows: list[RenderedRow] = []
    for sy in range(height):
        cells: list[Cell] = []
        for sx in range(body_width):
            cells.append(sub.at(sy, sx))
        rows.append(
            RenderedRow(
                leading_text="",
                leading_attrs=0,
                leading_color_kind="accent",
                body_spans=(),
                base_color=theme.fg,
                line_tag="tool_card",
                body_indent=0,
                prepainted_cells=tuple(cells),
            )
        )

    msg._card_rows_cache_key = cache_key
    msg._card_rows_cache = rows
    return rows


def render_running_tool_card_rows(
    msg: Any,
    body_width: int,
    theme: ThemeVariant,
    runner: Any,
) -> list[RenderedRow]:
    preview = msg.tool_card
    if preview is None:
        return []

    now = time.monotonic()
    stdout = runner.stdout
    stderr = runner.stderr

    height = measure_tool_card_running_height(
        preview, width=body_width,
        runner_stdout=stdout, runner_stderr=stderr,
    )
    if height <= 0:
        return []

    sub = Grid(height, body_width)
    paint_tool_card_running(
        sub, preview, x=0, y=0, w=body_width, theme=theme,
        runner_stdout=stdout, runner_stderr=stderr,
        elapsed_s=runner.elapsed(now),
        now=now,
    )

    rows: list[RenderedRow] = []
    for sy in range(height):
        cells: list[Cell] = []
        for sx in range(body_width):
            cells.append(sub.at(sy, sx))
        rows.append(
            RenderedRow(
                leading_text="",
                leading_attrs=0,
                leading_color_kind="accent",
                body_spans=(),
                base_color=theme.fg,
                line_tag="tool_card",
                body_indent=0,
                prepainted_cells=tuple(cells),
            )
        )
    return rows


def render_subagent_card_rows(
    msg: Any,
    body_width: int,
    theme: ThemeVariant,
) -> list[RenderedRow]:
    card = msg.subagent_card
    if card is None:
        return []

    cache_key = (body_width, id(theme))
    if (
        msg._card_rows_cache_key == cache_key
        and msg._card_rows_cache is not None
    ):
        return msg._card_rows_cache

    height = measure_subagent_card_height(card, width=body_width)
    if height <= 0:
        return []

    sub = Grid(height, body_width)
    paint_subagent_card(sub, card, x=0, y=0, w=body_width, theme=theme)

    rows: list[RenderedRow] = []
    for sy in range(height):
        cells: list[Cell] = []
        for sx in range(body_width):
            cells.append(sub.at(sy, sx))
        rows.append(
            RenderedRow(
                leading_text="",
                leading_attrs=0,
                leading_color_kind="accent",
                body_spans=(),
                base_color=theme.fg,
                line_tag="tool_card",
                body_indent=0,
                prepainted_cells=tuple(cells),
            )
        )
    msg._card_rows_cache_key = cache_key
    msg._card_rows_cache = rows
    return rows


def render_md_lines_with_search(
    md_lines: list[LaidOutLine],
    query: str,
    matches: list[tuple[int, int, int]],
    prefix: str,
    base_color: int,
    *,
    prefix_width: int,
) -> list[RenderedRow]:
    out: list[RenderedRow] = []
    for line_idx, md_line in enumerate(md_lines):
        if line_idx == 0:
            leading = prefix
            leading_attrs = ATTR_BOLD
        else:
            leading = " " * prefix_width
            leading_attrs = 0

        if query and matches:
            new_spans = tuple(highlight_spans(md_line.spans, query))
        else:
            new_spans = tuple(md_line.spans)

        out.append(
            RenderedRow(
                leading_text=leading,
                leading_attrs=leading_attrs,
                leading_color_kind="accent",
                body_spans=new_spans,
                base_color=base_color,
                line_tag=md_line.line_tag,
                body_indent=md_line.indent,
            )
        )
    return out


def highlight_spans(
    spans: list[LaidOutSpan],
    query: str,
) -> list[LaidOutSpan]:
    result: list[LaidOutSpan] = []
    for span in spans:
        if not query or span.tag == "code_lang":
            result.append(span)
            continue
        text = span.text
        text_lower = text.lower()
        i = 0
        n = len(text)
        qlen = len(query)
        while i < n:
            idx = text_lower.find(query, i)
            if idx < 0:
                result.append(
                    LaidOutSpan(
                        text=text[i:],
                        attrs=span.attrs,
                        tag=span.tag,
                        link=span.link,
                    )
                )
                break
            if idx > i:
                result.append(
                    LaidOutSpan(
                        text=text[i:idx],
                        attrs=span.attrs,
                        tag=span.tag,
                        link=span.link,
                    )
                )
            result.append(
                LaidOutSpan(
                    text=text[idx:idx + qlen],
                    attrs=span.attrs,
                    tag="search_hit",
                    link=span.link,
                )
            )
            i = idx + qlen
    return result


def paint_chat_row(
    grid: Grid,
    x: int,
    y: int,
    body_width: int,
    row: RenderedRow,
    theme: ThemeVariant,
    *,
    prefix_width: int,
    elapsed: float,
) -> None:
    if row.prepainted_cells:
        for col_offset, cell in enumerate(row.prepainted_cells):
            cx = x + col_offset
            if cx >= grid.cols or col_offset >= body_width:
                break
            if cell.wide_tail:
                continue
            grid.set(y, cx, cell)
        return

    if row.is_boundary:
        pulse_phase = elapsed if row.materialize_t >= 1.0 else 0.0
        paint_compaction_boundary(
            grid, x, y, body_width, theme,
            boundary=row.boundary_meta,
            materialize_t=row.materialize_t,
            pulse_phase=pulse_phase,
        )
        return

    line_bg = theme.bg
    if row.line_tag in ("code_block", "code_lang"):
        line_bg = theme.bg_input if row.line_tag == "code_block" else theme.bg_footer
        fill_region(
            grid, x, y, body_width, 1,
            style=Style(bg=line_bg),
        )
    elif row.line_tag == "header_rule":
        rule_text = "─" * max(0, body_width - prefix_width)
        paint_text(
            grid, rule_text, x + prefix_width, y,
            style=Style(fg=theme.fg_subtle, bg=theme.bg),
        )
        return
    elif row.line_tag == "hr":
        rule_text = "─" * max(0, body_width - prefix_width)
        paint_text(
            grid, rule_text, x + prefix_width, y,
            style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_BOLD),
        )
        return

    def faded(fg: int) -> int:
        if row.fade_alpha >= 1.0:
            return fg
        return lerp_rgb(theme.bg, fg, row.fade_alpha)

    leading_text = row.leading_text
    if leading_text:
        leading_color = resolve_leading_color(
            row.leading_color_kind, row.base_color, theme
        )
        leading_style = Style(
            fg=faded(leading_color),
            bg=line_bg,
            attrs=row.leading_attrs,
        )
        paint_text(grid, leading_text, x, y, style=leading_style)
        cx = x + len(leading_text)
    else:
        cx = x

    if row.line_tag == "blockquote":
        paint_text(
            grid, "▎", cx, y,
            style=Style(fg=faded(theme.accent_warm), bg=line_bg, attrs=ATTR_BOLD),
        )
        cx += 2

    cx += row.body_indent

    for span in row.body_spans:
        style = resolve_span_style(span, row.base_color, line_bg, theme)
        if row.fade_alpha < 1.0:
            style = Style(
                fg=faded(style.fg),
                bg=style.bg,
                attrs=style.attrs,
            )
        paint_text(grid, span.text, cx, y, style=style)
        cx += sum(1 for _ in span.text)


def paint_compaction_boundary(
    grid: Grid,
    x: int,
    y: int,
    body_width: int,
    theme: ThemeVariant,
    *,
    boundary: object | None,
    materialize_t: float = 1.0,
    pulse_phase: float = 0.0,
) -> None:
    if body_width < 12:
        return

    if boundary is not None:
        pill_text = format_boundary_pill(boundary)
    else:
        pill_text = " compaction "

    base_color = theme.accent_warm
    if pulse_phase > 0:
        pulse = 0.5 + 0.5 * math.sin(pulse_phase * 2 * math.pi * 0.4)
        base_color = lerp_rgb(theme.accent_warm, theme.accent, pulse * 0.3)

    line_style = Style(fg=base_color, bg=theme.bg, attrs=ATTR_BOLD)
    pill_style = Style(fg=theme.bg, bg=base_color, attrs=ATTR_BOLD)
    bracket_style = Style(fg=base_color, bg=theme.bg, attrs=ATTR_BOLD)

    paint_horizontal_divider(
        grid, x, y, body_width,
        style=line_style,
        char="━",
        t=materialize_t,
    )

    if materialize_t < 0.6 or not pill_text:
        return

    pill_w = len(pill_text) + 2
    if pill_w >= body_width - 4:
        return
    pill_x = x + (body_width - pill_w) // 2

    if 0 <= pill_x < grid.cols:
        grid.set(y, pill_x, Cell("┤", bracket_style))
    pill_alpha = max(0.0, min(1.0, (materialize_t - 0.6) / 0.4))
    if pill_alpha < 1.0:
        faded_bg = lerp_rgb(theme.bg, base_color, pill_alpha)
        pill_style = Style(
            fg=theme.bg if pill_alpha > 0.5 else faded_bg,
            bg=faded_bg,
            attrs=ATTR_BOLD,
        )
    paint_text(
        grid, pill_text, pill_x + 1, y,
        style=pill_style,
    )
    right_bracket_x = pill_x + 1 + len(pill_text)
    if 0 <= right_bracket_x < grid.cols:
        grid.set(y, right_bracket_x, Cell("├", bracket_style))


def format_boundary_pill(boundary: object) -> str:
    try:
        n_rounds = getattr(boundary, "rounds_summarized", 0)
        pre = getattr(boundary, "pre_compact_tokens", 0)
        post = getattr(boundary, "post_compact_tokens", 0)
        reduction = getattr(boundary, "reduction_pct", 0.0)
    except Exception:
        return " ▼ compaction ▼ "

    def fmt_tokens(n: int) -> str:
        if n >= 1000:
            return f"{n / 1000:.0f}k"
        return str(n)

    return (
        f" ▼ {n_rounds} rounds · {fmt_tokens(pre)} → "
        f"{fmt_tokens(post)} · {reduction:.0f}% saved ▼ "
    )


def resolve_leading_color(kind: str, base_color: int, theme: ThemeVariant) -> int:
    if kind == "fg":
        return theme.fg
    if kind == "fg_dim":
        return theme.fg_dim
    return base_color


def resolve_span_style(
    span: LaidOutSpan,
    base_color: int,
    line_bg: int,
    theme: ThemeVariant,
) -> Style:
    attrs = span.attrs
    if span.tag == "search_hit":
        return Style(
            fg=theme.bg, bg=theme.accent_warm, attrs=attrs | ATTR_BOLD
        )
    if span.tag == "code":
        return Style(fg=theme.fg, bg=theme.bg_input, attrs=attrs)
    if span.tag == "link":
        return Style(fg=theme.accent_warm, bg=line_bg, attrs=attrs)
    if span.tag == "header":
        return Style(fg=theme.accent, bg=line_bg, attrs=attrs)
    if span.tag == "list_marker":
        return Style(fg=theme.accent_warm, bg=line_bg, attrs=attrs)
    if span.tag == "code_lang":
        return Style(fg=theme.fg_dim, bg=line_bg, attrs=attrs)
    return Style(fg=base_color, bg=line_bg, attrs=attrs)
