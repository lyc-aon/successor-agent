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

import math

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
from ..render.text import lerp_rgb
from ..render.theme import ThemeVariant
from .cards import Risk, ToolCard
from .prepared_output import OutputLine, OutputSpan, PreparedToolOutput
from .verbclass import VerbClass, glyph_for_class, verb_class_for


# Spinner frames shared with the chat's "thinking" indicator. Imported
# locally so the bash module stays independent from chat.py — copy of
# the canonical sequence, not a re-import.
_RUNNER_SPINNER_FRAMES: tuple[str, ...] = (
    "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
)
_RUNNER_SPINNER_FPS: float = 12.0
# Border pulse frequency for the running state (Hz). 0.6 Hz is a
# slow breathing cadence — fast enough to read as alive, slow enough
# to not feel anxious. Matches the compaction settled-phase pulse.
_RUNNER_PULSE_HZ: float = 0.6


# ─── Constants ───

# Visible row cap for the settled card's output region. Long outputs
# (cat of a big file, ls -la /usr/lib, grep with many matches) get
# clipped to this many rows with a "⋯ +N more lines ⋯" overflow
# marker, keeping the card compact in the chat flow. The full
# untrimmed output (up to the exec-layer byte cap) is still passed
# to the model via _tool_card_content_for_api so the next turn can
# reason about the whole result — this cap is a DISPLAY concern only.
#
# 5 rows matches the streaming preview's scrolling window size, so
# the visual weight of a settled card and the in-flight preview
# stay consistent.
DEFAULT_MAX_OUTPUT_LINES = 5

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


def _first_line_of_raw_command(raw: str) -> tuple[str, int]:
    """Return (first_line, count_of_additional_lines).

    Heredoc writes, multi-step scripts, function definitions and
    control structures all produce multi-line raw_commands. The tool
    card's bottom border can only show ONE physical row, so we split
    off the first line and report how many more exist so the painter
    can append a "(+N lines)" hint.

    Blank / whitespace-only trailing lines are NOT counted because
    bash blocks often have a trailing newline that the split would
    otherwise see as a "phantom" extra line.
    """
    if not raw:
        return ("", 0)
    lines = raw.split("\n")
    first = lines[0].strip()
    remaining = [ln for ln in lines[1:] if ln.strip()]
    return (first, len(remaining))


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

    # Output rows + status line. The display-side line cap keeps
    # long cat/ls/grep output from dominating the chat flow — the
    # card shows a compact head window with an overflow marker
    # while the full content still reaches the model via the tool
    # result message. Exec-layer byte cap (MAX_OUTPUT_BYTES) is a
    # separate hard ceiling at a lower level.
    prep = prepared if prepared is not None else PreparedToolOutput(card)
    avail = max(20, width - OUTPUT_INDENT - 2)
    out_lines = prep.layout(avail, max_lines=DEFAULT_MAX_OUTPUT_LINES)
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
    # The bottom border is a SINGLE row — any newlines in the raw
    # command (heredocs, function bodies, if/then/fi blocks) would
    # cause paint_text to bleed into rows BELOW the card box,
    # breaking the layout. Clip to the first physical line, then
    # append a "+N lines" hint if the command was multi-line.
    raw_first, extra_line_count = _first_line_of_raw_command(card.raw_command)
    if extra_line_count > 0:
        raw_label = f" $ {raw_first}  (+{extra_line_count} lines) "
    else:
        raw_label = f" $ {raw_first} "
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
    out_lines = prep.layout(avail, max_lines=DEFAULT_MAX_OUTPUT_LINES)
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


# ─── Running-state painter ───
#
# Used while a BashRunner is in flight. Mirrors paint_tool_card's
# layout but adds three live elements:
#
#   1. Border color pulses at _RUNNER_PULSE_HZ between bg_input and
#      accent_warm via lerp_rgb. Reads as a slow breathing animation
#      — calm but unmistakable that something is alive.
#
#   2. Verb glyph in the header rotates through _RUNNER_SPINNER_FRAMES
#      at _RUNNER_SPINNER_FPS, replacing the static verb-class glyph.
#      The verb text itself stays — only the prefix glyph animates.
#
#   3. Status footer reads `running Xs · N lines` and ticks live as
#      the elapsed time increases and stdout grows. The line count
#      gives the user a concrete signal of progress for verbose
#      commands (find, grep, ls -la /usr/lib).
#
# Output streams in from the runner's live stdout/stderr buffers.
# Each new line just appears on the next paint frame because the
# painter reads `runner.stdout` directly — no caching, no diffing,
# the renderer's existing per-frame paint loop is the streaming
# mechanism.


