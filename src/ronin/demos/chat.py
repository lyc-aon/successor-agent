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
    """A user or ronin message in the conversation buffer.

    body is a PreparedText that includes the role prefix ("you ▸ " or
    "ronin ▸ ") so wrap caching keys correctly across frames. The
    prefix changes how the message wraps, so it has to be part of the
    text the wrapper sees.
    """

    __slots__ = ("role", "raw_text", "body", "created_at")

    def __init__(self, role: str, content: str) -> None:
        self.role = role  # "user" | "ronin"
        self.raw_text = content
        prefix = "you ▸ " if role == "user" else "ronin ▸ "
        self.body = PreparedText(prefix + content)
        self.created_at = time.monotonic()


# ─── Multi-byte escape sequence decoder ───
#
# This is the first piece of what will become the real key parser. For
# now it's a small dict-of-known-sequences inline in the chat App.

_ESC_KEYS: dict[bytes, str] = {
    # CSI cursor / navigation
    b"\x1b[A": "UP",
    b"\x1b[B": "DOWN",
    b"\x1b[C": "RIGHT",
    b"\x1b[D": "LEFT",
    b"\x1b[5~": "PG_UP",
    b"\x1b[6~": "PG_DOWN",
    b"\x1b[H": "HOME",
    b"\x1b[F": "END",
    # Alternative tilde-based home/end (xterm/linux)
    b"\x1b[1~": "HOME",
    b"\x1b[4~": "END",
    b"\x1b[7~": "HOME",
    b"\x1b[8~": "END",
    # Application cursor mode (some terminals send these instead)
    b"\x1bOA": "UP",
    b"\x1bOB": "DOWN",
    b"\x1bOC": "RIGHT",
    b"\x1bOD": "LEFT",
    b"\x1bOH": "HOME",
    b"\x1bOF": "END",
}


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
        self.streaming: dict | None = None

        # ─── Scrollback state ───
        # scroll_offset is in lines from the bottom of the chat content.
        # 0 = anchored to the bottom (newest visible).
        # N > 0 = scrolled up by N lines (showing older content).
        self.scroll_offset: int = 0
        # auto_scroll: when True, new messages stay anchored to bottom.
        # Flips to False when the user scrolls up; back to True when
        # they scroll all the way down or hit End / submit.
        self._auto_scroll: bool = True
        # Last frame's chat dimensions and content height — used by the
        # scroll key handlers (which fire between frames) to compute
        # page size and clamp the offset against current geometry.
        self._last_chat_h: int = 10
        self._last_chat_w: int = 80
        self._last_total_height: int = 0

        # Multi-byte ESC sequence accumulator. None when not in an esc
        # sequence; bytearray with collected bytes otherwise.
        self._esc_buf: bytearray | None = None

    # ─── Input handling ───

    def on_key(self, byte: int) -> None:
        # ─── ESC sequence accumulator ───
        # In raw mode, multi-byte key sequences (arrow keys, page up,
        # etc.) arrive as ESC + intermediates + final byte all in one
        # read(). We accumulate until we recognize a known sequence or
        # determine it's unknown.
        if self._esc_buf is not None:
            self._esc_buf.append(byte)
            decoded = self._try_decode_esc()
            if decoded == "PARTIAL":
                return  # wait for more bytes
            self._esc_buf = None
            if decoded and decoded != "UNKNOWN":
                self._handle_decoded_key(decoded)
            return

        if byte == 0x1B:  # ESC starts a sequence
            self._esc_buf = bytearray([0x1B])
            return

        # ─── Single-byte scroll shortcuts (always work) ───
        # Useful on terminals where multi-byte sequences are unreliable
        # and as a vim-style fallback. These never get blocked by the
        # streaming guard so the user can always navigate history.
        if byte == 0x02:  # Ctrl+B → page up (vim/less convention)
            self._scroll_lines(self._page_size())
            return
        if byte == 0x06:  # Ctrl+F → page down
            self._scroll_lines(-self._page_size())
            return
        if byte == 0x10:  # Ctrl+P → up 1 line
            self._scroll_lines(1)
            return
        if byte == 0x0E:  # Ctrl+N → down 1 line
            self._scroll_lines(-1)
            return
        if byte == 0x05:  # Ctrl+E → end (jump to newest)
            self._scroll_to_bottom()
            return
        if byte == 0x19:  # Ctrl+Y → top (jump to oldest)
            self._scroll_to_top()
            return

        # ─── Streaming guard ───
        # While ronin is responding, swallow input (no interrupting yet).
        if self.streaming is not None:
            return

        # ─── Backspace (DEL or BS) ───
        if byte == 0x7F or byte == 0x08:
            if self.input_buffer:
                self.input_buffer = self.input_buffer[:-1]
            return

        # ─── Enter (CR or LF) ───
        if byte == 0x0D or byte == 0x0A:
            if self.input_buffer.strip():
                self._submit()
            return

        # ─── Printable ASCII ───
        if 0x20 <= byte < 0x7F:
            self.input_buffer += chr(byte)
            return

        # Everything else (other control codes) ignored.

    # ─── ESC sequence decoder ───

    def _try_decode_esc(self) -> str:
        """Inspect the current esc accumulator and decide what to do.

        Returns:
            a key name string ("UP", "PG_DOWN", etc.) — fully decoded
            "PARTIAL" — buffer is a known prefix; wait for more bytes
            "UNKNOWN" — buffer doesn't match any known sequence
        """
        seq = bytes(self._esc_buf or b"")
        if seq in _ESC_KEYS:
            return _ESC_KEYS[seq]
        for known in _ESC_KEYS:
            if known.startswith(seq):
                return "PARTIAL"
        return "UNKNOWN"

    def _handle_decoded_key(self, key: str) -> None:
        if key == "UP":
            self._scroll_lines(1)
        elif key == "DOWN":
            self._scroll_lines(-1)
        elif key == "PG_UP":
            self._scroll_lines(self._page_size())
        elif key == "PG_DOWN":
            self._scroll_lines(-self._page_size())
        elif key == "HOME":
            self._scroll_to_top()
        elif key == "END":
            self._scroll_to_bottom()
        # LEFT / RIGHT will become input cursor movement when we
        # build the real input parser.

    # ─── Scroll state ───

    def _scroll_lines(self, delta: int) -> None:
        new_off = self.scroll_offset + delta
        max_off = self._max_scroll()
        if new_off < 0:
            new_off = 0
        if new_off > max_off:
            new_off = max_off
        self.scroll_offset = new_off
        self._auto_scroll = (new_off == 0)

    def _scroll_to_bottom(self) -> None:
        self.scroll_offset = 0
        self._auto_scroll = True

    def _scroll_to_top(self) -> None:
        self.scroll_offset = self._max_scroll()
        self._auto_scroll = (self.scroll_offset == 0)

    def _max_scroll(self) -> int:
        return max(0, self._last_total_height - self._last_chat_h)

    def _page_size(self) -> int:
        # One less than chat_h gives one row of overlap between pages,
        # which is the standard "page up/down" behavior in vim/less.
        return max(1, self._last_chat_h - 1)

    # ─── Submission ───

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
        # Submitting always anchors to the newest — the user wants to
        # see what they sent and what comes back, even if they were
        # scrolled up reading history.
        self._scroll_to_bottom()

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
        # Drop any incomplete ESC sequence left over from a previous tick.
        # In raw mode, multi-byte sequences arrive in a single read(),
        # so an unfinished sequence at frame boundary means a bare ESC
        # press or an unrecognized sequence we should give up on.
        self._esc_buf = None

        self._update_streaming()

        rows, cols = grid.rows, grid.cols
        if rows < 3 or cols < 4:
            fill_region(grid, 0, 0, cols, rows, style=Style(bg=INK_DEEP))
            return

        # Layout — bottom-up:
        #   row N-1               static footer (ctx bar, 1 row)
        #   rows N-1-input_h..N-2 input area (input_h rows)
        #   rows title_h..N-2-input_h  chat scroll area
        #   row 0                 title (1 row)
        title_h = 1
        input_h = self._input_height(cols)
        footer_static_h = 1
        static_y = rows - footer_static_h
        input_y = static_y - input_h
        chat_top = title_h
        chat_bottom = max(chat_top, input_y)

        # ─── Background ───
        fill_region(grid, 0, 0, cols, rows, style=Style(bg=INK_DEEP))

        # ─── Title row ───
        title = " ronin · chat "
        title_style = Style(fg=INK_BONE, bg=INK_DEEP, attrs=ATTR_BOLD)
        tx = max(0, (cols - len(title)) // 2)
        paint_text(grid, title, tx, 0, style=title_style)

        # ─── Chat scroll area ───
        self._paint_chat_area(grid, chat_top, chat_bottom, cols)

        # ─── Scroll indicator (right side of title row, when scrolled) ───
        # Painted AFTER the chat area so it overlays the title cleanly.
        if self.scroll_offset > 0:
            if self.streaming is not None:
                indicator = f" ↑ {self.scroll_offset} · ronin responding · Ctrl+E newest "
            else:
                indicator = f" ↑ {self.scroll_offset}/{self._max_scroll()} · End for newest "
            ix = max(0, cols - len(indicator))
            paint_text(
                grid,
                indicator,
                ix,
                0,
                style=Style(fg=INK_EMBER, bg=INK_DEEP, attrs=ATTR_BOLD),
            )

        # ─── Input area (above the ctx bar) ───
        if input_y >= 0 and input_y < rows:
            self._paint_input(grid, input_y, min(input_h, rows - input_y), cols)

        # ─── Static footer (context bar) — at the very bottom ───
        if 0 <= static_y < rows:
            self._paint_static_footer(grid, static_y, cols)

    # ─── Region painters ───

    def _paint_chat_area(self, grid: Grid, top: int, bottom: int, width: int) -> None:
        """Render a viewport-sized slice of the conversation.

        Builds a flat list of all rendered chat lines (one row each),
        then slices it according to scroll_offset. The streaming reply
        is rendered as a "virtual" trailing block — only visible when
        the user is anchored to the bottom (scroll_offset == 0).

        When the user is scrolled up and a new message commits, we
        advance scroll_offset by the new content's height so the
        historical view they were reading stays under their eyes.
        """
        if bottom <= top or width <= 2:
            return

        body_width = max(1, width - 2)
        body_x = 1
        chat_h = bottom - top

        # Build the flat list of committed-message lines.
        committed = self._build_message_lines(body_width)
        committed_h = len(committed)

        # Detect content growth since last frame. If we're scrolled up
        # (auto_scroll == False), advance scroll_offset by the delta so
        # the same historical content stays visible.
        if not self._auto_scroll and committed_h > self._last_total_height:
            delta = committed_h - self._last_total_height
            self.scroll_offset += delta

        # Update geometry caches for the scroll-key handlers.
        self._last_chat_h = chat_h
        self._last_chat_w = body_width
        self._last_total_height = committed_h

        # Clamp scroll_offset against current geometry.
        max_off = max(0, committed_h - chat_h)
        if self.scroll_offset > max_off:
            self.scroll_offset = max_off
        if self.scroll_offset < 0:
            self.scroll_offset = 0
        if self.scroll_offset == 0:
            self._auto_scroll = True

        # Slice the committed lines for the current scroll position.
        end = committed_h - self.scroll_offset
        start = max(0, end - chat_h)
        visible = committed[start:end]

        # Streaming reply: only when anchored at bottom. The streaming
        # reply does NOT count toward committed_h (it's not in
        # self.messages yet) so its appearance never affects scroll
        # geometry. When the message commits, the content-grew check
        # above advances offset to compensate if the user scrolled
        # away during the stream.
        if self.streaming is not None and self.scroll_offset == 0:
            stream_lines = self._build_streaming_lines(body_width)
            combined = visible + stream_lines
            # If combined exceeds chat_h, drop the oldest visible lines
            # (the streaming reply is the most important thing to show).
            if len(combined) > chat_h:
                combined = combined[-chat_h:]
        else:
            combined = visible

        # Anchor the visible block to the BOTTOM of the chat area.
        # If we have fewer lines than chat_h, leave empty space at the top.
        paint_y = bottom - len(combined)
        if paint_y < top:
            paint_y = top

        for i, (line, fg) in enumerate(combined):
            y = paint_y + i
            if y >= bottom:
                break
            if line:
                paint_text(grid, line, body_x, y, style=Style(fg=fg, bg=INK_DEEP))

    # ─── Flat-line builders ───

    def _build_message_lines(self, body_width: int) -> list[tuple[str, int]]:
        """Flatten committed messages into (line, fg_color) tuples.

        Spacer lines are inserted BETWEEN messages (not after the last
        one). Each line carries the fade-in-adjusted foreground color
        for its message — the line painter is colorblind to message
        boundaries, it just paints what's in the list.
        """
        out: list[tuple[str, int]] = []
        now = time.monotonic()
        n = len(self.messages)
        for i, msg in enumerate(self.messages):
            age = now - msg.created_at
            fade_t = (
                ease_out_cubic(min(1.0, age / FADE_IN_S))
                if age < FADE_IN_S
                else 1.0
            )
            base_color = INK_BONE if msg.role == "user" else INK_BLOOD
            if fade_t < 1.0:
                fg = lerp_rgb(INK_DIM, base_color, fade_t)
            else:
                fg = base_color
            for line in msg.body.lines(body_width):
                out.append((line, fg))
            if i < n - 1:
                out.append(("", fg))
        return out

    def _build_streaming_lines(self, body_width: int) -> list[tuple[str, int]]:
        """Render the in-flight streaming reply as a line list.

        Includes a leading spacer above the streaming block so it
        visually separates from the last committed message.
        """
        s = self.streaming
        if s is None:
            return []
        now = time.monotonic()
        if s["phase"] == "think":
            spinner_idx = int(now * SPINNER_FPS) % len(SPINNER_FRAMES)
            text = f"ronin ▸ {SPINNER_FRAMES[spinner_idx]} thinking..."
        else:
            elapsed = now - s["phase_start"]
            full = s["full_text"]
            target_chars = max(1, min(int(elapsed * TYPEWRITER_CPS), len(full)))
            text = f"ronin ▸ {full[:target_chars]}▌"
        stream_pt = PreparedText(text)
        out: list[tuple[str, int]] = [("", INK_BLOOD)]
        for line in stream_pt.lines(body_width):
            out.append((line, INK_BLOOD))
        return out

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
