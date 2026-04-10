"""SuccessorChat — chat interface backed by a real model backend.

The first chat-shaped piece of Successor. Wires together:

- The five-layer renderer (cells/paint/diff/terminal/app)
- The Pretext-shaped text layout primitives (PreparedText)
- The real key parser (input/keys.py — UTF-8, ESC sequences,
  bracketed paste, modifier-bearing arrows, all decoded into typed
  KeyEvents)
- The provider clients (llama.cpp and OpenAI-compatible endpoints),
  which stream reasoning/content chunks on a worker thread and post
  events to a thread-safe queue

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

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .chat_agent_loop import (
    ChatAgentLoop,
    _api_role_for_message,
    _message_has_tool_artifact,
    _message_tool_artifact,
)
from .chat_display_runtime import ChatDisplayRuntime
from .chat_tool_runtime import ChatToolRuntime
from .config import load_chat_config, save_chat_config
from .file_tools import (
    FileReadTracker,
    FileReadStateEntry,
)
from .graphemes import delete_prev_grapheme
from .playback import RecordingBundle, default_recording_bundle_dir
from .progress import (
    ProgressUpdate,
    combine_progress_updates,
    summarize_subagent_completion,
)
from .input.keys import (
    Key,
    KeyDecoder,
    KeyEvent,
    MouseButton,
    MouseEvent,
)
from .profiles import (
    DEFAULT_MAX_AGENT_TURNS,
    PROFILE_REGISTRY,
    Profile,
    get_active_profile,
    next_profile,
)
from .providers import make_provider
from .providers.llama import (
    ChatStream,
    LlamaCppClient,
)
from .render.app import App
from .render.cells import (
    Grid,
    Style,
)
from .agent import (
    ContextBudget,
    LogMessage,
    MessageLog,
    TokenCounter,
)
from .agent.bash_stream import BashStreamDetector
from .bash import (
    BashConfig,
    RefusedCommand,
    ToolCard,
    resolve_bash_config,
)
from .bash.runner import (
    BashRunner,
)
from .tools_registry import (
    filter_known,
)
from .skills import (
    enabled_profile_skills,
)
from .tasks import (
    SessionTaskLedger,
)
from .verification_contract import (
    VerificationLedger,
)
from .runbook import (
    SessionRunbook,
)
from .subagents.cards import SubagentToolCard
from .subagents.manager import (
    SubagentManager,
    SubagentTaskCounts,
    SubagentTaskSnapshot,
)
from .subagents.prompt import (
    build_notification_display,
    build_notification_payload,
)
from .web import (
    PlaywrightBrowserManager,
    browser_runtime_status,
    resolve_browser_config,
    resolve_vision_config,
    run_browser_action,
    run_holonet,
    run_vision_analysis,
    vision_runtime_status,
)
from .web.verification import (
    classify_browser_verification,
)
from .session_trace import SessionTrace, clip_text as _trace_clip_text
from .render.markdown import PreparedMarkdown
from .render.chat_frame import HitBox, compute_chat_frame
from .render.chat_header import build_header_plan
from .render.chat_input import (
    paint_input as paint_input_surface,
    paint_search_bar as paint_search_bar_surface,
)
from .render.chat_overlays import (
    paint_arg_mode as paint_arg_mode_overlay,
    paint_help_overlay as paint_help_overlay_surface,
    paint_name_mode as paint_name_mode_overlay,
    paint_no_matches as paint_no_matches_overlay,
)
from .render.chat_rows import RenderedRow
from .render.paint import fill_region, paint_text
from .render.terminal import Terminal
from .render.text import ease_out_cubic, hard_wrap
from .render.theme import (
    Theme,
    ThemeVariant,
    all_themes,
    blend_variants,
    find_theme_or_fallback,
    next_theme,
    normalize_display_mode,
    toggle_display_mode,
)

from .skills.skill import Skill


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
        ("Ctrl+N",      "vim-style line down"),
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
    ("command palette", (
        ("/",           "open the slash command palette"),
        ("↑ ↓",          "navigate suggestions"),
        ("Tab",         "accept highlighted suggestion"),
        ("Enter",       "accept and submit"),
        ("Esc",         "dismiss the palette"),
    )),
    ("misc", (
        ("?",           "show this help overlay"),
        ("Ctrl+F",      "search chat history"),
        ("Esc / any",   "dismiss the help overlay"),
    )),
)


def _build_slash_command_help_section() -> tuple[str, tuple[tuple[str, str], ...]]:
    """Build a help-section tuple from the live SLASH_COMMANDS registry.

    Computed at paint time (not at import) so the help overlay stays
    in sync with whatever commands the chat has registered. If a new
    command lands in SLASH_COMMANDS, it appears in the help overlay
    automatically — no parallel list to keep updated.
    """
    entries: list[tuple[str, str]] = []
    for cmd in SLASH_COMMANDS:
        key = f"/{cmd.name}"
        if cmd.args_hint:
            key = f"{key} {cmd.args_hint}"
        entries.append((key, cmd.description))
    return ("available commands", tuple(entries))


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


_HitBox = HitBox


# How many lines to scroll per wheel notch. 3 lines is the conventional
# value (matches xterm and most terminal scroll-rate defaults).
WHEEL_SCROLL_LINES = 3


# Historical fallback for tests or partially-upgraded profiles that do
# not yet carry the newer per-profile field.
MAX_AGENT_TURNS = DEFAULT_MAX_AGENT_TURNS


# ─── Input history recall ───
#
# Cap the per-session history at 100 entries. The ring buffer drops
# the oldest entries past the cap. 100 is enough for a long working
# session, low enough to never matter for memory.
INPUT_HISTORY_MAX = 100


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
                    "[paper|steel|cycle]" — empty if no args
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

    Pulls names from the supported theme catalog so chat, wizard, and
    reviewer all expose the same paper/steel choices. The "cycle"
    pseudo-arg always appears at the end of the list.
    """
    p = partial.lower()
    options = [theme.name for theme in all_themes()] + ["cycle"]
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
        name="fork",
        description="run a background subagent against the current chat context",
        args_hint="<directive>",
    ),
    SlashCommand(
        name="tasks",
        description="list background subagent tasks for this session",
    ),
    SlashCommand(
        name="task-cancel",
        description="cancel a queued or running subagent task",
        args_hint="[<task-id>|all]",
        complete_args=static_args("all"),
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
    SlashCommand(
        name="recording",
        description="toggle local auto-record playback bundles",
        args_hint="[on|off|toggle]",
        complete_args=static_args("on", "off", "toggle"),
    ),
    SlashCommand(
        name="playback",
        aliases=("review",),
        description="open the current reviewer or the recordings manager",
        args_hint="[current|latest|recordings|<bundle>]",
        complete_args=static_args("current", "latest", "recordings"),
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
def _native_tool_call_failure_message(
    tc: dict[str, Any],
    *,
    finish_reason: str,
    finish_reason_reported: bool,
) -> str:
    """Compact user-facing note for malformed native tool-call payloads."""
    name = str(tc.get("name") or "tool")
    raw_arguments = str(tc.get("raw_arguments") or "")
    parse_error = str(tc.get("arguments_parse_error") or "").strip()
    parse_error_pos = tc.get("arguments_parse_error_pos")
    details: list[str] = []
    if finish_reason == "length":
        details.append("finish_reason=length")
    elif not finish_reason_reported:
        details.append("stream ended without final finish_reason")
    if parse_error:
        if isinstance(parse_error_pos, int):
            details.append(f"{parse_error} at char {parse_error_pos}")
        else:
            details.append(parse_error)
    if raw_arguments:
        details.append(f"raw_len={len(raw_arguments)}")
    preview = _trace_clip_text(raw_arguments, limit=220) if raw_arguments else ""
    lead = f"{name} tool call arguments were malformed or truncated before dispatch"
    if name == "bash":
        lead = "bash tool call was malformed or truncated before dispatch"
    msg = lead
    if details:
        msg += f" ({'; '.join(details)})"
    if preview:
        msg += f". Preview: {preview}"
    if name == "bash":
        msg += " Retry with a smaller command or split the write into multiple steps."
    return msg


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

    tool_card / subagent_card, when non-None, mark this message as a
    structured native-tool artifact instead of markdown body text. The
    chat painter detects these and renders the appropriate card path
    instead of the normal span flow. Tool messages are synthetic for
    display, but the API serializer reconstructs the assistant/tool
    turns from them.

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
        "synthetic", "tool_card", "subagent_card", "running_tool",
        "is_boundary", "is_summary", "boundary_meta",
        "api_role_override",
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
        subagent_card: SubagentToolCard | None = None,
        running_tool: object | None = None,
        is_boundary: bool = False,
        is_summary: bool = False,
        boundary_meta: object | None = None,
        api_role_override: str | None = None,
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
        self.synthetic = (
            synthetic
            or (tool_card is not None)
            or (subagent_card is not None)
            or is_boundary
            or is_summary
        )
        self.tool_card = tool_card
        self.subagent_card = subagent_card
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
        # Some display messages need to serialize as a different role
        # in api_messages. Background-task notifications render as
        # successor notices but enter model context as user-role events.
        self.api_role_override = api_role_override
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


_RenderedRow = RenderedRow


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
            StreamEnded, StreamError,
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
        initial_input: str = "",
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
        self._owns_recorder = False

        # ─── Persisted preferences ───
        # Loaded from ~/.config/successor/chat.json on startup. Saved on
        # every change to theme/display_mode/density/mouse so the user's
        # choices survive between `successor chat` invocations. The migration
        # from the v1 schema (where dark/light/forge were flat themes)
        # happens transparently inside load_chat_config.
        self._config = load_chat_config()
        if self._recorder is None and bool(self._config.get("autorecord", True)):
            try:
                bundle = RecordingBundle(
                    default_recording_bundle_dir(),
                    title="Successor session playback",
                    description="Auto-recorded local session bundle.",
                )
                bundle.__enter__()
                self._recorder = bundle
                self._owns_recorder = True
            except Exception:
                self._recorder = None
                self._owns_recorder = False

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
        self._browser_manager: PlaywrightBrowserManager | None = None
        self._file_read_state: dict[str, FileReadStateEntry] = {}
        self._file_read_tracker = FileReadTracker()

        # ─── Session trace ───
        # Normal chat sessions write a small JSONL trace to the user's
        # config dir so hangs/tool loops can be inspected after the app
        # exits. Local-only; bounded retention lives in session_trace.py.
        self._trace = SessionTrace()
        provider_cfg = profile.provider or {}
        self._trace.emit(
            "session_start",
            version=__version__,
            cwd=os.getcwd(),
            profile=self.profile.name,
            provider_type=str(provider_cfg.get("type") or ""),
            base_url=str(provider_cfg.get("base_url") or ""),
            model=str(provider_cfg.get("model") or ""),
        )

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
        # Mouse reporting is opt-in. When off, the terminal owns wheel
        # scrolling and text selection. When on, Successor owns wheel
        # scroll + clickable widgets, and Shift restores native drag
        # selection in terminals that support the override.
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

        # Probe the server immediately so the empty-state info panel
        # can show a "reachable / UNREACHABLE" status without paying
        # the round-trip every frame.
        self._server_health_ok: bool | None = None
        try:
            self._server_health_ok = self.client.health()
        except Exception:  # noqa: BLE001
            self._server_health_ok = False

        # When chat_intro_art is set on the active profile, the empty
        # state is rendered as a hero portrait + info panel via the
        # _paint_empty_state painter — no synthetic greeting needed
        # since the panel itself communicates "I'm here, ready, here's
        # the model + tools + how to start." When chat_intro_art is
        # None, fall back to a single synthetic greeting message so
        # the chat doesn't open completely blank.
        self.messages: list[_Message] = []
        if not (self.profile and self.profile.chat_intro_art):
            if self._server_health_ok:
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
            self.messages.append(_Message("successor", greeting, synthetic=True))

        # Cached intro art for the empty-state hero panel. Loaded
        # lazily on first paint via _resolve_intro_art() so we don't
        # touch disk in __init__ for chats that never end up empty
        # (e.g. /replay sessions). None means "no hero, paint info
        # panel only" or "no profile field set, paint nothing".
        self._intro_art: object | None = None  # BrailleArt | None
        self._intro_art_resolved: bool = False

        self.input_buffer: str = initial_input

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
        self._task_ledger = SessionTaskLedger()
        self._verification_ledger = VerificationLedger()
        self._runbook = SessionRunbook()
        self._runbook_attempt_count: int = 0
        self._task_continue_nudged_this_turn: bool = False
        self._task_continue_nudge: str | None = None
        self._browser_verification_active: bool = False
        self._browser_verification_reason: str = ""
        self._verification_continue_nudged_this_turn: bool = False
        self._verification_continue_nudge: str | None = None
        self._file_tool_continue_nudged_this_turn: bool = False
        self._file_tool_continue_nudge: str | None = None
        self._subagent_continue_nudged_this_turn: bool = False
        self._subagent_continue_nudge: str | None = None
        self._recent_progress_summaries: list[tuple[float, str]] = []
        self._last_progress_summary: str = ""
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

        # Pending agent-turn after autocompact. Set when the autocompact
        # gate at the start of `_begin_agent_turn` decides to defer the
        # turn so compaction can run first. The compaction worker poll
        # checks this flag on success and re-enters `_begin_agent_turn`
        # so the user's message gets sent after the log shrinks.
        # Cleared on success, on compaction failure (the turn is still
        # attempted — reactive PTL recovery may save it), and on cancel.
        self._pending_agent_turn_after_compact: bool = False

        # Per-turn flag to prevent autocompact from firing twice for
        # the same user message. Set when the autocompact gate fires;
        # cleared at the start of every new `_submit`. This is the
        # chat-layer equivalent of agent.budget.RecompactChain — it
        # stops the loop "compact → still over → compact again" if
        # the model produces a huge summary.
        self._autocompact_attempted_this_turn: bool = False

        # ─── Input history recall (Up/Down arrow shell-style) ───
        # Ring buffer of submitted user messages so Up arrow can
        # recall previous prompts without retyping them. Bash and zsh
        # users expect this; the absence is jarring on the first
        # session. Behavior:
        #
        #   * EMPTY input + Up = enter recall mode, load most recent
        #   * Recall mode + Up = older entry
        #   * Recall mode + Down = newer entry, then back to draft
        #   * Any editing key in recall mode = exit recall, keep buffer
        #   * Esc in recall mode = restore the saved draft
        #   * Submit always exits recall mode and adds to history
        #
        # History is in-memory only (not persisted across sessions)
        # for v1. Slash commands are included but consecutive
        # duplicates are deduped so /profile cycle spam doesn't fill
        # the buffer.
        self._input_history: list[str] = []
        self._input_history_idx: int | None = None  # None = not in recall mode
        self._input_history_draft: str = ""  # saved draft for restore

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

        # Background subagents — manual `/fork` and the model-visible
        # `subagent` tool both spawn isolated, headless child chats
        # through this manager. Queue width is a profile-level knob;
        # enable/disable, timeout, and notification policy are checked
        # from `profile.subagents`.
        self._subagent_manager = SubagentManager(
            max_model_tasks=self.profile.subagents.effective_max_model_tasks(
                self.client
            ),
        )

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
        self._agent_loop = ChatAgentLoop(
            self,
            _Message,
            densities=DENSITIES,
            find_density=find_density,
            max_agent_turns_default=MAX_AGENT_TURNS,
        )
        self._display_runtime = ChatDisplayRuntime(
            self,
            rendered_row_cls=_RenderedRow,
            user_prefix=_USER_PREFIX,
            successor_prefix=_SUCCESSOR_PREFIX,
            prefix_width=_PREFIX_W,
            fade_in_s=FADE_IN_S,
            spinner_fps=SPINNER_FPS,
            spinner_frames=SPINNER_FRAMES,
            reasoning_preview_chars=_REASONING_PREVIEW_CHARS,
        )
        self._tool_runtime = ChatToolRuntime(self, _Message)

    def run(self) -> None:
        try:
            super().run()
        finally:
            if self._recorder is not None and hasattr(self._recorder, "capture_frame"):
                front = getattr(self, "_front", None)
                if front is not None:
                    try:
                        self._recorder.capture_frame(front, chat=self, force=True)
                    except Exception:
                        pass
            self._shutdown_runtime_for_exit()
            self._trace.close(
                trace_path=str(self._trace.path),
                active_stream=self._stream is not None,
                active_runner_count=sum(
                    1 for msg in self._running_tools if msg.running_tool is not None
                ),
                active_subagent_count=self._subagent_counts().active,
            )
            if self._owns_recorder and self._recorder is not None and hasattr(self._recorder, "finalize"):
                try:
                    self._recorder.__exit__(None, None, None)
                except Exception:
                    pass
                try:
                    self._recorder.finalize(trace_path=self.session_trace_path)
                except Exception:
                    pass

    def _trace_event(self, event_type: str, **payload: Any) -> None:
        self._trace.emit(event_type, **payload)

    @property
    def session_trace_path(self) -> Path:
        return self._trace.path

    def _shutdown_runtime_for_exit(self) -> None:
        active = [
            msg for msg in self._running_tools
            if msg.running_tool is not None
        ]
        if active:
            self._trace_event(
                "shutdown_cancel_running_tools",
                count=len(active),
                tool_call_ids=[
                    msg.running_tool.tool_call_id
                    for msg in active
                    if msg.running_tool is not None
                ],
            )
            self._cancel_running_tools()
            deadline = time.monotonic() + 1.5
            while (
                any(msg.running_tool is not None for msg in self._running_tools)
                and time.monotonic() < deadline
            ):
                self._pump_running_tools()
                time.sleep(0.02)
            remaining = sum(
                1 for msg in self._running_tools if msg.running_tool is not None
            )
            if remaining:
                self._trace_event(
                    "shutdown_runners_still_active",
                    count=remaining,
                )
        if self._stream is not None:
            self._trace_event("shutdown_close_stream")
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._compaction_worker is not None:
            try:
                self._compaction_worker.close()
            except Exception:
                pass
            self._compaction_worker = None
        if self._cache_warmer is not None:
            try:
                self._cache_warmer.close()
            except Exception:
                pass
            self._cache_warmer = None
        if self._browser_manager is not None:
            try:
                self._browser_manager.close()
            except Exception:
                pass
            self._browser_manager = None

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
                elif hb.action == "tasks":
                    self._handle_tasks_cmd()
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

    # ─── Input history recall ───

    def _history_in_recall_mode(self) -> bool:
        """True when the user is currently navigating input history.

        Set by Up arrow on an empty buffer, cleared by submit, by an
        editing key, or by Esc. Used by the Up/Down handlers to know
        whether they should navigate history vs scroll the chat, and
        by the printable/backspace handlers to know whether they
        should snap out of recall mode before applying the edit.
        """
        return self._input_history_idx is not None

    def _history_add(self, text: str) -> None:
        """Append `text` to the in-memory history if it is non-empty
        and not an exact duplicate of the most recent entry.

        Caps the history at INPUT_HISTORY_MAX entries by dropping the
        oldest. Called from _submit before the input buffer is cleared.
        """
        text = text.rstrip()
        if not text:
            return
        if self._input_history and self._input_history[-1] == text:
            return  # dedupe consecutive identical entries
        self._input_history.append(text)
        if len(self._input_history) > INPUT_HISTORY_MAX:
            # Drop the oldest entries until we are back at the cap.
            self._input_history = self._input_history[-INPUT_HISTORY_MAX:]

    def _history_enter_recall(self) -> None:
        """Enter history recall mode from the empty-buffer state.

        Saves whatever is currently in the input buffer (the
        in-progress draft, possibly empty) so Down-past-newest can
        restore it later. Loads the most recent history entry into
        the buffer and points the recall cursor at it.
        """
        if not self._input_history:
            return  # nothing to recall
        self._input_history_draft = self.input_buffer
        self._input_history_idx = len(self._input_history) - 1
        self.input_buffer = self._input_history[self._input_history_idx]
        # Reset autocomplete state — the recalled text is data, not
        # an interactive command-typing session.
        self._autocomplete_selected = 0
        self._autocomplete_dismissed = True

    def _history_recall_older(self) -> None:
        """Move the recall cursor one entry older. No-op at the top.

        Called from the Up arrow handler when already in recall mode.
        """
        if self._input_history_idx is None:
            return
        if self._input_history_idx > 0:
            self._input_history_idx -= 1
            self.input_buffer = self._input_history[self._input_history_idx]

    def _history_recall_newer(self) -> None:
        """Move the recall cursor one entry newer.

        If we step past the newest entry, exit recall mode and
        restore the saved draft (which may be the empty string).
        Called from the Down arrow handler when already in recall
        mode.
        """
        if self._input_history_idx is None:
            return
        if self._input_history_idx < len(self._input_history) - 1:
            self._input_history_idx += 1
            self.input_buffer = self._input_history[self._input_history_idx]
        else:
            # Past the newest entry — restore the draft and exit recall
            self._history_exit_recall(restore_draft=True)

    def _history_exit_recall(self, *, restore_draft: bool = False) -> None:
        """Exit recall mode without losing the buffer.

        With restore_draft=True, restores the in-progress draft that
        was saved when recall mode was entered. With restore_draft=False
        (the default), keeps whatever is currently in the buffer so
        the user can edit the recalled text directly.
        """
        if restore_draft:
            self.input_buffer = self._input_history_draft
        self._input_history_idx = None
        self._input_history_draft = ""

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
                    self._search_query, _ = delete_prev_grapheme(
                        self._search_query,
                        len(self._search_query),
                    )
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
            if self._has_active_subagent_tasks():
                self.messages.append(_Message(
                    "successor",
                    "wait for background subagent tasks to finish before opening /config.",
                    synthetic=True,
                ))
                return
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
        # Up/Down arrow key dispatch is layered:
        #   1. autocomplete dropdown open → navigate dropdown selection
        #   2. already in input history recall mode → navigate history
        #   3. empty input buffer + history exists → ENTER recall mode
        #   4. otherwise → scroll the chat by one line
        # That priority lets bash users hit Up to recall when they
        # have nothing typed, scroll the chat when they have a draft
        # in flight, and never accidentally clobber typed input.
        if event.key == Key.UP:
            if self._autocomplete_active():
                self._autocomplete_move(-1)
            elif self._history_in_recall_mode():
                self._history_recall_older()
            elif not self.input_buffer and self._input_history:
                self._history_enter_recall()
            else:
                self._scroll_lines(1)
            return
        if event.key == Key.DOWN:
            if self._autocomplete_active():
                self._autocomplete_move(1)
            elif self._history_in_recall_mode():
                self._history_recall_newer()
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
                # If this was an autocompact deferral, clear the
                # pending-resume flag too — the user explicitly
                # cancelled, so don't continue the deferred turn.
                # The user message stays in self.messages; they can
                # press Enter again or modify it.
                self._pending_agent_turn_after_compact = False
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
                # If we are mid-recall, exit recall mode FIRST so the
                # user is editing the recalled text as a normal draft,
                # not a frozen history snapshot. The buffer keeps its
                # current contents, the recall cursor is dropped.
                if self._history_in_recall_mode():
                    self._history_exit_recall(restore_draft=False)
                self.input_buffer, _ = delete_prev_grapheme(
                    self.input_buffer,
                    len(self.input_buffer),
                )
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
            # Esc in history recall mode: bail out of recall and put
            # back whatever in-progress draft was on screen before
            # the user pressed Up. This is the "I changed my mind"
            # escape hatch.
            if self._history_in_recall_mode():
                self._history_exit_recall(restore_draft=True)
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
                # If we are mid-recall, exit recall mode FIRST so the
                # user is editing the recalled text as a normal draft,
                # not appending to a frozen history snapshot.
                if self._history_in_recall_mode():
                    self._history_exit_recall(restore_draft=False)
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

        if self._browser_manager is not None:
            try:
                self._browser_manager.close()
            except Exception:
                pass
            self._browser_manager = None

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

        # Queue-width changes apply once the manager is idle. If
        # background tasks are active, keep the old scheduler shape
        # until they finish; future profile switches can retry.
        self._subagent_manager.reconfigure(
            max_model_tasks=new_profile.subagents.effective_max_model_tasks(
                self.client
            ),
        )

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
        self._config.setdefault("autorecord", True)
        save_chat_config(self._config)

    def _set_autorecord(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if bool(self._config.get("autorecord", True)) == enabled:
            return
        self._config["autorecord"] = enabled
        save_chat_config(self._config)

    def _open_viewer_path(self, viewer_path) -> bool:
        import webbrowser

        try:
            return bool(webbrowser.open(viewer_path.resolve().as_uri()))
        except Exception:
            return False

    def _open_playback_from_chat(self, arg_text: str) -> None:
        from .playback import prepare_recording_viewer

        arg = arg_text.strip()
        lower = arg.lower()
        if lower in {"", "current"} and hasattr(self._recorder, "refresh_viewer"):
            front = getattr(self, "_front", None)
            if front is not None and hasattr(self._recorder, "capture_frame"):
                try:
                    self._recorder.capture_frame(front, chat=self, force=True)
                except Exception:
                    pass
            try:
                self._recorder.refresh_viewer(trace_path=self.session_trace_path)
            except Exception as exc:
                self.messages.append(
                    _Message(
                        "successor",
                        f"could not refresh the current session reviewer: {type(exc).__name__}: {exc}",
                        synthetic=True,
                    )
                )
                return
            viewer_path = getattr(self._recorder, "viewer_path", None)
            if viewer_path is None:
                self.messages.append(
                    _Message(
                        "successor",
                        "current session recorder has no viewer path.",
                        synthetic=True,
                    )
                )
                return
            opened = self._open_viewer_path(viewer_path)
            state = "opened" if opened else "prepared"
            self.messages.append(
                _Message(
                    "successor",
                    f"{state} current session reviewer at {viewer_path}.",
                    synthetic=True,
                )
            )
            return

        target = None if lower in {"", "latest"} else arg
        try:
            viewer_path, _bundle_root, is_library = prepare_recording_viewer(
                target,
                library=lower == "recordings",
            )
        except FileNotFoundError as exc:
            self.messages.append(
                _Message("successor", str(exc), synthetic=True)
            )
            return
        except Exception as exc:
            self.messages.append(
                _Message(
                    "successor",
                    f"could not prepare playback viewer: {type(exc).__name__}: {exc}",
                    synthetic=True,
                )
            )
            return
        opened = self._open_viewer_path(viewer_path)
        label = "recordings manager" if is_library else "recording reviewer"
        state = "opened" if opened else "prepared"
        self.messages.append(
            _Message(
                "successor",
                f"{state} {label} at {viewer_path}.",
                synthetic=True,
            )
        )

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

    # ─── Background subagents ───

    def _subagent_counts(self) -> SubagentTaskCounts:
        return self._subagent_manager.counts()

    def _has_active_subagent_tasks(self) -> bool:
        return self._subagent_manager.has_active_tasks()

    def _detect_client_runtime_capabilities(self) -> object | None:
        detect = getattr(self.client, "detect_runtime_capabilities", None)
        if not callable(detect):
            return None
        try:
            return detect()
        except Exception:
            return None

    def _tool_working_directory(self) -> str:
        bash_cfg = resolve_bash_config(self.profile)
        return bash_cfg.working_directory or os.getcwd()

    def _enabled_tools_for_turn(self) -> list[str]:
        """Runtime-enabled native tools for the current turn.

        Profiles can remember tool toggles that are temporarily unusable
        at runtime, like `browser` when Playwright is not installed.
        Filter those here so the model only sees tools the current chat
        can actually execute.
        """
        enabled_tools = list(filter_known(self.profile.tools or ()))
        if (
            not self.profile.subagents.enabled
            or not self.profile.subagents.notify_on_finish
        ):
            enabled_tools = [name for name in enabled_tools if name != "subagent"]
        if "browser" in enabled_tools:
            browser_cfg = resolve_browser_config(self.profile)
            status = browser_runtime_status(self.profile.name, browser_cfg)
            if not status.package_available:
                enabled_tools = [name for name in enabled_tools if name != "browser"]
        if "vision" in enabled_tools:
            vision_cfg = resolve_vision_config(self.profile)
            status = vision_runtime_status(vision_cfg, client=self.client)
            if not status.tool_available:
                enabled_tools = [name for name in enabled_tools if name != "vision"]
        if (enabled_tools or self._task_ledger.has_items()) and "task" not in enabled_tools:
            enabled_tools.append("task")
        if (enabled_tools or self._verification_ledger.has_items()) and "verify" not in enabled_tools:
            enabled_tools.append("verify")
        if (enabled_tools or self._runbook.has_state()) and "runbook" not in enabled_tools:
            enabled_tools.append("runbook")
        if self._enabled_skills_for_turn(enabled_tools) and "skill" not in enabled_tools:
            enabled_tools.append("skill")
        return enabled_tools

    def _enabled_skills_for_turn(
        self,
        enabled_tools: list[str] | tuple[str, ...] | None = None,
    ) -> list[Skill]:
        """Runtime-usable skills for this turn.

        Profile skills are opt-in. A skill only becomes model-visible when:

        1. the profile lists it
        2. the skill file exists
        3. every tool named in `allowed_tools` is available this turn
        """
        if enabled_tools is None:
            tools = list(filter_known(self.profile.tools or ()))
            if (
                not self.profile.subagents.enabled
                or not self.profile.subagents.notify_on_finish
            ):
                tools = [name for name in tools if name != "subagent"]
            if "browser" in tools:
                browser_cfg = resolve_browser_config(self.profile)
                status = browser_runtime_status(self.profile.name, browser_cfg)
                if not status.package_available:
                    tools = [name for name in tools if name != "browser"]
            if "vision" in tools:
                vision_cfg = resolve_vision_config(self.profile)
                status = vision_runtime_status(vision_cfg, client=self.client)
                if not status.tool_available:
                    tools = [name for name in tools if name != "vision"]
        else:
            tools = list(enabled_tools)
        return enabled_profile_skills(
            self.profile.skills,
            enabled_tools=tools,
        )

    def _skill_already_loaded(self, skill_name: str) -> bool:
        target = skill_name.strip().lower()
        if not target:
            return False
        for msg in self.messages:
            card = msg.tool_card
            if card is None or card.tool_name != "skill":
                continue
            loaded = str(card.tool_arguments.get("skill") or "").strip().lower()
            if loaded == target:
                return True
        return False

    def _latest_real_user_text(self) -> str:
        for msg in reversed(self.messages):
            if msg.role != "user":
                continue
            if msg.synthetic or _message_has_tool_artifact(msg):
                continue
            return str(msg.raw_text or "")
        return ""

    def _browser_verification_context_text(self) -> str:
        active_task = self._task_ledger.in_progress_task()
        active_task_text = active_task.active_form if active_task is not None else ""
        active_verification = self._verification_ledger.in_progress_item()
        if active_verification is not None:
            active_task_text = " ".join(
                part
                for part in (
                    active_task_text,
                    active_verification.claim,
                    active_verification.evidence,
                )
                if part
            )
        if self._runbook.state is not None:
            active_task_text = " ".join(
                part
                for part in (
                    active_task_text,
                    self._runbook.state.objective,
                    self._runbook.state.active_hypothesis,
                )
                if part
            )
        return active_task_text

    def _refresh_browser_verification_mode(self) -> None:
        active_task_text = self._browser_verification_context_text()
        active, reason = classify_browser_verification(
            latest_user_text=self._latest_real_user_text(),
            active_task_text=active_task_text,
            browser_verifier_loaded=self._skill_already_loaded("browser-verifier"),
        )
        changed = (
            active != self._browser_verification_active
            or reason != self._browser_verification_reason
        )
        self._browser_verification_active = active
        self._browser_verification_reason = reason
        if changed:
            self._trace_event(
                "browser_verification_mode",
                turn=self._agent_turn,
                active=active,
                reason=reason,
                active_task=active_task_text,
                latest_user_excerpt=_trace_clip_text(
                    self._latest_real_user_text(),
                    limit=240,
                ),
            )

    def _emit_progress_summary(
        self,
        text: str,
        *,
        source: str,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        cleaned = " ".join(str(text or "").split()).strip()
        detail = dict(detail or {})
        if not cleaned:
            self._trace_event(
                "progress_summary_skipped",
                turn=self._agent_turn,
                source=source,
                reason="empty",
                **detail,
            )
            return False
        if cleaned == self._last_progress_summary:
            self._trace_event(
                "progress_summary_skipped",
                turn=self._agent_turn,
                source=source,
                reason="duplicate",
                text=cleaned,
                **detail,
            )
            return False
        self._last_progress_summary = cleaned
        now = time.monotonic()
        self._recent_progress_summaries.append((now, cleaned))
        self._recent_progress_summaries = [
            item for item in self._recent_progress_summaries
            if now - item[0] <= 300.0
        ][-8:]
        self.messages.append(_Message("successor", cleaned, synthetic=True))
        self._trace_event(
            "progress_summary_emitted",
            turn=self._agent_turn,
            source=source,
            text=cleaned,
            **detail,
        )
        if self._auto_scroll:
            self._scroll_to_bottom()
        return True

    def _handle_browser_verification_result(
        self,
        card: ToolCard,
        metadata: dict[str, Any],
    ) -> None:
        self._tool_runtime.handle_browser_verification_result(card, metadata)

    def _emit_completed_tool_batch_progress(
        self,
        completed: list[tuple[str, ToolCard, dict[str, Any]]],
    ) -> None:
        self._tool_runtime.emit_completed_tool_batch_progress(completed)

    def _browser_manager_for_profile(self) -> PlaywrightBrowserManager:
        if self._browser_manager is not None:
            return self._browser_manager
        cfg = resolve_browser_config(self.profile)
        self._browser_manager = PlaywrightBrowserManager(
            profile_name=self.profile.name,
            config=cfg,
        )
        return self._browser_manager

    def _native_tool_call_failure_message(
        self,
        tool_call: dict[str, Any],
        *,
        finish_reason: str = "stop",
        finish_reason_reported: bool = True,
    ) -> str:
        return _native_tool_call_failure_message(
            tool_call,
            finish_reason=finish_reason,
            finish_reason_reported=finish_reason_reported,
        )

    def _browser_runtime_status(self, browser_cfg: Any) -> Any:
        return browser_runtime_status(self.profile.name, browser_cfg)

    def _run_browser_action(
        self,
        arguments: dict[str, Any],
        *,
        manager: Any,
        progress: Any,
    ) -> Any:
        return run_browser_action(arguments, manager=manager, progress=progress)

    def _run_holonet(self, route: Any, cfg: Any, progress: Any) -> Any:
        return run_holonet(route, cfg, progress)

    def _vision_runtime_status(self, vision_cfg: Any) -> Any:
        return vision_runtime_status(vision_cfg, client=self.client)

    def _run_vision_analysis(
        self,
        arguments: dict[str, Any],
        vision_cfg: Any,
        *,
        progress: Any,
    ) -> Any:
        return run_vision_analysis(
            arguments,
            vision_cfg,
            client=self.client,
            progress=progress,
        )

    def _subagent_scheduler_summary(self) -> str:
        cfg = self.profile.subagents
        lanes = cfg.effective_max_model_tasks(self.client)
        if cfg.strategy == "serial":
            noun = "lane" if lanes == 1 else "lanes"
            return f"serial, {lanes} model {noun}"
        if cfg.strategy == "manual":
            noun = "lane" if lanes == 1 else "lanes"
            return f"manual, {lanes} model {noun}"
        capabilities = self._detect_client_runtime_capabilities()
        total_slots = getattr(capabilities, "total_slots", None)
        if isinstance(total_slots, int) and total_slots > 0:
            noun = "lane" if lanes == 1 else "lanes"
            return (
                f"llama slots, {lanes} background {noun} from "
                f"{total_slots} total slots"
            )
        noun = "lane" if lanes == 1 else "lanes"
        return f"llama slots, fallback to {lanes} model {noun}"

    def _subagent_context_snapshot(self) -> list[dict[str, object]]:
        """Snapshot the current chat context for a child task.

        Mirrors the API-facing history rules: real user/assistant
        turns, summaries, and tool cards survive; synthetic glue
        messages do not.
        """
        out: list[dict[str, object]] = []
        for msg in self._api_ordered_messages():
            if msg.is_boundary:
                continue
            if msg.running_tool is not None:
                continue
            if msg.synthetic and not _message_has_tool_artifact(msg) and not msg.is_summary:
                continue
            out.append({
                "role": msg.role,
                "content": msg.raw_text,
                "display_text": msg.display_text,
                "synthetic": msg.synthetic,
                "tool_card": msg.tool_card,
                "subagent_card": msg.subagent_card,
                "is_summary": msg.is_summary,
                "api_role_override": msg.api_role_override,
            })
        return out

    def _handle_fork_cmd(self, directive: str) -> None:
        cfg = self.profile.subagents
        if not cfg.enabled:
            self.messages.append(_Message(
                "successor",
                "subagents are disabled for this profile. Enable them in /config, under the subagents section.",
                synthetic=True,
            ))
            return
        if self._stream is not None or self._running_tools or self._compaction_worker is not None:
            self.messages.append(_Message(
                "successor",
                "wait for the current turn to settle before forking a subagent.",
                synthetic=True,
            ))
            return
        task = self._subagent_manager.spawn_fork(
            directive=directive,
            name="",
            context_snapshot=self._subagent_context_snapshot(),
            profile=self.profile,
            config=cfg,
        )
        note = "Use /tasks to inspect progress."
        if not cfg.notify_on_finish:
            note = (
                "Notifications are off for this profile, so use /tasks "
                "to inspect progress."
            )
        self.messages.append(_Message(
            "successor",
            f"forked {task.task_id}, queued. {note}",
            synthetic=True,
        ))

    def _format_task_snapshot(self, task: SubagentTaskSnapshot) -> list[str]:
        elapsed_s = int(task.elapsed_s)
        mm, ss = divmod(elapsed_s, 60)
        directive = " ".join(task.directive.split())
        if len(directive) > 72:
            directive = directive[:71].rstrip() + "…"
        label = f" {task.name}" if task.name else ""
        kind = "verify" if task.role == "verification" else "worker"
        header = (
            f"{task.task_id}{label}  {kind:<6} {task.status:<9} {mm:02d}:{ss:02d}  "
            f"{directive}"
        )
        lines = [header]
        if task.result_excerpt:
            lines.append(f"result: {task.result_excerpt}")
        if task.error:
            lines.append(f"error: {task.error}")
        lines.append(f"transcript: {task.transcript_path}")
        return lines

    def _handle_tasks_cmd(self) -> None:
        tasks = self._subagent_manager.snapshots()
        if not tasks:
            self.messages.append(_Message(
                "successor",
                "no background subagent tasks yet. Use /fork <directive> to spawn one.",
                synthetic=True,
            ))
            return
        counts = self._subagent_counts()
        lines = [
            f"subagent tasks: {counts.active} active, {counts.total} total",
            f"scheduler: {self._subagent_scheduler_summary()}",
            "",
        ]
        for idx, task in enumerate(tasks):
            if idx > 0:
                lines.append("")
            lines.extend(self._format_task_snapshot(task))
        self.messages.append(_Message(
            "successor",
            "\n".join(lines),
            synthetic=True,
        ))

    def _handle_task_cancel_cmd(self, arg: str) -> None:
        target = arg.strip()
        if not target:
            self.messages.append(_Message(
                "successor",
                "usage: /task-cancel <task-id|all>",
                synthetic=True,
            ))
            return
        cancelled = self._subagent_manager.cancel(target)
        if cancelled == 0:
            self.messages.append(_Message(
                "successor",
                f"no queued or running task matched '{target}'.",
                synthetic=True,
            ))
            return
        noun = "task" if cancelled == 1 else "tasks"
        self.messages.append(_Message(
            "successor",
            f"cancel requested for {cancelled} {noun}.",
            synthetic=True,
        ))

    def _pump_subagent_notifications(self) -> None:
        notes = self._subagent_manager.drain_notifications()
        if not notes:
            return
        updates: list[ProgressUpdate] = []
        for note in notes:
            self.messages.append(_Message(
                "successor",
                build_notification_payload(note.task),
                api_role_override="user",
                display_text=build_notification_display(note.task),
            ))
            update = summarize_subagent_completion(note.task)
            if update is not None:
                updates.append(update)
        if len(updates) > 1:
            summary = combine_progress_updates(updates)
            if summary:
                self._emit_progress_summary(
                    summary,
                    source="subagent",
                    detail={"notification_count": len(notes)},
                )
        else:
            self._trace_event(
                "progress_summary_skipped",
                turn=self._agent_turn,
                source="subagent",
                reason="single_notification_already_visible",
                notification_count=len(notes),
            )
        if (
            notes
            and self._agent_turn > 0
            and self._task_ledger.has_in_progress()
            and not self._subagent_continue_nudged_this_turn
            and self._stream is None
            and not self._running_tools
            and not self._pending_continuation
        ):
            note = notes[-1].task
            active = self._task_ledger.in_progress_task()
            summary = note.result_excerpt or note.error or note.status
            self._subagent_continue_nudged_this_turn = True
            self._subagent_continue_nudge = (
                "A background subagent just finished while a session task is still "
                f"`in_progress` ({active.active_form if active else 'active task'}). "
                f"Use the new subagent result before you decide the next step. Latest "
                f"subagent result: `{summary}`. If that resolves the active task, "
                "update the task ledger before you stop."
            )
            self._trace_event(
                "subagent_followup_nudge",
                turn=self._agent_turn,
                task_id=note.task_id,
                task_name=note.name,
                status=note.status,
                active_task=active.active_form if active else "",
                result_excerpt=summary,
            )
            self._begin_agent_turn()
            return
        if self._auto_scroll:
            self._scroll_to_bottom()

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
        self._agent_loop.submit()

    def _begin_agent_turn(self) -> None:
        self._agent_loop.begin_agent_turn()

    def _build_api_messages_native(self, sys_prompt: str) -> list[dict]:
        return self._agent_loop.build_api_messages_native(sys_prompt)

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
            if msg.synthetic and not _message_has_tool_artifact(msg) and not msg.is_summary:
                # Skip non-tool, non-summary synthetic messages
                # (greetings, error notes) — they were never the model's voice
                continue
            # Each non-tool user message starts a new round
            if _api_role_for_message(msg) == "user" and not _message_has_tool_artifact(msg):
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
            agent_role = _api_role_for_message(msg)
            log.append_to_current_round(LogMessage(
                role=agent_role,
                content=msg.raw_text or "",
                tool_card=_message_tool_artifact(msg),
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
                    "tool",
                    "",
                    tool_card=m.tool_card if isinstance(m.tool_card, ToolCard) else None,
                    subagent_card=(
                        m.tool_card if isinstance(m.tool_card, SubagentToolCard) else None
                    ),
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
        """Build a ContextBudget from the resolved context window AND
        the active profile's CompactionConfig.

        Window comes from _resolve_context_window() which consults the
        profile override first, then probes the provider, then falls
        back to the hardcoded default. Headroom buffers come from
        `self.profile.compaction.buffers_for_window(window)` which
        applies the configured percentages with the configured floors.

        This is the SOLE seam between profile configuration and the
        runtime budget. Both the /budget command and the loop's
        autocompact gate read through this function, so changing a
        profile's compaction config and re-resolving the budget is
        the only thing needed for the new thresholds to take effect.
        """
        window = self._resolve_context_window()
        cfg = self.profile.compaction
        warning_buf, autocompact_buf, blocking_buf = cfg.buffers_for_window(window)
        return ContextBudget(
            window=window,
            warning_buffer=warning_buf,
            autocompact_buffer=autocompact_buf,
            blocking_buffer=blocking_buf,
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
        artifact = _message_tool_artifact(msg)
        if isinstance(artifact, ToolCard):
            card = artifact
            text = f"$ {card.raw_command}"
            if card.output:
                text += "\n" + card.output
        elif isinstance(artifact, SubagentToolCard):
            text = artifact.spawn_result
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

    def _check_and_maybe_defer_for_autocompact(self) -> bool:
        """Decide whether the upcoming agent turn should be preceded
        by an automatic compaction.

        Returns True if the turn was deferred (compaction worker
        spawned, `_pending_agent_turn_after_compact` set). The caller
        must return immediately so the worker has the floor.

        Returns False if the turn should proceed normally.

        Decision rules (all must be true to defer):
          1. The active profile's `compaction.enabled` is True.
          2. No compaction worker is already running. (If one is,
             we're either in mid-compact or in the deferred-resume
             window; either way the autocompact gate is satisfied.)
          3. We have NOT already attempted autocompact for this
             user message. The `_autocompact_attempted_this_turn`
             flag (reset in `_submit`) prevents the loop "compact
             → still over → compact again" if a single compaction
             didn't shrink the log enough.
          4. The current token count is at or above the autocompact
             threshold derived from `_agent_budget()`.
          5. The log has at least `MIN_ROUNDS_TO_COMPACT` rounds.
             A handful of rounds isn't worth compacting.

        On a deferral, this method spawns the compaction worker via
        the same path as the manual /compact command (reusing the
        animation + worker plumbing) but tagged with reason="auto".
        """
        # Rule 1: profile's compaction must be enabled
        if not self.profile.compaction.enabled:
            return False

        # Rule 2: no in-flight worker
        if self._compaction_worker is not None:
            return False

        # Rule 3: don't loop on the same user message
        if self._autocompact_attempted_this_turn:
            return False

        # Rule 4 + 5 require building the budget and counting tokens.
        # These are cheap (cached) so the common-case "no compact
        # needed" path adds essentially zero overhead per turn.
        from .agent.compact import MIN_ROUNDS_TO_COMPACT
        counter = self._agent_token_counter()
        log = self._to_agent_log()
        if log.round_count < MIN_ROUNDS_TO_COMPACT:
            return False

        budget = self._agent_budget()
        used = counter.count_log(log)
        if not budget.should_autocompact(used):
            return False

        # All gates passed — fire compaction. Mirrors `_handle_compact_cmd`
        # but with reason="auto" and the deferred-resume flag set so
        # `_poll_compaction_worker` knows to re-enter `_begin_agent_turn`
        # after success.
        self._autocompact_attempted_this_turn = True
        self._pending_agent_turn_after_compact = True
        self._spawn_compaction_worker(log=log, counter=counter, reason="auto")
        return True

    def _spawn_compaction_worker(
        self,
        *,
        log,           # agent.MessageLog
        counter,       # TokenCounter
        reason: str,   # "manual" | "auto"
    ) -> None:
        """Spawn the compaction worker thread + arm the animation.

        Shared between manual /compact and the autocompact gate. The
        only difference between callers is the reason tag (which gets
        attached to the boundary marker for diagnostics) and what
        happens after the worker returns — manual just commits the
        new log; auto re-enters `_begin_agent_turn`.
        """
        pre_tokens = counter.count_log(log)
        keep_n = min(
            self.profile.compaction.keep_recent_rounds,
            max(1, log.round_count // 2),
        )
        rounds_to_summarize = log.round_count - keep_n

        # Snapshot for the fold animation
        snapshot = list(self.messages)
        snapshot_count = len(snapshot)

        self._compaction_anim = _CompactionAnimation(
            started_at=time.monotonic(),
            pre_compact_snapshot=snapshot,
            pre_compact_count=snapshot_count,
            boundary=None,
            summary_text="",
            reason=reason,
            pre_compact_tokens=pre_tokens,
            rounds_summarized=rounds_to_summarize,
        )

        self._compaction_worker = _CompactionWorker(
            log=log,
            client=self.client,
            counter=counter,
            reason=reason,
        )
        self._compaction_worker.start()
        self._scroll_to_bottom()

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

        from .agent.compact import MIN_ROUNDS_TO_COMPACT
        counter = self._agent_token_counter()
        log = self._to_agent_log()
        if log.round_count < MIN_ROUNDS_TO_COMPACT:
            self.messages.append(_Message(
                "successor",
                f"need at least {MIN_ROUNDS_TO_COMPACT} rounds to compact, "
                f"have {log.round_count}. Run /burn first to inflate the context.",
                synthetic=True,
            ))
            return

        # Spawn the worker via the shared helper. The autocompact gate
        # uses the same path with reason="auto".
        self._spawn_compaction_worker(
            log=log,
            counter=counter,
            reason="manual",
        )

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

        # Capture the deferred-resume flag BEFORE clearing it. The
        # flag is consumed exactly once: either we resume the agent
        # turn after a successful compaction, or we resume after a
        # FAILED compaction (the user is still waiting on their
        # message — reactive PTL recovery in the streaming layer
        # will catch the failure case downstream).
        was_pending_resume = self._pending_agent_turn_after_compact
        self._pending_agent_turn_after_compact = False

        if result.error is not None:
            # Failure — drop the animation and report
            self._compaction_anim = None
            self.messages.append(_Message(
                "successor",
                f"compaction failed: {result.error}",
                synthetic=True,
            ))
            # If this compaction was an autocompact deferral, the
            # user's message is still in self.messages waiting for a
            # response. Try to send it anyway — reactive PTL recovery
            # may save us, or the user gets a clear error from the
            # API. The `_autocompact_attempted_this_turn` guard
            # prevents the gate from re-firing infinitely.
            if was_pending_resume:
                self._begin_agent_turn()
            return

        # Success — apply the new log + transition animation to materialize
        if self._compaction_anim is None:
            # The animation was somehow cleared (e.g. cancel) — apply
            # the log silently and skip the visible transition
            self._from_agent_log(result.new_log, boundary_meta=result.boundary)
            if was_pending_resume:
                self._begin_agent_turn()
            return

        self._from_agent_log(result.new_log, boundary_meta=result.boundary)
        # Update the animation in place — the dataclass is mutable
        # because of slots=True (not frozen). The materialize phase
        # is computed relative to result_arrived_at.
        self._compaction_anim.boundary = result.boundary
        self._compaction_anim.summary_text = result.boundary.summary_text
        self._compaction_anim.result_arrived_at = time.monotonic()

        # If this compaction was an autocompact deferral, the user's
        # message is still in self.messages waiting for a response.
        # Re-enter `_begin_agent_turn` so the model gets the prompt
        # against the freshly-compacted history. The animation
        # continues painting underneath while the new stream opens.
        if was_pending_resume:
            self._begin_agent_turn()

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
        self._agent_loop.pump_stream()

    def _format_stream_error(self, raw: str) -> str:
        return self._agent_loop.format_stream_error(raw)

    def _spawn_bash_runner(
        self,
        command: str,
        *,
        bash_cfg: BashConfig,
        tool_call_id: str | None = None,
    ) -> bool:
        return self._tool_runtime.spawn_bash_runner(
            command,
            bash_cfg=bash_cfg,
            tool_call_id=tool_call_id,
        )

    def _dispatch_streamed_bash_blocks(self, blocks: list[str]) -> bool:
        return self._tool_runtime.dispatch_streamed_bash_blocks(blocks)

    def _spawn_subagent_task(
        self,
        prompt: str,
        *,
        name: str = "",
        tool_call_id: str | None = None,
    ) -> bool:
        return self._tool_runtime.spawn_subagent_task(
            prompt,
            name=name,
            tool_call_id=tool_call_id,
        )

    def _tool_error_card(
        self,
        *,
        tool_name: str,
        verb: str,
        raw_command: str,
        tool_call_id: str,
        params: tuple[tuple[str, str], ...],
        tool_arguments: dict[str, Any],
        raw_label_prefix: str,
        message: str,
        risk: str = "safe",
    ) -> ToolCard:
        return self._tool_runtime.tool_error_card(
            tool_name=tool_name,
            verb=verb,
            raw_command=raw_command,
            tool_call_id=tool_call_id,
            params=params,
            tool_arguments=tool_arguments,
            raw_label_prefix=raw_label_prefix,
            message=message,
            risk=risk,
        )

    def _spawn_skill_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        return self._tool_runtime.spawn_skill_runner(
            arguments,
            tool_call_id=tool_call_id,
        )

    def _spawn_task_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        return self._tool_runtime.spawn_task_runner(
            arguments,
            tool_call_id=tool_call_id,
        )

    def _spawn_verify_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        return self._tool_runtime.spawn_verify_runner(
            arguments,
            tool_call_id=tool_call_id,
        )

    def _spawn_runbook_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        return self._tool_runtime.spawn_runbook_runner(
            arguments,
            tool_call_id=tool_call_id,
        )

    def _spawn_read_file_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        return self._tool_runtime.spawn_read_file_runner(
            arguments,
            tool_call_id=tool_call_id,
        )

    def _spawn_write_file_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        return self._tool_runtime.spawn_write_file_runner(
            arguments,
            tool_call_id=tool_call_id,
        )

    def _spawn_edit_file_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        return self._tool_runtime.spawn_edit_file_runner(
            arguments,
            tool_call_id=tool_call_id,
        )

    def _spawn_holonet_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        return self._tool_runtime.spawn_holonet_runner(
            arguments,
            tool_call_id=tool_call_id,
        )

    def _spawn_browser_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        return self._tool_runtime.spawn_browser_runner(
            arguments,
            tool_call_id=tool_call_id,
        )

    def _spawn_vision_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        return self._tool_runtime.spawn_vision_runner(
            arguments,
            tool_call_id=tool_call_id,
        )

    def _dispatch_native_tool_calls(
        self,
        tool_calls: list[dict],
        *,
        stream_finish_reason: str = "stop",
        stream_finish_reason_reported: bool = True,
    ) -> bool:
        return self._tool_runtime.dispatch_native_tool_calls(
            tool_calls,
            stream_finish_reason=stream_finish_reason,
            stream_finish_reason_reported=stream_finish_reason_reported,
        )

    def _pump_running_tools(self) -> None:
        self._tool_runtime.pump_running_tools()

    def _finalize_runner(
        self,
        msg: "_Message",
    ) -> tuple[str, ToolCard, dict[str, Any]] | None:
        return self._tool_runtime.finalize_runner(msg)

    def _cancel_running_tools(self) -> None:
        self._tool_runtime.cancel_running_tools()

    def _refusal_hint(
        self, exc: RefusedCommand, bash_cfg: BashConfig,
    ) -> str:
        return self._tool_runtime.refusal_hint(exc, bash_cfg)

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

        # Surface completion/failure notifications from background
        # subagent tasks. The tasks themselves keep running on their
        # own worker threads; the foreground chat only needs to drain
        # these lightweight terminal-state notices.
        self._pump_subagent_notifications()

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

        input_h = self._input_height(cols)
        layout = compute_chat_frame(rows, cols, input_h)

        # ─── Background ───
        fill_region(grid, 0, 0, cols, rows, style=Style(bg=theme.bg))

        # ─── Chat scroll area ───
        counts = self._subagent_counts()
        self._paint_chat_area(
            grid, layout.chat_top, layout.chat_bottom, cols, theme
        )

        header = build_header_plan(
            cols=cols,
            theme=theme,
            theme_name=self.theme.name,
            theme_icon=self.theme.icon,
            display_mode=self.display_mode,
            density_name=self.density.name,
            profile_name=self.profile.name,
            task_total=counts.total,
            task_active=counts.active,
            scroll_offset=self.scroll_offset,
            max_scroll=self._max_scroll(),
            stream_active=self._stream is not None,
        )
        for placement in header.placements:
            paint_text(
                grid,
                placement.text,
                placement.x,
                placement.y,
                style=placement.style,
            )
        self._hit_boxes.extend(header.hitboxes)

        # ─── Input area ───
        if layout.input_y >= 0 and layout.input_y < rows:
            self._paint_input(
                grid,
                layout.input_y,
                min(input_h, rows - layout.input_y),
                cols,
                theme,
            )

        # ─── Static footer (ctx bar) ───
        if 0 <= layout.static_y < rows:
            self._paint_static_footer(grid, layout.static_y, cols, theme)

        # ─── Slash command autocomplete dropdown ───
        # Painted LAST so it overlays the chat area cells just above the
        # input. The chat content underneath is temporarily hidden while
        # the dropdown is visible; closing it (Esc / submit / type a
        # space) restores everything on the next frame's diff.
        self._paint_autocomplete(grid, theme, layout.input_y)

        # ─── Help overlay ───
        # Painted EVEN LATER so it overlays everything else, including
        # the autocomplete dropdown. Centered modal with a fade-in.
        if self._help_open:
            self._paint_help_overlay(grid, theme)

        if self._recorder is not None and hasattr(self._recorder, "capture_frame"):
            try:
                self._recorder.capture_frame(grid, chat=self)
            except Exception:
                pass

    # ─── Region painters ───

    def _paint_chat_area(
        self,
        grid: Grid,
        top: int,
        bottom: int,
        width: int,
        theme: ThemeVariant,
    ) -> None:
        self._display_runtime.paint_chat_area(grid, top, bottom, width, theme)

    # ─── Empty-state hero panel ───

    def _is_empty_chat(self) -> bool:
        return self._display_runtime.is_empty_chat()

    def _has_intro_art(self) -> bool:
        return self._display_runtime.has_intro_art()

    def _resolve_intro_art(self):
        return self._display_runtime.resolve_intro_art()

    def _paint_empty_state(
        self,
        grid: Grid,
        top: int,
        bottom: int,
        width: int,
        theme: ThemeVariant,
    ) -> None:
        self._display_runtime.paint_empty_state(grid, top, bottom, width, theme)

    def _build_intro_panel_lines(self) -> list[tuple[str, str, bool, bool]]:
        return self._display_runtime.build_intro_panel_lines()

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
        self._display_runtime.paint_chat_row(grid, x, y, body_width, row, theme)

    # ─── Flat-line builders ───

    def _build_message_lines(
        self,
        body_width: int,
        theme: ThemeVariant,
    ) -> list[_RenderedRow]:
        return self._display_runtime.build_message_lines(body_width, theme)

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
        return self._display_runtime.build_rows_from_messages(
            messages,
            body_width,
            theme,
            global_fade_alpha=global_fade_alpha,
            anticipation_glow=anticipation_glow,
            anim_phase=anim_phase,
            anim_t=anim_t,
        )

    def _fade_prepainted_rows(
        self,
        rows: list[_RenderedRow],
        bg_color: int,
        toward_bg_amount: float,
    ) -> list[_RenderedRow]:
        return self._display_runtime.fade_prepainted_rows(
            rows,
            bg_color,
            toward_bg_amount,
        )

    def _render_tool_card_rows(
        self,
        msg: "_Message",
        body_width: int,
        theme: ThemeVariant,
    ) -> list[_RenderedRow]:
        return self._display_runtime.render_tool_card_rows(msg, body_width, theme)

    def _render_running_tool_card_rows(
        self,
        msg: "_Message",
        body_width: int,
        theme: ThemeVariant,
        runner: BashRunner,
    ) -> list[_RenderedRow]:
        return self._display_runtime.render_running_tool_card_rows(
            msg,
            body_width,
            theme,
            runner,
        )

    def _render_subagent_card_rows(
        self,
        msg: "_Message",
        body_width: int,
        theme: ThemeVariant,
    ) -> list[_RenderedRow]:
        return self._display_runtime.render_subagent_card_rows(msg, body_width, theme)

    def _render_md_lines_with_search(
        self,
        md_lines: list,
        msg_raw_text: str,
        matches: list[tuple[int, int, int]],
        prefix: str,
        base_color: int,
    ) -> list[_RenderedRow]:
        return self._display_runtime.render_md_lines_with_search(
            md_lines,
            msg_raw_text,
            matches,
            prefix,
            base_color,
        )

    def _highlight_spans(
        self,
        spans: list,
        query: str,
    ) -> list:
        return self._display_runtime.highlight_spans(spans, query)

    def _build_streaming_lines(
        self,
        body_width: int,
        theme: ThemeVariant,
    ) -> list[_RenderedRow]:
        return self._display_runtime.build_streaming_lines(body_width, theme)

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
        return self._display_runtime.streaming_tool_call_preview_rows(
            name=name,
            raw_arguments=raw_arguments,
            call_index=call_index,
            body_width=body_width,
            theme=theme,
            spinner=spinner,
        )

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
        cols = grid.cols
        if cols < 30 or input_y < 4:
            return

        if isinstance(state, _NameMode):
            self._paint_name_mode(grid, theme, input_y, state)
        elif isinstance(state, _ArgMode):
            self._paint_arg_mode(grid, theme, input_y, state)
        elif isinstance(state, _NoMatches):
            self._paint_no_matches(grid, theme, input_y, state)

    def _paint_name_mode(
        self,
        grid: Grid,
        theme: ThemeVariant,
        input_y: int,
        state: _NameMode,
    ) -> None:
        self._hit_boxes.extend(
            paint_name_mode_overlay(
                grid,
                theme,
                input_y,
                state,
                prompt_width=PROMPT_WIDTH,
            )
        )

    def _paint_arg_mode(
        self,
        grid: Grid,
        theme: ThemeVariant,
        input_y: int,
        state: _ArgMode,
    ) -> None:
        self._hit_boxes.extend(
            paint_arg_mode_overlay(
                grid,
                theme,
                input_y,
                state,
                prompt_width=PROMPT_WIDTH,
            )
        )

    def _paint_no_matches(
        self,
        grid: Grid,
        theme: ThemeVariant,
        input_y: int,
        state: _NoMatches,
    ) -> None:
        paint_no_matches_overlay(
            grid,
            theme,
            input_y,
            state,
            prompt_width=PROMPT_WIDTH,
        )

    # ─── Help overlay ───

    def _paint_help_overlay(self, grid: Grid, theme: Theme) -> None:
        sections = _HELP_SECTIONS + (_build_slash_command_help_section(),)
        paint_help_overlay_surface(
            grid,
            theme,
            opened_at=self._help_opened_at,
            sections=sections,
        )

    # ─── Static footer ───

    def _paint_static_footer(
        self,
        grid: Grid,
        y: int,
        width: int,
        theme: ThemeVariant,
    ) -> None:
        self._display_runtime.paint_static_footer(grid, y, width, theme)

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

        all_wrapped = self._input_lines_at_width(width)
        hidden_above = max(0, len(all_wrapped) - height)
        wrapped = all_wrapped[-height:] if hidden_above else all_wrapped
        paint_input_surface(
            grid,
            y,
            height,
            width,
            theme,
            wrapped_lines=wrapped,
            hidden_above=hidden_above,
            prompt=PROMPT,
            prompt_width=PROMPT_WIDTH,
            cursor_blink_hz=CURSOR_BLINK_HZ,
            ghost_text=self._compute_ghost_text(),
            stream_active=self._stream is not None,
        )

    def _paint_search_bar(
        self,
        grid: Grid,
        y: int,
        width: int,
        theme: ThemeVariant,
    ) -> None:
        paint_search_bar_surface(
            grid,
            y,
            width,
            theme,
            query_text=self._search_query,
            match_count=len(self._search_matches),
            focused_index=self._search_focused,
            cursor_blink_hz=CURSOR_BLINK_HZ,
        )

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
