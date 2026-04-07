"""RoninChat — chat interface backed by a real local model.

The first chat-shaped piece of Ronin. Wires together:

- The five-layer renderer (cells/paint/diff/terminal/app)
- The Pretext-shaped text layout primitives (PreparedText)
- The real key parser (input/keys.py — UTF-8, ESC sequences,
  bracketed paste, modifier-bearing arrows, all decoded into typed
  KeyEvents)
- The llama.cpp streaming client (providers/llama.py — streams
  reasoning_content + content channels separately, runs the request
  on a worker thread, posts events to a thread-safe queue)

Layout (alt-screen with locked footer):

    ┌─────────────────────────────────────┐ row 0
    │            ronin · chat             │ title (1 row)
    ├─────────────────────────────────────┤
    │                                     │
    │  chat history scroll area           │ rows 1 .. N - 2 - input_h
    │  (newest at bottom)                 │
    │                                     │
    ├─────────────────────────────────────┤
    │ ▍ user input here                   │ input area (1+ rows)
    │   wraps upward as it grows          │
    ├─────────────────────────────────────┤
    │ ctx 1234/262144 ████░ 0.5%  qwopus  │ static footer (1 row)
    └─────────────────────────────────────┘

The streaming response lives in two phases:

  Phase 1 — reasoning. The model emits delta.reasoning_content for a
    while. We render a braille spinner with a live char counter so the
    user knows it's not stuck.

  Phase 2 — content. The first delta.content arrives. We transition
    to a typewriter rendering that grows as content chunks come in.
    The model usually emits content at 40-80 tokens/sec, which is
    faster than reading speed, so the typewriter is real not faked.

Both phases are driven by polling `ChatStream.drain()` each frame. The
worker thread does HTTP+SSE; the main thread does rendering. They
communicate via a thread-safe queue.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable

from ..input.keys import (
    InputEvent,
    Key,
    KeyDecoder,
    KeyEvent,
    MOD_ALT,
    MOD_CTRL,
    MOD_SHIFT,
    MouseButton,
    MouseEvent,
)
from ..providers.llama import (
    ChatStream,
    ContentChunk,
    LlamaCppClient,
    ReasoningChunk,
    StreamEnded,
    StreamError,
    StreamStarted,
)
from ..render.app import App
from ..render.cells import (
    ATTR_BOLD,
    ATTR_DIM,
    Cell,
    Grid,
    Style,
)
from ..render.paint import fill_region, paint_box, paint_text
from ..render.terminal import Terminal
from ..render.text import PreparedText, ease_out_cubic, hard_wrap, lerp_rgb
from ..render.theme import (
    DARK_THEME,
    FORGE_THEME,
    LIGHT_THEME,
    THEMES,
    Theme,
    blend_themes,
    find_theme,
    next_theme,
)


# Theme transition duration — how long it takes to lerp between themes
# when the user presses Ctrl+T or runs /theme. The renderer doesn't
# care about animation cost; this is just a visual touch that shows
# the entire UI smoothly fading from one palette to another.
THEME_TRANSITION_S = 0.4


# ─── Density (the "font size" widget) ───
#
# Terminal apps can't change the actual font in any portable way (the
# terminal owns the font). What we CAN control is how Ronin uses cells:
# how much padding around the chat content, how many blank rows between
# messages, how wide the content column is allowed to grow.
#
# Three density modes give the same FEEL as font size without touching
# the terminal's font:
#
#   compact   minimal padding, no inter-message spacing, full width.
#             Maximum information density. Useful for reading long
#             threads on small terminals.
#   normal    1-cell padding, 1-line spacing, content width capped at
#             120 cells on wide terminals. The default.
#   spacious  4-cell gutter, 2-line spacing, content capped at 80 cells.
#             Lots of breathing room. Each message has visual weight.
#             Feels "bigger" because there's more whitespace per word.


@dataclass(frozen=True, slots=True)
class Density:
    """A layout density preset for the chat content area.

    gutter:             cells of left+right padding around the chat body
    message_spacing:    blank rows between consecutive messages
    max_content_width:  cap on the chat body width in cells. Use the
                        sentinel _DENSITY_NO_CAP for "no cap"
                        (clamping then degenerates to the available
                        width). Storing as int (instead of int | None)
                        keeps the lerp math simple during transitions.
    """
    name: str
    gutter: int
    message_spacing: int
    max_content_width: int


def blend_densities(a: Density, b: Density, t: float) -> Density:
    """Lerp between two densities for smooth transitions.

    max_content_width is the dominant visual signal — it's a continuous
    int and lerps cleanly. The discrete fields (gutter, message_spacing)
    snap to the destination's value rather than rounding through
    intermediate steps that would look choppy.
    """
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    return Density(
        name=b.name,
        gutter=b.gutter,
        message_spacing=b.message_spacing,
        max_content_width=int(round(
            a.max_content_width + (b.max_content_width - a.max_content_width) * t
        )),
    )


# How long density transitions take. Snappier than theme transitions
# because the only thing actually animating is content width.
DENSITY_TRANSITION_S = 0.25


# Sentinel value for "no content-width cap" used by the compact density.
# Using a large int instead of None lets us lerp this field across density
# transitions without special-casing the "uncapped" state. The renderer
# clamps to min(avail, max_content_width) so this just degenerates to the
# available width when set to a huge number.
_DENSITY_NO_CAP = 99999

COMPACT = Density(
    name="compact",
    gutter=0,
    message_spacing=0,
    max_content_width=_DENSITY_NO_CAP,
)

NORMAL = Density(
    name="normal",
    gutter=1,
    message_spacing=1,
    max_content_width=120,
)

SPACIOUS = Density(
    name="spacious",
    gutter=4,
    message_spacing=2,
    max_content_width=80,
)


# Order matters for cycling: smaller → larger so Alt+= advances toward
# spacious and Alt+- retreats toward compact.
DENSITIES: tuple[Density, ...] = (COMPACT, NORMAL, SPACIOUS)


def find_density(name: str) -> Density | None:
    """Look up a density by name (case-insensitive)."""
    n = name.strip().lower()
    for d in DENSITIES:
        if d.name == n:
            return d
    return None


def density_index(d: Density) -> int:
    """Return the position of d in DENSITIES, or -1 if not found."""
    try:
        return DENSITIES.index(d)
    except ValueError:
        return -1


# ─── Hit boxes for clickable widgets ───
#
# Each tick, the chat App records the painted location of every
# clickable widget into self._hit_boxes. The mouse event handler scans
# this list to find which widget contains a click and dispatches to
# the existing keyboard handlers (e.g. _cycle_theme).
#
# Hit boxes are recomputed every frame because the widget positions
# can shift on resize, theme/density change, or scroll-state change.
# This is cheap (3-5 small tuples per frame).


@dataclass(slots=True, frozen=True)
class _HitBox:
    x: int
    y: int
    w: int
    h: int
    action: str  # "theme" | "density" | "scroll_to_bottom"

    def contains(self, col: int, row: int) -> bool:
        return (
            self.x <= col < self.x + self.w
            and self.y <= row < self.y + self.h
        )


# How many lines to scroll per wheel notch. 3 lines is the conventional
# value (matches xterm and most terminal scroll-rate defaults).
WHEEL_SCROLL_LINES = 3


# ─── Slash command registry ───
#
# Every slash command lives here as a SlashCommand instance. The
# autocomplete dropdown reads this list to populate suggestions, and
# the _submit handler matches commands by name. Adding a new command
# is one entry in SLASH_COMMANDS plus a handler in _submit.
#
# Args completion: each command can supply a `complete_args` callable
# that takes a partial-arg string and returns a list of full matches.
# Static commands use the static_args() helper. Dynamic commands
# (e.g. file paths) supply a custom callable.


def static_args(*choices: str) -> Callable[[str], list[str]]:
    """Build a completer for a fixed set of choices.

    Returns a function that takes a partial string and returns the
    choices that start with it (case-insensitive). The original casing
    of each choice is preserved in the returned list.
    """
    lower_choices = tuple(c.lower() for c in choices)

    def completer(partial: str) -> list[str]:
        p = partial.lower()
        return [c for c, lc in zip(choices, lower_choices) if lc.startswith(p)]

    return completer


# We can't use frozen=True with a Callable field because functions
# aren't hashable in a stable way across runs. Plain dataclass; the
# instances are constructed once at import and never mutated.
@dataclass(slots=True)
class SlashCommand:
    """A registered slash command.

    name:           canonical name (without leading slash)
    aliases:        other names that match (e.g. "q" for "quit")
    description:    short one-line summary for the dropdown
    args_hint:      short hint shown after the description, e.g.
                    "[dark|light|forge]" — empty if no args
    complete_args:  optional callable taking a partial string,
                    returning a list of full arg matches. None means
                    the command takes no args.
    """
    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    args_hint: str = ""
    complete_args: Callable[[str], list[str]] | None = None


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        name="quit",
        aliases=("q", "exit"),
        description="leave the chat",
    ),
    SlashCommand(
        name="theme",
        description="switch color theme",
        args_hint="[dark|light|forge|cycle]",
        complete_args=static_args("dark", "light", "forge", "cycle"),
    ),
    SlashCommand(
        name="density",
        description="adjust layout density",
        args_hint="[compact|normal|spacious|cycle]",
        complete_args=static_args("compact", "normal", "spacious", "cycle"),
    ),
    SlashCommand(
        name="mouse",
        description="toggle mouse reporting",
        args_hint="[on|off|toggle]",
        complete_args=static_args("on", "off", "toggle"),
    ),
)


def filter_slash_commands(prefix: str) -> list[SlashCommand]:
    """Return commands whose name or alias starts with `prefix`.

    Sorted alphabetically. An empty prefix returns every command.
    """
    p = prefix.lower()
    out: list[SlashCommand] = []
    for cmd in SLASH_COMMANDS:
        if cmd.name.startswith(p):
            out.append(cmd)
            continue
        for alias in cmd.aliases:
            if alias.startswith(p):
                out.append(cmd)
                break
    out.sort(key=lambda c: c.name)
    return out


def find_slash_command(name: str) -> SlashCommand | None:
    """Resolve a command name (or alias) to its SlashCommand."""
    n = name.lower()
    for cmd in SLASH_COMMANDS:
        if cmd.name == n:
            return cmd
        if n in cmd.aliases:
            return cmd
    return None


# ─── Autocomplete state machine ───
#
# The dropdown has three reachable states (plus None for hidden):
#
#   _NameMode   user is typing the command name; matches is the
#               filtered list of SlashCommand candidates
#   _ArgMode    user has accepted a command and is typing its arg;
#               matches is the list of valid arg strings
#   _NoMatches  buffer expects autocomplete but nothing matches;
#               we render an informational popover instead of hiding


@dataclass(slots=True)
class _NameMode:
    matches: list[SlashCommand]
    selected: int
    prefix: str  # what the user typed after the leading /


@dataclass(slots=True)
class _ArgMode:
    command: SlashCommand
    matches: list[str]
    selected: int
    partial: str  # what the user has typed for the arg so far


@dataclass(slots=True)
class _NoMatches:
    mode: str             # "name" or "arg"
    text: str             # the headline message ("no command matches '/xyz'")
    valid_options: tuple[str, ...] = ()  # for arg mode, the valid choices
    command: SlashCommand | None = None  # for arg mode, the resolved command


_AutocompleteState = _NameMode | _ArgMode | _NoMatches | None


# ─── Tunables ───

FADE_IN_S = 0.35
SPINNER_FPS = 12.0
CURSOR_BLINK_HZ = 1.5

PROMPT = "▍ "
PROMPT_WIDTH = 2

INPUT_MIN_ROWS = 1
INPUT_MAX_ROWS = 8

# Real context limit — Lycaon's local Qwen3.5-27B-Opus-Distilled-v2
# server is launched with -c 262144 (256K). Don't apologize for token
# cost on local inference.
CONTEXT_MAX = 262144

SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Theme widget label format. The widget is rendered in the top-right
# of the title row as a small "pill" with the theme's icon and name.
THEME_WIDGET_PAD = " "


# ─── System prompt ───
#
# Tuned for the thinking-mode Qwen3.5-27B-Opus-Distilled-v2 model. The
# explicit "no headers, no verification, no markdown structures"
# instruction is necessary because the distilled model otherwise
# defaults to emitting "Solution:" / "Verification:" / checkmark lists
# learned from its training data.

SYSTEM_PROMPT = """You are ronin — a terse, contemplative wandering samurai assistant.

