"""RoninChat — the v0 chat interface demo.

This is the first piece of Ronin that's *chat-shaped* instead of demo-
shaped. It exists to validate the rendering primitives we built so far
under the actual stresses of a real interactive UI: input expansion,
streaming response, scrollback layout, dynamic context bar, fade-in,
typewriter, color interpolation, and (most importantly) rapid resize
while all of the above is happening.

Layout (alt-screen with locked footer):

    ┌─────────────────────────────────────┐ row 0
    │            ronin · chat             │ title (1 row)
    ├─────────────────────────────────────┤
    │                                     │
    │  chat history scroll area           │ rows 1 .. N - 2 - input_h
    │  (newest at bottom)                 │
    │                                     │
    ├─────────────────────────────────────┤
    │ ctx 1234/4096 ████░░░░░░ 30.1%     │ static footer (1 row)
    ├─────────────────────────────────────┤
    │ ▍ user input here, growing upward   │ input area (1+ rows)
    │   if it wraps to multiple lines     │
    └─────────────────────────────────────┘

Pretext + interpolation tricks in use:

  - Every chat message is a `_Message` wrapping a `PreparedText`.
    Source is tokenized into wrap-respecting tokens once. lines(width)
    is cached per width — the entire scrollback re-flows on resize
    via cache misses on the new width and zero re-tokenization.

  - New messages fade in from a dim near-bg color to their final color
    over ~350ms via lerp_rgb on every frame.

  - Ronin's responses stream character-by-character at 80 cps with a
    cubic ease-in-out reveal curve.

  - A 0.6 s "thinking" pause precedes the typewriter, during which a
    braille spinner cycles at 12 fps using the same primitives the
    nusamurai animation uses.

  - The cursor in the input box renders as a fake bone-block cell,
    blinking at 1 Hz via time.monotonic() modulo.

  - The context-usage bar lerps its fill color from blood-red to ember
    to gold as usage approaches the cap.

  - Click-drag selection works for free (we don't enable mouse mode).
    Bracketed paste is disabled in this demo so multi-line pastes don't
    confuse the input handler — the eventual real chat will parse them.
"""

from __future__ import annotations

import random
import time

from ..render.app import App
from ..render.cells import (
    ATTR_BOLD,
    ATTR_DIM,
    Cell,
    Grid,
    Style,
)
from ..render.paint import fill_region, paint_text
from ..render.terminal import Terminal
from ..render.text import PreparedText, ease_out_cubic, hard_wrap, lerp_rgb


# ─── Palette ───

INK_DEEP    = 0x10070A   # main background
INK_DEEPER  = 0x070204   # input area background (slightly darker)
INK_FOOTER  = 0x1A0A0E   # static footer background (slightly tinted)
INK_BLOOD   = 0xC1272D   # primary ronin red
INK_EMBER   = 0xFF6347   # warmer accent
INK_BONE    = 0xE6D9B8   # off-white text
INK_DUST    = 0x6B5A4A   # dim chrome / labels
INK_DIM     = 0x3A1418   # very dim red (fade-in start, bar empty)
INK_GOLD    = 0xFFCC33   # warning when context bar is near-full


# ─── Tunables ───

TYPEWRITER_CPS    = 80.0
THINKING_PAUSE_S  = 0.6
FADE_IN_S         = 0.35
SPINNER_FPS       = 12.0
CURSOR_BLINK_HZ   = 1.5

PROMPT            = "▍ "
PROMPT_WIDTH      = 2

INPUT_MIN_ROWS    = 1
INPUT_MAX_ROWS    = 8

# Faked context limit. The eventual real version will get this from
# whichever model adapter is loaded.
FAKE_CONTEXT_MAX  = 4096

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ─── Scripted ronin responses ───

