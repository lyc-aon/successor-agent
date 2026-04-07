"""SuccessorChat — chat interface backed by a real local model.

The first chat-shaped piece of Successor. Wires together:

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
    │            successor · chat             │ title (1 row)
    ├─────────────────────────────────────┤
    │                                     │
    │  chat history scroll area           │ rows 1 .. N - 2 - input_h
    │  (newest at bottom)                 │
    │                                     │
    ├─────────────────────────────────────┤
    │ ▍ user input here                   │ input area (1+ rows)
    │   wraps upward as it grows          │
    ├─────────────────────────────────────┤
    │ ctx 1234/262144 ████░ 0.5%  local   │ static footer (1 row)
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

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import load_chat_config, save_chat_config
from .input.keys import (
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
from .profiles import (
    PROFILE_REGISTRY,
    Profile,
    all_profiles,
    get_active_profile,
    get_profile,
    next_profile,
    set_active_profile,
)
from .providers import make_provider
from .providers.llama import (
    ChatStream,
    ContentChunk,
    LlamaCppClient,
    ReasoningChunk,
    StreamEnded,
    StreamError,
    StreamStarted,
)
from .render.app import App
from .render.cells import (
    ATTR_BOLD,
    ATTR_DIM,
    ATTR_ITALIC,
    ATTR_REVERSE,
    ATTR_STRIKE,
    ATTR_UNDERLINE,
    Cell,
    Grid,
    Style,
)
from .agent import (
    BudgetTracker,
    CompactionError,
    ContextBudget,
    LogMessage,
    MessageLog,
    TokenCounter,
    compact as agent_compact,
)
from .agent.bash_stream import BashStreamDetector
from .bash import (
    BashConfig,
    DangerousCommandRefused,
    MutatingCommandRefused,
    RefusedCommand,
    ToolCard,
    dispatch_bash,
    measure_tool_card_height,
    paint_tool_card,
    preview_bash,
    resolve_bash_config,
)
from .bash.prepared_output import PreparedToolOutput
from .bash.render import (
    measure_tool_card_running_height,
    paint_tool_card_running,
)
from .bash.runner import (
    BashRunner,
    OutputLine as RunnerOutputLine,
    RunnerCompleted,
    RunnerErrored,
    RunnerStarted,
)
from .tools_registry import (
    AVAILABLE_TOOLS,
    build_system_prompt_tools_section,
    default_enabled_tools,
    filter_known,
)
from .render.markdown import (
    LaidOutLine,
    LaidOutSpan,
    PreparedMarkdown,
)
from .render.paint import fill_region, paint_box, paint_horizontal_divider, paint_text
from .render.terminal import Terminal
from .render.text import PreparedText, ease_out_cubic, hard_wrap, lerp_rgb
from .render.theme import (
    THEME_REGISTRY,
    Theme,
    ThemeVariant,
    all_themes,
    blend_variants,
    find_theme_or_fallback,
    get_theme,
    next_theme,
    normalize_display_mode,
    toggle_display_mode,
)


# Theme transition duration — how long it takes to lerp between themes
# when the user presses Ctrl+T or runs /theme. The renderer doesn't
# care about animation cost; this is just a visual touch that shows
# the entire UI smoothly fading from one palette to another.
THEME_TRANSITION_S = 0.4


# ─── Density (the "font size" widget) ───
#
# Terminal apps can't change the actual font in any portable way (the
# terminal owns the font). What we CAN control is how Successor uses cells:
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

# How long the help overlay takes to fade in.
HELP_FADE_IN_S = 0.18

# How many chars of reasoning to show in the live preview lane below
# the thinking spinner. Trimmed to fit the body width and clamped to
# the most-recent text.
_REASONING_PREVIEW_CHARS = 80

# ─── Help content ───
#
# Tuples of (key combination, description). Grouped sections render as
# clusters in the overlay. Adding a new keybinding here makes it appear
# automatically in the help screen — keep this in sync as features land.

_HELP_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("editing", (
        ("Enter",       "submit message"),
        ("Backspace",   "delete previous character"),
        ("Ctrl+C",      "quit successor"),
        ("Ctrl+G",      "interrupt streaming reply"),
    )),
    ("scroll", (
        ("↑ ↓",          "scroll one line"),
        ("PgUp PgDn",   "scroll one page"),
        ("Home End",    "jump to top / bottom"),
        ("Ctrl+B Ctrl+F", "vim-style page up / down"),
        ("Ctrl+P Ctrl+N", "vim-style line up / down"),
        ("Ctrl+E Ctrl+Y", "vim-style end / top"),
    )),
    ("look & feel", (
        ("Ctrl+,",      "open profile config menu"),
        ("Ctrl+P",      "cycle active profile"),
        ("Ctrl+T",      "cycle color theme"),
        ("Alt+D",       "toggle display mode (dark / light)"),
        ("Ctrl+]",      "cycle density (compact / normal / spacious)"),
        ("Alt+= Alt+-", "step density up / down"),
    )),
    ("slash commands", (
        ("/",           "open the command palette"),
        ("↑ ↓",          "navigate suggestions"),
        ("Tab",         "accept highlighted suggestion"),
        ("Enter",       "accept and submit"),
        ("Esc",         "dismiss the dropdown"),
    )),
    ("misc", (
        ("?",           "show this help overlay"),
        ("Esc / any",   "dismiss the help overlay"),
    )),
)


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
    action: str  # "theme" | "mode" | "density" | "profile" | "scroll_to_bottom"

    def contains(self, col: int, row: int) -> bool:
        return (
            self.x <= col < self.x + self.w
            and self.y <= row < self.y + self.h
        )


# How many lines to scroll per wheel notch. 3 lines is the conventional
# value (matches xterm and most terminal scroll-rate defaults).
WHEEL_SCROLL_LINES = 3


# Hard cap on agent-loop turns within a single user submission. Each
# user message kicks off turn 1; if the model emits bash blocks, the
# harness executes them and calls back into the model for turn 2, then
# 3, etc. Stops when the model returns no bash blocks (text-only) OR
# when this cap is hit, whichever comes first. 25 is generous enough
# for realistic multi-step tasks (scaffold → install → verify ≈ 5-8
# turns) and low enough to catch pathological loops in seconds.
MAX_AGENT_TURNS = 25


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


# OpenAI-style tool schema for the bash tool. Sent as the `tools`
# parameter in stream_chat() requests so Qwen 3.5's chat template
# fires its `if tools` branch — that branch injects the canonical
# "use <tool_call>" instructions into the system message and puts
# the model in its trained tool-calling mode. The schema is plain
# JSON so it round-trips cleanly through the chat completions API.
_BASH_TOOL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Execute a shell command (or multi-line script / heredoc) "
            "in the user's working directory and return its stdout, "
            "stderr, and exit code. Successful commands may produce "
            "no stdout — that is normal for writes, redirects, mkdir, "
            "touch, chmod, and most mutating commands."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "The bash command to execute. Pipes, "
                        "redirects, substitutions, heredocs, and "
                        "multi-step scripts all work because the "
                        "harness runs the string with shell=True."
                    ),
                },
            },
            "required": ["command"],
        },
    },
}


def _infer_tool_preview(
    command_text: str,
) -> tuple[str, str, str] | None:
    """Parse a (partial) bash command via `preview_bash` and return a
    (glyph, verb_name, hint) triple for the streaming preview header.

    Returns None when the command is too short, too malformed, or
    the parser falls back to the generic "bash ?" card with
    confidence < 0.7. The threshold is deliberately strict so the
    preview verb only resolves once the parser is reasonably sure
    — before that the user sees the generic "receiving arguments"
    header, avoiding flicker on partial input.

    The beauty of this function: it calls the SAME preview_bash that
    the final card uses, so the inferred verb+params EXACTLY match
    what the settled card will display. As the command streams in,
    the preview resolves in stages:

      "cat"                → generic (conf 0.60, too low)
      "cat > ab"           → write-file, path=ab   (conf 0.90)
      "cat > about.html"   → write-file, path=about.html

    Risk escalation is preserved: `rm -rf /` shows as DANGER (⚠)
    even mid-stream because the risk classifier runs on each
    parse. This is useful — the user sees the danger class
    resolve immediately, not after the dispatch gate fires.
    """
    if not command_text or len(command_text.strip()) < 3:
        return None
    try:
        from .bash import preview_bash
        from .bash.verbclass import glyph_for_class, verb_class_for
        card = preview_bash(command_text)
    except Exception:
        return None
    if card.confidence < 0.7:
        return None
    cls = verb_class_for(card.verb, card.risk)
    glyph = glyph_for_class(cls)
    # Pull the most informative parameter for the hint — usually
    # the first one (parsers put `path`, `script`, `pattern` first).
    hint = ""
    for key, value in card.params:
        if not value or value == "(missing)":
            continue
        # Clip the hint so multi-line heredoc content or very long
        # paths don't blow out the preview row width.
        text = str(value).replace("\n", " ").strip()
        if len(text) > 50:
            text = text[:47] + "…"
        hint = f"{key}: {text}"
        break
    return (glyph, card.verb, hint)


def _extract_command_tail(raw_args: str) -> str:
    """Extract the progressively-streaming `command` field from a
    partial tool_call arguments JSON blob, returning the unescaped
    command body so the user sees readable heredoc content instead
    of `\\n` escapes.

    Input: a partial JSON string like:
        '{"command":"cat > foo.html <<\\'EOF\\'\\n<!DOCTYPE html>\\n<h'
    Output:
        'cat > foo.html <<\'EOF\'\n<!DOCTYPE html>\n<h'

    Best-effort — falls back to the raw text when the JSON is too
    malformed to find the opening `"command":"` marker, so the
    preview always shows SOMETHING rather than blanking on parse
    failure. Doesn't need to produce a syntactically valid result
    because this is a display-only preview.
    """
    if not raw_args:
        return ""
    # Look for the start of the command field's string value. Accept
    # variants with or without whitespace around the colon.
    for key_marker in ('"command":"', '"command": "'):
        idx = raw_args.find(key_marker)
        if idx != -1:
            body = raw_args[idx + len(key_marker):]
            # Progressive JSON unescape. We bail on unknown escapes
            # rather than raising — the stream is still arriving
            # and a partial escape is normal.
            out: list[str] = []
            i = 0
            while i < len(body):
                ch = body[i]
                if ch == '"':
                    # End of the string value — stop here
                    break
                if ch == "\\" and i + 1 < len(body):
                    nxt = body[i + 1]
                    if nxt == "n":
                        out.append("\n"); i += 2; continue
                    if nxt == "t":
                        out.append("\t"); i += 2; continue
                    if nxt == "r":
                        out.append("\r"); i += 2; continue
                    if nxt == '"':
                        out.append('"'); i += 2; continue
                    if nxt == "\\":
                        out.append("\\"); i += 2; continue
                    if nxt == "/":
                        out.append("/"); i += 2; continue
                    # Unknown escape — just emit the backslash and move on
                    out.append("\\"); i += 1; continue
                out.append(ch)
                i += 1
            return "".join(out)
    # Couldn't find the marker — show the raw JSON so the user at
    # least sees progress. Better than a blank preview.
    return raw_args


def _assistant_with_tool_calls(content: str, cards: list[ToolCard]) -> dict:
    """Build the assistant-message dict for an assistant turn that
    issued one or more tool calls.

    Each card becomes one entry in the `tool_calls` list with its
    `tool_call_id` as the call id, `bash` as the function name, and
    the raw command serialized as a JSON `{"command": ...}` string
    in `arguments`. This matches the OpenAI tool-call shape that
    Qwen's chat template renders into native `<tool_call>` blocks.
    """
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": card.tool_call_id,
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": json.dumps({"command": card.raw_command}),
                },
            }
            for card in cards
        ],
    }


def _tool_card_content_for_api(card: ToolCard) -> str:
    """Build the message content for a ToolCard going back to the model.

    Sent as a `role: "tool"` message. Qwen 3.5's chat template renders
    role=tool as `<|im_start|>user\\n<tool_response>\\n…\\n</tool_response>`,
    which matches the format Qwen was trained on for tool use. The model
    natively recognizes empty content + role=tool as "command ran with
    no output" — exactly what writes / mkdir / chmod / redirects produce
    on success — and does NOT re-run.

    Earlier iterations wrapped the content in `<tool-output name="bash" …>`
    XML inside a user-role message. The model has never seen that format
    in training, treated it as random user text, and looped on writes
    because it couldn't tell success from failure. The fix is structural,
    not prompted: use the role the chat template understands.

    Content shape (mirrors free-code's pattern at BashTool.tsx:617-622):
      - successful command (exit 0):       stdout (or empty string)
      - failed command   (exit non-zero):  stdout + stderr + exit marker
      - command with stderr but exit 0:    stdout + stderr (no marker)

    No exit-code prefix for success. The empty-content / role=tool
    pairing is the success signal — adding prose dilutes it.
    """
    parts: list[str] = []
    if card.output:
        parts.append(card.output.rstrip())
    if card.stderr and card.stderr.strip():
        parts.append(card.stderr.rstrip())
    if card.exit_code is not None and card.exit_code != 0:
        parts.append(f"[command exited with code {card.exit_code}]")
    return "\n".join(parts)


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


def _theme_arg_completer(partial: str) -> list[str]:
    """Dynamic completer for /theme args.

    Pulls names live from THEME_REGISTRY so newly-added user theme
    files show up in autocomplete the next time the user opens the
    dropdown — no chat restart needed. The "cycle" pseudo-arg always
    appears at the end of the list.
    """
    p = partial.lower()
    options = sorted(THEME_REGISTRY.names()) + ["cycle"]
    return [o for o in options if o.startswith(p)]


def _profile_arg_completer(partial: str) -> list[str]:
    """Dynamic completer for /profile args.

    Pulls names live from PROFILE_REGISTRY so newly-added user profile
    files show up in autocomplete the next time the dropdown opens.
    """
    p = partial.lower()
    options = sorted(PROFILE_REGISTRY.names()) + ["cycle"]
    return [o for o in options if o.startswith(p)]


SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        name="quit",
        aliases=("q", "exit"),
        description="leave the chat",
    ),
    SlashCommand(
        name="bash",
        description="run a bash command and render it as a structured tool card",
        args_hint="<command>",
    ),
    SlashCommand(
        name="budget",
        description="show current context fill % + token usage stats",
    ),
    SlashCommand(
        name="burn",
        description="inject N synthetic tokens to stress-test compaction",
        args_hint="<N|loop N>",
    ),
    SlashCommand(
        name="compact",
        description="manually trigger compaction of the current chat history",
    ),
    SlashCommand(
        name="config",
        description="open the profile config menu",
    ),
    SlashCommand(
        name="profile",
        description="switch active profile (theme + prompt + provider)",
        args_hint="[<profile>|cycle]",
        complete_args=_profile_arg_completer,
    ),
    SlashCommand(
        name="theme",
        description="switch color theme",
        args_hint="[<theme>|cycle]",
        complete_args=_theme_arg_completer,
    ),
    SlashCommand(
        name="mode",
        description="switch display mode",
        args_hint="[dark|light|toggle]",
        complete_args=static_args("dark", "light", "toggle"),
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

# Default context-window denominator. Mid-grade models running on
# llama.cpp typically expose 32K-256K windows; the harness assumes
# generous budgets because local inference is free. Profiles can
# override this via provider.context_window.
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

SYSTEM_PROMPT = """You are successor — a thoughtful, intentional assistant. Speak with brevity, as if every word costs effort. Reply in a single flowing paragraph.

Do not use markdown headers. Do not use bullet lists or numbered lists. Do not write "Solution:", "Answer:", "Verification:", "Note:", or any preamble label. Do not use checkmarks. Do not wrap your reply in code fences unless the user asked for code.

Think as carefully as you need. When you have finished thinking, simply give your answer as if speaking aloud. Brevity is honor. When you must convey several things, weave them into one paragraph rather than enumerating them."""


# ─── Conversation model ───


class _Message:
    """A user or successor message in the conversation buffer.

    body is a PreparedMarkdown that parses the content ONCE and then
    lays out at any width on demand with caching. The prefix
    ('you ▸ ' / 'successor ▸ ') is rendered separately at paint time so
    it can use a different style than the body and so the markdown
    parser doesn't see prefix characters in its source.

    raw_text is the original content (without prefix) — what we send
    to the model in the conversation history.

    tool_card, when non-None, marks this message as a structured bash
    action card instead of a markdown body. The chat painter detects
    this and renders the message via paint_tool_card instead of the
    normal span flow. Tool messages are NEVER sent to the model in
    the conversation history (synthetic-by-construction).

    is_boundary, when True, marks this as a compaction boundary. The
    chat painter renders a horizontal divider with a central pill
    showing the compaction stats from boundary_meta. Always synthetic.

    is_summary, when True, marks this as a compaction summary message.
    The chat painter applies a special "summary" treatment (dim, italic,
    indented) so it visually distinguishes from a real assistant turn.
    Always synthetic.
    """

    __slots__ = (
        "role", "raw_text", "_display_text", "body", "created_at",
        "synthetic", "tool_card", "running_tool",
        "is_boundary", "is_summary", "boundary_meta",
        "_token_count",
        "_prepared_tool_output",
        "_card_rows_cache_key", "_card_rows_cache",
    )

    def __init__(
        self,
        role: str,
        content: str,
        *,
        synthetic: bool = False,
        tool_card: ToolCard | None = None,
        running_tool: object | None = None,
        is_boundary: bool = False,
        is_summary: bool = False,
        boundary_meta: object | None = None,
        display_text: str | None = None,
    ) -> None:
        self.role = role  # "user" | "successor" | "tool"
        # raw_text is the canonical body — what gets sent to the model
        # in API history. It must include any fenced bash blocks the
        # model emitted, otherwise the model can't see what commands
        # it ran when reading its own context.
        self.raw_text = content
        # display_text is what the chat renderer paints. For ordinary
        # messages it's the same as raw_text; for assistant messages
        # that contained fenced bash blocks, the chat passes a cleaned
        # variant (block-elided) so the user sees the surrounding
        # narrative without a duplicate of the tool card's content.
        # Tool cards render the bash separately as a structured card.
        self._display_text = display_text if display_text is not None else content
        self.body = PreparedMarkdown(self._display_text)
        self.created_at = time.monotonic()
        # Synthetic messages (the greeting, error notices) are NOT sent
        # to the model in the conversation history. Tool cards, boundary
        # markers, and summary messages are all forced synthetic.
        self.synthetic = synthetic or (tool_card is not None) or is_boundary or is_summary
        self.tool_card = tool_card
        # In-flight BashRunner companion. While set, the chat's
        # _pump_running_tools() polls the runner's queue each tick
        # and the renderer paints `tool_card` as the LIVE preview
        # via paint_tool_card_running. When the runner completes,
        # the chat replaces tool_card with the final enriched card
        # and clears running_tool. None for static cards (legacy
        # synchronous dispatch path, /bash slash command after
        # completion, refused cards, etc.).
        self.running_tool = running_tool
        self.is_boundary = is_boundary
        self.is_summary = is_summary
        # The BoundaryMarker dataclass from agent.log, holding pre/post
        # token counts + reduction_pct + summary_text. The painter reads
        # these to render the divider's central pill.
        self.boundary_meta = boundary_meta
        # Lazy per-message token count cache. Computed on first access
        # via the chat's TokenCounter and remembered. Invariant for the
        # message's lifetime because raw_text is set at construction
        # and never mutated. None = not yet computed.
        self._token_count: int | None = None
        # Pretext-shaped PreparedToolOutput, built once per tool-card
        # message on first paint and reused across frames. The output
        # is immutable (card is frozen) so this cache never invalidates.
        self._prepared_tool_output = None  # PreparedToolOutput | None
        # Cache of the pre-painted card row list, keyed by
        # (body_width, theme_name, display_mode). On cache hit, the
        # renderer skips the entire sub-grid paint. Changing width
        # (resize) or switching theme/mode invalidates the cache.
        self._card_rows_cache_key: tuple | None = None
        self._card_rows_cache: list | None = None

    @property
    def display_text(self) -> str:
        """The text the renderer should paint. For ordinary messages
        this matches `raw_text`; for assistant messages that emitted
        fenced bash blocks, this is the cleaned (block-elided)
        variant so the user doesn't see a duplicate of the tool card.
        """
        return self._display_text


# Prefix strings shown at the start of every message.
_USER_PREFIX = "you ▸ "
_SUCCESSOR_PREFIX = "successor ▸ "
_PREFIX_W = len(_USER_PREFIX)  # both prefixes are 6 cells


@dataclass(slots=True)
class _RenderedRow:
    """A single row ready for the chat painter.

    leading_text:   characters at the very left edge — message prefix on
                    the first line, blank padding on continuation lines,
                    or a special leading mark like the blockquote bar.
    leading_attrs:  attribute bitmask for leading_text (ATTR_BOLD etc.)
    leading_color_kind: which theme slot to use for leading_text — one
                    of "fg", "fg_dim", "accent" — resolved at paint time.
    body_spans:     laid-out markdown spans for the body content
    base_color:     the message's base body color (resolved at build
                    time, may be lerped during fade-in)
    line_tag:       optional row treatment from the markdown layout
    body_indent:    cells of indent within the body region (after the
                    leading prefix), used by blockquotes and code blocks
    prepainted_cells: when non-empty, the painter copies these Cells
                    directly to the grid at body_x and skips the normal
                    span flow. Used by tool card messages where the
                    paint_tool_card primitive has already produced
                    fully-styled cells in a sub-grid.
    is_boundary:    when True, this row is a compaction boundary marker.
                    boundary_meta carries the BoundaryMarker for the
                    painter to render the divider + central pill.
    boundary_meta:  attached BoundaryMarker (from agent.log) used by the
                    painter when is_boundary is True.
    is_summary:     when True, this row is part of a compaction summary
                    message — painted with a dim/italic treatment.
    fade_alpha:     0.0 - 1.0 — when < 1.0, the painter blends the row's
                    text color toward bg by (1 - fade_alpha). Used by the
                    fold animation phase. Default 1.0 = fully visible.
    """
    leading_text: str = ""
    leading_attrs: int = 0
    leading_color_kind: str = "accent"  # "fg" | "fg_dim" | "accent"
    body_spans: tuple = ()  # tuple of LaidOutSpan
    base_color: int = 0
    line_tag: str = ""
    body_indent: int = 0
    prepainted_cells: tuple = ()  # tuple of Cell — pre-rendered tool card row
    is_boundary: bool = False
    boundary_meta: object | None = None
    materialize_t: float = 1.0  # for boundary rows: 0-1 draw-in progress
    is_summary: bool = False
    fade_alpha: float = 1.0


# ─── Compaction animation ───
#
# When /compact fires (or autocompact triggers in the future), the chat
# enters a 6-phase animation sequence that's the harness's signature
# visual moment. The phases overlap to create a seamless narrative arc:
#
#   T=0       compaction starts → snapshot pre-compact messages, spawn worker
#   T=0-300   ANTICIPATION : pre-compact rounds get a subtle glow
#   T=300-1500 FOLD        : pre-compact rounds fade fg → bg via lerp_rgb
#   T=1500-?? WAITING      : indefinite — model is generating the summary.
#                            Spinner + "compacting N rounds" indicator visible.
#                            The chat painter routes through self.messages
#                            (post-snapshot) but with everything dimmed.
#   T=R-R+400 MATERIALIZE  : (R = result_arrived_at) divider draws in
#                            from center outward
#   T=R+400-R+1000 REVEAL  : summary message fades in from bg → fg_dim
#   T=R+1000-R+3500 TOAST  : settled state with subtle pulse
#
# Total: ~3.5 seconds + however long the model takes to summarize.
# At 256K context that's ~5 minutes of WAITING before materialize starts.

_COMPACT_ANTICIPATION_S = 0.30
_COMPACT_FOLD_S = 1.20         # 300ms → 1500ms
_COMPACT_MATERIALIZE_S = 0.40
_COMPACT_REVEAL_S = 0.60
_COMPACT_TOAST_HOLD_S = 2.50

_COMPACT_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass(slots=True)
class _CompactionAnimation:
    """In-progress compaction animation state.

    Held by the chat between /compact (or autocompact firing) and the
    end of the toast hold. The painter checks this on every frame and
    overlays the appropriate per-phase treatment on the chat region.

    Two-stage timing model: phases anticipation/fold play immediately
    on a fixed schedule. After fold ends, we enter WAITING which has
    indefinite duration. The worker thread sets `result_arrived_at`
    when the summary lands, and the materialize/reveal/toast phases
    play relative to that timestamp.

    Fields:
      started_at:        time.monotonic() when compaction was triggered
      pre_compact_snapshot: the chat's _Message list captured BEFORE
                         the compaction was applied. Used by the FOLD
                         phase as the visual content to fade out.
      pre_compact_count: how many messages were in the snapshot
      boundary:          the BoundaryMarker — None during waiting,
                         set by the worker callback when ready
      summary_text:      the summary text — empty during waiting,
                         set by the worker callback
      reason:            "manual" | "auto" | "reactive"
      result_arrived_at: monotonic time when the worker reported
                         success. None while waiting. Once set, the
                         materialize/reveal/toast phases run relative
                         to this anchor (not started_at) so the wait
                         duration doesn't compress the visible animation.
      pre_compact_tokens / rounds_summarized: pre-known stats so the
                         spinner indicator can show "compacting N rounds
                         (X tokens)" before the boundary lands.
    """
    started_at: float
    pre_compact_snapshot: list  # list of _Message — captured before swap
    pre_compact_count: int
    boundary: object | None  # BoundaryMarker, None until result arrives
    summary_text: str
    reason: str = "manual"
    result_arrived_at: float | None = None
    pre_compact_tokens: int = 0
    rounds_summarized: int = 0

    def phase_at(self, now: float) -> tuple[str, float]:
        """Return (phase_name, t) where t is 0-1 progress within the phase.

        For the WAITING phase, t is the wall time elapsed in waiting
        (in seconds, not normalized) so the painter can drive a
        spinner from it.
        """
        elapsed = now - self.started_at
        if elapsed < 0:
            return ("pending", 0.0)
        anticipation_end = _COMPACT_ANTICIPATION_S
        fold_end = anticipation_end + _COMPACT_FOLD_S

        if elapsed < anticipation_end:
            return ("anticipation", elapsed / _COMPACT_ANTICIPATION_S)
        if elapsed < fold_end:
            return ("fold", (elapsed - anticipation_end) / _COMPACT_FOLD_S)

        # After fold ends we wait for the worker
        if self.result_arrived_at is None:
            wait_elapsed = elapsed - fold_end
            return ("waiting", wait_elapsed)

        # Result has arrived — phases play relative to result_arrived_at
        post_arrival = now - self.result_arrived_at
        materialize_end = _COMPACT_MATERIALIZE_S
        reveal_end = materialize_end + _COMPACT_REVEAL_S
        settled_end = reveal_end + _COMPACT_TOAST_HOLD_S

        if post_arrival < materialize_end:
            return ("materialize", post_arrival / _COMPACT_MATERIALIZE_S)
        if post_arrival < reveal_end:
            return ("reveal", (post_arrival - materialize_end) / _COMPACT_REVEAL_S)
        if post_arrival < settled_end:
            return ("toast", (post_arrival - reveal_end) / _COMPACT_TOAST_HOLD_S)
        return ("done", 1.0)

    def is_done(self, now: float) -> bool:
        return self.phase_at(now)[0] == "done"

    def is_waiting(self, now: float) -> bool:
        return self.phase_at(now)[0] == "waiting"

    def spinner_frame(self, now: float) -> str:
        """Return the current spinner glyph (animates at ~10 Hz)."""
        idx = int(now * 10) % len(_COMPACT_SPINNER_FRAMES)
        return _COMPACT_SPINNER_FRAMES[idx]


# ─── Compaction worker thread ───
#
# Wraps a background thread that runs compact() against the live client.
# Mirrors the ChatStream pattern: create + start, poll for result on
# every tick, close to abort. The worker is the only thing that calls
# blocking HTTP from the chat path; everything else stays interactive.


@dataclass(slots=True)
class _CompactionResult:
    """The output of a compaction worker thread."""
    new_log: object | None  # MessageLog — None on error
    boundary: object | None  # BoundaryMarker — None on error
    error: str | None  # error message, None on success


class _CacheWarmer:
    """Worker thread that pre-warms llama.cpp's KV cache for the
    post-compact prefix.

    After compaction completes, the chat's `self.messages` becomes
    [boundary][summary][last_N_kept_rounds]. The next user message
    will send `[sys][boundary][summary][...kept...][new_user]`.

    llama.cpp's KV cache currently holds the OLD chat structure
    plus the compaction summarization request — none of which
    matches the new prefix. The next user message would pay a full
    ~40s cache miss to evaluate the post-compact prefix from scratch.

    The warmer fires a `max_tokens=1` chat completion against the
    post-compact prefix. The prompt eval populates the cache; the
    1-token generation is essentially free. After warming completes,
    the next REAL user message hits a warm cache and prompt eval is
    near-instant.

    Cancellable via close():
      1. Sets the worker's stop event
      2. Closes the underlying ChatStream so the worker thread
         unblocks from its drain loop
      3. The warmer thread exits without storing a result

    The warmer is a TRANSPARENT optimization — it has no observable
    effect on chat behavior beyond making the next message faster.
    Failure modes are silent (we just don't get the speedup).

    Why max_tokens=1: the smallest legal generation. We don't care
    about the generated content — we only want the model to evaluate
    the prompt and populate the cache. One token of generation costs
    ~20ms which is negligible.
    """

    __slots__ = (
        "_messages", "_client",
        "_thread", "_stream", "_stop", "_done",
        "_started_at", "_ended_at",
    )

    def __init__(
        self,
        *,
        messages: list[dict],
        client,  # CompactionClient / LlamaCppClient
    ) -> None:
        self._messages = messages
        self._client = client
        self._thread = None  # threading.Thread
        self._stream = None  # ChatStream — held so close() can abort it
        self._stop = None  # threading.Event
        self._done = False
        self._started_at: float = 0.0
        self._ended_at: float = 0.0

    def start(self) -> None:
        """Spawn the warmer thread. Returns immediately."""
        import threading
        self._stop = threading.Event()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name="successor-cache-warmer",
        )
        self._thread.start()

    def is_done(self) -> bool:
        return self._done

    def is_running(self) -> bool:
        return self._thread is not None and not self._done

    def elapsed(self) -> float:
        end = self._ended_at if self._ended_at else time.monotonic()
        return end - self._started_at if self._started_at else 0.0

    def close(self) -> None:
        """Signal the warmer to stop ASAP. Discards any pending result."""
        if self._stop is not None:
            self._stop.set()
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass

    def _run(self) -> None:
        """The worker body — opens a stream, drains it to completion,
        sets _done. Catches all exceptions silently because warming
        is a transparent optimization that should never crash the chat."""
        from .providers.llama import (
            ContentChunk, ReasoningChunk,
            StreamEnded, StreamError, StreamStarted,
        )
        try:
            self._stream = self._client.stream_chat(
                self._messages,
                max_tokens=1,        # smallest legal generation
                temperature=0.0,
            )
            # Drain to completion (or until stop event)
            deadline = time.monotonic() + 1800.0  # 30 min hard cap
            while time.monotonic() < deadline:
                if self._stop is not None and self._stop.is_set():
                    return  # canceled
                events = self._stream.drain()
                done = False
                for ev in events:
                    if isinstance(ev, (StreamEnded, StreamError)):
                        done = True
                        break
                if done:
                    break
                time.sleep(0.05)
        except Exception:
            # Warming is best-effort — silent failure
            pass
        finally:
            self._ended_at = time.monotonic()
            self._done = True


class _CompactionWorker:
    """Worker thread that runs compact() in the background.

    Construction: pass the agent log, client, counter, and reason.
    Start the thread with start(). Poll for result with poll() — it
    returns None until the worker is done, then a _CompactionResult.
    Abort with close() (sets a stop event; the worker may still
    block on HTTP for the current request, but the result will be
    discarded).
    """

    __slots__ = (
        "_log", "_client", "_counter", "_reason",
        "_thread", "_result", "_stop", "_started_at", "_done_at",
    )

    def __init__(
        self,
        *,
        log,           # agent.MessageLog
        client,        # CompactionClient
        counter,       # TokenCounter
        reason: str = "manual",
    ) -> None:
        self._log = log
        self._client = client
        self._counter = counter
        self._reason = reason
        self._thread: object | None = None  # threading.Thread
        self._result: _CompactionResult | None = None
        self._stop = None  # threading.Event — set in start()
        self._started_at: float = 0.0
        self._done_at: float = 0.0

    def start(self) -> None:
        """Spawn the worker thread."""
        import threading
        self._stop = threading.Event()
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, daemon=True,
            name="successor-compaction-worker",
        )
        self._thread.start()

    def poll(self) -> _CompactionResult | None:
        """Return the result if the worker is done, else None.
        Non-blocking — safe to call from on_tick every frame."""
        return self._result

    def is_running(self) -> bool:
        return self._thread is not None and self._result is None

    def elapsed(self) -> float:
        end = self._done_at if self._done_at else time.monotonic()
        return end - self._started_at if self._started_at else 0.0

    def close(self) -> None:
        """Signal the worker to stop ASAP. The HTTP call may still
        complete; we just discard the result."""
        if self._stop is not None:
            self._stop.set()

    def _run(self) -> None:
        from .agent.compact import CompactionError, compact
        try:
            new_log, boundary = compact(
                self._log, self._client,
                counter=self._counter,
                reason=self._reason,
            )
            if self._stop is not None and self._stop.is_set():
                # Aborted while we were running. Don't store the result.
                return
            self._result = _CompactionResult(
                new_log=new_log, boundary=boundary, error=None,
            )
        except (CompactionError, ValueError) as exc:
            self._result = _CompactionResult(
                new_log=None, boundary=None, error=str(exc),
            )
        except Exception as exc:
            self._result = _CompactionResult(
                new_log=None, boundary=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            self._done_at = time.monotonic()


# ─── The chat App ───


class SuccessorChat(App):
    def __init__(
        self,
        *,
        client: LlamaCppClient | None = None,
        theme: Theme | None = None,
        display_mode: str | None = None,
        profile: Profile | None = None,
        terminal: Terminal | None = None,
        recorder=None,
    ) -> None:
        super().__init__(
            target_fps=30.0,
            quit_keys=b"\x03",  # Ctrl+C only — q must remain typeable
            terminal=terminal if terminal is not None else Terminal(bracketed_paste=True),
        )
        # Optional recorder — captures every input byte to a JSONL file.
        # Set via the `successor record <file>` subcommand. None for normal use.
        self._recorder = recorder

        # ─── Persisted preferences ───
        # Loaded from ~/.config/successor/chat.json on startup. Saved on
        # every change to theme/display_mode/density/mouse so the user's
        # choices survive between `successor chat` invocations. The migration
        # from the v1 schema (where dark/light/forge were flat themes)
        # happens transparently inside load_chat_config.
        self._config = load_chat_config()

        # ─── Active profile ───
        # If the caller didn't pass an explicit profile, resolve the
        # active one from config (which falls back to "default" → first
        # registered → hardcoded fallback). The profile supplies
        # defaults for theme/mode/density/system_prompt/provider; saved
        # config still wins per-setting so the user's manual changes
        # persist across restarts.
        if profile is None:
            profile = get_active_profile()
        self.profile: Profile = profile

        # ─── Provider/client state ───
        # Resolution: explicit `client` arg > profile.provider > default
        # LlamaCppClient. The factory constructs from the profile's
        # provider config dict; missing/empty config falls back to the
        # default LlamaCppClient construction.
        if client is not None:
            self.client = client
        elif profile.provider:
            try:
                self.client = make_provider(profile.provider)
            except Exception:
                # A bad provider config in a profile shouldn't prevent
                # the chat from starting. Fall back and let the user
                # see "forge is cold" with the default URL.
                self.client = LlamaCppClient()
        else:
            self.client = LlamaCppClient()

        # ─── System prompt ───
        # Comes from the profile. Used in _submit when building the
        # api_messages payload. Profiles can ship their own system
        # prompts so a "successor-dev" persona behaves differently from
        # "default" without changing any code.
        self.system_prompt: str = profile.system_prompt

        # ─── Theme state ───
        # Resolution per setting: explicit constructor arg → saved
        # config → profile field → fallback. Saved config wins over
        # the profile so the user's manual Ctrl+T cycling persists,
        # even though the profile defines a default theme.
        if theme is None:
            saved_name = self._config.get("theme")
            if not isinstance(saved_name, str) or not saved_name:
                saved_name = profile.theme
            theme = find_theme_or_fallback(saved_name)
        self.theme: Theme = theme
        self._theme_from: Theme | None = None
        self._theme_t0: float = 0.0

        # ─── Display mode state ───
        if display_mode is None:
            saved_mode = self._config.get("display_mode")
            if not isinstance(saved_mode, str) or not saved_mode:
                saved_mode = profile.display_mode
            display_mode = normalize_display_mode(saved_mode)
        self.display_mode: str = display_mode
        # Mode-only transitions get tracked separately so toggling
        # dark/light without changing the theme bundle still animates
        # smoothly. Set when mode changes; cleared when transition done.
        self._mode_from: str | None = None

        # ─── Density state ───
        # Same resolution chain as theme/display_mode.
        saved_density_name = self._config.get("density")
        if not isinstance(saved_density_name, str) or not saved_density_name:
            saved_density_name = profile.density
        initial_density = find_density(saved_density_name) or NORMAL
        self.density: Density = initial_density
        self._density_from: Density | None = None
        self._density_t0: float = 0.0

        # ─── Mouse state ───
        # Mouse reporting is opt-in via /mouse on. When enabled, the
        # title bar widgets become clickable and the scroll wheel works.
        # The trade-off: native click-drag selection requires holding
        # Shift while mouse reporting is on. Default is OFF so users
        # who never opt in keep their normal selection behavior.
        self._mouse_enabled: bool = bool(self._config.get("mouse", False))
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

        # ─── Help overlay state ───
        # When True, a centered modal listing all keybindings + slash
        # commands appears over the chat. Press '?' to open, any key
        # (including Esc) to dismiss. Fades in over HELP_FADE_IN_S.
        self._help_open: bool = False
        self._help_opened_at: float = 0.0

        # ─── Search state ───
        # When _search_active is True, the input area is replaced with
        # a search bar. As the user types a query, every match in the
        # past messages gets highlighted with a different bg color.
        # n / N (or Ctrl+N / Ctrl+P) jump between matches with smooth
        # animated scroll. Esc closes the search.
        self._search_active: bool = False
        self._search_query: str = ""
        # Cached matches: list of (message_idx, char_start, char_end)
        # tuples, ordered top-to-bottom in the conversation.
        self._search_matches: list[tuple[int, int, int]] = []
        # Index into _search_matches that's currently focused.
        self._search_focused: int = 0

        # If we just finished restoring mouse from config, push the
        # escape sequence to the terminal so reporting matches the flag.
        # (We do this AFTER super().__init__ so self.term exists.)
        if self._mouse_enabled:
            # Defer until __enter__ runs (see below)
            self.term.mouse_reporting = True

        # Probe the server immediately so we can show a useful greeting.
        server_up = self.client.health()
        if server_up:
            greeting = (
                f"I am successor. The forge is hot — {self.client.model} stands ready. "
                f"Speak freely. Ctrl+C, /quit, or `?` for help."
            )
        else:
            greeting = (
                f"I am successor. The forge is cold — no model answers at "
                f"{self.client.base_url}. Start llama.cpp and try again, "
                f"or read in silence."
            )
        self.messages: list[_Message] = [
            _Message("successor", greeting, synthetic=True),
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
        # Bash detector for the current stream — when bash is in
        # profile.tools, _submit creates one of these and _pump_stream
        # feeds ContentChunk text to it. After StreamEnded, we drain
        # the detector and dispatch each completed bash command,
        # appending tool cards to self.messages.
        self._stream_bash_detector: BashStreamDetector | None = None
        # Agent-loop turn counter for the continue-loop. _submit
        # resets this to 0 before kicking off a new user turn; each
        # _begin_agent_turn call increments. When a bash batch
        # finishes, _pump_stream calls _begin_agent_turn again so
        # the model can react to its own tool output. Hard-capped
        # at MAX_AGENT_TURNS to bound runaway loops.
        self._agent_turn: int = 0
        # In-flight BashRunner instances. Each entry is a _Message
        # whose `running_tool` field points at a BashRunner that
        # hasn't completed yet. on_tick polls this list every frame,
        # finalizes any runners that have completed, and (if the
        # batch came from an agent-loop turn) fires the continuation
        # stream when the last runner in the batch is done.
        self._running_tools: list[_Message] = []
        # When True, the most recently-completed agent stream queued
        # tool calls and the chat is waiting for them to finish before
        # opening the continuation stream. Set in _pump_stream's
        # StreamEnded handler whenever runners are spawned, cleared
        # in _pump_running_tools when continuation fires.
        self._pending_continuation: bool = False
        # Sticky cache of inferred verb previews for in-flight tool
        # calls, keyed by (stream_id, call_index). Once preview_bash
        # returns a high-confidence verb for a streaming tool call,
        # we remember it so a later partial that momentarily confuses
        # the parser (e.g., an unclosed quote mid-stream) doesn't
        # flicker the header back to the generic "receiving" message.
        # Reset when a stream starts or ends.
        self._streaming_verb_cache: dict[tuple[int, int], tuple[str, str, str]] = {}

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

        # ─── Pending action ───
        # When the user opens a sub-App from inside the chat (config
        # menu, future setup-edit, etc.), the slash/keybind handler
        # sets this flag and calls self.stop(). The cli.py main loop
        # checks the flag after run() returns to decide what to do
        # next. None means a normal exit.
        self._pending_action: str | None = None

        # Compaction animation state — None when no animation is in
        # progress. Armed by /compact when the worker spawns; the chat
        # painter checks it on every frame to drive the 6-phase
        # animation. Cleared in on_tick when the animation reaches "done".
        self._compaction_anim: _CompactionAnimation | None = None

        # Compaction worker thread — None when no compaction is running.
        # Set by /compact alongside _compaction_anim. on_tick polls it
        # every frame; when the worker reports a result, the chat
        # applies the new log and the animation transitions from
        # WAITING → MATERIALIZE.
        self._compaction_worker: _CompactionWorker | None = None

        # Cache pre-warming worker — fires after compaction completes
        # to populate llama.cpp's KV cache with the post-compact prefix.
        # Without this, the next user message after compaction pays a
        # ~40s cache miss. With it, the next message is near-instant.
        # Auto-canceled by _submit when the user types a real message.
        self._cache_warmer: _CacheWarmer | None = None

        # Cached TokenCounter for the agent log adapter — lazy-init
        # on first /budget or /compact so we don't pay the construction
        # cost for chats that never use the agent loop.
        self._cached_token_counter: TokenCounter | None = None

        # Chat-level cached total token count for the static footer.
        # Without this cache the footer walks every message via the
        # TokenCounter every frame — at 200K context that's 1 fps.
        # With it, the footer is O(1) in steady state.
        #
        # Cache key: (id(self.messages), len(self.messages))
        #   - id catches wholesale list reassignment (test fixtures
        #     replacing messages with a same-length list, /from_agent_log
        #     swapping post-compact, etc) because each new list gets a
        #     fresh CPython id
        #   - len catches in-place appends (the same list grows)
        # Together they cover every mutation pattern the chat does.
        # The only thing they MISS is in-place text edits to an existing
        # message at the same index, which the chat never does because
        # raw_text is set at construction and never mutated.
        self._cached_total_tokens: int | None = None
        self._cached_total_tokens_key: tuple[int, int] = (-1, -1)

    # ─── Input handling ───

    def on_key(self, byte: int) -> None:
        """Bytes from stdin → InputEvents → dispatched.

        The decoder may emit KeyEvent or MouseEvent depending on what
        the byte stream encodes. Mouse events only arrive when mouse
        reporting is enabled (via /mouse on).
        """
        # Record EVERY raw byte before decoding so the recording is a
        # faithful reproduction of the input stream — including escape
        # sequences, multi-byte UTF-8, and bracketed paste markers.
        if self._recorder is not None:
            try:
                self._recorder.record_byte(byte)
            except Exception:
                pass

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
        # When help overlay is open, any click dismisses it.
        if self._help_open and event.button == MouseButton.LEFT and event.pressed:
            self._help_open = False
            return

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
                elif hb.action == "mode":
                    self._toggle_display_mode()
                elif hb.action == "density":
                    self._cycle_density()
                elif hb.action == "profile":
                    self._cycle_profile()
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
        # ─── Help overlay dismiss ───
        # When the help overlay is open, ANY keypress dismisses it
        # (Esc, any letter, any arrow). The dismissed key is consumed —
        # we don't pass it through to the normal handlers.
        if self._help_open:
            self._help_open = False
            return

        # ─── Search mode (when active, intercepts most keys) ───
        if self._search_active:
            if event.key == Key.ESC:
                self._search_close()
                return
            if event.key == Key.ENTER:
                # Enter advances to the next match
                self._search_jump(+1)
                return
            if event.key == Key.UP or (event.is_ctrl and event.char == "p"):
                self._search_jump(-1)
                return
            if event.key == Key.DOWN or (event.is_ctrl and event.char == "n"):
                self._search_jump(+1)
                return
            if event.key == Key.BACKSPACE:
                if self._search_query:
                    self._search_query = self._search_query[:-1]
                    self._search_recompute()
                return
            if event.is_char and event.char and not event.is_ctrl:
                # Add to search query (printable + UTF-8)
                safe = "".join(c for c in event.char if ord(c) >= 0x20)
                if safe:
                    self._search_query += safe
                    self._search_recompute()
                return
            # Unknown key in search mode — swallow.
            return

        # ─── Bracketed paste boundaries ───
        if event.key == Key.PASTE_START:
            self._in_paste = True
            return
        if event.key == Key.PASTE_END:
            self._in_paste = False
            return

        # ─── Help open (?) ───
        # Only opens when the input buffer is empty so '?' inside a
        # message stays as a literal char.
        if event.is_char and event.char == "?" and not event.is_ctrl and not event.is_alt:
            if not self.input_buffer:
                self._help_open = True
                self._help_opened_at = time.monotonic()
                return

        # ─── Search open (Ctrl+F) ───
        if event.is_ctrl and event.char == "f":
            self._search_open()
            return

        # ─── Theme cycle (always available, even mid-stream) ───
        if event.is_ctrl and event.char == "t":
            self._cycle_theme()
            return

        # ─── Display mode toggle — Alt+D dark↔light, theme preserved ───
        # Ctrl+D is reserved for terminal EOT so we use Alt+D, which
        # also matches the alt-modifier convention used by density's
        # Alt+= / Alt+- step controls.
        if event.is_alt and event.char == "d":
            self._toggle_display_mode()
            return

        # ─── Open the config menu — Ctrl+, is the convention everywhere ───
        # (VS Code, Sublime, Cursor, JetBrains all use Ctrl+, for
        # "open settings"). Sets the pending action flag so the cli
        # main loop knows to open the config menu after the chat exits.
        if event.is_ctrl and event.char == ",":
            self._pending_action = "config"
            self.stop()
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

        # ─── Ctrl-prefix shortcuts ───
        if event.is_ctrl and not event.is_alt:
            # Profile cycling (Ctrl+P) — replaces the old vim "scroll
            # up one line" binding because Up arrow already does that
            # and profile switching is the more valuable shortcut now.
            if event.char == "p":
                self._cycle_profile()
                return
            # Vim-style page navigation kept for muscle memory.
            if event.char == "b":
                self._scroll_lines(self._page_size())
                return
            if event.char == "f":
                self._scroll_lines(-self._page_size())
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
                # Also cancel any in-flight bash runners — the user
                # is taking control back, the runners shouldn't keep
                # eating CPU/wall-time after the stream they belong
                # to is dead.
                self._cancel_running_tools()
                self._pending_continuation = False
                return
            # Ctrl+G to abort in-flight bash runners (no stream)
            if event.char == "g" and self._running_tools:
                self._cancel_running_tools()
                self._pending_continuation = False
                return
            # Ctrl+G to abort an in-flight compaction
            if event.char == "g" and self._compaction_worker is not None:
                self._compaction_worker.close()
                self._compaction_worker = None
                self._compaction_anim = None
                self.messages.append(_Message(
                    "successor",
                    "compaction cancelled.",
                    synthetic=True,
                ))
                return

        # ─── Streaming guard ───
        # While successor is responding, swallow editing/typing keys.
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
            # Normalize pasted content before filtering:
            #   \r\n / \r → \n  (Windows / classic-Mac line endings)
            #   \t       → 4 spaces (most pasted code is 4-space indent)
            # Then strip orphan focus-event tails ([I / [O) that some
            # terminals leak inside bracketed paste, and finally drop
            # any leftover control codes below 0x20.
            chunk = event.char.replace("\r\n", "\n").replace("\r", "\n")
            chunk = chunk.replace("\t", "    ")
            if self._in_paste and chunk.endswith(("\x1b[I", "\x1b[O")):
                chunk = chunk[:-3]
            safe = "".join(
                c for c in chunk
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

    # ─── Theme + display mode management ───

    def _set_theme(self, new_theme: Theme) -> None:
        """Switch to a new theme with a smooth lerp transition.

        Captures the CURRENT visible variant as `_theme_from` so the
        blend interpolates from whatever the user is actually seeing
        (which may itself be mid-transition) to the new target. The
        display_mode is preserved across theme switches.
        """
        if new_theme.name == self.theme.name:
            return
        # Snapshot what's on screen RIGHT NOW so the blend starts from
        # the actual visible state, not the logical previous theme.
        self._theme_from = self.theme
        self.theme = new_theme
        # Cancel any in-flight mode transition since the snapshot
        # already encodes the current mode-blend point.
        self._mode_from = None
        self._theme_t0 = time.monotonic()
        self._persist_preferences()

    def _cycle_theme(self) -> None:
        self._set_theme(next_theme(self.theme))

    def _set_display_mode(self, new_mode: str) -> None:
        """Switch dark↔light with a smooth lerp transition.

        Mode swaps preserve the current theme. The transition shares
        the same `_theme_t0` clock and duration as theme swaps so a
        rapid theme-then-mode switch (or vice versa) doesn't double up
        animations.
        """
        new_mode = normalize_display_mode(new_mode)
        if new_mode == self.display_mode:
            return
        self._mode_from = self.display_mode
        self.display_mode = new_mode
        # Use the same clock as theme transitions; if a theme transition
        # was already in flight, this resets it from "now" so both
        # axes finish together at a clean target.
        self._theme_t0 = time.monotonic()
        self._theme_from = None  # mode-only transition, no theme blend
        self._persist_preferences()

    def _toggle_display_mode(self) -> None:
        self._set_display_mode(toggle_display_mode(self.display_mode))

    # ─── Profile management ───

    def _set_profile(self, new_profile: Profile) -> None:
        """Switch the active profile, applying its settings atomically.

        A profile defines theme + display_mode + density + system_prompt
        + provider as a coherent persona unit. Switching:

          1. Replaces self.profile and persists active_profile to config
          2. Applies the new theme (with smooth transition)
          3. Applies the new display_mode (with smooth transition)
          4. Applies the new density (with smooth transition)
          5. Replaces system_prompt for the next /submit
          6. Reconstructs the provider client (next stream uses it)

        Active conversation history is preserved. The new system prompt
        and provider apply to the NEXT message the user submits, not
        retroactively to the current scrollback.
        """
        if new_profile.name == self.profile.name:
            return

        self.profile = new_profile

        # Apply theme — uses the existing transition machinery
        new_theme = find_theme_or_fallback(new_profile.theme)
        if new_theme.name != self.theme.name:
            self._set_theme(new_theme)

        # Apply display mode (separate transition axis)
        target_mode = normalize_display_mode(new_profile.display_mode)
        if target_mode != self.display_mode:
            self._set_display_mode(target_mode)

        # Apply density
        new_density = find_density(new_profile.density) or NORMAL
        if new_density.name != self.density.name:
            self._set_density(new_density)

        # System prompt + provider apply to the next submission. We
        # don't tear down an in-flight stream — it finishes on the
        # old provider, and the next user message starts on the new one.
        self.system_prompt = new_profile.system_prompt

        if new_profile.provider:
            try:
                self.client = make_provider(new_profile.provider)
            except Exception:
                # Bad provider config in a profile shouldn't break the
                # active session. Keep the old client; user can fix
                # the profile and try again.
                pass
        else:
            self.client = LlamaCppClient()

        # Persist the new active profile name to chat.json so the next
        # `successor chat` startup uses it. Failures are silent — chat.json
        # writes are best-effort.
        self._config["active_profile"] = new_profile.name
        save_chat_config(self._config)

        # Add a synthetic message announcing the swap so the user has
        # a clear breadcrumb in the scrollback. Reuses the existing
        # synthetic-message machinery (rendered dim, not sent to model).
        self.messages.append(
            _Message(
                "successor",
                f"switched to profile: {new_profile.name}"
                + (f" — {new_profile.description}" if new_profile.description else ""),
                synthetic=True,
            )
        )

    def _cycle_profile(self) -> None:
        """Cycle to the next profile in registry order."""
        target = next_profile(self.profile)
        self._set_profile(target)

    # ─── Density management ───

    def _set_density(self, new_density: Density) -> None:
        if new_density is self.density:
            return
        self._density_from = self._current_density()
        self.density = new_density
        self._density_t0 = time.monotonic()
        self._persist_preferences()

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

    # ─── Search ───

    def _search_open(self) -> None:
        """Enter search mode. The input area is replaced with a search bar."""
        self._search_active = True
        self._search_query = ""
        self._search_matches = []
        self._search_focused = 0

    def _search_close(self) -> None:
        """Exit search mode. The input area returns to its normal state."""
        self._search_active = False
        self._search_query = ""
        self._search_matches = []
        self._search_focused = 0

    def _search_recompute(self) -> None:
        """Re-scan all messages for the current query.

        Builds a flat list of (message_idx, char_start, char_end)
        match tuples in conversation order. Empty queries clear the
        match list. Reset the focused index to 0.
        """
        self._search_matches = []
        self._search_focused = 0
        if not self._search_query:
            return
        q = self._search_query.lower()
        if not q:
            return
        for msg_idx, msg in enumerate(self.messages):
            text = msg.raw_text.lower()
            start = 0
            while True:
                found = text.find(q, start)
                if found < 0:
                    break
                self._search_matches.append((msg_idx, found, found + len(q)))
                start = found + 1
        # Auto-jump to the LAST match (most recent context)
        if self._search_matches:
            self._search_focused = len(self._search_matches) - 1
            self._search_scroll_to_focused()

    def _search_jump(self, delta: int) -> None:
        """Move the focused match by delta and scroll to it."""
        if not self._search_matches:
            return
        n = len(self._search_matches)
        self._search_focused = (self._search_focused + delta) % n
        self._search_scroll_to_focused()

    def _search_scroll_to_focused(self) -> None:
        """Scroll the chat so the focused match is visible.

        Computes the line offset of the message containing the focused
        match and adjusts scroll_offset so that line lands somewhere
        in the visible chat area. Best-effort — uses the cached
        message heights from the last frame.
        """
        if not self._search_matches:
            return
        focused_msg_idx, _, _ = self._search_matches[self._search_focused]
        # Sum the lines from the focused message to the end of the
        # conversation. That gives us the offset-from-bottom needed to
        # land on the focused message's first line.
        body_width = max(1, self._last_chat_w)
        lines_from_focused_to_end = 0
        spacing = self._current_density().message_spacing
        for i in range(focused_msg_idx, len(self.messages)):
            msg = self.messages[i]
            md_height = msg.body.height(max(1, body_width - _PREFIX_W))
            if md_height == 0:
                md_height = 1  # empty body still has the prefix line
            lines_from_focused_to_end += md_height
            if i < len(self.messages) - 1:
                lines_from_focused_to_end += spacing
        # Position the focused message a few lines below the top of
        # the visible area for context.
        chat_h = max(1, self._last_chat_h)
        target_offset = max(0, lines_from_focused_to_end - chat_h + 4)
        max_off = self._max_scroll()
        target_offset = min(target_offset, max_off)
        self.scroll_offset = target_offset
        self._auto_scroll = (target_offset == 0)

    def _is_search_match(self, msg_idx: int, char_idx: int) -> int:
        """Return 0 if not a match, 1 if non-focused match, 2 if focused.

        Used by the message painter to apply highlight backgrounds to
        the cells that overlap a search match.
        """
        if not self._search_matches:
            return 0
        for i, (mi, start, end) in enumerate(self._search_matches):
            if mi == msg_idx and start <= char_idx < end:
                return 2 if i == self._search_focused else 1
        return 0

    # ─── Mouse mode toggle ───

    def _enable_mouse(self) -> None:
        if self._mouse_enabled:
            return
        self.term.set_mouse_reporting(True)
        self._mouse_enabled = True
        self._persist_preferences()

    def _disable_mouse(self) -> None:
        if not self._mouse_enabled:
            return
        self.term.set_mouse_reporting(False)
        self._mouse_enabled = False
        self._persist_preferences()

    def _persist_preferences(self) -> None:
        """Save user-toggleable preferences to ~/.config/successor/chat.json.

        Failures are silent — persistence is best-effort. The user keeps
        their session preferences even if the file write fails; they
        just won't carry over to the next launch. The schema version
        is stamped by save_chat_config so future loads skip migration.
        """
        self._config["theme"] = self.theme.name
        self._config["display_mode"] = self.display_mode
        self._config["density"] = self.density.name
        self._config["mouse"] = self._mouse_enabled
        save_chat_config(self._config)

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

    def _current_variant(self) -> ThemeVariant:
        """The ThemeVariant to use for THIS frame's render.

        Resolves the orthogonal (theme, display_mode) state into one
        concrete variant. If either axis is mid-transition, blends
        accordingly:

          - theme transition only: lerp current theme's variant from
            old-theme[mode] toward new-theme[mode]
          - mode transition only:  lerp current theme's variant from
            theme[old_mode] toward theme[new_mode]
          - neither: just return theme[mode]

        Theme + mode transitions can't be in flight simultaneously by
        construction (each setter clears the other's `_from` field),
        which keeps the blend math single-axis and easy to reason about.
        """
        target_variant = self.theme.variant(self.display_mode)

        # Theme transition in flight
        if self._theme_from is not None:
            elapsed = time.monotonic() - self._theme_t0
            if elapsed >= THEME_TRANSITION_S:
                self._theme_from = None
                return target_variant
            t = ease_out_cubic(elapsed / THEME_TRANSITION_S)
            from_variant = self._theme_from.variant(self.display_mode)
            return blend_variants(from_variant, target_variant, t)

        # Display-mode transition in flight
        if self._mode_from is not None:
            elapsed = time.monotonic() - self._theme_t0
            if elapsed >= THEME_TRANSITION_S:
                self._mode_from = None
                return target_variant
            t = ease_out_cubic(elapsed / THEME_TRANSITION_S)
            from_variant = self.theme.variant(self._mode_from)
            return blend_variants(from_variant, target_variant, t)

        return target_variant

    # ─── Submission ───

    def _submit(self) -> None:
        text = self.input_buffer.strip()
        self.input_buffer = ""

        # Cancel any in-flight cache warmer — the user's message takes
        # priority over background warming. If we let the warmer keep
        # running, the user's request would queue behind it on the
        # llama.cpp slot and they'd wait LONGER than if we'd never
        # warmed at all.
        if self._cache_warmer is not None:
            self._cache_warmer.close()
            self._cache_warmer = None

        # Cancel any in-flight bash runners from a previous turn. The
        # user starting a new turn voids the continuation queue —
        # whatever the previous turn was doing, the new message takes
        # over. Runners will surface as cancelled cards on the next
        # tick via _pump_running_tools.
        if self._running_tools:
            self._cancel_running_tools()
            self._pending_continuation = False

        if text in ("/quit", "/exit", "/q"):
            self.stop()
            return

        # /config — open the three-pane profile config menu
        # The chat stops with _pending_action = "config" so the cli
        # main loop opens the config menu, then resumes the chat.
        if text == "/config":
            self._pending_action = "config"
            self.stop()
            return

        # /bash <command> — run a bash command client-side and render
        # it as a structured tool card. The user message preserves the
        # /bash command verbatim; a tool-message follows with the parsed
        # ToolCard. Dispatch goes through the SAME async runner path
        # as the agent loop's tool calls so the user gets the live
        # animated execution UX even for manual commands. The /bash
        # path doesn't queue a continuation (no agent turn is in
        # flight), so the runner finishes and the card settles
        # without firing a model call.
        if text.startswith("/bash"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                self.messages.append(_Message(
                    "successor",
                    "usage: /bash <command>. The command runs locally and "
                    "renders as a structured tool card. Dangerous commands "
                    "(rm -rf /, sudo, curl|sh, etc.) are refused with an "
                    "explanation.",
                    synthetic=True,
                ))
                return
            command = parts[1].strip()
            # Echo the command as a synthetic user-style message so the
            # scrollback shows the input that triggered the card
            self.messages.append(_Message(
                "user",
                f"`{command}`",
                synthetic=True,
            ))
            bash_cfg = resolve_bash_config(self.profile)
            self._spawn_bash_runner(command, bash_cfg=bash_cfg)
            return

        # /budget — show context fill % + token usage stats
        if text == "/budget":
            self._handle_budget_cmd()
            return

        # /burn N — inject synthetic context to stress-test compaction
        if text.startswith("/burn"):
            parts = text.split()
            if len(parts) < 2:
                self.messages.append(_Message(
                    "successor",
                    "usage: /burn <N>  → inject N synthetic tokens of "
                    "varied content into the chat history. Use this to "
                    "stress-test compaction without burning real model "
                    "calls. Pair with /budget to watch the fill % climb "
                    "and /compact to fire the summarizer.",
                    synthetic=True,
                ))
                return
            try:
                n_tokens = int(parts[1])
            except ValueError:
                self.messages.append(_Message(
                    "successor",
                    f"unknown /burn argument '{parts[1]}'. Expected an integer token count.",
                    synthetic=True,
                ))
                return
            self._handle_burn_cmd(n_tokens)
            return

        # /compact — manually fire compaction against the live client
        if text == "/compact":
            self._handle_compact_cmd()
            return

        # /profile         — show current profile and available options
        # /profile <name>  — switch to a registered profile by name
        # /profile cycle   — cycle to the next profile in registry order
        if text.startswith("/profile"):
            parts = text.split(maxsplit=1)
            available_names = sorted(PROFILE_REGISTRY.names())
            if len(parts) == 1:
                names = ", ".join(available_names) or "(none loaded)"
                hint = (
                    f"current profile: {self.profile.name}"
                    + (f" — {self.profile.description}" if self.profile.description else "")
                    + f". Available: {names}. "
                    f"Use /profile <name> or Ctrl+P to cycle."
                )
                self.messages.append(_Message("successor", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "cycle":
                self._cycle_profile()
                return
            target = get_profile(arg)
            if target is None:
                self.messages.append(
                    _Message(
                        "successor",
                        f"no profile named '{arg}'. try one of: "
                        f"{', '.join(available_names) or '(none)'}.",
                        synthetic=True,
                    )
                )
                return
            self._set_profile(target)
            return

        # /theme       — show current theme and available options
        # /theme <name>— switch to a registered theme by name
        # /theme cycle — cycle to next theme in registry order
        if text.startswith("/theme"):
            parts = text.split(maxsplit=1)
            available_names = sorted(THEME_REGISTRY.names())
            if len(parts) == 1:
                names = ", ".join(available_names) or "(none loaded)"
                hint = (
                    f"current theme: {self.theme.name} {self.theme.icon}. "
                    f"Available: {names}. Use /theme <name> or Ctrl+T to cycle."
                )
                self.messages.append(_Message("successor", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "cycle":
                self._cycle_theme()
                return
            target = get_theme(arg)
            if target is None:
                self.messages.append(
                    _Message(
                        "successor",
                        f"no theme named '{arg}'. try one of: "
                        f"{', '.join(available_names) or '(none)'}.",
                        synthetic=True,
                    )
                )
                return
            self._set_theme(target)
            return

        # /mode         — show current display mode
        # /mode dark    — switch to dark mode (preserve theme)
        # /mode light   — switch to light mode (preserve theme)
        # /mode toggle  — flip dark↔light
        if text.startswith("/mode"):
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                hint = (
                    f"display mode: {self.display_mode}. "
                    f"Use /mode dark|light|toggle or Alt+D to flip. "
                    f"Mode is independent of theme — switching mode keeps "
                    f"the same theme."
                )
                self.messages.append(_Message("successor", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "toggle":
                self._toggle_display_mode()
                return
            if arg in ("dark", "light"):
                self._set_display_mode(arg)
                return
            self.messages.append(
                _Message(
                    "successor",
                    f"unknown /mode argument '{arg}'. try dark, light, or toggle.",
                    synthetic=True,
                )
            )
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
                self.messages.append(_Message("successor", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "on":
                self._enable_mouse()
                self.messages.append(
                    _Message(
                        "successor",
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
                        "successor",
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
                    "successor",
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
                self.messages.append(_Message("successor", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "cycle":
                self._cycle_density()
                return
            target = find_density(arg)
            if target is None:
                self.messages.append(
                    _Message(
                        "successor",
                        f"no density named '{arg}'. try one of: "
                        f"{', '.join(d.name for d in DENSITIES)}.",
                        synthetic=True,
                    )
                )
                return
            self._set_density(target)
            return

        # Add the user's message and kick off turn 1 of the agent loop.
        # _begin_agent_turn opens the stream and the continue-loop in
        # _pump_stream calls it again when tool results come back.
        self.messages.append(_Message("user", text))
        self._scroll_to_bottom()
        self._agent_turn = 0
        self._begin_agent_turn()

    def _begin_agent_turn(self) -> None:
        """Open a new stream for the next turn of the agent loop.

        Called once at the start of each user submission (from
        `_submit`) AND again from `_pump_stream` after a bash batch
        finishes, so the model gets a chance to react to its own
        tool output. Increments `self._agent_turn` and refuses to
        exceed `MAX_AGENT_TURNS` — a synthetic message is committed
        instead so the user knows the loop stopped on its own.

        The whole pipeline (enabled-tools resolution, system-prompt
        assembly, api-messages build, stream open, detector init)
        lives here rather than in `_submit` because the continue-loop
        needs to re-run it for each turn against the updated
        `self.messages` (which now contains the last turn's tool
        cards serialized as assistant-role history).
        """
        self._agent_turn += 1
        if self._agent_turn > MAX_AGENT_TURNS:
            self.messages.append(_Message(
                "successor",
                f"[agent loop halted at {MAX_AGENT_TURNS} turns — "
                f"send a new message to continue]",
                synthetic=True,
            ))
            self._agent_turn = 0
            return

        # Resolve which tools are enabled for THIS turn from the active
        # profile. filter_known() drops any unrecognized names so a
        # stale profile referencing a future tool doesn't crash us.
        enabled_tools = filter_known(self.profile.tools or ())

        # Build the system prompt for THIS turn. When `tools=` is sent
        # to the model, Qwen's chat template auto-injects its OWN
        # canonical "use <tool_call> blocks" instructions into the
        # system message — that's the format the model is trained on.
        # We intentionally do NOT inject the legacy BASH_DOC fenced-
        # bash guidance in that case because two conflicting sets of
        # instructions confuse the model into emitting half-formed
        # raw text instead of structured calls.
        #
        # We DO append two short sections for bash:
        #   1. cwd hint — the chat template doesn't know about our
        #      workspace pinning, so the model has to be told
        #      explicitly. Without this, files land in the wrong
        #      place or the model uses bare relative paths the user
        #      didn't intend.
        #   2. multi-step guidance — Qwen 3.5's reasoning chains
        #      occasionally get stuck retrying the same successful
        #      tool call when the task involves multiple steps and
        #      tool results come back empty. The fix is one explicit
        #      sentence: read your previous tool results before
        #      deciding what to do next. This is the model behavior
        #      analog of free-code's "If the user denies a tool you
        #      call, do not re-attempt the exact same tool call"
        #      guidance — same shape, different trigger.
        sys_prompt = self.system_prompt
        if enabled_tools and "bash" in enabled_tools:
            bash_cfg = resolve_bash_config(self.profile)
            effective_cwd = bash_cfg.working_directory or os.getcwd()
            sys_prompt = (
                f"{sys_prompt}\n\n"
                f"## Bash working directory\n\n"
                f"Every bash command you run executes with "
                f"`cwd={effective_cwd}`. Relative paths resolve from "
                f"there. If the user asks for a file in a specific "
                f"location (like `~/Desktop/foo.html`), use the "
                f"absolute path — do not assume your cwd is what the "
                f"user had in mind.\n\n"
                f"## Working with tool results\n\n"
                f"Before making each tool call, scan the conversation "
                f"history above and check what you have ALREADY done. "
                f"A tool result with no stdout means the command "
                f"succeeded — that is normal for writes, redirects, "
                f"`mkdir`, `touch`, `chmod`, and most mutating "
                f"commands. NEVER re-issue a tool call that already "
                f"appears earlier in the conversation; instead, take "
                f"the next step toward the user's goal, or respond "
                f"with plain text if you are done. Plain text "
                f"(no tool call) is how you finish the task and "
                f"return control to the user."
            )

        # Build the conversation history for the model in NATIVE Qwen
        # tool-call shape. Each pass through the message list pairs an
        # assistant message with any immediately-following tool cards
        # and emits them as ONE assistant message with `tool_calls`
        # populated, followed by `role: "tool"` messages linked via
        # `tool_call_id`. Qwen's chat template renders this as
        # `<tool_call>` and `<tool_response>` blocks — the format the
        # model was trained on. Using fenced bash text in the
        # assistant content (the previous approach) caused the model
        # to loop on heredocs because text-format bash blocks are
        # ambiguous between "I'm running this" and "I'm citing this
        # for documentation".
        api_messages = self._build_api_messages_native(sys_prompt)

        # Build the OpenAI-style tools schema when bash is enabled.
        # The chat template's `if tools` branch fires and injects the
        # canonical "use <tool_call>" instructions into the system
        # message, putting the model in its trained tool-calling mode.
        # Only pass tools= when non-None so older client implementations
        # (test mocks, providers without tool support) keep working.
        if "bash" in enabled_tools:
            self._stream = self.client.stream_chat(
                messages=api_messages,
                tools=[_BASH_TOOL_SCHEMA],
            )
        else:
            self._stream = self.client.stream_chat(messages=api_messages)
        self._stream_content = []
        self._stream_reasoning_chars = 0
        # The bash detector stays active as a LEGACY fallback for
        # streams where the model emits raw fenced bash blocks instead
        # of structured tool_calls. With `tools` set, the model should
        # almost always use the structured channel — but we keep the
        # detector wired so a model that goes off-format still works.
        if "bash" in enabled_tools:
            self._stream_bash_detector = BashStreamDetector()
        else:
            self._stream_bash_detector = None

    def _build_api_messages_native(self, sys_prompt: str) -> list[dict]:
        """Build the api_messages list in native Qwen tool-call shape.

        Walks `_api_ordered_messages` and groups each assistant message
        with the tool cards that immediately follow it. The group
        becomes:

            {"role": "assistant", "content": <prose>, "tool_calls": [
                {"id", "type": "function",
                 "function": {"name": "bash", "arguments": '{"command": ...}'}},
                ...
            ]}
            {"role": "tool", "tool_call_id": <id>, "content": <stdout>}
            {"role": "tool", "tool_call_id": <id>, "content": <stdout>}

        Tool cards without a preceding assistant (e.g., the /bash
        slash command echoes a card directly) get a synthesized
        empty-content assistant turn so the tool message has a tool
        call to link back to.

        Summary messages from compaction are still emitted as user
        messages with the standard `[summary of earlier conversation
        …]` prefix.
        """
        api_messages: list[dict] = [{"role": "system", "content": sys_prompt}]
        ordered = self._api_ordered_messages()

        def _append_text_merging(role: str, content: str) -> None:
            """Append with same-role merge for plain text turns. Tool
            and tool_call-bearing messages bypass this and go straight
            onto the list.
            """
            if not content:
                return
            if (
                len(api_messages) > 1
                and api_messages[-1].get("role") == role
                and "tool_calls" not in api_messages[-1]
            ):
                api_messages[-1]["content"] = (
                    api_messages[-1]["content"].rstrip() + "\n\n" + content
                )
                return
            api_messages.append({"role": role, "content": content})

        i = 0
        n = len(ordered)
        while i < n:
            m = ordered[i]

            if m.is_summary:
                _append_text_merging(
                    "user",
                    "[summary of earlier conversation, provided by the "
                    "harness — treat as authoritative context, not a "
                    "user turn]\n\n" + m.raw_text,
                )
                i += 1
                continue

            # Plain user message
            if m.role == "user" and m.tool_card is None:
                if m.synthetic:
                    i += 1
                    continue
                _append_text_merging("user", m.raw_text)
                i += 1
                continue

            # Assistant message → look ahead for following tool cards
            if m.role == "successor" and m.tool_card is None:
                if m.synthetic:
                    i += 1
                    continue
                tool_cards: list[ToolCard] = []
                j = i + 1
                while j < n and ordered[j].tool_card is not None:
                    tool_cards.append(ordered[j].tool_card)
                    j += 1

                if tool_cards:
                    api_messages.append(_assistant_with_tool_calls(
                        m.raw_text or "", tool_cards,
                    ))
                    for card in tool_cards:
                        api_messages.append({
                            "role": "tool",
                            "tool_call_id": card.tool_call_id,
                            "content": _tool_card_content_for_api(card),
                        })
                else:
                    _append_text_merging("assistant", m.raw_text)
                i = j
                continue

            # Tool card with no preceding assistant in this batch
            # (e.g., /bash slash command). Synthesize an empty-content
            # assistant turn carrying the tool call so the tool result
            # has something to link to.
            if m.tool_card is not None:
                tool_cards = [m.tool_card]
                j = i + 1
                while j < n and ordered[j].tool_card is not None:
                    tool_cards.append(ordered[j].tool_card)
                    j += 1
                api_messages.append(_assistant_with_tool_calls("", tool_cards))
                for card in tool_cards:
                    api_messages.append({
                        "role": "tool",
                        "tool_call_id": card.tool_call_id,
                        "content": _tool_card_content_for_api(card),
                    })
                i = j
                continue

            # Anything else (synthetic placeholders, etc.) — skip
            i += 1

        return api_messages

    # ─── Agent loop adapter (for /budget /burn /compact) ───
    #
    # The chat's existing _Message list is what the streaming path
    # uses. The agent module's MessageLog is what compaction operates
    # on. These two helpers convert in both directions so we can
    # exercise the agent code against the chat's live history without
    # rewriting the chat to use MessageLog directly.

    def _api_ordered_messages(self) -> list["_Message"]:
        """Return self.messages in API/chronological order.

        self.messages is stored in DISPLAY order — the summary is at
        the END of the list, after the kept rounds. For sending to
        the model we need CHRONOLOGICAL order: the summary FIRST
        (representing older content that was summarized), then the
        kept rounds, then any new turns.

        No-compaction case: returns self.messages unchanged.
        """
        summary_msg = next((m for m in self.messages if m.is_summary), None)
        if summary_msg is None:
            return list(self.messages)

        # Reorder: summary first, then everything else (preserving
        # the original chronological order of the non-summary messages)
        regular_msgs = [m for m in self.messages if not m.is_summary]
        return [summary_msg] + regular_msgs

    def _to_agent_log(self) -> MessageLog:
        """Snapshot self.messages as an agent.MessageLog.

        Walks messages in API order (summary first) so the log is
        chronologically correct for the model.
        """
        log = MessageLog(system_prompt=self.system_prompt)
        for msg in self._api_ordered_messages():
            if msg.synthetic and msg.tool_card is None and not msg.is_summary:
                # Skip non-tool, non-summary synthetic messages
                # (greetings, error notes) — they were never the model's voice
                continue
            # Each non-tool user message starts a new round
            if msg.role == "user" and not msg.tool_card:
                log.begin_round(started_at=msg.created_at)
            elif not log.rounds:
                log.begin_round(started_at=msg.created_at)
            # Summary messages → log summary message
            if msg.is_summary:
                log.append_to_current_round(LogMessage(
                    role="system",
                    content=msg.raw_text or "",
                    is_summary=True,
                    created_at=msg.created_at,
                ))
                continue
            agent_role = (
                "assistant" if msg.role == "successor"
                else "tool" if msg.role == "tool"
                else msg.role
            )
            log.append_to_current_round(LogMessage(
                role=agent_role,
                content=msg.raw_text or "",
                tool_card=msg.tool_card,
                created_at=msg.created_at,
            ))
        return log

    def _from_agent_log(self, log: MessageLog, *, boundary_meta: object | None = None) -> None:
        """Replace self.messages from an agent.MessageLog (after compact).

        Display order is `[kept rounds][summary]` — the summary is the
        LAST message and is displayed at the bottom of the chat. The
        boundary divider is NOT a separate message; it's rendered as
        the first row of the summary message itself, glued to the
        summary's top edge so they always appear together.

        Why integrated: when the summary is verbose (which Qwen
        sometimes produces), a separate boundary message could get
        pushed off-screen above the summary. By making the divider
        part of the summary's own render, they're never separated.

        API order is computed separately by `_api_ordered_messages()`
        which puts the summary FIRST so the model sees the
        chronologically correct sequence.

        boundary_meta carries the BoundaryMarker — the painter reads
        it when rendering the integrated divider header.
        """
        # Walk the log once and collect by type. We DON'T create a
        # separate boundary _Message — the boundary is folded into
        # the summary message's render via boundary_meta.
        summary_msg: _Message | None = None
        kept_msgs: list[_Message] = []
        for m in log.iter_messages():
            if m.is_boundary:
                continue  # boundary is folded into the summary's render
            if m.is_summary:
                summary_msg = _Message(
                    "successor", m.content,
                    is_summary=True,
                    boundary_meta=boundary_meta,
                )
                continue
            if m.tool_card is not None:
                kept_msgs.append(_Message(
                    "tool", "", tool_card=m.tool_card,
                ))
                continue
            chat_role = "successor" if m.role == "assistant" else m.role
            kept_msgs.append(_Message(chat_role, m.content))

        # Display order: kept rounds, then summary at the end
        new_messages: list[_Message] = list(kept_msgs)
        if summary_msg is not None:
            new_messages.append(summary_msg)
        self.messages = new_messages

    def _agent_token_counter(self) -> TokenCounter:
        """Lazy: build a TokenCounter pointed at the chat's client.
        Cached so subsequent /budget calls reuse the same per-string LRU."""
        if not hasattr(self, "_cached_token_counter") or self._cached_token_counter is None:
            self._cached_token_counter = TokenCounter(endpoint=self.client)
        return self._cached_token_counter

    def _resolve_context_window(self) -> int:
        """Resolve the active context window with provider-aware detection.

        Precedence:
          1. profile.provider.context_window  (explicit user override)
          2. self.client.detect_context_window()  (lazy probe — llama.cpp
             /props or OpenRouter-style /v1/models per-model context_length,
             cached on the client instance after the first round trip)
          3. CONTEXT_MAX (262_144) as the historical fallback

        Cached on the chat instance after the first resolution so the
        per-frame footer doesn't pay any overhead in the steady state.
        """
        if hasattr(self, "_cached_resolved_window"):
            return self._cached_resolved_window
        provider_cfg = self.profile.provider or {}
        override = provider_cfg.get("context_window")
        if isinstance(override, int) and override > 0:
            window = override
        else:
            detected = None
            try:
                detect = getattr(self.client, "detect_context_window", None)
                if callable(detect):
                    detected = detect()
            except Exception:
                detected = None
            window = detected if isinstance(detected, int) and detected > 0 else CONTEXT_MAX
        self._cached_resolved_window = window
        return window

    def _agent_budget(self) -> ContextBudget:
        """Build a ContextBudget from the resolved context window.

        Window comes from _resolve_context_window() which consults the
        profile override first, then probes the provider, then falls
        back to the hardcoded default. Headroom buffers are static for
        now — they could move into the profile later.
        """
        window = self._resolve_context_window()
        return ContextBudget(
            window=window,
            warning_buffer=max(8_000, window // 16),
            autocompact_buffer=max(4_000, window // 32),
            blocking_buffer=max(1_000, window // 128),
        )

    # ─── Token count caching ───
    #
    # The static footer needs the total token count for its fill bar +
    # threshold badges. Computing this naively (walk every message,
    # tokenize body, sum) is O(N) per frame and at 200K context that's
    # ~1000ms — drops the chat to 1 fps.
    #
    # Two layers of caching to make this O(1) in the steady state:
    #
    #   _Message._token_count    per-message cache, computed once on
    #                            first access (text is invariant for
    #                            the message's lifetime — raw_text is
    #                            set at construction and never mutated)
    #
    #   self._cached_total_tokens  chat-level cache of the SUM, set
    #                              by _total_tokens() on first read
    #                              after a mutation, invalidated by
    #                              _invalidate_token_cache() at every
    #                              self.messages mutation site
    #
    # The mutation sites are: _submit (user message append), _pump_stream
    # (assistant commit), _handle_burn_cmd (synthetic injection),
    # _from_agent_log (compaction swap), _handle_compact_cmd (snapshot+
    # swap), and the few synthetic-message appends scattered through the
    # slash command handlers. All audited and wired.

    def _invalidate_token_cache(self) -> None:
        """Mark the chat-level total token cache as stale.

        Optional explicit-invalidation hook — most call sites don't
        need to call this because `_total_tokens()` auto-detects
        mutations via (id, len) of self.messages. Use it only when
        you mutate a message's content in-place (which we don't
        currently do).
        """
        self._cached_total_tokens = None
        self._cached_total_tokens_key = (-1, -1)

    def _token_count_for_message(self, msg: "_Message") -> int:
        """Return (and lazy-compute) the token count for a single
        chat _Message. Includes the standard 4-token role overhead.

        Per-message counts are cached on the _Message itself in the
        `_token_count` slot. The cache is invariant because raw_text
        and tool_card are set at construction and never mutated.
        """
        if msg._token_count is not None:
            return msg._token_count
        # Determine the text payload for this message
        if msg.tool_card is not None:
            card = msg.tool_card
            text = f"$ {card.raw_command}"
            if card.output:
                text += "\n" + card.output
        else:
            text = msg.raw_text
        # Use the agent counter when available (accurate via /tokenize),
        # otherwise the char-count heuristic
        if self._cached_token_counter is not None:
            n = self._cached_token_counter.count(text) + 4
        else:
            n = max(1, len(text) // 4) + 4
        msg._token_count = n
        return n

    def _total_tokens(self) -> int:
        """Return the (cached) total token count of self.messages.

        After the first read following a mutation, this is O(1).
        Cache invalidation is automatic via (id, len) of self.messages
        — appends, wholesale replacements (even same-length ones), and
        truncations all bump at least one of the two values.

        Streaming buffer is added on top via a cheap char-count
        heuristic so the bar grows during streaming without paying the
        endpoint cost on every frame.
        """
        cur_key = (id(self.messages), len(self.messages))
        if (
            self._cached_total_tokens is not None
            and self._cached_total_tokens_key == cur_key
        ):
            committed_total = self._cached_total_tokens
        else:
            # Recompute from scratch (per-message counts hit the
            # _token_count cache on _Message after the first walk)
            if self._cached_token_counter is not None and self.system_prompt:
                sys_tokens = self._cached_token_counter.count(self.system_prompt) + 4
            elif self.system_prompt:
                sys_tokens = max(1, len(self.system_prompt) // 4) + 4
            else:
                sys_tokens = 0
            total = sys_tokens
            for msg in self.messages:
                total += self._token_count_for_message(msg)
            self._cached_total_tokens = total
            self._cached_total_tokens_key = cur_key
            committed_total = total

        # Streaming buffer — char heuristic for speed (the streaming
        # buffer text changes every frame so caching is impossible
        # anyway, and accuracy isn't critical for a live indicator).
        streaming_delta = 0
        if self._stream is not None and self._stream_content:
            stream_text = "".join(self._stream_content)
            streaming_delta = max(0, len(stream_text) // 4)

        return committed_total + streaming_delta

    # ─── /budget ───

    def _handle_budget_cmd(self) -> None:
        log = self._to_agent_log()
        counter = self._agent_token_counter()
        budget = self._agent_budget()
        used = counter.count_log(log)
        state = budget.state(used)
        fill = budget.fill_pct(used) * 100
        headroom = budget.headroom(used)
        msg = (
            f"context: {used:,} / {budget.window:,} tokens · "
            f"{fill:.1f}% full · {headroom:,} headroom · state: {state}\n"
            f"thresholds: warn @ {budget.warning_at:,} · "
            f"autocompact @ {budget.autocompact_at:,} · "
            f"blocking @ {budget.blocking_at:,}\n"
            f"rounds: {log.round_count} · "
            f"messages (excl synthetic): {log.total_messages()}"
        )
        self.messages.append(_Message("successor", msg, synthetic=True))

    # ─── /burn ───

    def _handle_burn_cmd(self, target_tokens: int) -> None:
        """Inject synthetic chat history until total token count reaches
        the target. Each synthetic round is a (user question + assistant
        answer) pair with varied content (lorem-ipsum-style filler with
        occasional code blocks and fake file paths) so it tokenizes
        realistically.

        Marks every injected message as NON-synthetic so /budget and
        /compact see it as real history. Uses fake created_at timestamps
        spaced 1s apart so microcompact's idle logic doesn't fire.

        Performance note: we deliberately use the char-count heuristic
        for sizing (instead of the /tokenize endpoint) AND pre-fill
        msg._token_count on each injected message. This avoids:
          1. ~700 HTTP /tokenize calls for /burn 200000 (each burn
             payload is unique because the index is embedded)
          2. The first-frame stall when the footer would otherwise
             walk every message and tokenize it once
        Synthetic burn text doesn't need real-tokenizer accuracy —
        the heuristic is calibrated to slightly overestimate which
        is the right side to err on for budget tracking.
        """
        # Make sure the counter exists for the chat-level total cache
        self._agent_token_counter()
        # Build the burn rounds
        added_rounds = 0
        added_tokens = 0
        base_t = time.monotonic() - 100.0  # synthetic recent timestamps
        while added_tokens < target_tokens:
            payload = self._make_burn_payload(added_rounds)
            user_text = payload["user"]
            asst_text = payload["assistant"]
            t = base_t + added_rounds * 0.5

            # Cheap heuristic count + 4-token role overhead
            user_tokens = max(1, len(user_text) // 4) + 4
            asst_tokens = max(1, len(asst_text) // 4) + 4

            user_msg = _Message("user", user_text)
            user_msg.created_at = t
            user_msg._token_count = user_tokens  # pre-fill
            self.messages.append(user_msg)

            asst_msg = _Message("successor", asst_text)
            asst_msg.created_at = t + 0.1
            asst_msg._token_count = asst_tokens  # pre-fill
            self.messages.append(asst_msg)

            added_rounds += 1
            added_tokens += user_tokens + asst_tokens
            if added_rounds > 10000:
                break  # safety bail

        # Report — use the running added_tokens directly instead of
        # rebuilding the agent log and re-walking
        budget = self._agent_budget()
        # Force a recompute via the chat cache (which now uses pre-filled
        # per-message counts so it's fast)
        self._cached_total_tokens = None
        new_total = self._total_tokens()
        fill = budget.fill_pct(new_total) * 100
        self.messages.append(_Message(
            "successor",
            f"injected {added_rounds} synthetic rounds · "
            f"≈{added_tokens:,} tokens · "
            f"context now {new_total:,} / {budget.window:,} ({fill:.1f}%)",
            synthetic=True,
        ))
        self._scroll_to_bottom()

    @staticmethod
    def _make_burn_payload(idx: int) -> dict:
        """Generate one synthetic burn round. Varies content to avoid
        tokenizer-cache cheating."""
        topics = [
            ("the rendering layers in successor",
             "five layers — measure, cells, paint, composite, diff"),
            ("how the bash subsystem parses commands",
             "shlex split, registry lookup, fall back to generic card"),
            ("what compaction does",
             "summarizes old turns into one block, keeps recent rounds verbatim"),
            ("the steel theme palette",
             "cool blue oklch instrument-panel — bg navy, accent steel, warm copper"),
            ("how the message log handles tool results",
             "ApiRound holds them; PTL retry drops oldest rounds whole"),
            ("why we use bash instead of structured tool schemas",
             "qwen3.5 distill is unreliable at tool schemas, fluent in bash"),
        ]
        topic_q, topic_a = topics[idx % len(topics)]
        # Long-form pad — varies per index so token count grows roughly
        # linearly without becoming a single cache hit
        pad_lines = [
            f"In iteration {idx}, the user paid attention to {topic_q}.",
            "Here is some longer-form discussion that fills tokens",
            "without being purely random gibberish, because the burn",
            f"rig wants the tokenizer to see realistic prose at index {idx}.",
            f"```\nsample-code-{idx} = {idx * 7 + 13}\nsample-fn({idx})\n```",
            f"Followed by another paragraph at index {idx} discussing",
            "the renderer pipeline, the diff layer, and the",
            "five-layer architecture that successor is built around.",
        ]
        pad = "\n".join(pad_lines)
        return {
            "user": f"Tell me again about {topic_q}.\n{pad}",
            "assistant": (
                f"Sure, on iteration {idx}: {topic_a}. "
                f"Going into more detail: {pad}"
            ),
        }

    # ─── /compact ───

    def _handle_compact_cmd(self) -> None:
        """Trigger compaction asynchronously.

        Snapshots the chat state, spawns a worker thread that runs
        agent.compact() in the background, and arms the animation
        immediately. The animation enters the WAITING phase after
        fold completes and stays there until the worker reports a
        result, at which point it transitions to MATERIALIZE.

        The chat REMAINS INTERACTIVE during the entire compaction
        — frame ticks continue, the spinner animates, the user can
        cancel with Ctrl+G. This is the difference between this
        handler and the previous synchronous version that froze the
        UI for the entire ~5+ minute duration of compaction at large
        contexts.
        """
        if self._compaction_worker is not None:
            self.messages.append(_Message(
                "successor",
                "compaction already in progress — wait for it to finish "
                "or press Ctrl+G to cancel.",
                synthetic=True,
            ))
            return

        counter = self._agent_token_counter()
        log = self._to_agent_log()
        if log.round_count < 4:
            self.messages.append(_Message(
                "successor",
                f"need at least 4 rounds to compact, have {log.round_count}. "
                f"Run /burn first to inflate the context.",
                synthetic=True,
            ))
            return

        # Pre-compute token count + rounds-to-summarize so the spinner
        # can show "compacting N rounds (X tokens)" right away.
        pre_tokens = counter.count_log(log)
        from .agent.compact import DEFAULT_KEEP_RECENT_ROUNDS
        keep_n = min(DEFAULT_KEEP_RECENT_ROUNDS, max(1, log.round_count // 2))
        rounds_to_summarize = log.round_count - keep_n

        # Snapshot the messages BEFORE running compaction so the fold
        # phase can paint them dimming out. The chat retains its
        # current view during anticipation+fold; after fold, the
        # waiting phase shows the spinner.
        snapshot = list(self.messages)
        snapshot_count = len(snapshot)

        # Arm the animation IMMEDIATELY — phases begin now. The
        # waiting phase activates automatically when fold ends if the
        # worker hasn't returned yet.
        self._compaction_anim = _CompactionAnimation(
            started_at=time.monotonic(),
            pre_compact_snapshot=snapshot,
            pre_compact_count=snapshot_count,
            boundary=None,  # filled in by _poll_compaction_worker
            summary_text="",
            reason="manual",
            pre_compact_tokens=pre_tokens,
            rounds_summarized=rounds_to_summarize,
        )

        # Spawn the worker. It runs compact() against the live client
        # in a daemon thread; on_tick polls it every frame.
        self._compaction_worker = _CompactionWorker(
            log=log,
            client=self.client,
            counter=counter,
            reason="manual",
        )
        self._compaction_worker.start()
        self._scroll_to_bottom()

    def _poll_compaction_worker(self) -> None:
        """Check whether the compaction worker has finished and apply
        the result. Called from on_tick on every frame.

        Three possible states:
          - No worker → return
          - Worker still running → return
          - Worker done with result → apply + transition animation
          - Worker done with error → clear animation, surface error
        """
        worker = self._compaction_worker
        if worker is None:
            return
        result = worker.poll()
        if result is None:
            return  # still running

        # Worker finished
        self._compaction_worker = None

        if result.error is not None:
            # Failure — drop the animation and report
            self._compaction_anim = None
            self.messages.append(_Message(
                "successor",
                f"compaction failed: {result.error}",
                synthetic=True,
            ))
            return

        # Success — apply the new log + transition animation to materialize
        if self._compaction_anim is None:
            # The animation was somehow cleared (e.g. cancel) — apply
            # the log silently and skip the visible transition
            self._from_agent_log(result.new_log, boundary_meta=result.boundary)
            return

        self._from_agent_log(result.new_log, boundary_meta=result.boundary)
        # Update the animation in place — the dataclass is mutable
        # because of slots=True (not frozen). The materialize phase
        # is computed relative to result_arrived_at.
        self._compaction_anim.boundary = result.boundary
        self._compaction_anim.summary_text = result.boundary.summary_text
        self._compaction_anim.result_arrived_at = time.monotonic()

        # Fire cache pre-warming for the post-compact prefix. This
        # populates llama.cpp's KV cache so the next user message
        # is near-instant instead of paying ~40s of cache miss.
        # Runs in parallel with the materialize/reveal/toast animation.
        # Auto-canceled by _submit when the user types a message.
        try:
            post_compact_messages = result.new_log.api_messages()
            if post_compact_messages:
                # llama.cpp's chat completion endpoint rejects prompts
                # that end on an assistant message when thinking mode
                # is enabled (HTTP 400: "Assistant response prefill is
                # incompatible with enable_thinking"). The post-compact
                # log ends on the last kept assistant turn, so we
                # append a synthetic user message to make the prompt
                # valid. The cache match against this prepended user
                # message will fail when the REAL user sends their
                # next message, but that's fine — the cache match for
                # everything BEFORE that synthetic message (which is
                # the post-compact prefix proper) is what we want.
                warmer_messages = list(post_compact_messages)
                if warmer_messages and warmer_messages[-1].get("role") == "assistant":
                    warmer_messages.append({"role": "user", "content": "."})
                self._cache_warmer = _CacheWarmer(
                    messages=warmer_messages,
                    client=self.client,
                )
                self._cache_warmer.start()
        except Exception:
            # Warming is best-effort — never block the chat on a
            # warmer construction failure
            self._cache_warmer = None

    def _pump_stream(self) -> None:
        """Drain any pending stream events and update accumulators.

        When bash is enabled in the active profile, ContentChunk text
        is also fed to the BashStreamDetector. After StreamEnded, any
        completed bash blocks are dispatched and tool cards appended
        to self.messages, BELOW the assistant message.
        """
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
                # Feed the bash detector incrementally so we catch
                # blocks as they arrive. Detection of completed blocks
                # is queued internally; we drain the queue after
                # StreamEnded so tool cards always appear AFTER the
                # full assistant message in the chat flow.
                if self._stream_bash_detector is not None:
                    self._stream_bash_detector.feed(ev.text)
            elif isinstance(ev, StreamEnded):
                # The model can produce tool calls in two channels:
                #
                #   1. NATIVE  — `delta.tool_calls` chunks accumulated
                #      by the provider into `ev.tool_calls`. This is
                #      the format Qwen 3.5 was trained on, fired when
                #      we send the `tools` parameter.
                #
                #   2. LEGACY — fenced ```bash blocks parsed by the
                #      `BashStreamDetector` from `ContentChunk` text.
                #      Kept as a fallback for streams where the model
                #      goes off-format.
                #
                # We process BOTH and merge the results. Native takes
                # precedence visually because the cards land with the
                # exact tool_call_id the model used, which keeps the
                # next turn's history coherent.
                raw_content = "".join(self._stream_content).strip()
                if self._stream_bash_detector is not None:
                    self._stream_bash_detector.flush()
                    display_content = self._stream_bash_detector.cleaned_text().strip()
                    legacy_blocks = self._stream_bash_detector.completed()
                    self._stream_bash_detector = None
                else:
                    display_content = raw_content
                    legacy_blocks = []
                native_calls = list(getattr(ev, "tool_calls", ()) or ())

                # Commit an assistant message for this stream UNCONDITIONALLY
                # when there's prose text OR when there are tool calls
                # of any kind. The empty-content variant acts as a TURN
                # BOUNDARY MARKER so the api_messages builder knows
                # where one assistant turn ends and the next begins.
                # Without it, multiple tool cards from separate turns
                # get bundled into a single assistant call in the next
                # api_messages payload, and the model reads "I made N
                # parallel calls in one turn" and loops re-running them.
                if raw_content:
                    self.messages.append(_Message(
                        "successor",
                        raw_content,
                        display_text=display_content,
                    ))
                elif legacy_blocks or native_calls:
                    # Empty-display assistant marker — display_text=""
                    # so the renderer skips it; raw_text="" so the
                    # api builder still recognizes the role boundary.
                    self.messages.append(_Message(
                        "successor",
                        "",
                        display_text="",
                    ))
                else:
                    self.messages.append(_Message(
                        "successor",
                        "(no answer — model produced only reasoning)",
                    ))
                self._last_usage = ev.usage
                self._stream = None
                self._stream_content = []
                self._stream_reasoning_chars = 0
                # Clear the sticky verb-preview cache; a new stream
                # will populate it fresh if it spawns more tool calls.
                self._streaming_verb_cache = {}

                # Dispatch native tool calls first (they carry the
                # model-provided ids), then any legacy bash blocks the
                # detector caught from raw text. Both paths now spawn
                # async runners — they don't block the tick loop. The
                # continuation stream fires from _pump_running_tools
                # when the LAST runner in this batch completes.
                any_ran = False
                if native_calls:
                    any_ran |= self._dispatch_native_tool_calls(native_calls)
                if legacy_blocks:
                    any_ran |= self._dispatch_streamed_bash_blocks(legacy_blocks)

                # The `_agent_turn > 0` guard is important: tests drive
                # `_pump_stream` directly with a pre-installed fake
                # stream WITHOUT going through `_submit`, so they never
                # increment the counter. Only user-initiated submissions
                # trigger continuation; synthetic test streams don't.
                if any_ran and self._agent_turn > 0:
                    # Mark this batch as needing a continuation. The
                    # actual _begin_agent_turn call happens once all
                    # runners in self._running_tools finish.
                    self._pending_continuation = True
                    return

                # No tool calls OR nothing successfully ran — turn
                # ends here. Reset the agent-turn counter so the next
                # user submission starts fresh.
                self._agent_turn = 0
            elif isinstance(ev, StreamError):
                partial = "".join(self._stream_content)
                if partial:
                    msg = f"{partial}\n\n[stream interrupted: {ev.message}]"
                else:
                    msg = self._format_stream_error(ev.message)
                self.messages.append(_Message("successor", msg, synthetic=True))
                self._stream = None
                self._stream_content = []
                self._stream_reasoning_chars = 0
                # Clear the sticky verb-preview cache; any tool-call
                # previews for this dead stream no longer apply.
                self._streaming_verb_cache = {}
                # Drop the bash detector — partial bash blocks in a
                # failed stream aren't safe to execute
                self._stream_bash_detector = None
                # A stream error inside a continuation kills the loop
                # for this user submission. Reset the turn counter so
                # the chat returns to IDLE instead of waiting forever
                # for a stream that'll never come back.
                self._agent_turn = 0

    def _format_stream_error(self, raw: str) -> str:
        """Translate a raw StreamError message into a friendlier hint.

        The most common failure modes for new users:
          - "[stream failed: connection failed: <urlopen error [Errno 111]
             Connection refused>]" — local server not running
          - "[stream failed: HTTP 401: Unauthorized]" — bad / missing api_key
          - "[stream failed: HTTP 402: Payment Required]" — out of credits
          - "[stream failed: HTTP 429: Too Many Requests]" — rate limited

        Each of these gets translated into an actionable hint that names
        the active profile's base_url and explains what the user should do.
        Other errors fall through with the raw message.
        """
        provider_cfg = self.profile.provider or {}
        base_url = provider_cfg.get("base_url", "http://localhost:8080")
        lower = raw.lower()
        is_conn_refused = (
            "connection refused" in lower
            or "errno 111" in lower
            or "could not connect" in lower
        )
        is_dns = (
            "name or service not known" in lower
            or "nodename nor servname" in lower
            or "temporary failure in name resolution" in lower
        )
        is_unreachable = "network is unreachable" in lower
        is_timeout = (
            "timed out" in lower
            or "timeout" in lower
            or "the read operation timed out" in lower
        )
        if is_conn_refused or is_dns or is_unreachable or is_timeout:
            return (
                f"[no server at {base_url}]\n"
                f"\n"
                f"successor expects an OpenAI-compatible HTTP endpoint at\n"
                f"the URL above. For local llama.cpp, the standard\n"
                f"quickstart is:\n"
                f"\n"
                f"  llama-server -m <your-model.gguf> --host 0.0.0.0 --port 8080\n"
                f"\n"
                f"For a hosted endpoint (OpenRouter, OpenAI, Groq, etc.),\n"
                f"open /config and check the active profile's\n"
                f"provider.base_url and provider.api_key fields."
            )
        if "http 401" in lower or "unauthorized" in lower:
            return (
                f"[unauthorized — {base_url}]\n"
                f"\n"
                f"The server rejected the request as unauthorized. Either\n"
                f"the api_key is missing, malformed, or revoked. Open\n"
                f"/config and check the active profile's provider.api_key\n"
                f"field."
            )
        if "http 402" in lower or "payment required" in lower:
            return (
                f"[out of credits — {base_url}]\n"
                f"\n"
                f"The provider says the account is out of credits or owes\n"
                f"a balance. Top up at the provider dashboard, then retry."
            )
        if "http 429" in lower or "too many requests" in lower:
            return (
                f"[rate limited by {base_url}]\n"
                f"\n"
                f"The provider is throttling requests. Wait a moment and\n"
                f"retry, or switch to a different model / paid tier in\n"
                f"the active profile via /config."
            )
        return f"[stream failed: {raw}]"

    def _spawn_bash_runner(
        self,
        command: str,
        *,
        bash_cfg: BashConfig,
        tool_call_id: str | None = None,
    ) -> bool:
        """Build a preview card, classify risk, and either:

          - append a REFUSED card synchronously (no runner spawned), OR
          - create a BashRunner, register it in self._running_tools,
            and start it. The chat's tick loop polls it from then on.

        Returns True iff a runner was started (the agent loop should
        continue when the batch completes). Refused-only batches return
        False so the continue-loop dead-ends and the user can resolve.
        """
        from dataclasses import replace as _replace
        from .bash.parser import parse_bash
        from .bash.risk import classify_risk, max_risk

        # Build the preview card from parser + classifier WITHOUT
        # executing. Same logic as bash/exec.py:dispatch_bash up to
        # the refusal gate, then we hand off to a runner.
        try:
            parsed = parse_bash(command)
        except Exception as exc:
            self.messages.append(_Message(
                "successor",
                f"bash parse failed for {command!r}: {exc}",
                synthetic=True,
            ))
            return False

        classifier_risk, classifier_reason = classify_risk(command)
        final_risk = max_risk(parsed.risk, classifier_risk)

        # Resolve a stable id once so refusal cards and execution
        # cards both carry the same value.
        from .bash.exec import _new_tool_call_id  # noqa: PLC0415
        resolved_call_id = tool_call_id or _new_tool_call_id()
        preview = _replace(parsed, risk=final_risk, tool_call_id=resolved_call_id)

        # Refusal gate — synchronous, no runner spawned
        if final_risk == "dangerous" and not bash_cfg.allow_dangerous:
            refused = DangerousCommandRefused(
                preview,
                classifier_reason or "command pattern flagged as dangerous",
            )
            self.messages.append(_Message("tool", "", tool_card=refused.card))
            hint = self._refusal_hint(refused, bash_cfg)
            self.messages.append(_Message(
                "successor",
                f"refused: {refused.reason}. {hint}",
                synthetic=True,
            ))
            return False
        if final_risk == "mutating" and not bash_cfg.allow_mutating:
            refused = MutatingCommandRefused(
                preview,
                classifier_reason or "mutating command refused in read-only mode",
            )
            self.messages.append(_Message("tool", "", tool_card=refused.card))
            hint = self._refusal_hint(refused, bash_cfg)
            self.messages.append(_Message(
                "successor",
                f"refused: {refused.reason}. {hint}",
                synthetic=True,
            ))
            return False

        # Spawn the runner — execution happens on a worker thread,
        # the chat's tick loop polls it via _pump_running_tools.
        runner = BashRunner(
            command,
            cwd=bash_cfg.working_directory,
            timeout=bash_cfg.timeout_s,
            max_output_bytes=bash_cfg.max_output_bytes,
            tool_call_id=resolved_call_id,
        )
        msg = _Message(
            "tool",
            "",
            tool_card=preview,
            running_tool=runner,
        )
        self.messages.append(msg)
        self._running_tools.append(msg)
        runner.start()
        self._scroll_to_bottom()
        return True

    def _dispatch_streamed_bash_blocks(self, blocks: list[str]) -> bool:
        """Spawn a BashRunner for each fenced bash block detected by
        the legacy stream parser. Returns True if at least one runner
        was spawned (so the caller can wire continuation), False if
        every block was refused.
        """
        if not blocks:
            return False
        bash_cfg = resolve_bash_config(self.profile)
        any_ran = False
        for command in blocks:
            if self._spawn_bash_runner(command, bash_cfg=bash_cfg):
                any_ran = True
        return any_ran

    def _dispatch_native_tool_calls(self, tool_calls: list[dict]) -> bool:
        """Spawn a BashRunner for each native tool_call from the
        model's structured `delta.tool_calls` stream. Mirrors the
        legacy path but propagates the model-provided call id onto
        the spawned runner so the next api_messages serialization
        can link the tool result back to the originating assistant
        turn coherently.
        """
        if not tool_calls:
            return False
        bash_cfg = resolve_bash_config(self.profile)
        any_ran = False
        for tc in tool_calls:
            name = tc.get("name") or ""
            args = tc.get("arguments") or {}
            call_id = tc.get("id") or ""

            if name != "bash":
                self.messages.append(_Message(
                    "successor",
                    f"unknown tool {name!r} — only `bash` is supported",
                    synthetic=True,
                ))
                continue

            command = args.get("command") if isinstance(args, dict) else ""
            if not command:
                self.messages.append(_Message(
                    "successor",
                    f"bash tool call had no command (raw_arguments={tc.get('raw_arguments','')!r})",
                    synthetic=True,
                ))
                continue

            if self._spawn_bash_runner(
                command, bash_cfg=bash_cfg, tool_call_id=call_id,
            ):
                any_ran = True
        return any_ran

    def _pump_running_tools(self) -> None:
        """Drain output deltas from in-flight runners. When a runner
        completes, finalize its card (replace the preview with the
        full enriched ToolCard) and clear running_tool. When the LAST
        runner in a pending continuation batch completes, fire the
        next agent-loop turn.

        Called from on_tick on every frame, right after _pump_stream.
        Cheap when there are no runners (single len() check).
        """
        if not self._running_tools:
            return

        completed_msgs: list[_Message] = []
        for msg in self._running_tools:
            runner = msg.running_tool
            if runner is None:
                completed_msgs.append(msg)
                continue
            # Drain the runner's queue. We don't strictly need the
            # events for state — runner.stdout / runner.stderr are
            # the source of truth — but draining keeps the queue
            # bounded and lets future code react to specific events
            # (e.g., per-line fade-in animations).
            runner.drain()
            # Force a fresh paint each frame while running so the
            # spinner/border pulse and live output stream visibly.
            msg._card_rows_cache_key = None
            msg._card_rows_cache = None
            if runner.is_done():
                self._finalize_runner(msg)
                completed_msgs.append(msg)

        for msg in completed_msgs:
            try:
                self._running_tools.remove(msg)
            except ValueError:
                pass

        # If we just finished the last runner in a continuation batch,
        # fire the next agent-loop turn so the model can react.
        if (
            self._pending_continuation
            and not self._running_tools
            and self._agent_turn > 0
            and self._stream is None
        ):
            self._pending_continuation = False
            self._begin_agent_turn()

    def _finalize_runner(self, msg: "_Message") -> None:
        """Replace the preview tool_card on `msg` with the final
        enriched card built from the runner's accumulated stdout,
        stderr, exit code, and duration. Clears running_tool so the
        renderer falls through to the static paint path.
        """
        from dataclasses import replace as _replace
        runner = msg.running_tool
        preview = msg.tool_card
        if runner is None or preview is None:
            return
        stdout = runner.stdout
        stderr = runner.stderr
        # If the worker errored before Popen succeeded (FileNotFoundError
        # etc.), exit_code may be None — surface as -1.
        exit_code = runner.exit_code if runner.exit_code is not None else -1
        # Cancellation / timeout → preserve the error in stderr so the
        # next model turn can read what happened.
        if runner.error:
            if stderr and not stderr.endswith("\n"):
                stderr = stderr + "\n"
            stderr = (stderr or "") + f"[{runner.error}]"
        final_card = _replace(
            preview,
            output=stdout,
            stderr=stderr,
            exit_code=exit_code,
            duration_ms=runner.elapsed() * 1000.0,
            truncated=runner.truncated,
        )
        msg.tool_card = final_card
        msg.running_tool = None
        msg._card_rows_cache_key = None
        msg._card_rows_cache = None

    def _cancel_running_tools(self) -> None:
        """Signal every in-flight runner to terminate. Used by Ctrl+G
        and by _submit when the user starts a new turn while previous
        runners are still in flight."""
        for msg in self._running_tools:
            if msg.running_tool is not None:
                msg.running_tool.cancel()

    def _refusal_hint(
        self, exc: RefusedCommand, bash_cfg: BashConfig,
    ) -> str:
        """One-line hint directing the user to the safety flag that
        would have let the command through.

        We point at the config menu path (`/config` → settings →
        tools → bash flags) rather than naming an env var so users
        have a single place to look. Future: a one-shot per-command
        override via confirmation modal.
        """
        if isinstance(exc, DangerousCommandRefused):
            if bash_cfg.allow_dangerous:
                # Shouldn't happen; the dispatch path wouldn't raise.
                # Still safe to fall through.
                return "enable bash.allow_dangerous in the profile to run."
            return (
                "enable bash.allow_dangerous in the profile to opt in "
                "(yolo mode) — /config → tools → bash."
            )
        if isinstance(exc, MutatingCommandRefused):
            return (
                "profile is in read-only mode. Enable bash.allow_mutating "
                "in /config → tools → bash to run this."
            )
        return ""

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

        # Drain any in-flight bash runners. When the LAST runner in
        # a continuation batch finishes, this fires the next agent
        # turn so the model can react to its tool output.
        self._pump_running_tools()

        # Poll the compaction worker. If it's done, apply the result
        # and transition the animation from waiting → materialize.
        self._poll_compaction_worker()

        # Clear the cache warmer reference once the worker thread
        # has finished. We don't need to do anything with the result —
        # the side effect (populated KV cache in llama.cpp) is what
        # we care about.
        if self._cache_warmer is not None and self._cache_warmer.is_done():
            self._cache_warmer = None

        # Resolve the active theme variant for THIS frame. Combines the
        # (theme, display_mode) state into one ThemeVariant. If either
        # axis is mid-transition this returns a blended palette; otherwise
        # it's just self.theme.variant(self.display_mode). Every painter
        # takes `theme: ThemeVariant` so the same code paints in any
        # palette × any mode.
        theme = self._current_variant()

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
        title = " successor · chat "
        title_style = Style(fg=theme.fg, bg=theme.bg, attrs=ATTR_BOLD)
        tx = max(0, (cols - len(title)) // 2)
        paint_text(grid, title, tx, 0, style=title_style)

        # ─── Chat scroll area ───
        self._paint_chat_area(grid, chat_top, chat_bottom, cols, theme)

        # ─── Theme widget (rightmost cell of title row) ───
        # Shows the theme's identity (icon + name). Keybinding: Ctrl+T.
        # Click target when mouse mode is on. Background uses the
        # theme's accent so the pill changes color with the theme.
        theme_label = f" {self.theme.icon} {self.theme.name} "
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

        # ─── Display mode widget (just left of the theme widget) ───
        # Three-cell pill showing ☾ for dark or ☀ for light. The two
        # axes (theme + mode) are independent, so this widget gets its
        # own pill instead of being squashed into the theme widget.
        # Keybinding: Alt+D. Click target when mouse mode is on.
        mode_icon = "\u263e" if self.display_mode == "dark" else "\u2600"
        mode_label = f" {mode_icon} "
        mode_style = Style(
            fg=theme.bg,
            bg=theme.fg_dim,
            attrs=ATTR_BOLD,
        )
        mode_x = max(0, theme_x - len(mode_label) - 1)
        paint_text(grid, mode_label, mode_x, 0, style=mode_style)
        self._hit_boxes.append(
            _HitBox(mode_x, 0, len(mode_label), 1, "mode")
        )

        # ─── Density widget (just left of the display mode widget) ───
        # Different background color so it visually distinguishes from
        # the theme + mode widgets. Keybindings: Alt+=, Alt+-, Ctrl+].
        # Click target when mouse mode is on.
        density_label = f" {self.density.name} "
        density_style = Style(
            fg=theme.bg,
            bg=theme.accent_warm,
            attrs=ATTR_BOLD,
        )
        density_x = max(0, mode_x - len(density_label) - 1)
        paint_text(grid, density_label, density_x, 0, style=density_style)
        self._hit_boxes.append(
            _HitBox(density_x, 0, len(density_label), 1, "density")
        )

        # ─── Profile widget (just left of the density widget) ───
        # Dim text on the chat background so it reads as a label, not
        # an interactive pill — but still clickable when mouse mode is
        # on. The profile is the persona unit; showing it always lets
        # the user instantly recognize which mode they're in.
        # Keybinding: Ctrl+P (cycles to the next registered profile).
        profile_label = f" {self.profile.name} "
        profile_style = Style(
            fg=theme.fg_dim,
            bg=theme.bg,
            attrs=ATTR_DIM | ATTR_BOLD,
        )
        profile_x = max(0, density_x - len(profile_label) - 1)
        paint_text(grid, profile_label, profile_x, 0, style=profile_style)
        self._hit_boxes.append(
            _HitBox(profile_x, 0, len(profile_label), 1, "profile")
        )

        # ─── Scroll indicator (left of the profile widget when scrolled) ───
        if self.scroll_offset > 0:
            if self._stream is not None:
                indicator = f" ↑ {self.scroll_offset} · successor responding · Ctrl+E newest "
            else:
                indicator = f" ↑ {self.scroll_offset}/{self._max_scroll()} · End for newest "
            ix = max(0, profile_x - len(indicator))
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

        # ─── Help overlay ───
        # Painted EVEN LATER so it overlays everything else, including
        # the autocomplete dropdown. Centered modal with a fade-in.
        if self._help_open:
            self._paint_help_overlay(grid, theme)

    # ─── Region painters ───

    def _paint_chat_area(
        self,
        grid: Grid,
        top: int,
        bottom: int,
        width: int,
        theme: ThemeVariant,
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

        # NB: there's no scroll override during compaction anymore.
        # The boundary + summary now live at the END of self.messages
        # (display order), so they naturally appear at the bottom of
        # the chat where the user is auto-scrolled. The materialize
        # animation plays in view without any forced snap.

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

        for i, row in enumerate(combined):
            y = paint_y + i
            if y >= bottom:
                break
            self._paint_chat_row(grid, body_x, y, body_width, row, theme)

        # NB: there is no centered "compacting" overlay anymore. The
        # chat content stays fully visible during compaction so the
        # user can scroll, search, and read freely while the model
        # generates the summary in the background. The "compaction
        # in progress" signal lives in the static footer instead.

    # ─── Paint a single _RenderedRow ───

    def _paint_chat_row(
        self,
        grid: Grid,
        x: int,
        y: int,
        body_width: int,
        row: _RenderedRow,
        theme: ThemeVariant,
    ) -> None:
        """Render one paint-ready row at (x, y) with `body_width` cells.

        Handles row-level treatments (code block bg, blockquote left
        border, header rule, horizontal rule) and per-span tag color
        resolution. Empty rows (no leading, no spans) just leave the
        background fill from `_paint_chat_area`'s clear pass.

        For tool-card rows (prepainted_cells set), copy cells verbatim
        to the chat region — bypassing the entire span/leading flow
        because the bash renderer has already produced fully-styled cells.
        """
        # ─── Pre-painted (tool card) row — fast path ───
        if row.prepainted_cells:
            for col_offset, cell in enumerate(row.prepainted_cells):
                cx = x + col_offset
                if cx >= grid.cols or col_offset >= body_width:
                    break
                if cell.wide_tail:
                    continue
                grid.set(y, cx, cell)
            return

        # ─── Boundary divider row — special path ───
        # This is the permanent visible artifact of a past compaction.
        # The painter draws a horizontal line + a central pill showing
        # the compaction stats. The pill carries the BoundaryMarker info.
        # row.materialize_t controls the partial draw-in animation
        # during the compaction MATERIALIZE phase.
        if row.is_boundary:
            # Subtle continuous pulse on settled boundaries — gives the
            # divider a "living artifact" feel rather than dead chrome.
            pulse_phase = self.elapsed if row.materialize_t >= 1.0 else 0.0
            self._paint_compaction_boundary(
                grid, x, y, body_width, theme,
                boundary=row.boundary_meta,
                materialize_t=row.materialize_t,
                pulse_phase=pulse_phase,
            )
            return

        # ─── Row-level treatments ───
        line_bg = theme.bg
        if row.line_tag in ("code_block", "code_lang"):
            line_bg = theme.bg_input if row.line_tag == "code_block" else theme.bg_footer
            # Fill the body region with the tinted bg
            fill_region(
                grid, x, y, body_width, 1,
                style=Style(bg=line_bg),
            )
        elif row.line_tag == "header_rule":
            # Thin separator under h1/h2
            rule_text = "─" * max(0, body_width - _PREFIX_W)
            paint_text(
                grid, rule_text, x + _PREFIX_W, y,
                style=Style(fg=theme.fg_subtle, bg=theme.bg),
            )
            return
        elif row.line_tag == "hr":
            # Horizontal rule across the full body width
            rule_text = "─" * max(0, body_width - _PREFIX_W)
            paint_text(
                grid, rule_text, x + _PREFIX_W, y,
                style=Style(fg=theme.fg_dim, bg=theme.bg, attrs=ATTR_BOLD),
            )
            return
        elif row.line_tag == "blockquote":
            # Will paint a left bar after we've drawn the leading region
            pass

        # Helper: apply per-row fade_alpha to a foreground color so the
        # compaction fold animation (and any future per-row dim) is
        # uniform across leading + body. Pulls alpha toward theme.bg.
        def _faded(fg: int) -> int:
            if row.fade_alpha >= 1.0:
                return fg
            return lerp_rgb(theme.bg, fg, row.fade_alpha)

        # ─── Leading text (prefix or continuation indent) ───
        leading_text = row.leading_text
        if leading_text:
            leading_color = self._resolve_leading_color(
                row.leading_color_kind, row.base_color, theme
            )
            leading_style = Style(
                fg=_faded(leading_color),
                bg=line_bg,
                attrs=row.leading_attrs,
            )
            paint_text(grid, leading_text, x, y, style=leading_style)
            cx = x + len(leading_text)
        else:
            cx = x

        # ─── Blockquote left border ───
        if row.line_tag == "blockquote":
            paint_text(
                grid, "▎", cx, y,
                style=Style(fg=_faded(theme.accent_warm), bg=line_bg, attrs=ATTR_BOLD),
            )
            cx += 1
            # Skip an extra space after the bar
            cx += 1

        # ─── Body indent (for blockquotes inside the body) ───
        cx += row.body_indent

        # ─── Body spans ───
        for span in row.body_spans:
            style = self._resolve_span_style(span, row.base_color, line_bg, theme)
            if row.fade_alpha < 1.0:
                style = Style(
                    fg=_faded(style.fg),
                    bg=style.bg,
                    attrs=style.attrs,
                )
            paint_text(grid, span.text, cx, y, style=style)
            cx += sum(1 for _ in span.text)  # rough; assumes width-1 chars
            # NOTE: for full UTF-8 width support we'd use char_width here,
            # but markdown content is overwhelmingly width-1 in practice.

    def _paint_compaction_boundary(
        self,
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
        """Paint a compaction boundary divider with central pill.

        Layout (full materialize):

            ━━━━━━━━━━━━━━━━━━┤ ▼ summary · 165 → 6 rounds · 96.9% saved ▼ ├━━━━━━━━━━━━

        materialize_t controls the partial draw-in (0.0 → 1.0). At t=0
        nothing is drawn; at t=1 the divider is fully visible. The line
        materializes from the center outward (gives a sense of inevitability
        rather than a directional sweep).

        pulse_phase, when > 0, adds a subtle brightness pulse to the
        pill — used after the animation completes to make the divider
        a living artifact rather than dead chrome.

        boundary may be None (e.g. before the BoundaryMarker has been
        attached), in which case we paint just an unlabeled divider.
        """
        if body_width < 12:
            return

        # The pill text — concise but information-dense
        if boundary is not None:
            pill_text = self._format_boundary_pill(boundary)
        else:
            pill_text = " compaction "

        # Color: accent_warm — warm, attention-getting but not alarming.
        # Pulse_phase adds a subtle brightness modulation.
        base_color = theme.accent_warm
        if pulse_phase > 0:
            import math
            pulse = 0.5 + 0.5 * math.sin(pulse_phase * 2 * math.pi * 0.4)
            base_color = lerp_rgb(theme.accent_warm, theme.accent, pulse * 0.3)

        line_style = Style(fg=base_color, bg=theme.bg, attrs=ATTR_BOLD)
        pill_style = Style(fg=theme.bg, bg=base_color, attrs=ATTR_BOLD)
        bracket_style = Style(fg=base_color, bg=theme.bg, attrs=ATTR_BOLD)

        # Step 1: paint the horizontal line via the primitive (handles
        # the partial materialize from the center outward)
        paint_horizontal_divider(
            grid, x, y, body_width,
            style=line_style,
            char="━",
            t=materialize_t,
        )

        # Step 2: at materialize_t < 0.6 we don't show the pill yet.
        # The pill appears as the line nears full extent so the user
        # sees the line draw FIRST and then the metadata snap in.
        if materialize_t < 0.6 or not pill_text:
            return

        # Compute pill geometry
        pill_w = len(pill_text) + 2  # 2 for the bracket characters
        if pill_w >= body_width - 4:
            return  # too narrow to show the pill, just leave the line
        pill_x = x + (body_width - pill_w) // 2

        # Step 3: erase the line under where the pill goes (just the bracket
        # characters and the pill body), then paint the pill on top.
        # Bracket characters frame the pill: ┤ ... ├
        if 0 <= pill_x < grid.cols:
            grid.set(y, pill_x, Cell("┤", bracket_style))
        # Pill body — fade in alpha based on materialize_t (0.6 → 1.0)
        pill_alpha = max(0.0, min(1.0, (materialize_t - 0.6) / 0.4))
        if pill_alpha < 1.0:
            faded_bg = lerp_rgb(theme.bg, base_color, pill_alpha)
            faded_fg = lerp_rgb(theme.bg, theme.bg, 1.0)  # bg → bg = stay bg
            pill_style = Style(fg=theme.bg if pill_alpha > 0.5 else faded_bg,
                               bg=faded_bg, attrs=ATTR_BOLD)
        paint_text(
            grid, pill_text, pill_x + 1, y,
            style=pill_style,
        )
        right_bracket_x = pill_x + 1 + len(pill_text)
        if 0 <= right_bracket_x < grid.cols:
            grid.set(y, right_bracket_x, Cell("├", bracket_style))

    @staticmethod
    def _format_boundary_pill(boundary: object) -> str:
        """Format the BoundaryMarker as a one-line pill label.

        Examples:
          " ▼ 161 rounds · 40k → 1k tokens · 96.9% saved ▼ "
          " ▼ summary · 12 rounds · 6k → 1k · 80% saved ▼ "
        """
        # Duck-typed read of BoundaryMarker — we accept anything with
        # the right attributes so the painter doesn't need to import
        # from agent.log
        try:
            n_rounds = getattr(boundary, "rounds_summarized", 0)
            pre = getattr(boundary, "pre_compact_tokens", 0)
            post = getattr(boundary, "post_compact_tokens", 0)
            reduction = getattr(boundary, "reduction_pct", 0.0)
        except Exception:
            return " ▼ compaction ▼ "

        def _fmt_tokens(n: int) -> str:
            if n >= 1000:
                return f"{n / 1000:.0f}k"
            return str(n)

        return (
            f" ▼ {n_rounds} rounds · {_fmt_tokens(pre)} → "
            f"{_fmt_tokens(post)} · {reduction:.0f}% saved ▼ "
        )

    @staticmethod
    def _resolve_leading_color(kind: str, base_color: int, theme: ThemeVariant) -> int:
        if kind == "fg":
            return theme.fg
        if kind == "fg_dim":
            return theme.fg_dim
        return base_color  # "accent" or unknown — use the message's base color

    @staticmethod
    def _resolve_span_style(
        span: LaidOutSpan,
        base_color: int,
        line_bg: int,
        theme: ThemeVariant,
    ) -> Style:
        """Resolve a span's semantic tag to a concrete Style.

        Tags:
            ""             — default body text using base_color
            "code"         — inline code with bg_input tint
            "link"         — accent_warm + underline (from attrs)
            "header"       — accent fg + bold
            "list_marker"  — accent_warm fg
            "code_lang"    — fg_dim on bg_footer
            "search_hit"   — accent_warm bg, bg fg (highlight)
        """
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

    # ─── Flat-line builders ───

    def _build_message_lines(
        self,
        body_width: int,
        theme: ThemeVariant,
    ) -> list[_RenderedRow]:
        """Flatten the conversation into a list of paint-ready rows.

        Each row holds enough information for the painter to draw it
        without consulting the message list again. Spans inside rows
        carry semantic tags (code, link, header, etc.) which the
        painter resolves to theme colors at paint time.

        When search is active, spans are split at match boundaries and
        the matching cells get a highlight background applied via a
        special tag the painter recognizes.

        When a compaction animation is in progress, this method
        switches between the snapshot (FOLD phase) and the post-compact
        message list (MATERIALIZE / REVEAL / TOAST phases), and applies
        per-row fade_alpha + boundary materialize_t overrides so the
        painter draws the right frame.
        """
        # ─── Compaction animation routing ───
        if self._compaction_anim is not None:
            now = time.monotonic()
            phase, phase_t = self._compaction_anim.phase_at(now)
            if phase == "done":
                # Animation finished — clear it and fall through to
                # normal painting of self.messages
                self._compaction_anim = None
            elif phase in ("anticipation", "fold", "waiting"):
                # The chat stays FULLY READABLE during waiting. The
                # snapshot is the canonical "what the chat looked
                # like before compaction started" — we paint it at
                # full opacity so the user can scroll, search, and
                # read freely while the model generates the summary
                # in the background. The "compaction in progress"
                # signal lives in the static footer (a small badge
                # next to the context bar), NOT as a blocking overlay.
                #
                # This is the harness's strongpoint: every cell of
                # past content is mutable data the user can interact
                # with. Walling it off during a forced idle moment
                # would be the opposite of what the architecture
                # is good at.
                return self._build_rows_from_messages(
                    self._compaction_anim.pre_compact_snapshot,
                    body_width, theme,
                )
            else:
                # materialize / reveal / toast — paint the post-compact
                # state with overrides on boundary materialize_t and
                # summary fade_alpha
                return self._build_rows_from_messages(
                    self.messages, body_width, theme,
                    anim_phase=phase, anim_t=phase_t,
                )

        return self._build_rows_from_messages(self.messages, body_width, theme)

    def _build_rows_from_messages(
        self,
        messages: list,
        body_width: int,
        theme: ThemeVariant,
        *,
        global_fade_alpha: float = 1.0,
        anticipation_glow: bool = False,
        anim_phase: str = "",
        anim_t: float = 1.0,
    ) -> list[_RenderedRow]:
        """The actual row builder. Pulled out of _build_message_lines so
        the animation routing can pass the snapshot or self.messages
        with appropriate per-row overrides.

        global_fade_alpha: applied to every emitted row's fade_alpha
            (used by the FOLD phase)
        anticipation_glow: when True, base_color shifts toward accent_warm
            for the soon-to-be-summarized rounds
        anim_phase: when "materialize" / "reveal", boundary/summary
            rows get the appropriate animation state
        anim_t: 0-1 progress within the current phase
        """
        out: list[_RenderedRow] = []
        now = time.monotonic()
        n = len(messages)
        spacing = self._current_density().message_spacing
        md_width = max(1, body_width - _PREFIX_W)

        for i, msg in enumerate(messages):
            age = now - msg.created_at
            fade_t = (
                ease_out_cubic(min(1.0, age / FADE_IN_S))
                if age < FADE_IN_S
                else 1.0
            )
            base_color = theme.fg if msg.role == "user" else theme.accent
            if msg.synthetic:
                base_color = theme.fg_dim
            if anticipation_glow:
                # Subtle warm tint on every row to "preview" the fold
                base_color = lerp_rgb(base_color, theme.accent_warm, 0.35)
            if fade_t < 1.0:
                base_color = lerp_rgb(theme.fg_subtle, base_color, fade_t)

            # ─── Boundary marker message ───
            # Emit a special row that the painter routes to
            # _paint_compaction_boundary. During materialize phase the
            # divider draws in from the center outward; otherwise it's
            # fully visible.
            if msg.is_boundary:
                materialize_t = 1.0
                if anim_phase == "materialize":
                    materialize_t = ease_out_cubic(anim_t)
                elif anim_phase in ("", "reveal", "toast"):
                    materialize_t = 1.0
                out.append(_RenderedRow(
                    is_boundary=True,
                    boundary_meta=msg.boundary_meta,
                    materialize_t=materialize_t,
                    base_color=base_color,
                    fade_alpha=global_fade_alpha,
                ))
                if i < n - 1:
                    for _ in range(spacing):
                        out.append(_RenderedRow(base_color=base_color))
                continue

            # ─── Summary message ───
            # The boundary divider is the FIRST row of the summary's
            # render (integrated header). The summary content fades
            # in below it. Both come together — the user always sees
            # the divider with the summary, no scrolling needed.
            if msg.is_summary:
                summary_alpha = global_fade_alpha
                materialize_t = 1.0
                if anim_phase == "materialize":
                    # Boundary divider grows from center; summary not
                    # yet visible
                    summary_alpha = 0.0
                    materialize_t = ease_out_cubic(anim_t)
                elif anim_phase == "reveal":
                    # Boundary fully drawn; summary fading in
                    summary_alpha = ease_out_cubic(anim_t)
                    materialize_t = 1.0
                elif anim_phase in ("toast", ""):
                    # Settled — boundary fully visible, summary at full opacity
                    materialize_t = 1.0
                    if not anim_phase:
                        summary_alpha = global_fade_alpha

                # ROW 1: the integrated boundary divider header
                if msg.boundary_meta is not None:
                    out.append(_RenderedRow(
                        is_boundary=True,
                        boundary_meta=msg.boundary_meta,
                        materialize_t=materialize_t,
                        base_color=base_color,
                        fade_alpha=global_fade_alpha,
                    ))

                # ROW 2+: the summary text with dim styling
                prefix = "▼ "  # marker glyph instead of role prefix
                md_lines = msg.body.lines(md_width)
                summary_color = lerp_rgb(theme.fg_subtle, theme.fg_dim, 0.6)
                rendered_rows = self._render_md_lines_with_search(
                    md_lines, msg.display_text, [], prefix, summary_color,
                )
                for r in rendered_rows:
                    r.is_summary = True
                    r.fade_alpha = summary_alpha
                out.extend(rendered_rows)
                if i < n - 1:
                    for _ in range(spacing):
                        out.append(_RenderedRow(base_color=base_color))
                continue

            # ─── Tool card message ───
            if msg.tool_card is not None:
                card_rows = self._render_tool_card_rows(
                    msg, body_width, theme,
                )
                # Apply global_fade_alpha to tool card rows by tinting
                # their pre-painted cells (a no-op when 1.0)
                if global_fade_alpha < 1.0:
                    card_rows = self._fade_prepainted_rows(
                        card_rows, theme.bg, 1.0 - global_fade_alpha,
                    )
                out.extend(card_rows)
                if i < n - 1:
                    for _ in range(spacing):
                        out.append(_RenderedRow(base_color=base_color))
                continue

            # If the message's display body is empty (e.g., assistant
            # turn that was nothing but a fenced bash block — the
            # block lives in raw_text for the model but shouldn't
            # paint as a bare "successor ▸" line above the tool card),
            # skip the row entirely. The tool card that follows
            # speaks for the message.
            if msg.role != "user" and not msg.display_text.strip():
                if i < n - 1:
                    for _ in range(spacing):
                        out.append(_RenderedRow(base_color=base_color))
                continue

            prefix = _USER_PREFIX if msg.role == "user" else _SUCCESSOR_PREFIX
            md_lines = msg.body.lines(md_width)

            msg_matches: list[tuple[int, int, int]] = []
            if self._search_active and self._search_matches:
                for mi_focused, start, end in self._search_matches:
                    if mi_focused == i:
                        is_focused = (
                            self._search_matches.index(
                                (mi_focused, start, end)
                            ) == self._search_focused
                        )
                        msg_matches.append((start, end, 2 if is_focused else 1))

            if not md_lines:
                out.append(
                    _RenderedRow(
                        leading_text=prefix,
                        leading_attrs=ATTR_BOLD,
                        leading_color_kind="accent",
                        base_color=base_color,
                        fade_alpha=global_fade_alpha,
                    )
                )
            else:
                rendered_rows = self._render_md_lines_with_search(
                    md_lines, msg.display_text, msg_matches, prefix, base_color,
                )
                # Apply the global fade to every row produced by this msg
                if global_fade_alpha < 1.0:
                    for r in rendered_rows:
                        r.fade_alpha = global_fade_alpha
                out.extend(rendered_rows)

            if i < n - 1:
                for _ in range(spacing):
                    out.append(_RenderedRow(base_color=base_color))
        return out

    @staticmethod
    def _fade_prepainted_rows(
        rows: list[_RenderedRow],
        bg_color: int,
        toward_bg_amount: float,
    ) -> list[_RenderedRow]:
        """Tint all cells in pre-painted rows toward bg_color by the
        given amount (0.0 = unchanged, 1.0 = fully bg). Used to fade
        out tool cards during the compaction fold animation.
        """
        if toward_bg_amount <= 0:
            return rows
        out: list[_RenderedRow] = []
        for r in rows:
            if not r.prepainted_cells:
                out.append(r)
                continue
            new_cells = tuple(
                Cell(
                    c.char,
                    Style(
                        fg=lerp_rgb(c.style.fg, bg_color, toward_bg_amount),
                        bg=lerp_rgb(c.style.bg, bg_color, toward_bg_amount),
                        attrs=c.style.attrs,
                    ),
                    wide_tail=c.wide_tail,
                )
                for c in r.prepainted_cells
            )
            out.append(_RenderedRow(
                leading_text=r.leading_text,
                leading_attrs=r.leading_attrs,
                leading_color_kind=r.leading_color_kind,
                body_spans=r.body_spans,
                base_color=r.base_color,
                line_tag=r.line_tag,
                body_indent=r.body_indent,
                prepainted_cells=new_cells,
                is_boundary=r.is_boundary,
                boundary_meta=r.boundary_meta,
                is_summary=r.is_summary,
                fade_alpha=r.fade_alpha,
            ))
        return out

    def _render_tool_card_rows(
        self,
        msg: "_Message",
        body_width: int,
        theme: ThemeVariant,
    ) -> list[_RenderedRow]:
        """Pre-paint a ToolCard into a sub-grid and convert each row
        into a _RenderedRow with prepainted_cells set.

        Two paint paths:

          1. RUNNING — msg.running_tool is set. The renderer reads the
             runner's live stdout/stderr each frame and paints via
             paint_tool_card_running. The pulsing border + spinner +
             elapsed-time footer animate every tick. NO caching:
             every frame is fresh because the underlying state is
             changing (animations + new output lines).

          2. STATIC — runner has finished (or never had one). Existing
             cached path: build PreparedToolOutput once, cache the
             rendered rows by (width, theme), reuse on subsequent
             paints. Resize / theme swap invalidates.
        """
        card = msg.tool_card
        if card is None:
            return []

        runner = msg.running_tool
        if runner is not None:
            return self._render_running_tool_card_rows(
                msg, body_width, theme, runner,
            )

        # Build the PreparedToolOutput once per message. Immutable
        # (card is frozen dataclass), so it never needs to invalidate.
        if msg._prepared_tool_output is None:
            msg._prepared_tool_output = PreparedToolOutput(card)
        prepared = msg._prepared_tool_output

        # Second-level cache: the final pre-painted row list. Key is
        # (width, id(theme_variant)) — the variant is resolved freshly
        # every frame but the object identity stays stable between
        # theme swaps, so `id()` is a precise cache key. A theme swap
        # builds a new ThemeVariant and the cache auto-invalidates.
        cache_key = (body_width, id(theme))
        if (
            msg._card_rows_cache_key == cache_key
            and msg._card_rows_cache is not None
        ):
            return msg._card_rows_cache

        # Compute the height the card will need at this width.
        height = measure_tool_card_height(
            card, width=body_width, show_output=card.executed,
            prepared=prepared,
        )
        if height <= 0:
            return []

        # Paint the card into a sub-grid. The sub-grid is exactly the
        # right size — no clipping, no scroll inside the card.
        sub = Grid(height, body_width)
        paint_tool_card(
            sub, card, x=0, y=0, w=body_width, theme=theme,
            prepared=prepared,
        )

        # Snapshot each row of the sub-grid as an immutable tuple of
        # Cells. The painter just copies these to the chat region.
        rows: list[_RenderedRow] = []
        for sy in range(height):
            cells: list[Cell] = []
            for sx in range(body_width):
                cells.append(sub.at(sy, sx))
            rows.append(
                _RenderedRow(
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

    def _render_running_tool_card_rows(
        self,
        msg: "_Message",
        body_width: int,
        theme: ThemeVariant,
        runner: BashRunner,
    ) -> list[_RenderedRow]:
        """Pre-paint a LIVE tool card (running state) into a sub-grid
        and convert each row into a _RenderedRow.

        No caching here — every frame is a fresh paint because:
          - the spinner glyph rotates per frame
          - the border color pulses per frame
          - new output lines may have arrived since last frame
          - the elapsed-time counter ticks per frame

        At 30 FPS for one card the cost is dominated by the small
        sub-grid alloc + the row snapshot loop, which is sub-millisecond
        for typical card heights (5-15 rows × 80-160 cols).
        """
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

        rows: list[_RenderedRow] = []
        for sy in range(height):
            cells: list[Cell] = []
            for sx in range(body_width):
                cells.append(sub.at(sy, sx))
            rows.append(
                _RenderedRow(
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

    def _render_md_lines_with_search(
        self,
        md_lines: list[LaidOutLine],
        msg_raw_text: str,
        matches: list[tuple[int, int, int]],
        prefix: str,
        base_color: int,
    ) -> list[_RenderedRow]:
        """Convert markdown lines to _RenderedRows, optionally applying
        search-match highlights to spans whose text overlaps a match.

        For v0 we use a simple approach: walk through each rendered
        line's spans, and for each span, check if any chars in its text
        overlap a match position in the original raw_text. We can't
        precisely map rendered text back to source positions because
        markdown reflows; instead we substring-match each span's text
        against the search query directly. This produces correct
        highlights for plain text and most paragraphs; code blocks and
        complex inline syntax may miss highlights at boundary chars.
        """
        out: list[_RenderedRow] = []
        # Simpler heuristic: substring-match each span's text against
        # the (lowercased) search query. The search_active check is
        # done by the caller.
        query = self._search_query.lower() if self._search_active else ""
        focused_msg_idx, focused_start, focused_end = (
            self._search_matches[self._search_focused]
            if self._search_active and self._search_matches
            else (-1, 0, 0)
        )

        for line_idx, md_line in enumerate(md_lines):
            if line_idx == 0:
                leading = prefix
                leading_attrs = ATTR_BOLD
            else:
                leading = " " * _PREFIX_W
                leading_attrs = 0

            new_spans: tuple[LaidOutSpan, ...]
            if query and matches:
                new_spans = tuple(
                    self._highlight_spans(md_line.spans, query)
                )
            else:
                new_spans = tuple(md_line.spans)

            out.append(
                _RenderedRow(
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

    def _highlight_spans(
        self,
        spans: list[LaidOutSpan],
        query: str,
    ) -> list[LaidOutSpan]:
        """Walk a list of spans and split them at query matches.

        Each match becomes its own span with the special "search_hit"
        tag, which the painter renders with a highlighted background.
        Other span attrs (bold, italic, code, etc.) are preserved on
        both the matched and unmatched portions.
        """
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
                    # No more matches in this span
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
                # The matched substring becomes a search_hit span
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

    def _build_streaming_lines(
        self,
        body_width: int,
        theme: ThemeVariant,
    ) -> list[_RenderedRow]:
        """Render the in-flight streaming reply as paint-ready rows.

        Four phases can appear in a single turn:

          1. THINKING — no content yet. Spinner + reasoning preview
             tail (last ~80 chars of reasoning_content) as a dim
             scrolling lane beneath the spinner.
          2. CONTENT — model emits user-visible text. Rendered as
             markdown with a typewriter cursor. Fenced bash blocks
             get elided via BashStreamDetector.cleaned_text() so
             they don't pop in and then disappear.
          3. TOOL CALL ARGUMENTS — model emits `delta.tool_calls`
             chunks that accumulate in stream.tool_calls_so_far.
             We paint a "tool call arriving" preview card showing
             the raw_arguments JSON streaming in live, just like
             the thinking reasoning tail. Without this the user
             sees a dead pause while 44 lines of heredoc content
             stream in silently.
          4. QUEUED BASH (legacy detector) — a dim marker showing
             "queuing bash command…" when the fenced-block detector
             is inside a block but hasn't seen the closing fence.
        """
        if self._stream is None:
            return []
        now = time.monotonic()
        spinner_idx = int(now * SPINNER_FPS) % len(SPINNER_FRAMES)
        spinner = SPINNER_FRAMES[spinner_idx]

        # Visible stream content: prefer the detector's cleaned text
        # (which elides fenced bash blocks in real time) over the raw
        # stream buffer. Falls back to the raw buffer when bash isn't
        # enabled for this turn.
        block_in_flight = False
        if self._stream_bash_detector is not None:
            content_so_far = self._stream_bash_detector.cleaned_text()
            block_in_flight = self._stream_bash_detector.is_inside_block()
        else:
            content_so_far = "".join(self._stream_content)

        # Live tool_call accumulator snapshot. Each entry has
        # `{"index", "id", "name", "raw_arguments"}` where
        # raw_arguments is the running JSON text. Empty until the
        # model starts emitting `delta.tool_calls`. Use getattr so
        # test fakes that don't implement this interface still work.
        tool_calls_in_flight = getattr(
            self._stream, "tool_calls_so_far", None,
        ) or []

        out: list[_RenderedRow] = [_RenderedRow(base_color=theme.accent)]

        if not content_so_far:
            # Thinking phase — show spinner + char counter on the
            # successor line, plus a live reasoning preview underneath
            # showing the last few words of the model's internal
            # reasoning. Makes the wait feel productive instead of
            # opaque. Other harnesses can't show this because they
            # don't separate the reasoning channel.
            if self._stream_reasoning_chars > 0:
                text = f"{spinner} thinking… ({self._stream_reasoning_chars} chars)"
            else:
                text = f"{spinner} thinking…"
            out.append(
                _RenderedRow(
                    leading_text=_SUCCESSOR_PREFIX,
                    leading_attrs=ATTR_BOLD,
                    leading_color_kind="accent",
                    body_spans=(LaidOutSpan(text=text),),
                    base_color=theme.accent,
                )
            )

            # Live reasoning preview lane — show the last ~80 chars of
            # the model's reasoning_content as a dim italic indented
            # line under the spinner. Updates every frame as new chars
            # arrive.
            reasoning_text = self._stream.reasoning_so_far
            if reasoning_text:
                tail = reasoning_text[-_REASONING_PREVIEW_CHARS:]
                # Collapse internal whitespace runs to a single space
                # so the preview reads as a continuous flow.
                tail = " ".join(tail.split())
                if tail:
                    # Account for the leading "  ↳ " prefix when wrapping.
                    avail_w = max(1, body_width - _PREFIX_W - 4)
                    if len(tail) > avail_w:
                        # Show only the END of the tail (most recent text)
                        tail = "…" + tail[-(avail_w - 1):]
                    out.append(
                        _RenderedRow(
                            leading_text=" " * _PREFIX_W + "  ↳ ",
                            leading_color_kind="fg_dim",
                            leading_attrs=ATTR_DIM,
                            body_spans=(
                                LaidOutSpan(
                                    text=tail,
                                    attrs=ATTR_DIM | ATTR_ITALIC,
                                ),
                            ),
                            base_color=theme.fg_subtle,
                        )
                    )
            # Fall through to the tool-call preview block below —
            # the model may skip text entirely and go straight from
            # reasoning → tool_calls, in which case the preview is
            # the only visual cue that anything is happening.
            for tc in tool_calls_in_flight:
                raw_args = tc.get("raw_arguments", "")
                if not raw_args:
                    continue
                out.extend(self._streaming_tool_call_preview_rows(
                    name=tc.get("name") or "bash",
                    raw_arguments=raw_args,
                    call_index=tc.get("index", 0),
                    body_width=body_width,
                    theme=theme,
                    spinner=spinner,
                ))
            return out

        # Content streaming — render the live text as markdown.
        # Append a trailing block-cursor to the visible text so the
        # user can see the typewriter advancing.
        live_md = PreparedMarkdown(content_so_far + "▌")
        md_width = max(1, body_width - _PREFIX_W)
        md_lines = live_md.lines(md_width)
        for line_idx, md_line in enumerate(md_lines):
            leading = _SUCCESSOR_PREFIX if line_idx == 0 else " " * _PREFIX_W
            out.append(
                _RenderedRow(
                    leading_text=leading,
                    leading_attrs=ATTR_BOLD if line_idx == 0 else 0,
                    leading_color_kind="accent",
                    body_spans=tuple(md_line.spans),
                    base_color=theme.accent,
                    line_tag=md_line.line_tag,
                    body_indent=md_line.indent,
                )
            )

        # Mid-stream bash block indicator. When the detector is inside
        # a fenced bash block, those characters are already elided
        # from the visible text above — this row gives the user a
        # concrete signal that a command is being queued so the layout
        # doesn't feel like it froze or dropped content.
        if block_in_flight:
            out.append(
                _RenderedRow(
                    leading_text=" " * _PREFIX_W + "  ↳ ",
                    leading_color_kind="fg_dim",
                    leading_attrs=ATTR_DIM,
                    body_spans=(
                        LaidOutSpan(
                            text=f"{spinner} queuing bash command…",
                            attrs=ATTR_DIM | ATTR_ITALIC,
                        ),
                    ),
                    base_color=theme.fg_subtle,
                )
            )

        # ─── Streaming tool-call preview ──────────────────────────
        # The model's tool_calls arrive as `delta.tool_calls` chunks
        # over the same stream. Without any visual, the user stares
        # at a dead screen while the heredoc body (which can be
        # dozens of lines) streams in silently. Show it as a scrolling
        # tail just like the reasoning preview — the last ~3 wrapped
        # lines of the accumulated raw_arguments, with a cursor.
        for tc in tool_calls_in_flight:
            raw_args = tc.get("raw_arguments", "")
            if not raw_args:
                continue
            out.extend(self._streaming_tool_call_preview_rows(
                name=tc.get("name") or "bash",
                raw_arguments=raw_args,
                call_index=tc.get("index", 0),
                body_width=body_width,
                theme=theme,
                spinner=spinner,
            ))
        return out

    def _streaming_tool_call_preview_rows(
        self,
        *,
        name: str,
        raw_arguments: str,
        call_index: int,
        body_width: int,
        theme: ThemeVariant,
        spinner: str,
    ) -> list[_RenderedRow]:
        """Build the rows for a "tool call arriving" live preview.

        Design:
          Row 1:  ✎ write-file  path: about.html   (header, inferred
                  verb + glyph + param hint when parse_bash resolves
                  the partial command; falls back to a generic
                  "⟡ bash — receiving arguments…" header when the
                  command is too short or too malformed to classify)
          Row 2+: scrolling tail of the command body, last N wrapped
                  lines, dim italic with a typewriter cursor on the
                  final line. Mirrors the reasoning-preview aesthetic
                  so the user immediately recognizes "this is content
                  pouring in, not a hang".
        """
        # Try to extract the "command" field from the partial JSON
        # so the preview shows a readable command body instead of
        # escaped JSON. Failing that, fall back to the raw text.
        display_text = _extract_command_tail(raw_arguments)

        # How many lines of tail we show. Bounded so the preview
        # doesn't take over the chat.
        MAX_PREVIEW_LINES = 5
        avail_w = max(10, body_width - _PREFIX_W - 6)

        # Split on newlines first, then wrap long lines hard to fit
        # the available width. Take the LAST MAX_PREVIEW_LINES so
        # the freshest content is always visible.
        raw_lines = display_text.replace("\\n", "\n").split("\n")
        wrapped: list[str] = []
        for rl in raw_lines:
            if not rl:
                wrapped.append("")
                continue
            offset = 0
            while offset < len(rl):
                wrapped.append(rl[offset:offset + avail_w])
                offset += avail_w
        tail_lines = wrapped[-MAX_PREVIEW_LINES:]
        if not tail_lines:
            tail_lines = [""]
        # Append a cursor to the very last line so the user sees it
        # advancing as chars arrive.
        tail_lines[-1] = tail_lines[-1] + "▌"

        rows: list[_RenderedRow] = []
        # Header row — try to infer the verb from the partial command
        # so the header mirrors the final card instead of saying
        # "receiving arguments…" forever.
        #
        # Stickiness: once a high-confidence inference resolves for a
        # given call, cache it keyed by (stream_id, call_index) so
        # subsequent frames where the parser momentarily loses
        # confidence (mid-stream unclosed quotes, etc.) don't flicker
        # the header back to the generic message. We only REPLACE a
        # cached inference with a NEW successful inference — never
        # with a fallback.
        inferred = _infer_tool_preview(display_text)
        cache_key = (id(self._stream), call_index)
        if inferred is not None:
            self._streaming_verb_cache[cache_key] = inferred
        else:
            inferred = self._streaming_verb_cache.get(cache_key)

        if inferred is not None:
            glyph, verb_name, hint = inferred
            if hint:
                header_text = f"{spinner} {glyph} {verb_name}  {hint}"
            else:
                header_text = f"{spinner} {glyph} {verb_name}"
        else:
            header_text = f"{spinner} ⟡ {name} — receiving arguments…"
        rows.append(
            _RenderedRow(
                leading_text=" " * _PREFIX_W + "  ↳ ",
                leading_color_kind="fg_dim",
                leading_attrs=ATTR_DIM,
                body_spans=(
                    LaidOutSpan(
                        text=header_text,
                        attrs=ATTR_DIM | ATTR_BOLD,
                    ),
                ),
                base_color=theme.accent_warm,
            )
        )
        # Tail rows
        for line in tail_lines:
            rows.append(
                _RenderedRow(
                    leading_text=" " * _PREFIX_W + "    ",
                    leading_color_kind="fg_dim",
                    leading_attrs=ATTR_DIM,
                    body_spans=(
                        LaidOutSpan(
                            text=line,
                            attrs=ATTR_DIM | ATTR_ITALIC,
                        ),
                    ),
                    base_color=theme.fg_subtle,
                )
            )
        return rows

    # ─── Slash command autocomplete dropdown ───

    def _paint_autocomplete(
        self,
        grid: Grid,
        theme: ThemeVariant,
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

    def _blank_dropdown_rows(self, grid: Grid, theme: ThemeVariant, box_y: int, box_h: int) -> None:
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
        theme: ThemeVariant,
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
        theme: ThemeVariant,
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
        theme: ThemeVariant,
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

    # ─── Help overlay ───

    def _paint_help_overlay(self, grid: Grid, theme: Theme) -> None:
        """Centered modal showing every keybinding + slash command.

        Faded in over HELP_FADE_IN_S using lerp_rgb on every color so
        the modal smoothly arrives over the existing UI. Dismissed
        by any keypress.
        """
        rows, cols = grid.rows, grid.cols
        if rows < 8 or cols < 50:
            return

        # ─── Compute box dimensions ───
        # Two columns: key, description. Pad each column for alignment.
        key_col_w = max(
            max(len(key) for key, _ in entries)
            for _, entries in _HELP_SECTIONS
        )
        desc_col_w = max(
            max(len(desc) for _, desc in entries)
            for _, entries in _HELP_SECTIONS
        )
        title_text = "successor · keybindings"
        # Inner content width = key + 3 + desc, plus inner padding (4)
        inner_w = max(key_col_w + 3 + desc_col_w, len(title_text) + 4)
        box_w = min(inner_w + 6, cols - 4)

        # Inner content height: 1 for title, 1 for blank, then sections
        # (1 header row + N entry rows + 1 blank row each), then a
        # final 1 hint row.
        sections_h = 0
        for _, entries in _HELP_SECTIONS:
            sections_h += 1 + len(entries) + 1  # header + entries + spacer
        # Drop the last spacer
        sections_h -= 1
        inner_h = 1 + 1 + sections_h + 1 + 1  # title, blank, sections, blank, hint
        box_h = min(inner_h + 2, rows - 2)

        box_x = max(0, (cols - box_w) // 2)
        box_y = max(0, (rows - box_h) // 2)

        # ─── Fade-in lerp ───
        elapsed = time.monotonic() - self._help_opened_at
        fade_t = ease_out_cubic(min(1.0, elapsed / HELP_FADE_IN_S))

        def fade(target: int) -> int:
            return lerp_rgb(theme.bg, target, fade_t)

        # ─── Backdrop dim — slightly darken the chat behind the modal ───
        # Skip for v0; the modal's solid bg is enough visual separation.

        # ─── Draw the box ───
        border_color = fade(theme.accent)
        border_style = Style(fg=border_color, bg=theme.bg_input, attrs=ATTR_BOLD)
        fill_style = Style(fg=fade(theme.fg), bg=theme.bg_input)
        paint_box(
            grid, box_x, box_y, box_w, box_h,
            style=border_style, fill_style=fill_style,
        )

        # ─── Title ───
        title_y = box_y + 1
        if title_y < box_y + box_h - 1:
            tx = box_x + (box_w - len(title_text)) // 2
            paint_text(
                grid, title_text, tx, title_y,
                style=Style(fg=fade(theme.accent), bg=theme.bg_input, attrs=ATTR_BOLD),
            )

        # ─── Sections ───
        cur_y = title_y + 2  # blank row after title
        section_header_color = fade(theme.fg_dim)
        key_color = fade(theme.accent_warm)
        desc_color = fade(theme.fg)

        for section_idx, (section_name, entries) in enumerate(_HELP_SECTIONS):
            if cur_y >= box_y + box_h - 2:
                break
            # Section header
            paint_text(
                grid, f"  {section_name}",
                box_x + 2, cur_y,
                style=Style(
                    fg=section_header_color,
                    bg=theme.bg_input,
                    attrs=ATTR_DIM,
                ),
            )
            cur_y += 1

            for key, desc in entries:
                if cur_y >= box_y + box_h - 2:
                    break
                # Key column (right-aligned to its width)
                key_padded = key.rjust(key_col_w)
                paint_text(
                    grid, key_padded,
                    box_x + 4, cur_y,
                    style=Style(fg=key_color, bg=theme.bg_input, attrs=ATTR_BOLD),
                )
                # Description column
                paint_text(
                    grid, desc,
                    box_x + 4 + key_col_w + 3, cur_y,
                    style=Style(fg=desc_color, bg=theme.bg_input),
                )
                cur_y += 1

            if section_idx < len(_HELP_SECTIONS) - 1:
                cur_y += 1  # blank row between sections

        # ─── Hint row (always at the last interior row) ───
        hint_y = box_y + box_h - 2
        if box_y + 1 <= hint_y < box_y + box_h - 1:
            hint = "press any key to close"
            hx = box_x + (box_w - len(hint)) // 2
            paint_text(
                grid, hint, hx, hint_y,
                style=Style(
                    fg=fade(theme.fg_subtle),
                    bg=theme.bg_input,
                    attrs=ATTR_DIM,
                ),
            )

    # ─── Static footer ───

    def _paint_static_footer(
        self,
        grid: Grid,
        y: int,
        width: int,
        theme: ThemeVariant,
    ) -> None:
        """Paint the bottom-row context fill bar.

        Token count comes from the agent's TokenCounter (driven by
        llama.cpp's /tokenize endpoint when available, char heuristic
        fallback). The window size comes from the profile's
        provider.context_window — this lets a small-context profile
        (e.g. 50K for compaction stress testing) show a properly-scaled
        fill bar instead of the default 262K denominator.

        Threshold state drives:
          - bar fill color (accent → accent_warm → accent_warn)
          - a subtle continuous pulse when past the autocompact
            threshold (signals "compact me!")
          - an explicit state badge ("AUTOCOMPACT" / "BLOCKING") on
            the right side when over threshold
        """
        # Compute token usage via the chat-level cached total. The
        # cache is invalidated at every self.messages mutation site;
        # in steady state this is O(1). The first read after a mutation
        # is O(N) but uses per-message caches on _Message so even at
        # 200K context it's well under a frame budget.
        if self._cached_token_counter is not None:
            try:
                used = self._total_tokens()
            except Exception:
                used = self._fallback_token_count()
        elif self._last_usage and "total_tokens" in self._last_usage:
            used = int(self._last_usage["total_tokens"])
        else:
            used = self._fallback_token_count()

        # Window: profile override → provider auto-detect → CONTEXT_MAX.
        # Cached on the chat after the first resolution so the per-frame
        # painter doesn't pay any overhead.
        window = self._resolve_context_window()
        used = max(0, min(used, window))
        pct = used / window if window > 0 else 0.0

        # Footer background
        fill_region(grid, 0, y, width, 1, style=Style(bg=theme.bg_footer))

        # ─── Threshold state classification ───
        # Mirrors agent.budget.ContextBudget but inlined here so the
        # footer doesn't need an active BudgetTracker.
        autocompact_at = window - max(4_000, window // 32)
        warning_at = window - max(8_000, window // 16)
        blocking_at = window - max(1_000, window // 128)
        if used >= blocking_at:
            state = "blocking"
        elif used >= autocompact_at:
            state = "autocompact"
        elif used >= warning_at:
            state = "warning"
        else:
            state = "ok"

        # ─── Continuous pulse when past autocompact ───
        # Slow sine wave (0.5 Hz) blends the bar color toward fg so
        # the bar gently breathes — signals "compact me!" without
        # being jarring.
        pulse = 0.0
        if state in ("autocompact", "blocking"):
            import math
            pulse = 0.5 + 0.5 * math.sin(self.elapsed * 0.5 * 2 * math.pi)

        label = f" ctx {used:>7}/{window:>7} "
        # State badge: empty in ok/warning, explicit in autocompact/blocking
        state_badge = ""
        if state == "autocompact":
            state_badge = " ◉ COMPACT "
        elif state == "blocking":
            state_badge = " ⚠ BLOCKED "
        # Compacting indicator: tiny spinner + elapsed time while a
        # /compact is in progress in the background. The chat content
        # stays fully readable during compaction; this badge is the
        # only visual signal that something's happening behind the
        # scenes. Disappears once the result lands and the materialize
        # animation begins.
        compacting_badge = ""
        if self._compaction_worker is not None and self._compaction_anim is not None:
            spinner_idx = int(self.elapsed * 10) % len(SPINNER_FRAMES)
            elapsed_compact = self._compaction_worker.elapsed()
            m, s = divmod(int(elapsed_compact), 60)
            n_rounds = self._compaction_anim.rounds_summarized
            compacting_badge = (
                f" {SPINNER_FRAMES[spinner_idx]} compacting "
                f"{n_rounds}r · {m:02d}:{s:02d} "
            )
        # Warming indicator: tiny spinner when the cache pre-warmer is
        # running in the background. Disappears when warming completes.
        warming_badge = ""
        if self._cache_warmer is not None and self._cache_warmer.is_running():
            spinner_idx = int(self.elapsed * 10) % len(SPINNER_FRAMES)
            warming_badge = f" {SPINNER_FRAMES[spinner_idx]} warming "
        right_label = (
            f"{compacting_badge}{warming_badge}{state_badge} "
            f"{pct * 100:5.2f}%  {self.client.model[:20]} "
        )
        label_style = Style(fg=theme.fg_dim, bg=theme.bg_footer, attrs=ATTR_DIM)

        # Right-label color depends on state — warm in autocompact,
        # warn in blocking, accent_warm if compacting/warming is active
        if state == "blocking":
            right_fg = theme.accent_warn
        elif state == "autocompact":
            right_fg = lerp_rgb(theme.accent_warm, theme.accent_warn, pulse)
        elif compacting_badge or warming_badge:
            right_fg = theme.accent_warm
        elif state == "warning":
            right_fg = theme.accent_warm
        else:
            right_fg = theme.fg
        right_style = Style(fg=right_fg, bg=theme.bg_footer, attrs=ATTR_BOLD)

        paint_text(grid, label, 0, y, style=label_style)

        bar_x = len(label) + 1
        right_x = max(0, width - len(right_label))
        bar_w = max(0, right_x - bar_x - 1)

        if bar_w > 0:
            filled = int(round(bar_w * pct))
            empty = bar_w - filled
            # Bar color thresholds (smoother than the old hardcoded 0.6/0.85)
            if state == "ok":
                bar_fg = lerp_rgb(theme.accent, theme.accent_warm, pct / 0.85)
            elif state == "warning":
                bar_fg = theme.accent_warm
            elif state == "autocompact":
                # Pulse between accent_warm and accent_warn
                bar_fg = lerp_rgb(theme.accent_warm, theme.accent_warn, pulse)
            else:  # blocking
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

    def _fallback_token_count(self) -> int:
        """Char-count heuristic for the footer when no agent counter is
        available. Includes the streaming buffer if a stream is active."""
        used = sum(len(m.raw_text) for m in self.messages) // 4
        if self._stream is not None:
            used += (self._stream_reasoning_chars + len("".join(self._stream_content))) // 4
        return used

    # ─── Input area ───

    def _paint_input(
        self,
        grid: Grid,
        y: int,
        height: int,
        width: int,
        theme: ThemeVariant,
    ) -> None:
        # Search mode replaces the input with a search bar.
        if self._search_active:
            self._paint_search_bar(grid, y, width, theme)
            return

        fill_region(grid, 0, y, width, height, style=Style(bg=theme.bg_input))

        all_wrapped = self._input_lines_at_width(width)
        hidden_above = max(0, len(all_wrapped) - height)
        wrapped = all_wrapped[-height:] if hidden_above else all_wrapped

        prompt_style = Style(fg=theme.accent, bg=theme.bg_input, attrs=ATTR_BOLD)
        paint_text(grid, PROMPT, 0, y, style=prompt_style)

        text_style = Style(fg=theme.fg, bg=theme.bg_input)
        for i, line in enumerate(wrapped):
            ly = y + i
            if ly >= y + height:
                break
            paint_text(grid, line, PROMPT_WIDTH, ly, style=text_style)

        # Overflow indicator: when a paste (or long manual input) wraps
        # to more lines than the 8-row input cap can show, surface a
        # right-aligned "↑ N more lines" badge on the topmost visible
        # row so the user knows their content didn't get truncated —
        # the cursor is at the end and the older lines scrolled up.
        if hidden_above > 0:
            badge = f"↑ {hidden_above} more {'line' if hidden_above == 1 else 'lines'}"
            badge_x = max(PROMPT_WIDTH, width - len(badge) - 1)
            paint_text(
                grid,
                badge,
                badge_x,
                y,
                style=Style(
                    fg=theme.accent_warm,
                    bg=theme.bg_input,
                    attrs=ATTR_DIM | ATTR_ITALIC,
                ),
            )

        # Cursor / streaming-status indicator on the last visible line.
        if self._stream is None:
            last_line = wrapped[-1] if wrapped else ""
            last_y = y + min(len(wrapped) - 1, height - 1)
            cursor_x = min(width - 1, PROMPT_WIDTH + len(last_line))

            # ─── Inline argument ghost text ───
            # When the user has typed a slash command + space and is
            # ready for args, show the args_hint as dim text right
            # after the cursor (Copilot-style ghost text). As they type
            # the arg, the hint is hidden by their input. Disappears
            # entirely once the input contains a non-whitespace arg.
            ghost_text = self._compute_ghost_text()
            if ghost_text and cursor_x < width:
                ghost_x = cursor_x
                # Reserve a cell for the cursor itself if visible.
                cursor_visible = (int(time.monotonic() * CURSOR_BLINK_HZ * 2) % 2) == 0
                if cursor_visible:
                    ghost_x += 1  # ghost starts after the cursor cell
                avail = max(0, width - ghost_x)
                if avail > 0:
                    paint_text(
                        grid,
                        ghost_text[:avail],
                        ghost_x,
                        last_y,
                        style=Style(
                            fg=theme.fg_subtle,
                            bg=theme.bg_input,
                            attrs=ATTR_DIM | ATTR_ITALIC,
                        ),
                    )

            visible = (int(time.monotonic() * CURSOR_BLINK_HZ * 2) % 2) == 0
            if visible:
                cursor_cell = Cell(" ", Style(fg=theme.bg_input, bg=theme.fg))
                grid.set(last_y, cursor_x, cursor_cell)
        else:
            hint = "successor is responding…  Ctrl+G to interrupt"
            paint_text(
                grid,
                hint,
                PROMPT_WIDTH,
                y,
                style=Style(fg=theme.fg_dim, bg=theme.bg_input, attrs=ATTR_DIM),
            )

    def _paint_search_bar(
        self,
        grid: Grid,
        y: int,
        width: int,
        theme: ThemeVariant,
    ) -> None:
        """Render the search bar in place of the input area.

        Layout:
            🔎 query                              N/M  ↑↓ next  Esc close
        """
        fill_region(grid, 0, y, width, 1, style=Style(bg=theme.bg_input))

        # Left side: search prompt + query
        prompt = "🔎 "
        # If 🔎 is wide on this terminal, fall back to a plain text marker.
        # We'll just use plain text to avoid width issues:
        prompt = "search ▸ "
        prompt_style = Style(
            fg=theme.accent_warm,
            bg=theme.bg_input,
            attrs=ATTR_BOLD,
        )
        paint_text(grid, prompt, 0, y, style=prompt_style)

        query_text = self._search_query
        query_style = Style(fg=theme.fg, bg=theme.bg_input)
        paint_text(grid, query_text, len(prompt), y, style=query_style)

        # Cursor at the end of the query
        cursor_x = min(width - 1, len(prompt) + len(query_text))
        cursor_visible = (int(time.monotonic() * CURSOR_BLINK_HZ * 2) % 2) == 0
        if cursor_visible:
            grid.set(
                y, cursor_x,
                Cell(" ", Style(fg=theme.bg_input, bg=theme.fg)),
            )

        # Right side: match counter + key hints
        if self._search_matches:
            counter = (
                f" {self._search_focused + 1}/{len(self._search_matches)} "
            )
            counter_style = Style(
                fg=theme.bg,
                bg=theme.accent_warm,
                attrs=ATTR_BOLD,
            )
        else:
            if self._search_query:
                counter = " no matches "
            else:
                counter = " type to search "
            counter_style = Style(
                fg=theme.fg_dim,
                bg=theme.bg_input,
                attrs=ATTR_DIM,
            )

        hint = "  ↑↓ jump  Esc close"
        hint_style = Style(fg=theme.fg_subtle, bg=theme.bg_input, attrs=ATTR_DIM)

        right_text = counter + hint
        right_x = max(len(prompt) + len(query_text) + 2, width - len(right_text))

        # Counter pill (with its bg)
        counter_x = right_x
        if 0 <= counter_x < width:
            paint_text(grid, counter, counter_x, y, style=counter_style)
        # Hint after the counter
        hint_x = counter_x + len(counter)
        if 0 <= hint_x < width:
            paint_text(grid, hint, hint_x, y, style=hint_style)

    def _compute_ghost_text(self) -> str:
        """Inline ghost-text hint shown after the input cursor.

        Triggered when the user has typed a slash command name and a
        trailing space (i.e., they're about to type arguments). The
        ghost shows the command's args_hint until the user types a
        non-whitespace character.

        Returns the empty string when no ghost should be shown.
        """
        text = self.input_buffer
        if not text.startswith("/"):
            return ""
        rest = text[1:]
        if " " not in rest:
            return ""
        cmd_name, _, arg_partial = rest.partition(" ")
        cmd = find_slash_command(cmd_name)
        if cmd is None or not cmd.args_hint:
            return ""
        # If the user has already started typing args, hide the ghost.
        if arg_partial.strip():
            return ""
        return cmd.args_hint