Speak as ronin would: with intention, with brevity, as if seated by a wayside fire speaking to a fellow traveler. Reply in a single flowing paragraph.

Do not use markdown headers. Do not use bullet lists or numbered lists. Do not write "Solution:", "Answer:", "Verification:", "Note:", or any preamble label. Do not use checkmarks. Do not wrap your reply in code fences unless the user asked for code.

Think as carefully as you need. When you have finished thinking, simply give your answer as if speaking aloud. Brevity is honor. When you must convey several things, weave them into one paragraph rather than enumerating them."""


# ─── Conversation model ───


class _Message:
    """A user or ronin message in the conversation buffer.

    body is a PreparedText that includes the role prefix ("you ▸ " or
    "ronin ▸ ") so wrap caching keys correctly across frames. The
    prefix changes how the message wraps, so it has to be part of the
    text the wrapper sees.

    raw_text is the original content (without prefix) — what we send
    to the model in the conversation history.
    """

    __slots__ = ("role", "raw_text", "body", "created_at", "synthetic")

    def __init__(self, role: str, content: str, *, synthetic: bool = False) -> None:
        self.role = role  # "user" | "ronin"
        self.raw_text = content
        prefix = "you ▸ " if role == "user" else "ronin ▸ "
        self.body = PreparedText(prefix + content)
        self.created_at = time.monotonic()
        # Synthetic messages (the greeting, error notices) are NOT sent
        # to the model in the conversation history.
        self.synthetic = synthetic


# ─── The chat App ───


class RoninChat(App):
    def __init__(
        self,
        *,
        client: LlamaCppClient | None = None,
        theme: Theme = DARK_THEME,
    ) -> None:
        super().__init__(
            target_fps=30.0,
            quit_keys=b"\x03",  # Ctrl+C only — q must remain typeable
            terminal=Terminal(bracketed_paste=True),
        )
        self.client = client if client is not None else LlamaCppClient()

        # ─── Theme state ───
        # `theme` is the committed target. `_theme_from` and `_theme_t0`
        # drive a smooth lerp transition when the user switches themes.
        self.theme: Theme = theme
        self._theme_from: Theme | None = None
        self._theme_t0: float = 0.0

        # ─── Density state ───
        # Layout density (compact / normal / spacious) — the "font size
        # feel" widget. The terminal owns the actual font; this controls
        # how Ronin uses cells. Transitions lerp the max_content_width
        # over DENSITY_TRANSITION_S so the text smoothly slides in/out.
        self.density: Density = NORMAL
        self._density_from: Density | None = None
        self._density_t0: float = 0.0

        # ─── Mouse state ───
        # Mouse reporting is opt-in via /mouse on. When enabled, the
        # title bar widgets become clickable and the scroll wheel works.
        # The trade-off: native click-drag selection requires holding
        # Shift while mouse reporting is on. Default is OFF so users
        # who never opt in keep their normal selection behavior.
        self._mouse_enabled: bool = False
        # Hit boxes recorded each frame by the painters. Cleared at
        # the start of on_tick and refilled as widgets are painted.
        self._hit_boxes: list[_HitBox] = []

        # ─── Slash command autocomplete state ───
        # The dropdown is shown whenever the input buffer starts with
        # '/' and the autocomplete state machine returns a non-None
        # state. Selection is the index into the currently-active list
        # (commands in name mode, args in arg mode). The state itself
        # is computed on demand via _autocomplete_state() because the
        # filter is cheap.
        self._autocomplete_selected: int = 0
        # When True, the dropdown is hidden even though the buffer
        # would otherwise show it. Set by Esc; cleared by any input
        # mutation (typing or backspace) so the dropdown comes back
        # the moment the user starts engaging again.
        self._autocomplete_dismissed: bool = False

        # Probe the server immediately so we can show a useful greeting.
        server_up = self.client.health()
        if server_up:
            greeting = (
                f"I am ronin. The forge is hot — {self.client.model} stands ready. "
                f"Speak freely. Ctrl+C or /quit to leave."
            )
        else:
            greeting = (
                f"I am ronin. The forge is cold — no model answers at "
                f"{self.client.base_url}. Start llama.cpp and try again, "
                f"or read in silence."
            )
        self.messages: list[_Message] = [
            _Message("ronin", greeting, synthetic=True),
        ]

        self.input_buffer: str = ""

        # ─── Streaming state ───
        # The in-flight ChatStream, or None when no response is in flight.
        self._stream: ChatStream | None = None
        # Accumulators that the renderer reads from each frame.
        self._stream_content: list[str] = []
        self._stream_reasoning_chars: int = 0
        # Best-effort approximate token count for status display
        # (chars / 4, since average tokens are ~3-4 chars).
        self._last_usage: dict | None = None

        # ─── Scrollback state ───
        self.scroll_offset: int = 0
        self._auto_scroll: bool = True
        self._last_chat_h: int = 10
        self._last_chat_w: int = 80
        self._last_total_height: int = 0

        # ─── Input parsing ───
        self._key_decoder = KeyDecoder()
        # Bracketed paste suppression flag — while inside a paste, we
        # accumulate content into the input buffer (treating CR/LF as
        # literal newlines) instead of triggering submit on Enter.
        self._in_paste: bool = False

    # ─── Input handling ───

    def on_key(self, byte: int) -> None:
        """Bytes from stdin → InputEvents → dispatched.

        The decoder may emit KeyEvent or MouseEvent depending on what
        the byte stream encodes. Mouse events only arrive when mouse
        reporting is enabled (via /mouse on).
        """
        for event in self._key_decoder.feed(byte):
            if isinstance(event, MouseEvent):
                self._handle_mouse_event(event)
            else:
                self._handle_key_event(event)

    def _handle_mouse_event(self, event: MouseEvent) -> None:
        """Dispatch a mouse event to the appropriate handler.

        Scroll wheel works regardless of widget hit boxes. Left clicks
        check the recorded hit boxes for the title-bar widgets and
        dispatch to the same handlers the keyboard shortcuts use.
        Other buttons are ignored for v0.
        """
        # Scroll wheel — always navigate the chat history.
        if event.button == MouseButton.WHEEL_UP:
            self._scroll_lines(WHEEL_SCROLL_LINES)
            return
        if event.button == MouseButton.WHEEL_DOWN:
            self._scroll_lines(-WHEEL_SCROLL_LINES)
            return

        # We only act on left button presses (not releases).
        if event.button != MouseButton.LEFT or not event.pressed:
            return

        # Find which hit box contains the click.
        for hb in self._hit_boxes:
            if hb.contains(event.col, event.row):
                if hb.action == "theme":
                    self._cycle_theme()
                elif hb.action == "density":
                    self._cycle_density()
                elif hb.action == "scroll_to_bottom":
                    self._scroll_to_bottom()
                elif hb.action.startswith("slash:"):
                    # Click on an autocomplete name-mode row — jump
                    # selection to this command and accept it.
                    name = hb.action[len("slash:"):]
                    state = self._autocomplete_state()
                    if isinstance(state, _NameMode):
                        for i, cmd in enumerate(state.matches):
                            if cmd.name == name:
                                self._autocomplete_selected = i
                                break
                        self._autocomplete_accept()
                elif hb.action.startswith("arg:"):
                    # Click on an autocomplete arg-mode row — jump
                    # selection to this arg and accept it.
                    arg = hb.action[len("arg:"):]
                    state = self._autocomplete_state()
                    if isinstance(state, _ArgMode):
                        for i, candidate in enumerate(state.matches):
                            if candidate == arg:
                                self._autocomplete_selected = i
                                break
                        self._autocomplete_accept()
                return

    def _handle_key_event(self, event: KeyEvent) -> None:
        # ─── Bracketed paste boundaries ───
        if event.key == Key.PASTE_START:
            self._in_paste = True
            return
        if event.key == Key.PASTE_END:
            self._in_paste = False
            return

        # ─── Theme cycle (always available, even mid-stream) ───
        if event.is_ctrl and event.char == "t":
            self._cycle_theme()
            return

        # ─── Density (font-size feel) — Alt+=/Alt+-/Ctrl+] always available ───
        if event.is_alt and event.char in ("=", "+"):
            self._density_step(+1)
            return
        if event.is_alt and event.char == "-":
            self._density_step(-1)
            return
        if event.is_ctrl and event.char == "]":
            self._cycle_density()
            return

        # ─── Scroll keys (always available, even mid-stream) ───
        # When the autocomplete dropdown is open, Up/Down navigate the
        # dropdown selection instead of scrolling the chat history.
        if event.key == Key.UP:
            if self._autocomplete_active():
                self._autocomplete_move(-1)
            else:
                self._scroll_lines(1)
            return
        if event.key == Key.DOWN:
            if self._autocomplete_active():
                self._autocomplete_move(1)
            else:
                self._scroll_lines(-1)
            return
        if event.key == Key.PG_UP:
            self._scroll_lines(self._page_size())
            return
        if event.key == Key.PG_DOWN:
            self._scroll_lines(-self._page_size())
            return
        if event.key == Key.HOME:
            self._scroll_to_top()
            return
        if event.key == Key.END:
            self._scroll_to_bottom()
            return

        # ─── Ctrl-prefix shortcuts (vim-style scroll fallback) ───
        if event.is_ctrl and not event.is_alt:
            if event.char == "b":
                self._scroll_lines(self._page_size())
                return
            if event.char == "f":
                self._scroll_lines(-self._page_size())
                return
            if event.char == "p":
                self._scroll_lines(1)
                return
            if event.char == "n":
                self._scroll_lines(-1)
                return
            if event.char == "e":
                self._scroll_to_bottom()
                return
            if event.char == "y":
                self._scroll_to_top()
                return
            # Ctrl+G to abort an in-flight stream (interrupt)
            if event.char == "g" and self._stream is not None:
                self._stream.close()
                return

        # ─── Streaming guard ───
        # While ronin is responding, swallow editing/typing keys.
        if self._stream is not None:
            return

        # ─── Editing ───
        if event.key == Key.BACKSPACE:
            if self.input_buffer:
                self.input_buffer = self.input_buffer[:-1]
                # Reset autocomplete selection when the buffer changes,
                # and clear the dismiss flag so the dropdown returns the
                # moment the user starts engaging again.
                self._autocomplete_selected = 0
                self._autocomplete_dismissed = False
            return
        if event.key == Key.ENTER:
            if self._in_paste:
                # Inside a paste, Enter is a literal newline.
                self.input_buffer += "\n"
                return
            # Dispatch based on autocomplete state.
            state = self._autocomplete_state()
            if isinstance(state, _NameMode):
                cmd = state.matches[state.selected]
                expected = f"/{cmd.name}"
                # If the buffer doesn't yet match the highlighted
                # command, Enter accepts. For commands with no args,
                # we accept-and-submit in one keystroke (single-key UX).
                if self.input_buffer.rstrip() != expected:
                    self._autocomplete_accept()
                    if cmd.complete_args is None:
                        if self.input_buffer.strip():
                            self._submit()
                    return
                # Buffer already matches the command (no args, no
                # remaining work) — submit.
            elif isinstance(state, _ArgMode):
                # In arg mode, Enter always accept-and-submits.
                full_arg = state.matches[state.selected]
                expected = f"/{state.command.name} {full_arg}"
                if self.input_buffer.rstrip() != expected:
                    self._autocomplete_accept()
                if self.input_buffer.strip():
                    self._submit()
                return
            # No dropdown open OR _NoMatches OR name-mode-already-matches
            # — fall through to the normal submit path.
            if self.input_buffer.strip():
                self._submit()
            return
        if event.key == Key.TAB:
            # Tab always accepts the current selection but NEVER submits.
            # Lets users complete a command name or arg and then keep
            # typing (e.g. accept /theme then keep editing the args).
            # No-op when there's no selectable dropdown.
            if self._autocomplete_active():
                self._autocomplete_accept()
            return
        if event.key == Key.ESC:
            # Non-destructive: Esc hides the dropdown but leaves the
            # buffer alone. The user can keep typing or backspace to
            # recover. Esc is a no-op when the buffer doesn't start
            # with / so we never blow away a long message.
            if self.input_buffer.startswith("/"):
                self._autocomplete_dismiss()
            return

        # ─── Character input (printable + UTF-8 + paste chunks) ───
        if event.is_char and event.char and not event.is_ctrl:
            # Filter out anything that's not safe to display in the input.
            # Allow newlines (for pasted multi-line content), printable
            # ASCII, and any Unicode codepoint >= 0x20.
            safe = "".join(
                c for c in event.char
                if c == "\n" or ord(c) >= 0x20
            )
            if safe:
                self.input_buffer += safe
                # New character → re-filter the autocomplete from the top
                # and bring the dropdown back if it was dismissed.
                self._autocomplete_selected = 0
                self._autocomplete_dismissed = False
            return

        # Anything else (unknown CSI, F-keys, etc.) silently ignored.

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
        return max(1, self._last_chat_h - 1)

    # ─── Theme management ───

    def _set_theme(self, new_theme: Theme) -> None:
        """Switch to a new theme with a smooth lerp transition."""
        if new_theme is self.theme:
            return
        self._theme_from = self._current_theme()
        self.theme = new_theme
        self._theme_t0 = time.monotonic()

    def _cycle_theme(self) -> None:
        self._set_theme(next_theme(self.theme))

    # ─── Density management ───

    def _set_density(self, new_density: Density) -> None:
        if new_density is self.density:
            return
        self._density_from = self._current_density()
        self.density = new_density
        self._density_t0 = time.monotonic()

    def _density_step(self, delta: int) -> None:
        """Step density by +1 (toward spacious) or -1 (toward compact)."""
        idx = density_index(self.density)
        if idx < 0:
            idx = DENSITIES.index(NORMAL)
        new_idx = max(0, min(len(DENSITIES) - 1, idx + delta))
        self._set_density(DENSITIES[new_idx])

    def _cycle_density(self) -> None:
        idx = density_index(self.density)
        if idx < 0:
            idx = 0
        self._set_density(DENSITIES[(idx + 1) % len(DENSITIES)])

    def _current_density(self) -> Density:
        """The density to use for THIS frame's render.

        If a density transition is in progress, returns a blended
        density partway between the source and the target. When the
        transition completes, drops the source and returns self.density
        directly.
        """
        if self._density_from is None:
            return self.density
        elapsed = time.monotonic() - self._density_t0
        if elapsed >= DENSITY_TRANSITION_S:
            self._density_from = None
            return self.density
        t = ease_out_cubic(elapsed / DENSITY_TRANSITION_S)
        return blend_densities(self._density_from, self.density, t)

    # ─── Mouse mode toggle ───

    def _enable_mouse(self) -> None:
        if self._mouse_enabled:
            return
        self.term.set_mouse_reporting(True)
        self._mouse_enabled = True

    def _disable_mouse(self) -> None:
        if not self._mouse_enabled:
            return
        self.term.set_mouse_reporting(False)
        self._mouse_enabled = False

    # ─── Slash command autocomplete ───

    def _autocomplete_state(self) -> _AutocompleteState:
        """Compute the current autocomplete state from the input buffer.

        Returns one of:
          None        — dropdown is hidden (no slash, dismissed, etc.)
          _NameMode   — user is typing a command name and there are matches
          _ArgMode    — user is typing args for a known command and there
                        are matches
          _NoMatches  — slash mode but nothing matches (informational popover)
        """
        if self._autocomplete_dismissed:
            return None

        text = self.input_buffer
        if not text.startswith("/"):
            return None

        rest = text[1:]

        # Past the command name? (has a space — even just trailing)
        if " " in rest:
            cmd_name, _, arg_partial = rest.partition(" ")
            cmd = find_slash_command(cmd_name)
            if cmd is None or cmd.complete_args is None:
                # Unknown command, or this command takes no args.
                # No autocomplete in either case.
                return None
            matches = cmd.complete_args(arg_partial)
            if not matches:
                # Show the no-matches popover with the valid options
                # so the user knows what they can pick.
                return _NoMatches(
                    mode="arg",
                    text=f"no {cmd.name} matches '{arg_partial}'",
                    valid_options=tuple(cmd.complete_args("")),
                    command=cmd,
                )
            sel = max(0, min(self._autocomplete_selected, len(matches) - 1))
            return _ArgMode(
                command=cmd,
                matches=matches,
                selected=sel,
                partial=arg_partial,
            )

        # Name completion mode
        matches = filter_slash_commands(rest)
        if not matches:
            return _NoMatches(
                mode="name",
                text=f"no command matches '/{rest}'",
            )
        sel = max(0, min(self._autocomplete_selected, len(matches) - 1))
        return _NameMode(
            matches=matches,
            selected=sel,
            prefix=rest,
        )

    def _autocomplete_active(self) -> bool:
        """True iff there's an active selectable dropdown right now."""
        state = self._autocomplete_state()
        return isinstance(state, (_NameMode, _ArgMode))

    def _autocomplete_move(self, delta: int) -> None:
        """Move the highlighted selection in whichever dropdown is open."""
        state = self._autocomplete_state()
        if isinstance(state, _NameMode):
            n = len(state.matches)
            if n > 0:
                self._autocomplete_selected = (state.selected + delta) % n
        elif isinstance(state, _ArgMode):
            n = len(state.matches)
            if n > 0:
                self._autocomplete_selected = (state.selected + delta) % n

    def _autocomplete_accept(self) -> bool:
        """Accept the highlighted suggestion.

        In name mode: replace the buffer with /cmd (or /cmd<space> if
        the command takes args).
        In arg mode: replace the partial arg with the full match.

        Returns True if anything was accepted (used by Enter handling
        to decide whether to also submit afterward).
        """
        state = self._autocomplete_state()
        if isinstance(state, _NameMode):
            cmd = state.matches[state.selected]
            self.input_buffer = f"/{cmd.name}"
            if cmd.complete_args is not None:
                self.input_buffer += " "
            self._autocomplete_selected = 0
            return True
        if isinstance(state, _ArgMode):
            full_arg = state.matches[state.selected]
            self.input_buffer = f"/{state.command.name} {full_arg}"
            self._autocomplete_selected = 0
            return True
        return False

    def _autocomplete_dismiss(self) -> None:
        """Hide the dropdown without clearing the buffer.

        Esc calls this. The buffer is preserved so the user can keep
        typing or backspace to recover. Any input mutation (typing,
        backspace) clears the dismissed flag and the dropdown comes
        back automatically.
        """
        self._autocomplete_dismissed = True
        self._autocomplete_selected = 0

    def _current_theme(self) -> Theme:
        """The theme to use for THIS frame's render.

        If a transition is in progress, return a blended theme that's
        partway between `_theme_from` and `self.theme`. When the
        transition completes, drop the source and return `self.theme`
        directly.
        """
        if self._theme_from is None:
            return self.theme
        elapsed = time.monotonic() - self._theme_t0
        if elapsed >= THEME_TRANSITION_S:
            self._theme_from = None
            return self.theme
        t = ease_out_cubic(elapsed / THEME_TRANSITION_S)
        return blend_themes(self._theme_from, self.theme, t)

    # ─── Submission ───

    def _submit(self) -> None:
        text = self.input_buffer.strip()
        self.input_buffer = ""

        if text in ("/quit", "/exit", "/q"):
            self.stop()
            return

        # /theme       — show current theme and available options
        # /theme dark  — switch to dark theme
        # /theme light — switch to light theme
        # /theme forge — switch to forge theme
        # /theme cycle — cycle to next theme
        if text.startswith("/theme"):
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                names = ", ".join(t.name for t in THEMES)
                hint = (
                    f"current theme: {self.theme.name} {self.theme.icon}. "
                    f"Available: {names}. Use /theme <name> or Ctrl+T to cycle."
                )
                self.messages.append(_Message("ronin", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "cycle":
                self._cycle_theme()
                return
            target = find_theme(arg)
            if target is None:
                self.messages.append(
                    _Message(
                        "ronin",
                        f"no theme named '{arg}'. try one of: "
                        f"{', '.join(t.name for t in THEMES)}.",
                        synthetic=True,
                    )
                )
                return
            self._set_theme(target)
            return

        # /mouse         — show current state
        # /mouse on      — enable mouse reporting (clickable widgets, scroll wheel)
        # /mouse off     — disable
        # /mouse toggle  — flip
        if text.startswith("/mouse"):
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                state = "on" if self._mouse_enabled else "off"
                hint = (
                    f"mouse: {state}. /mouse on enables clickable widgets and "
                    f"scroll wheel; while on, hold Shift to drag-select text."
                )
                self.messages.append(_Message("ronin", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "on":
                self._enable_mouse()
                self.messages.append(
                    _Message(
                        "ronin",
                        "mouse on. Click the title-bar widgets, use scroll wheel "
                        "to navigate history. Hold Shift while click-dragging to "
                        "use native text selection.",
                        synthetic=True,
                    )
                )
                return
            if arg == "off":
                self._disable_mouse()
                self.messages.append(
                    _Message(
                        "ronin",
                        "mouse off. Native click-drag selection works again.",
                        synthetic=True,
                    )
                )
                return
            if arg == "toggle":
                if self._mouse_enabled:
                    self._disable_mouse()
                else:
                    self._enable_mouse()
                return
            self.messages.append(
                _Message(
                    "ronin",
                    f"unknown /mouse argument '{arg}'. try on, off, or toggle.",
                    synthetic=True,
                )
            )
            return

        # /density       — show current density and available options
        # /density compact / normal / spacious — set
        # /density cycle — cycle to next
        if text.startswith("/density"):
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                names = ", ".join(d.name for d in DENSITIES)
                hint = (
                    f"current density: {self.density.name}. "
                    f"Available: {names}. Use /density <name> or Alt+=/Alt+- "
                    f"or Ctrl+] to cycle."
                )
                self.messages.append(_Message("ronin", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "cycle":
                self._cycle_density()
                return
            target = find_density(arg)
            if target is None:
                self.messages.append(
                    _Message(
                        "ronin",
                        f"no density named '{arg}'. try one of: "
                        f"{', '.join(d.name for d in DENSITIES)}.",
                        synthetic=True,
                    )
                )
                return
            self._set_density(target)
            return

        # Add the user's message and start a stream.
        self.messages.append(_Message("user", text))
        self._scroll_to_bottom()

        # Build the conversation history for the model. Skip synthetic
        # messages (the greeting); they were never the model's output
        # and shouldn't be in its conversation context.
        api_messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for m in self.messages:
            if m.synthetic:
                continue
            api_role = "user" if m.role == "user" else "assistant"
            api_messages.append({"role": api_role, "content": m.raw_text})

        self._stream = self.client.stream_chat(messages=api_messages)
        self._stream_content = []
        self._stream_reasoning_chars = 0

    def _pump_stream(self) -> None:
        """Drain any pending stream events and update accumulators."""
        if self._stream is None:
            return

        events = self._stream.drain()
        for ev in events:
            if isinstance(ev, StreamStarted):
                pass
            elif isinstance(ev, ReasoningChunk):
                self._stream_reasoning_chars += len(ev.text)
            elif isinstance(ev, ContentChunk):
                self._stream_content.append(ev.text)
            elif isinstance(ev, StreamEnded):
                full_content = "".join(self._stream_content)
                if not full_content:
                    full_content = "(no answer — model produced only reasoning)"
                self.messages.append(_Message("ronin", full_content))
                self._last_usage = ev.usage
                self._stream = None
                self._stream_content = []
                self._stream_reasoning_chars = 0
            elif isinstance(ev, StreamError):
                partial = "".join(self._stream_content)
                if partial:
                    msg = f"{partial}\n\n[stream interrupted: {ev.message}]"
                else:
                    msg = f"[stream failed: {ev.message}]"
                self.messages.append(_Message("ronin", msg, synthetic=True))
                self._stream = None
                self._stream_content = []
                self._stream_reasoning_chars = 0

    # ─── Layout helpers ───

    def _input_lines_at_width(self, width: int) -> list[str]:
        avail = max(1, width - PROMPT_WIDTH)
        return hard_wrap(self.input_buffer, avail)

    def _input_height(self, width: int) -> int:
        wrapped = self._input_lines_at_width(width)
        h = max(INPUT_MIN_ROWS, min(INPUT_MAX_ROWS, len(wrapped)))
        return h

    # ─── Rendering ───

    def on_tick(self, grid: Grid) -> None:
        # Flush bare ESC / incomplete sequences from the key decoder.
        for event in self._key_decoder.flush():
            if isinstance(event, MouseEvent):
                self._handle_mouse_event(event)
            else:
                self._handle_key_event(event)

        # Reset hit boxes — refilled as widgets are painted this frame.
        self._hit_boxes = []

        # Drain any pending llama.cpp stream events.
        self._pump_stream()

        # Resolve the active theme for THIS frame. If a theme transition
        # is in progress this returns a blended palette; otherwise it's
        # just self.theme. Every painter takes `theme` as a parameter so
        # the same code paints in any palette.
        theme = self._current_theme()

        rows, cols = grid.rows, grid.cols
        if rows < 3 or cols < 4:
            fill_region(grid, 0, 0, cols, rows, style=Style(bg=theme.bg))
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
        fill_region(grid, 0, 0, cols, rows, style=Style(bg=theme.bg))

        # ─── Title row ───
        title = " ronin · chat "
        title_style = Style(fg=theme.fg, bg=theme.bg, attrs=ATTR_BOLD)
        tx = max(0, (cols - len(title)) // 2)
        paint_text(grid, title, tx, 0, style=title_style)

        # ─── Chat scroll area ───
        self._paint_chat_area(grid, chat_top, chat_bottom, cols, theme)

        # ─── Theme widget (rightmost cell of title row) ───
        # Renders as a small accent-colored pill so it visually reads
        # as an interactive element. Keybinding: Ctrl+T. Click target
        # when mouse mode is on.
        theme_label = f" {theme.icon} {theme.name} "
        theme_style = Style(
            fg=theme.bg,
            bg=theme.accent,
            attrs=ATTR_BOLD,
        )
        theme_x = max(0, cols - len(theme_label))
        paint_text(grid, theme_label, theme_x, 0, style=theme_style)
        self._hit_boxes.append(
            _HitBox(theme_x, 0, len(theme_label), 1, "theme")
        )

        # ─── Density widget (just left of the theme widget) ───
        # Different background color so it visually distinguishes from
        # the theme widget. Keybindings: Alt+=, Alt+-, Ctrl+]. Click
        # target when mouse mode is on.
        density_label = f" {self.density.name} "
        density_style = Style(
            fg=theme.bg,
            bg=theme.accent_warm,
            attrs=ATTR_BOLD,
        )
        density_x = max(0, theme_x - len(density_label) - 1)
        paint_text(grid, density_label, density_x, 0, style=density_style)
        self._hit_boxes.append(
            _HitBox(density_x, 0, len(density_label), 1, "density")
        )

        # ─── Scroll indicator (left of the density widget when scrolled) ───
        if self.scroll_offset > 0:
            if self._stream is not None:
                indicator = f" ↑ {self.scroll_offset} · ronin responding · Ctrl+E newest "
            else:
                indicator = f" ↑ {self.scroll_offset}/{self._max_scroll()} · End for newest "
            ix = max(0, density_x - len(indicator))
            paint_text(
                grid,
                indicator,
                ix,
                0,
                style=Style(fg=theme.accent_warm, bg=theme.bg, attrs=ATTR_BOLD),
            )
            self._hit_boxes.append(
                _HitBox(ix, 0, len(indicator), 1, "scroll_to_bottom")
            )

        # ─── Input area ───
        if input_y >= 0 and input_y < rows:
            self._paint_input(grid, input_y, min(input_h, rows - input_y), cols, theme)

        # ─── Static footer (ctx bar) ───
        if 0 <= static_y < rows:
            self._paint_static_footer(grid, static_y, cols, theme)

        # ─── Slash command autocomplete dropdown ───
        # Painted LAST so it overlays the chat area cells just above the
        # input. The chat content underneath is temporarily hidden while
        # the dropdown is visible; closing it (Esc / submit / type a
        # space) restores everything on the next frame's diff.
        self._paint_autocomplete(grid, theme, input_y)

    # ─── Region painters ───

    def _paint_chat_area(
        self,
        grid: Grid,
        top: int,
        bottom: int,
        width: int,
        theme: Theme,
    ) -> None:
        if bottom <= top or width <= 2:
            return

        # Density-driven layout. Use _current_density() so content
        # width smoothly slides during transitions instead of snapping.
        # Compact uses no cap (the sentinel _DENSITY_NO_CAP degenerates
        # to "no effective limit" because it's larger than any real
        # terminal width). Normal/spacious cap to a comfortable reading
        # width and add gutter cells on each side.
        density = self._current_density()
        gutter = density.gutter
        avail = max(1, width - 2 * gutter)
        avail = min(avail, density.max_content_width)
        body_width = avail
        # Center the content column within the available cells so the
        # extra space (when content is capped) goes to both sides.
        body_x = max(gutter, (width - body_width) // 2)
        chat_h = bottom - top

        # Build the flat list of committed-message lines.
        committed = self._build_message_lines(body_width, theme)
        committed_h = len(committed)

        # Auto-anchor: if scrolled up and content grew, advance offset
        # to keep the same historical view under the user's eyes.
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

        # Streaming reply — only when anchored at bottom.
        if self._stream is not None and self.scroll_offset == 0:
            stream_lines = self._build_streaming_lines(body_width, theme)
            combined = visible + stream_lines
            if len(combined) > chat_h:
                combined = combined[-chat_h:]
        else:
            combined = visible

        # Anchor the visible block to the BOTTOM of the chat area.
        paint_y = bottom - len(combined)
        if paint_y < top:
            paint_y = top

        for i, (line, fg) in enumerate(combined):
            y = paint_y + i
            if y >= bottom:
                break
            if line:
                paint_text(grid, line, body_x, y, style=Style(fg=fg, bg=theme.bg))

    # ─── Flat-line builders ───

    def _build_message_lines(
        self,
        body_width: int,
        theme: Theme,
    ) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        now = time.monotonic()
        n = len(self.messages)
        spacing = self._current_density().message_spacing
        for i, msg in enumerate(self.messages):
            age = now - msg.created_at
            fade_t = (
                ease_out_cubic(min(1.0, age / FADE_IN_S))
                if age < FADE_IN_S
                else 1.0
            )
            base_color = theme.fg if msg.role == "user" else theme.accent
            if msg.synthetic:
                base_color = theme.fg_dim
            if fade_t < 1.0:
                fg = lerp_rgb(theme.fg_subtle, base_color, fade_t)
            else:
                fg = base_color
            for line in msg.body.lines(body_width):
                out.append((line, fg))
            # Density-driven spacer rows between messages.
            if i < n - 1:
                for _ in range(spacing):
                    out.append(("", fg))
        return out

    def _build_streaming_lines(
        self,
        body_width: int,
        theme: Theme,
    ) -> list[tuple[str, int]]:
        if self._stream is None:
            return []
        now = time.monotonic()
        spinner_idx = int(now * SPINNER_FPS) % len(SPINNER_FRAMES)
        spinner = SPINNER_FRAMES[spinner_idx]

        content_so_far = "".join(self._stream_content)
        if not content_so_far:
            if self._stream_reasoning_chars > 0:
                text = f"ronin ▸ {spinner} thinking… ({self._stream_reasoning_chars} chars)"
            else:
                text = f"ronin ▸ {spinner} thinking…"
        else:
            text = f"ronin ▸ {content_so_far}▌"

        stream_pt = PreparedText(text)
        out: list[tuple[str, int]] = [("", theme.accent)]
        for line in stream_pt.lines(body_width):
            out.append((line, theme.accent))
        return out

    # ─── Slash command autocomplete dropdown ───

    def _paint_autocomplete(
        self,
        grid: Grid,
        theme: Theme,
        input_y: int,
    ) -> None:
        """Render the autocomplete popover above the input area.

        Dispatches to the appropriate painter based on the current
        autocomplete state (name mode, arg mode, or no-matches).
        """
        state = self._autocomplete_state()
        if state is None:
            return
        rows, cols = grid.rows, grid.cols
        if cols < 30 or input_y < 4:
            return

        if isinstance(state, _NameMode):
            self._paint_name_mode(grid, theme, input_y, state)
        elif isinstance(state, _ArgMode):
            self._paint_arg_mode(grid, theme, input_y, state)
        elif isinstance(state, _NoMatches):
            self._paint_no_matches(grid, theme, input_y, state)

    def _blank_dropdown_rows(self, grid: Grid, theme: Theme, box_y: int, box_h: int) -> None:
        """Blank the full row width of the rows the dropdown occupies.

        Without this the chat content underneath would leak around the
        dropdown's left and right edges. Doing this gives every dropdown
        variant the same clean visual frame.
        """
        for blank_y in range(box_y, box_y + box_h):
            if 0 <= blank_y < grid.rows:
                fill_region(
                    grid, 0, blank_y, grid.cols, 1,
                    style=Style(bg=theme.bg),
                )

    def _paint_name_mode(
        self,
        grid: Grid,
        theme: Theme,
        input_y: int,
        state: _NameMode,
    ) -> None:
        cols = grid.cols

        # Cap visible rows so the box never exceeds the room above the input.
        max_visible = max(1, min(len(state.matches), max(3, input_y - 3)))
        visible = state.matches[:max_visible]

        cmd_col_w = max(len(f"/{c.name}") for c in visible)
        desc_col_w = max((len(c.description) for c in visible), default=0)
        hint_col_w = max((len(c.args_hint) for c in visible), default=0)

        inner_w = cmd_col_w + 2 + desc_col_w
        if hint_col_w > 0:
            inner_w += 2 + hint_col_w
        inner_w = max(inner_w, 36)
        box_w = min(inner_w + 4, cols - 2)
        box_h = max_visible + 2

        box_x = max(0, PROMPT_WIDTH)
        box_y = input_y - box_h - 1
        if box_y < 1:
            box_y = 1
            box_h = min(box_h, input_y - box_y - 1)
            if box_h < 3:
                return

        self._blank_dropdown_rows(grid, theme, box_y, box_h)

        border_style = Style(fg=theme.accent_warm, bg=theme.bg_input, attrs=ATTR_BOLD)
        fill_style = Style(fg=theme.fg, bg=theme.bg_input)
        paint_box(
            grid, box_x, box_y, box_w, box_h,
            style=border_style, fill_style=fill_style,
        )

        item_x = box_x + 2
        for i, cmd in enumerate(visible):
            row_y = box_y + 1 + i
            if row_y >= box_y + box_h - 1:
                break

            is_selected = i == state.selected
            row_bg = theme.accent if is_selected else theme.bg_input
            row_fg = theme.bg if is_selected else theme.fg
            dim_fg = theme.bg if is_selected else theme.fg_dim
            subtle_fg = theme.bg if is_selected else theme.fg_subtle

            fill_region(
                grid, box_x + 1, row_y, box_w - 2, 1,
                style=Style(bg=row_bg),
            )

            cmd_text = f"/{cmd.name}"
            paint_text(grid, cmd_text, item_x, row_y,
                       style=Style(fg=row_fg, bg=row_bg, attrs=ATTR_BOLD))

            desc_x = item_x + cmd_col_w + 2
            paint_text(grid, cmd.description, desc_x, row_y,
                       style=Style(fg=dim_fg, bg=row_bg))

            if cmd.args_hint:
                hint_x = desc_x + desc_col_w + 2
                paint_text(grid, cmd.args_hint, hint_x, row_y,
                           style=Style(fg=subtle_fg, bg=row_bg, attrs=ATTR_DIM))

            # Hit box for clickable rows when mouse mode is on.
            self._hit_boxes.append(
                _HitBox(box_x + 1, row_y, box_w - 2, 1, f"slash:{cmd.name}")
            )

    def _paint_arg_mode(
        self,
        grid: Grid,
        theme: Theme,
        input_y: int,
        state: _ArgMode,
    ) -> None:
        cols = grid.cols
        cmd = state.command

        max_visible = max(1, min(len(state.matches), max(3, input_y - 4)))
        visible = state.matches[:max_visible]

        # Header row shows "<cmd> · <arg hint>" so the user knows what
        # they're picking from. The arg rows below show each option.
        header = f" /{cmd.name} · {cmd.description} "
        arg_col_w = max(len(a) for a in visible)
        inner_w = max(len(header) - 2, arg_col_w + 4)
        inner_w = max(inner_w, 36)
        box_w = min(inner_w + 4, cols - 2)
        # Box height: top border + header row + items + bottom border
        box_h = 1 + max_visible + 2

        box_x = max(0, PROMPT_WIDTH)
        box_y = input_y - box_h - 1
        if box_y < 1:
            box_y = 1
            box_h = min(box_h, input_y - box_y - 1)
            if box_h < 4:
                return

        self._blank_dropdown_rows(grid, theme, box_y, box_h)

        border_style = Style(fg=theme.accent, bg=theme.bg_input, attrs=ATTR_BOLD)
        fill_style = Style(fg=theme.fg, bg=theme.bg_input)
        paint_box(
            grid, box_x, box_y, box_w, box_h,
            style=border_style, fill_style=fill_style,
        )

        # Header (just below the top border)
        header_y = box_y + 1
        if header_y < box_y + box_h - 1:
            fill_region(
                grid, box_x + 1, header_y, box_w - 2, 1,
                style=Style(bg=theme.bg_footer),
            )
            paint_text(
                grid, header, box_x + 2, header_y,
                style=Style(fg=theme.fg_dim, bg=theme.bg_footer, attrs=ATTR_BOLD),
            )

        # Item rows (start one row below the header)
        item_x = box_x + 2
        first_item_y = box_y + 2
        for i, arg in enumerate(visible):
            row_y = first_item_y + i
            if row_y >= box_y + box_h - 1:
                break

            is_selected = i == state.selected
            row_bg = theme.accent if is_selected else theme.bg_input
            row_fg = theme.bg if is_selected else theme.fg
            dim_fg = theme.bg if is_selected else theme.fg_dim

            fill_region(
                grid, box_x + 1, row_y, box_w - 2, 1,
                style=Style(bg=row_bg),
            )

            # Highlight the matched prefix in the arg
            paint_text(
                grid, arg, item_x, row_y,
                style=Style(fg=row_fg, bg=row_bg, attrs=ATTR_BOLD),
            )
            # Show the partial as a dim suffix to make matching obvious
            if state.partial:
                hint_x = item_x + arg_col_w + 2
                hint_text = f"matched '{state.partial}'"
                paint_text(
                    grid, hint_text, hint_x, row_y,
                    style=Style(fg=dim_fg, bg=row_bg, attrs=ATTR_DIM),
                )

            self._hit_boxes.append(
                _HitBox(box_x + 1, row_y, box_w - 2, 1, f"arg:{arg}")
            )

    def _paint_no_matches(
        self,
        grid: Grid,
        theme: Theme,
        input_y: int,
        state: _NoMatches,
    ) -> None:
        """Informational popover when nothing matches the typed prefix.

        Dimmer styling than the regular dropdown — fg_dim border and
        text — so it reads as 'FYI, no results' rather than an error.
        """
        cols = grid.cols

        # Build the lines we want to display
        lines: list[str] = [state.text]
        if state.mode == "name":
            lines.append("type / alone to see all commands")
        elif state.mode == "arg" and state.valid_options:
            valid = ", ".join(state.valid_options)
            lines.append(f"valid: {valid}")

        inner_w = max(len(l) for l in lines)
        inner_w = max(inner_w, 32)
        box_w = min(inner_w + 4, cols - 2)
        box_h = len(lines) + 2  # top + bottom borders + lines

        box_x = max(0, PROMPT_WIDTH)
        box_y = input_y - box_h - 1
        if box_y < 1:
            return

        self._blank_dropdown_rows(grid, theme, box_y, box_h)

        # Quieter colors than the regular dropdown — this is informational.
        border_style = Style(fg=theme.fg_dim, bg=theme.bg_input)
        fill_style = Style(fg=theme.fg_dim, bg=theme.bg_input)
        paint_box(
            grid, box_x, box_y, box_w, box_h,
            style=border_style, fill_style=fill_style,
        )

        for i, text in enumerate(lines):
            row_y = box_y + 1 + i
            if row_y >= box_y + box_h - 1:
                break
            # First line is the headline; subsequent lines are dimmer.
            fg = theme.fg_dim if i == 0 else theme.fg_subtle
            paint_text(
                grid, text, box_x + 2, row_y,
                style=Style(fg=fg, bg=theme.bg_input, attrs=ATTR_DIM),
            )

    # ─── Static footer ───

    def _paint_static_footer(
        self,
        grid: Grid,
        y: int,
        width: int,
        theme: Theme,
    ) -> None:
        # Compute approximate token usage from the latest known usage
        # info, falling back to a char-count estimate.
        if self._last_usage and "total_tokens" in self._last_usage:
            used = int(self._last_usage["total_tokens"])
        else:
            used = sum(len(m.raw_text) for m in self.messages) // 4
            if self._stream is not None:
                used += (self._stream_reasoning_chars + len("".join(self._stream_content))) // 4
        used = max(0, min(used, CONTEXT_MAX))
        pct = used / CONTEXT_MAX

        # Footer background
        fill_region(grid, 0, y, width, 1, style=Style(bg=theme.bg_footer))

        label = f" ctx {used:>6}/{CONTEXT_MAX} "
        right_label = f" {pct * 100:5.2f}%  {self.client.model[:20]} "
        label_style = Style(fg=theme.fg_dim, bg=theme.bg_footer, attrs=ATTR_DIM)
        right_style = Style(fg=theme.fg, bg=theme.bg_footer, attrs=ATTR_BOLD)

        paint_text(grid, label, 0, y, style=label_style)

        bar_x = len(label) + 1
        right_x = max(0, width - len(right_label))
        bar_w = max(0, right_x - bar_x - 1)

        if bar_w > 0:
            filled = int(round(bar_w * pct))
            empty = bar_w - filled
            if pct < 0.6:
                bar_fg = lerp_rgb(theme.accent, theme.accent_warm, pct / 0.6)
            elif pct < 0.85:
                bar_fg = lerp_rgb(theme.accent_warm, theme.accent_warn, (pct - 0.6) / 0.25)
            else:
                bar_fg = theme.accent_warn
            if filled > 0:
                paint_text(
                    grid,
                    "█" * filled,
                    bar_x,
                    y,
                    style=Style(fg=bar_fg, bg=theme.bg_footer),
                )
            if empty > 0:
                paint_text(
                    grid,
                    "░" * empty,
                    bar_x + filled,
                    y,
                    style=Style(fg=theme.fg_subtle, bg=theme.bg_footer),
                )

        paint_text(grid, right_label, right_x, y, style=right_style)

    # ─── Input area ───

    def _paint_input(
        self,
        grid: Grid,
        y: int,
        height: int,
        width: int,
        theme: Theme,
    ) -> None:
        fill_region(grid, 0, y, width, height, style=Style(bg=theme.bg_input))

        wrapped = self._input_lines_at_width(width)
        wrapped = wrapped[-height:] if len(wrapped) > height else wrapped

        prompt_style = Style(fg=theme.accent, bg=theme.bg_input, attrs=ATTR_BOLD)
        paint_text(grid, PROMPT, 0, y, style=prompt_style)

        text_style = Style(fg=theme.fg, bg=theme.bg_input)
        for i, line in enumerate(wrapped):
            ly = y + i
            if ly >= y + height:
                break
            paint_text(grid, line, PROMPT_WIDTH, ly, style=text_style)

        # Cursor / streaming-status indicator on the last visible line.
        if self._stream is None:
            last_line = wrapped[-1] if wrapped else ""
            last_y = y + min(len(wrapped) - 1, height - 1)
            cursor_x = min(width - 1, PROMPT_WIDTH + len(last_line))
            visible = (int(time.monotonic() * CURSOR_BLINK_HZ * 2) % 2) == 0
            if visible:
                cursor_cell = Cell(" ", Style(fg=theme.bg_input, bg=theme.fg))
                grid.set(last_y, cursor_x, cursor_cell)
        else:
            hint = "ronin is responding…  Ctrl+G to interrupt"
            paint_text(
                grid,
                hint,
                PROMPT_WIDTH,
                y,
                style=Style(fg=theme.fg_dim, bg=theme.bg_input, attrs=ATTR_DIM),
            )