def measure_tool_card_running_height(
    preview_card: ToolCard,
    *,
    width: int,
    runner_stdout: str,
    runner_stderr: str,
) -> int:
    """Compute the row height a running tool card will consume.

    Mirrors `measure_tool_card_height` but takes the runner's live
    output text directly instead of reading from card.output. The
    box height is fixed (verb header + params + raw command border)
    and the output region grows with the number of wrapped lines.
    """
    if width < 20:
        return 0
    n_params = max(1, len(preview_card.params)) if preview_card.params else 1
    box_h = 2 + n_params

    avail = max(20, width - OUTPUT_INDENT - 2)
    n_out_lines = _count_running_output_lines(
        runner_stdout, runner_stderr, avail,
    )
    return box_h + n_out_lines + 1  # +1 for status footer


def _wrap_running_output_lines(
    stdout: str, stderr: str, width: int,
) -> list[tuple[str, str]]:
    """Wrap stdout + stderr into (text, kind) pairs ready to paint.

    Each pair is a single visual row; `kind` is either "stdout" or
    "stderr" so the painter can apply the warn tint to stderr rows.
    Long lines are greedy-wrapped to the available width. Empty
    trailing lines (from a trailing newline on the source text)
    are dropped so the visual tail is always meaningful content.
    """
    rows: list[tuple[str, str]] = []
    for src, kind in ((stdout, "stdout"), (stderr, "stderr")):
        if not src:
            continue
        for raw in src.split("\n"):
            if not raw:
                continue
            offset = 0
            while offset < len(raw):
                rows.append((raw[offset:offset + width], kind))
                offset += width
    return rows


def _count_running_output_lines(
    stdout: str, stderr: str, width: int,
) -> int:
    """Height contribution of the live output region for the running
    card. Capped at DEFAULT_MAX_OUTPUT_LINES (tail window) so the
    card has a stable height regardless of how much output the
    subprocess produces. Always reserves at least 1 row even when
    there's no output yet so the status footer doesn't sit flush
    against the box border.
    """
    wrapped = _wrap_running_output_lines(stdout, stderr, width)
    n = min(len(wrapped), DEFAULT_MAX_OUTPUT_LINES)
    return n if n > 0 else 1