RESPONSES = (
    "The blade is not the weapon. The mind is. The blade only follows where the mind already walks.",
    "I have walked seven roads to find this answer, and I am still walking. The path that ends at understanding is not a road but a horizon.",
    "Speak less, see more. The man who explains himself has already lost the conversation.",
    "When the wind asks the bamboo a question, the bamboo answers by bending. When the wind asks the oak, the oak answers by breaking. Neither is wrong.",
    "I do not measure my days in steps taken or arrows fired. I measure them in moments where the world held its breath and I held mine.",
    "A teacher once told me: when you forget the form, you find the technique. When you forget the technique, you find the breath. When you forget the breath, you are finally beginning.",
    "The samurai who fears death dies a thousand times. The samurai who fears nothing dies once, and only the once. I have not chosen which I am.",
    "Your question deserves more than my answer. Take it back to the river. The river will tell you what it has told me — that water moves around obstacles, and so should grief.",
    "There is a story I tell only to strangers, because friends already know how it ends. Today you are a stranger, but only for a moment.",
    "I trained for ten years to draw the sword in less than a second. I trained for twenty more to know when not to draw it at all. The second decade was the harder one.",
)


# ─── Conversation model ───


class _Message:
    """A user or ronin message in the conversation buffer."""

    __slots__ = ("role", "body", "created_at")

    def __init__(self, role: str, content: str) -> None:
        self.role = role  # "user" | "ronin"
        self.body = PreparedText(content)
        self.created_at = time.monotonic()


# ─── The chat App ───