def paint_tool_card_running(
    grid: Grid,
    preview_card: ToolCard,
    *,
    x: int,
    y: int,
    w: int,
    theme: ThemeVariant,
    runner_stdout: str,
    runner_stderr: str,
    elapsed_s: float,
    now: float,
) -> int:
    """Paint a tool card in the LIVE / RUNNING state.

    `preview_card` is the immutable parsed metadata (verb, params,
    raw_command, risk, tool_call_id). Output text is read live from
    `runner_stdout` / `runner_stderr` so the card appears to grow as
    the subprocess produces output.

    Returns rows consumed.
    """
    if w < 20 or y >= grid.rows:
        return 0

    # ─── Pulsing border color ───
    base_border = _border_color(preview_card.risk, theme)
    pulse_t = 0.5 + 0.5 * math.sin(now * 2 * math.pi * _RUNNER_PULSE_HZ)
    pulse_t = pulse_t * 0.55 + 0.20  # bias toward accent_warm
    border = lerp_rgb(theme.bg_input, theme.accent_warm, pulse_t)

    # ─── Compute box dimensions ───
    n_params = max(1, len(preview_card.params)) if preview_card.params else 1
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

    # ─── Header pill — verb + spinner ───
    verb_text = preview_card.verb
    spinner_idx = int(now * _RUNNER_SPINNER_FPS) % len(_RUNNER_SPINNER_FRAMES)
    spinner = _RUNNER_SPINNER_FRAMES[spinner_idx]
    confidence_badge = " ?" if preview_card.confidence < 0.7 else ""
    header_text = f" {spinner} {verb_text}{confidence_badge} "
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
        max((len(k) for k, _ in preview_card.params), default=0),
    )
    body_x = x + CARD_INNER_PAD_X
    body_y = y + 1
    body_w = box_w - 2 * CARD_INNER_PAD_X

    if not preview_card.params:
        if body_y < grid.rows:
            paint_text(
                grid, "(no parameters)", body_x + 1, body_y,
                style=Style(
                    fg=theme.fg_subtle, bg=theme.bg_input,
                    attrs=ATTR_DIM | ATTR_ITALIC,
                ),
            )
    else:
        for i, (key, value) in enumerate(preview_card.params):
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
    raw_first, extra_line_count = _first_line_of_raw_command(preview_card.raw_command)
    if extra_line_count > 0:
        raw_label = f" $ {raw_first}  (+{extra_line_count} lines) "
    else:
        raw_label = f" $ {raw_first} "
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

    # ─── Live output rows ───
    avail = max(20, w - OUTPUT_INDENT - 2)
    out_x = x + OUTPUT_INDENT
    n_lines = 0
    cur_y = _paint_running_output_lines(
        grid, runner_stdout, runner_stderr,
        x=x, y=cur_y, w=w, avail=avail, out_x=out_x, theme=theme,
    )
    n_lines = cur_y - (y + box_h)
    if n_lines == 0 and cur_y < grid.rows:
        # Reserve one empty row so the status footer doesn't sit
        # flush against the box border before any output arrives.
        fill_region(
            grid, x + 1, cur_y, w - 2, 1,
            style=Style(bg=theme.bg_input),
        )
        cur_y += 1
        n_lines = 1

    # ─── Status footer — live elapsed time + line count ───
    if cur_y < grid.rows:
        if elapsed_s < 1.0:
            elapsed_text = f"{elapsed_s * 1000:.0f}ms"
        else:
            elapsed_text = f"{elapsed_s:.1f}s"
        # Count newlines in the buffers as a rough "lines so far" hint.
        line_count = (runner_stdout.count("\n") + runner_stderr.count("\n"))
        if line_count > 0:
            status_text = f"  ↳ {spinner} running {elapsed_text} · {line_count} lines"
        else:
            status_text = f"  ↳ {spinner} running {elapsed_text}"
        paint_text(
            grid, status_text, x + OUTPUT_INDENT, cur_y,
            style=Style(
                fg=theme.accent_warm, bg=theme.bg, attrs=ATTR_DIM | ATTR_BOLD,
            ),
        )
        cur_y += 1

    return cur_y - y


def _paint_running_output_lines(
    grid: Grid,
    stdout: str,
    stderr: str,
    *,
    x: int,
    y: int,
    w: int,
    avail: int,
    out_x: int,
    theme: ThemeVariant,
) -> int:
    """Paint the LAST DEFAULT_MAX_OUTPUT_LINES wrapped rows of the
    runner's live stdout + stderr as a tail-latest scrolling window.

    No verb-class parsing here — the running state shows raw output
    exactly as the subprocess produces it, letter-for-letter. The
    window is a tail (not head) because the running state is about
    "what just arrived"; the settled card shows a head window after
    the subprocess exits. Both states cap at the same row count so
    the card's visual weight stays constant through the transition.

    Stderr rows get the warn-tint (accent_warn, dim). Stdout rows
    use the theme's regular fg.
    """
    wrapped = _wrap_running_output_lines(stdout, stderr, avail)
    # Take the LAST N rows — the tail-latest window. Keeps the user's
    # eye on fresh content during long-running commands (find, grep,
    # seq-with-sleep). No overflow marker because new content is
    # actively arriving; the status footer's "N lines" counter is the
    # implicit signal of how much has been dropped.
    tail = wrapped[-DEFAULT_MAX_OUTPUT_LINES:]

    cur_y = y
    stdout_style = Style(fg=theme.fg, bg=theme.bg_input)
    stderr_style = Style(
        fg=theme.accent_warn, bg=theme.bg_input, attrs=ATTR_DIM,
    )
    for fragment, kind in tail:
        if cur_y >= grid.rows:
            break
        fill_region(
            grid, x + 1, cur_y, w - 2, 1,
            style=Style(bg=theme.bg_input),
        )
        style = stderr_style if kind == "stderr" else stdout_style
        paint_text(grid, fragment, out_x, cur_y, style=style)
        cur_y += 1
    return cur_y