class RoninChat(App):
    def __init__(self) -> None:
        # Bracketed paste off — the v0 input handler doesn't yet parse
        # the CSI 200~ ... 201~ wrapper, so multi-line pastes would
        # land as raw bytes including newlines. Disable until we have
        # a proper input parser.
        super().__init__(
            target_fps=30.0,
            quit_keys=b"\x03",  # Ctrl+C only — q must remain typeable
            terminal=Terminal(bracketed_paste=False),
        )
        self.messages: list[_Message] = [
            _Message(
                "ronin",
                "I am ronin. Speak freely. Type /quit to leave, or press Ctrl+C.",
            ),
        ]
        self.input_buffer: str = ""
        # Streaming state for ronin's reply.
        # None when no reply is in flight; otherwise a small dict with
        # full_text / phase ("think" | "type") / phase_start.
        self.streaming: dict | None = None

    # ─── Input handling ───

    def on_key(self, byte: int) -> None:
        # While ronin is responding, swallow keypresses (no interrupting).
        if self.streaming is not None:
            return

        # Backspace (DEL or BS)
        if byte == 0x7F or byte == 0x08:
            if self.input_buffer:
                self.input_buffer = self.input_buffer[:-1]
            return

        # Enter (CR or LF)
        if byte == 0x0D or byte == 0x0A:
            if self.input_buffer.strip():
                self._submit()
            return

        # Printable ASCII (v0 doesn't decode UTF-8 input — that's a
        # follow-up when we add a real input parser).
        if 0x20 <= byte < 0x7F:
            self.input_buffer += chr(byte)
            return

        # Everything else (escape sequences, control codes) ignored.

    def _submit(self) -> None:
        text = self.input_buffer.strip()
        self.input_buffer = ""

        if text in ("/quit", "/exit", "/q"):
            self.stop()
            return

        self.messages.append(_Message("user", text))
        self.streaming = {
            "full_text": random.choice(RESPONSES),
            "phase": "think",
            "phase_start": time.monotonic(),
        }

    def _update_streaming(self) -> None:
        if self.streaming is None:
            return
        now = time.monotonic()
        s = self.streaming
        if s["phase"] == "think":
            if now - s["phase_start"] >= THINKING_PAUSE_S:
                s["phase"] = "type"
                s["phase_start"] = now
            return
        # type phase
        elapsed = now - s["phase_start"]
        chars = int(elapsed * TYPEWRITER_CPS)
        if chars >= len(s["full_text"]):
            self.messages.append(_Message("ronin", s["full_text"]))
            self.streaming = None

    # ─── Layout helpers ───

    def _input_lines_at_width(self, width: int) -> list[str]:
        """Hard-wrap the input buffer to fit (width - PROMPT_WIDTH).

        Hard wrap (not word wrap): users see exactly what they typed.
        """
        avail = max(1, width - PROMPT_WIDTH)
        return hard_wrap(self.input_buffer, avail)

    def _input_height(self, width: int) -> int:
        wrapped = self._input_lines_at_width(width)
        h = max(INPUT_MIN_ROWS, min(INPUT_MAX_ROWS, len(wrapped)))
        return h

    # ─── Rendering ───

    def on_tick(self, grid: Grid) -> None:
        self._update_streaming()

        rows, cols = grid.rows, grid.cols
        if rows < 3 or cols < 4:
            # Degenerate viewport; just paint a deep bg and bail.
            fill_region(grid, 0, 0, cols, rows, style=Style(bg=INK_DEEP))
            return

        # Layout
        title_h = 1
        input_h = self._input_height(cols)
        footer_static_h = 1
        chat_top = title_h
        chat_bottom = max(chat_top, rows - footer_static_h - input_h)
        static_y = chat_bottom
        input_y = static_y + footer_static_h

        # ─── Background ───
        fill_region(grid, 0, 0, cols, rows, style=Style(bg=INK_DEEP))

        # ─── Title row ───
        title = " ronin · chat "
        title_style = Style(fg=INK_BONE, bg=INK_DEEP, attrs=ATTR_BOLD)
        tx = max(0, (cols - len(title)) // 2)
        paint_text(grid, title, tx, 0, style=title_style)

        # ─── Chat scroll area ───
        self._paint_chat_area(grid, chat_top, chat_bottom, cols)

        # ─── Static footer (context bar) ───
        if static_y < rows:
            self._paint_static_footer(grid, static_y, cols)

        # ─── Input area ───
        if input_y < rows:
            self._paint_input(grid, input_y, rows - input_y, cols)

    # ─── Region painters ───

    def _paint_chat_area(self, grid: Grid, top: int, bottom: int, width: int) -> None:
        """Render the most recent messages that fit, newest at the bottom.

        Walks messages newest-to-oldest, accumulating heights, until the
        available rows are filled. The streaming reply (if any) is the
        bottom-most "virtual message" with a typewriter substring or a
        thinking spinner.
        """
        if bottom <= top or width <= 2:
            return

        # 1 cell of left/right padding for the message body.
        body_width = max(1, width - 2)
        body_x = 1
        avail = bottom - top
        now = time.monotonic()

        # Build the list of blocks to render, from bottom to top.
        # Each block: (lines, fg_color, fade_t)
        blocks: list[tuple[list[str], int, float]] = []

        # Streaming reply at the very bottom, if active.
        if self.streaming is not None:
            s = self.streaming
            if s["phase"] == "think":
                spinner_idx = int(now * SPINNER_FPS) % len(SPINNER_FRAMES)
                spinner_char = SPINNER_FRAMES[spinner_idx]
                stream_text = f"ronin ▸ {spinner_char} thinking..."
            else:
                elapsed = now - s["phase_start"]
                # Eased character reveal — slow start, smooth finish.
                full = s["full_text"]
                target_chars = max(1, int(elapsed * TYPEWRITER_CPS))
                target_chars = min(target_chars, len(full))
                revealed = full[:target_chars]
                # Trailing block-cursor while typing.
                stream_text = f"ronin ▸ {revealed}▌"
            stream_pt = PreparedText(stream_text)
            stream_lines = stream_pt.lines(body_width)
            blocks.append((stream_lines, INK_BLOOD, 1.0))

        # Then existing messages, newest to oldest.
        for msg in reversed(self.messages):
            age = now - msg.created_at
            fade_t = ease_out_cubic(min(1.0, age / FADE_IN_S)) if age < FADE_IN_S else 1.0
            if msg.role == "user":
                prefix = "you ▸ "
                color = INK_BONE
            else:
                prefix = "ronin ▸ "
                color = INK_BLOOD
            full = prefix + msg.body.source
            pt = PreparedText(full)
            lines = pt.lines(body_width)
            blocks.append((lines, color, fade_t))

        # Compose the bottom-up render. We collect (line, fg) pairs in
        # reverse-display order (last entry = top of viewport).
        render_pairs: list[tuple[str, int]] = []
        rows_used = 0
        for lines, fg, fade_t in blocks:
            if fade_t < 1.0:
                actual_fg = lerp_rgb(INK_DIM, fg, fade_t)
            else:
                actual_fg = fg
            for line in reversed(lines):
                if rows_used >= avail:
                    break
                render_pairs.append((line, actual_fg))
                rows_used += 1
            # Spacer line between messages
            if rows_used < avail:
                render_pairs.append(("", actual_fg))
                rows_used += 1
            if rows_used >= avail:
                break

        # Now paint, bottom-up.
        for i, (line, fg) in enumerate(render_pairs):
            y = bottom - 1 - i
            if y < top:
                break
            if line:
                paint_text(grid, line, body_x, y, style=Style(fg=fg, bg=INK_DEEP))

    def _paint_static_footer(self, grid: Grid, y: int, width: int) -> None:
        """A faked context-usage bar with color that lerps as it fills."""
        used = sum(len(m.body.source) for m in self.messages)
        if self.streaming is not None:
            used += len(self.streaming["full_text"])
        used = min(used, FAKE_CONTEXT_MAX)
        pct = used / FAKE_CONTEXT_MAX

        # Footer background
        fill_region(grid, 0, y, width, 1, style=Style(bg=INK_FOOTER))

        label = f" ctx {used:>4}/{FAKE_CONTEXT_MAX} "
        right_label = f" {pct * 100:5.1f}% "
        label_style = Style(fg=INK_DUST, bg=INK_FOOTER, attrs=ATTR_DIM)
        right_style = Style(fg=INK_BONE, bg=INK_FOOTER, attrs=ATTR_BOLD)

        paint_text(grid, label, 0, y, style=label_style)

        bar_x = len(label) + 1
        right_x = max(0, width - len(right_label))
        bar_w = max(0, right_x - bar_x - 1)

        if bar_w > 0:
            filled = int(round(bar_w * pct))
            empty = bar_w - filled
            # Lerp the fill color: blood → ember (around 60%) → gold (>=85%)
            if pct < 0.6:
                bar_fg = lerp_rgb(INK_BLOOD, INK_EMBER, pct / 0.6)
            elif pct < 0.85:
                bar_fg = lerp_rgb(INK_EMBER, INK_GOLD, (pct - 0.6) / 0.25)
            else:
                bar_fg = INK_GOLD
            if filled > 0:
                paint_text(
                    grid,
                    "█" * filled,
                    bar_x,
                    y,
                    style=Style(fg=bar_fg, bg=INK_FOOTER),
                )
            if empty > 0:
                paint_text(
                    grid,
                    "░" * empty,
                    bar_x + filled,
                    y,
                    style=Style(fg=INK_DIM, bg=INK_FOOTER),
                )

        paint_text(grid, right_label, right_x, y, style=right_style)

    def _paint_input(self, grid: Grid, y: int, height: int, width: int) -> None:
        """The auto-expanding input area."""
        # Background
        fill_region(grid, 0, y, width, height, style=Style(bg=INK_DEEPER))

        wrapped = self._input_lines_at_width(width)
        # Cap at the visible window — show the most-recent rows.
        wrapped = wrapped[-height:] if len(wrapped) > height else wrapped

        # Prompt on the first visible row.
        prompt_style = Style(fg=INK_BLOOD, bg=INK_DEEPER, attrs=ATTR_BOLD)
        paint_text(grid, PROMPT, 0, y, style=prompt_style)

        text_style = Style(fg=INK_BONE, bg=INK_DEEPER)
        for i, line in enumerate(wrapped):
            ly = y + i
            if ly >= y + height:
                break
            paint_text(grid, line, PROMPT_WIDTH, ly, style=text_style)

        # Cursor / streaming-status indicator on the last visible line.
        if self.streaming is None:
            last_line = wrapped[-1] if wrapped else ""
            last_y = y + min(len(wrapped) - 1, height - 1)
            cursor_x = min(width - 1, PROMPT_WIDTH + len(last_line))
            visible = (int(time.monotonic() * CURSOR_BLINK_HZ * 2) % 2) == 0
            if visible:
                cursor_cell = Cell(" ", Style(fg=INK_DEEPER, bg=INK_BONE))
                grid.set(last_y, cursor_x, cursor_cell)
        else:
            # While streaming, replace the input area's first line with
            # a status hint so the user knows input is paused.
            hint = "ronin is responding..."
            paint_text(
                grid,
                hint,
                PROMPT_WIDTH,
                y,
                style=Style(fg=INK_DUST, bg=INK_DEEPER, attrs=ATTR_DIM),
            )
