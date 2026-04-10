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

import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import load_chat_config, save_chat_config
from .file_tools import (
    FileReadTracker,
    FileReadStateEntry,
    build_file_tool_recovery_nudge,
    edit_file_preview_card,
    normalize_file_path,
    note_non_read_tool_call,
    read_file_preview_card,
    run_edit_file,
    run_read_file,
    run_write_file,
    write_file_preview_card,
)
from .graphemes import delete_prev_grapheme
from .playback import RecordingBundle, default_recording_bundle_dir
from .progress import (
    ProgressUpdate,
    combine_progress_updates,
    summarize_subagent_completion,
    summarize_tool_completion,
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
    get_profile,
    next_profile,
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
    DangerousCommandRefused,
    MutatingCommandRefused,
    RefusedCommand,
    ToolCard,
    resolve_bash_config,
)
from .bash.runner import (
    BashRunner,
    RunnerErrored,
    RunnerStarted,
)
from .tool_runner import CallableToolRunner
from .tools_registry import (
    build_model_tool_guidance,
    build_native_tool_schemas,
    filter_known,
    tool_label,
)
from .skills import (
    build_skill_card_output,
    build_skill_discovery_section,
    build_skill_hint_section,
    build_skill_reuse_result,
    build_skill_tool_result,
    enabled_profile_skills,
)
from .tasks import (
    SessionTaskLedger,
    TaskLedgerError,
    build_task_card_output,
    build_task_continue_nudge,
    build_task_execution_guidance,
    build_task_prompt_section,
    build_task_tool_result,
    parse_task_items,
    task_items_to_payload,
)
from .verification_contract import (
    VerificationContractError,
    VerificationLedger,
    build_assertions_artifact,
    build_verification_card_output,
    build_verification_execution_guidance,
    build_verification_prompt_section,
    build_verification_tool_result,
    parse_verification_items,
    verification_items_to_payload,
)
from .runbook import (
    RunbookError,
    SessionRunbook,
    build_runbook_artifact,
    build_runbook_card_output,
    build_runbook_execution_guidance,
    build_runbook_prompt_section,
    build_runbook_tool_result,
    experiment_attempt_to_payload,
    parse_experiment_attempt,
    parse_runbook_state,
    runbook_state_to_payload,
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
    build_spawn_result_display,
    build_spawn_result_payload,
)
from .web import (
    PlaywrightBrowserManager,
    browser_preview_card,
    browser_runtime_status,
    holonet_preview_card,
    resolve_browser_config,
    resolve_holonet_config,
    resolve_vision_config,
    resolve_route as resolve_holonet_route,
    run_browser_action,
    run_holonet,
    run_vision_analysis,
    vision_preview_card,
    vision_runtime_status,
)
from .web.verification import (
    build_browser_verification_guidance,
    build_verification_nudge,
    classify_browser_verification,
)
from .session_trace import SessionTrace, clip_text as _trace_clip_text
from .render.markdown import (
    LaidOutLine,
    LaidOutSpan,
    PreparedMarkdown,
)
from .render.chat_frame import HitBox, compute_chat_frame
from .render.chat_header import build_header_plan
from .render.chat_input import (
    paint_input as paint_input_surface,
    paint_search_bar as paint_search_bar_surface,
)
from .render.chat_intro import paint_empty_state as paint_empty_state_surface
from .render.chat_overlays import (
    paint_arg_mode as paint_arg_mode_overlay,
    paint_help_overlay as paint_help_overlay_surface,
    paint_name_mode as paint_name_mode_overlay,
    paint_no_matches as paint_no_matches_overlay,
)
from .render.chat_rows import (
    RenderedRow,
    fade_prepainted_rows as fade_prepainted_chat_rows,
    highlight_spans as highlight_row_spans,
    paint_chat_row as paint_chat_scene_row,
    render_md_lines_with_search as render_markdown_rows_with_search,
    render_subagent_card_rows as render_subagent_chat_card_rows,
    render_running_tool_card_rows as render_running_chat_card_rows,
    render_tool_card_rows as render_tool_chat_card_rows,
)
from .render.chat_viewport import compute_viewport_decision
from .render.paint import fill_region, paint_text
from .render.terminal import Terminal
from .render.text import ease_out_cubic, hard_wrap, lerp_rgb
from .render.theme import (
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


ToolArtifact = ToolCard | SubagentToolCard


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
                        out.append("\n")
                        i += 2
                        continue
                    if nxt == "t":
                        out.append("\t")
                        i += 2
                        continue
                    if nxt == "r":
                        out.append("\r")
                        i += 2
                        continue
                    if nxt == '"':
                        out.append('"')
                        i += 2
                        continue
                    if nxt == "\\":
                        out.append("\\")
                        i += 2
                        continue
                    if nxt == "/":
                        out.append("/")
                        i += 2
                        continue
                    # Unknown escape — just emit the backslash and move on
                    out.append("\\")
                    i += 1
                    continue
                out.append(ch)
                i += 1
            return "".join(out)
    # Couldn't find the marker — show the raw JSON so the user at
    # least sees progress. Better than a blank preview.
    return raw_args


def _assistant_with_tool_calls(content: str, cards: list[ToolArtifact]) -> dict:
    """Build the assistant-message dict for an assistant turn that
    issued one or more tool calls.

    Each card becomes one entry in the `tool_calls` list with its
    call id, native tool name, and JSON arguments. This matches the
    OpenAI tool-call shape that Qwen's chat template renders into
    native `<tool_call>` blocks.
    """
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": card.tool_call_id,
                "type": "function",
                "function": {
                    "name": _tool_name_for_card(card),
                    "arguments": json.dumps(_tool_arguments_for_card(card)),
                },
            }
            for card in cards
        ],
    }


def _tool_card_content_for_api(card: ToolArtifact) -> str:
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
    if isinstance(card, SubagentToolCard):
        return card.spawn_result
    if card.api_content_override is not None:
        return card.api_content_override

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


def _extract_json_string_field(raw_args: str, field: str) -> str:
    """Best-effort extraction of one JSON string field from a partial blob."""
    if not raw_args:
        return ""
    markers = (f'"{field}":"', f'"{field}": "')
    for key_marker in markers:
        idx = raw_args.find(key_marker)
        if idx == -1:
            continue
        body = raw_args[idx + len(key_marker):]
        out: list[str] = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == '"':
                break
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                if nxt == "n":
                    out.append("\n")
                    i += 2
                    continue
                if nxt == "t":
                    out.append("\t")
                    i += 2
                    continue
                if nxt == "r":
                    out.append("\r")
                    i += 2
                    continue
                if nxt in ('"', "\\", "/"):
                    out.append(nxt)
                    i += 2
                    continue
                out.append("\\")
                i += 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    return raw_args


def _tool_preview_text(name: str, raw_args: str) -> str:
    if name == "bash":
        return _extract_command_tail(raw_args)
    if name == "task":
        content = _extract_json_string_field(raw_args, "content")
        return f"update tasks {content}" if content else "update tasks"
    if name == "skill":
        skill = _extract_json_string_field(raw_args, "skill")
        task = _extract_json_string_field(raw_args, "task")
        bits = [bit for bit in (skill, task) if bit]
        return " ".join(bits) if bits else raw_args
    if name == "subagent":
        return _extract_json_string_field(raw_args, "prompt")
    if name == "holonet":
        provider = _extract_json_string_field(raw_args, "provider")
        query = _extract_json_string_field(raw_args, "query")
        url = _extract_json_string_field(raw_args, "url")
        bits = [bit for bit in (provider, query or url) if bit]
        return " ".join(bits) if bits else raw_args
    if name == "browser":
        action = _extract_json_string_field(raw_args, "action")
        target = _extract_json_string_field(raw_args, "target")
        url = _extract_json_string_field(raw_args, "url")
        bits = [bit for bit in (action, target or url) if bit]
        return " ".join(bits) if bits else raw_args
    return raw_args


def _message_has_tool_artifact(msg: "_Message") -> bool:
    return msg.tool_card is not None or msg.subagent_card is not None


def _message_tool_artifact(msg: "_Message") -> ToolArtifact | None:
    if msg.tool_card is not None:
        return msg.tool_card
    return msg.subagent_card


def _api_role_for_message(msg: "_Message") -> str:
    if msg.api_role_override:
        return msg.api_role_override
    if msg.role == "successor":
        return "assistant"
    return msg.role


def _tool_name_for_card(card: ToolArtifact) -> str:
    return "subagent" if isinstance(card, SubagentToolCard) else card.tool_name


def _tool_arguments_for_card(card: ToolArtifact) -> dict[str, Any]:
    if isinstance(card, SubagentToolCard):
        payload = {"prompt": card.directive}
        if card.name:
            payload["name"] = card.name
        return payload
    if card.tool_arguments:
        return dict(card.tool_arguments)
    return {"command": card.raw_command}


def _find_last_user_excerpt(api_messages: list[dict[str, Any]]) -> str:
    for msg in reversed(api_messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return _trace_clip_text(content, limit=320)
    return ""


def _trace_tool_call_summary(tc: dict[str, Any]) -> dict[str, object]:
    name = str(tc.get("name") or "")
    args = tc.get("arguments") or {}
    entry: dict[str, object] = {
        "id": str(tc.get("id") or ""),
        "name": name,
    }
    raw_arguments = str(tc.get("raw_arguments") or "")
    parse_error = str(tc.get("arguments_parse_error") or "").strip()
    parse_error_pos = tc.get("arguments_parse_error_pos")
    if raw_arguments:
        entry["raw_arguments_len"] = len(raw_arguments)
    if parse_error:
        entry["arguments_parse_error"] = parse_error
        if isinstance(parse_error_pos, int):
            entry["arguments_parse_error_pos"] = parse_error_pos
    if name == "bash" and isinstance(args, dict):
        entry["command_excerpt"] = _trace_clip_text(
            str(args.get("command") or ""),
            limit=320,
        )
    elif name == "bash" and raw_arguments:
        entry["raw_arguments_excerpt"] = _trace_clip_text(raw_arguments, limit=320)
    elif name == "task" and isinstance(args, dict):
        items = args.get("items")
        if isinstance(items, list):
            entry["task_count"] = len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("status") or "").strip().lower() != "in_progress":
                    continue
                entry["active_task"] = _trace_clip_text(
                    str(item.get("active_form") or item.get("content") or ""),
                    limit=320,
                )
                break
    elif name == "verify" and isinstance(args, dict):
        items = args.get("items")
        if isinstance(items, list):
            entry["assertion_count"] = len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("status") or "").strip().lower() != "in_progress":
                    continue
                entry["active_claim"] = _trace_clip_text(
                    str(item.get("claim") or ""),
                    limit=320,
                )
                entry["active_evidence"] = _trace_clip_text(
                    str(item.get("evidence") or ""),
                    limit=320,
                )
                break
    elif name == "runbook" and isinstance(args, dict):
        if bool(args.get("clear")):
            entry["cleared"] = True
        objective = str(args.get("objective") or "").strip()
        if objective:
            entry["objective_excerpt"] = _trace_clip_text(objective, limit=320)
        hypothesis = str(args.get("active_hypothesis") or "").strip()
        if hypothesis:
            entry["active_hypothesis"] = _trace_clip_text(hypothesis, limit=320)
        status = str(args.get("status") or "").strip()
        if status:
            entry["status"] = status
        baseline_status = str(args.get("baseline_status") or "").strip()
        if baseline_status:
            entry["baseline_status"] = baseline_status
        evaluator = args.get("evaluator")
        if isinstance(evaluator, list):
            entry["evaluator_count"] = len(evaluator)
        attempt = args.get("attempt")
        if isinstance(attempt, dict):
            entry["attempt_decision"] = str(attempt.get("decision") or "")
            attempt_hypothesis = str(attempt.get("hypothesis") or "").strip()
            if attempt_hypothesis:
                entry["attempt_hypothesis"] = _trace_clip_text(
                    attempt_hypothesis,
                    limit=320,
                )
    elif name == "skill" and isinstance(args, dict):
        entry["skill_name"] = str(args.get("skill") or "")
        task = str(args.get("task") or "")
        if task:
            entry["task_excerpt"] = _trace_clip_text(task, limit=320)
    elif name == "subagent" and isinstance(args, dict):
        entry["prompt_excerpt"] = _trace_clip_text(
            str(args.get("prompt") or ""),
            limit=320,
        )
        label = str(args.get("name") or "")
        if label:
            entry["task_name"] = label
    elif name == "holonet" and isinstance(args, dict):
        entry["provider"] = str(args.get("provider") or "")
        entry["query_excerpt"] = _trace_clip_text(
            str(args.get("query") or args.get("url") or ""),
            limit=320,
        )
    elif name == "browser" and isinstance(args, dict):
        entry["action"] = str(args.get("action") or "")
        entry["target_excerpt"] = _trace_clip_text(
            str(args.get("target") or args.get("url") or ""),
            limit=320,
        )
    elif args:
        entry["arguments_excerpt"] = _trace_clip_text(str(args), limit=320)
    return entry


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
        if not self._browser_verification_active:
            return
        intervention = metadata.get("verification_intervention")
        if not isinstance(intervention, dict):
            return
        self._trace_event(
            "browser_verification_intervention",
            turn=self._agent_turn,
            reason=self._browser_verification_reason,
            kind=str(intervention.get("kind") or ""),
            action=str(card.tool_arguments.get("action") or ""),
            target=str(card.tool_arguments.get("target") or card.tool_arguments.get("url") or ""),
            recommended_action=str(intervention.get("recommended_action") or ""),
        )
        if self._verification_continue_nudged_this_turn:
            return
        nudge = build_verification_nudge(intervention)
        if not nudge:
            return
        self._verification_continue_nudged_this_turn = True
        self._verification_continue_nudge = nudge

    def _emit_completed_tool_batch_progress(
        self,
        completed: list[tuple[str, ToolCard, dict[str, Any]]],
    ) -> None:
        updates: list[ProgressUpdate] = []
        for tool_name, card, metadata in completed:
            if tool_name == "browser":
                self._handle_browser_verification_result(card, metadata)
            update = summarize_tool_completion(card, metadata=metadata)
            if update is not None:
                updates.append(update)
        summary = combine_progress_updates(updates)
        if summary is None:
            self._trace_event(
                "progress_summary_skipped",
                turn=self._agent_turn,
                source="tool_batch",
                reason="not_meaningful",
                tool_names=[tool_name for tool_name, _, _ in completed],
                update_count=len(updates),
            )
            return
        self._emit_progress_summary(
            summary,
            source="tool_batch",
            detail={
                "tool_names": [tool_name for tool_name, _, _ in completed],
                "update_count": len(updates),
            },
        )

    def _browser_manager_for_profile(self) -> PlaywrightBrowserManager:
        if self._browser_manager is not None:
            return self._browser_manager
        cfg = resolve_browser_config(self.profile)
        self._browser_manager = PlaywrightBrowserManager(
            profile_name=self.profile.name,
            config=cfg,
        )
        return self._browser_manager

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
        header = (
            f"{task.task_id}{label}  {task.status:<9} {mm:02d}:{ss:02d}  "
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
        text = self.input_buffer.strip()
        if text:
            self._trace_event(
                "user_submit",
                text=text,
                excerpt=_trace_clip_text(text, limit=320),
            )
        # Add the submitted text to the input history ring buffer
        # BEFORE clearing the buffer. _history_add dedupes consecutive
        # duplicates and skips empty strings, so /profile cycle spam
        # and accidental empty Enters do not pollute the history.
        self._history_add(text)
        # Drop out of recall mode whether or not we used a recalled
        # entry — the act of submitting always returns the user to a
        # fresh "ready to type" state.
        if self._history_in_recall_mode():
            self._history_exit_recall(restore_draft=False)
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
        note_non_read_tool_call(self._file_read_tracker)

        if text in ("/quit", "/exit", "/q"):
            self.stop()
            return

        # /config — open the three-pane profile config menu
        # The chat stops with _pending_action = "config" so the cli
        # main loop opens the config menu, then resumes the chat.
        if text == "/config":
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

        # /fork <directive> — spawn a background subagent with the
        # current chat context snapshot and a new directive.
        if text.startswith("/fork"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                self.messages.append(_Message(
                    "successor",
                    "usage: /fork <directive>. Spawns a background subagent that inherits the current chat context.",
                    synthetic=True,
                ))
                return
            self._handle_fork_cmd(parts[1].strip())
            return

        # /tasks — list queued/running/completed background tasks
        if text == "/tasks":
            self._handle_tasks_cmd()
            return

        # /task-cancel <id|all> — request cancellation
        if text.startswith("/task-cancel"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                self.messages.append(_Message(
                    "successor",
                    "usage: /task-cancel <task-id|all>",
                    synthetic=True,
                ))
                return
            self._handle_task_cancel_cmd(parts[1].strip())
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
        # /theme cycle — cycle to next theme in the supported catalog
        if text.startswith("/theme"):
            parts = text.split(maxsplit=1)
            available_names = [theme.name for theme in all_themes()]
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
                    f"mouse: {state}. Off means the terminal owns wheel/selection. "
                    f"On enables in-chat wheel scrolling and clickable widgets; "
                    f"hold Shift to drag-select text."
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
                        "mouse off. The terminal owns wheel scrolling and native "
                        "click-drag selection again; clickable widgets are disabled.",
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

        # /recording         — show current state
        # /recording on      — enable local auto-record bundles
        # /recording off     — disable
        # /recording toggle  — flip
        if text.startswith("/playback") or text.startswith("/review"):
            parts = text.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            self._open_playback_from_chat(arg)
            return

        if text.startswith("/recording"):
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                state = "on" if bool(self._config.get("autorecord", True)) else "off"
                hint = (
                    f"recording: {state}. Auto-record writes local playback bundles under "
                    "~/.local/share/successor/recordings/ by default. Bundles stay on "
                    "local disk and pair playback.html with session_trace.json for debugging."
                )
                self.messages.append(_Message("successor", hint, synthetic=True))
                return
            arg = parts[1].strip().lower()
            if arg == "on":
                self._set_autorecord(True)
                self.messages.append(
                    _Message(
                        "successor",
                        "auto-record on. Future chat sessions will save local playback bundles "
                        "for debugging and harness-building.",
                        synthetic=True,
                    )
                )
                return
            if arg == "off":
                self._set_autorecord(False)
                self.messages.append(
                    _Message(
                        "successor",
                        "auto-record off. Future chat sessions will stop writing playback bundles.",
                        synthetic=True,
                    )
                )
                return
            if arg == "toggle":
                enabled = not bool(self._config.get("autorecord", True))
                self._set_autorecord(enabled)
                state = "on" if enabled else "off"
                self.messages.append(
                    _Message(
                        "successor",
                        f"auto-record {state}.",
                        synthetic=True,
                    )
                )
                return
            self.messages.append(
                _Message(
                    "successor",
                    f"unknown /recording argument '{arg}'. try on, off, or toggle.",
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
        self._task_continue_nudged_this_turn = False
        self._task_continue_nudge = None
        self._browser_verification_active = False
        self._browser_verification_reason = ""
        self._verification_continue_nudged_this_turn = False
        self._verification_continue_nudge = None
        self._file_tool_continue_nudged_this_turn = False
        self._file_tool_continue_nudge = None
        self._subagent_continue_nudged_this_turn = False
        self._subagent_continue_nudge = None
        # Reset the per-turn autocompact guard so a new user message
        # gets exactly one autocompact attempt before falling through
        # to the API (which may then trip reactive PTL recovery).
        self._autocompact_attempted_this_turn = False
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

        AUTOCOMPACT GATE: before opening the stream, check whether
        the active profile's CompactionConfig says we should compact
        proactively. If yes, defer the turn — `_check_and_maybe_defer_for_autocompact()`
        spawns the compaction worker and returns True; the worker's
        poll callback re-enters this function after compaction
        succeeds. If compaction fails, the turn falls through anyway
        and reactive PTL recovery in the streaming layer catches it.
        """
        # Autocompact gate. Returns True if the turn was deferred.
        if self._check_and_maybe_defer_for_autocompact():
            return

        self._agent_turn += 1
        max_agent_turns = max(
            1,
            int(getattr(self.profile, "max_agent_turns", MAX_AGENT_TURNS) or MAX_AGENT_TURNS),
        )
        if self._agent_turn > max_agent_turns:
            self.messages.append(_Message(
                "successor",
                f"[agent loop halted at {max_agent_turns} turns — "
                f"send a new message to continue]",
                synthetic=True,
            ))
            self._agent_turn = 0
            return

        self._refresh_browser_verification_mode()

        # Resolve which tools are enabled for THIS turn from the active
        # profile. filter_known() drops any unrecognized names so a
        # stale profile referencing a future tool doesn't crash us.
        enabled_tools = self._enabled_tools_for_turn()
        enabled_skills = self._enabled_skills_for_turn(enabled_tools)

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
        task_execution_guidance = ""
        task_section = ""
        verification_execution_guidance = ""
        verification_section = ""
        runbook_execution_guidance = ""
        runbook_section = ""
        browser_verification_guidance = ""
        if "task" in enabled_tools:
            task_execution_guidance = build_task_execution_guidance(self._task_ledger)
            task_section = build_task_prompt_section(self._task_ledger)
        if "verify" in enabled_tools:
            verification_execution_guidance = build_verification_execution_guidance(
                self._verification_ledger
            )
            verification_section = build_verification_prompt_section(
                self._verification_ledger
            )
        if "runbook" in enabled_tools:
            runbook_execution_guidance = build_runbook_execution_guidance(
                self._runbook
            )
            runbook_section = build_runbook_prompt_section(self._runbook)
        if self._browser_verification_active and "browser" in enabled_tools:
            browser_verification_guidance = build_browser_verification_guidance(
                latest_user_text=self._latest_real_user_text(),
                active_task_text=self._browser_verification_context_text(),
                vision_available="vision" in enabled_tools,
                browser_verifier_available=(
                    "skill" in enabled_tools
                    and any(skill.name == "browser-verifier" for skill in enabled_skills)
                ),
                browser_verifier_loaded=self._skill_already_loaded("browser-verifier"),
            )
        if enabled_tools and (
            "bash" in enabled_tools
            or any(name in {"read_file", "write_file", "edit_file"} for name in enabled_tools)
        ):
            effective_cwd = self._tool_working_directory()
            sys_prompt = (
                f"{sys_prompt}\n\n"
                f"## Working directory\n\n"
                f"Native file tools and bash both resolve relative paths "
                f"from `cwd={effective_cwd}`. If the user asks for a file "
                f"in a specific location (like `~/Desktop/foo.html`), use "
                f"the absolute path — do not assume your cwd is what the "
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
        if enabled_tools:
            execution_parts = [
                "Use tools whenever they materially improve correctness, "
                "completeness, grounding, or verification. Do not stop "
                "early when another tool call would materially improve the "
                "result. If you say you will inspect, edit, run, verify, or "
                "check something, make the corresponding tool call in the "
                "SAME response instead of promising future action. If a tool "
                "returns partial, empty, or unhelpful results, change "
                "strategy instead of blindly repeating the same call. Keep "
                "working until the task is complete AND verified, then end "
                "with plain text.",
            ]
            if task_execution_guidance:
                execution_parts.append(task_execution_guidance)
            if verification_execution_guidance:
                execution_parts.append(verification_execution_guidance)
            if runbook_execution_guidance:
                execution_parts.append(runbook_execution_guidance)
            if browser_verification_guidance:
                execution_parts.append(browser_verification_guidance)
            sys_prompt = (
                f"{sys_prompt}\n\n"
                f"## Execution discipline\n\n"
                f"{execution_parts[0]}"
            )
            if len(execution_parts) > 1:
                sys_prompt = f"{sys_prompt}\n\n" + "\n\n".join(execution_parts[1:])
            if task_section:
                sys_prompt = f"{sys_prompt}\n\n{task_section}"
            if verification_section:
                sys_prompt = f"{sys_prompt}\n\n{verification_section}"
            if runbook_section:
                sys_prompt = f"{sys_prompt}\n\n{runbook_section}"
            capabilities = self._detect_client_runtime_capabilities()
            if bool(getattr(capabilities, "supports_parallel_tool_calls", False)):
                sys_prompt = (
                    f"{sys_prompt}\n\n"
                    f"## Parallel tool calls\n\n"
                    f"When multiple tool calls are independent and the "
                    f"result of one does not determine the arguments of "
                    f"another, emit them in the SAME assistant turn instead "
                    f"of serializing them one-by-one. This is especially "
                    f"useful for parallel read-only inspection such as "
                    f"multiple `read_file` calls, read-only `bash` checks, "
                    f"or separate `holonet` lookups. Keep dependent steps, "
                    f"writes, browser interaction sequences, and any "
                    f"read-after-write verification serialized."
                )
        tool_guidance = build_model_tool_guidance(enabled_tools)
        if tool_guidance:
            sys_prompt = f"{sys_prompt}\n\n{tool_guidance}"
        skill_hints = build_skill_hint_section(enabled_skills)
        if skill_hints:
            sys_prompt = f"{sys_prompt}\n\n{skill_hints}"
        skill_discovery = build_skill_discovery_section(
            enabled_skills,
            context_window_tokens=self._resolve_context_window(),
        )
        if skill_discovery:
            sys_prompt = f"{sys_prompt}\n\n{skill_discovery}"
        if self._task_continue_nudge:
            sys_prompt = (
                f"{sys_prompt}\n\n"
                f"## Continuation Reminder\n\n"
                f"{self._task_continue_nudge}"
            )
            self._task_continue_nudge = None
        if self._verification_continue_nudge:
            sys_prompt = (
                f"{sys_prompt}\n\n"
                f"## Browser Verification Reminder\n\n"
                f"{self._verification_continue_nudge}"
            )
            self._verification_continue_nudge = None
        if self._file_tool_continue_nudge:
            sys_prompt = (
                f"{sys_prompt}\n\n"
                f"## File Tool Recovery Reminder\n\n"
                f"{self._file_tool_continue_nudge}"
            )
            self._file_tool_continue_nudge = None
        if self._subagent_continue_nudge:
            sys_prompt = (
                f"{sys_prompt}\n\n"
                f"## Background Task Reminder\n\n"
                f"{self._subagent_continue_nudge}"
            )
            self._subagent_continue_nudge = None

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
        native_tool_schemas = build_native_tool_schemas(enabled_tools)
        self._trace_event(
            "agent_turn_begin",
            turn=self._agent_turn,
            continuation=(self._agent_turn > 1),
            enabled_tools=enabled_tools,
            enabled_skills=[skill.name for skill in enabled_skills],
            browser_verification_active=self._browser_verification_active,
            browser_verification_reason=self._browser_verification_reason,
            api_message_count=len(api_messages),
            last_user_excerpt=_find_last_user_excerpt(api_messages),
        )
        try:
            if native_tool_schemas:
                self._stream = self.client.stream_chat(
                    messages=api_messages,
                    tools=native_tool_schemas,
                )
            else:
                self._stream = self.client.stream_chat(messages=api_messages)
        except Exception as exc:
            self._trace_event(
                "stream_open_failed",
                turn=self._agent_turn,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        self._trace_event(
            "stream_opened",
            turn=self._agent_turn,
            tool_schema_names=enabled_tools if native_tool_schemas else [],
        )
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
            if _api_role_for_message(m) == "user" and not _message_has_tool_artifact(m):
                if m.synthetic:
                    i += 1
                    continue
                _append_text_merging("user", m.raw_text)
                i += 1
                continue

            # Assistant message → look ahead for following tool cards
            if _api_role_for_message(m) == "assistant" and not _message_has_tool_artifact(m):
                if m.synthetic:
                    i += 1
                    continue
                tool_cards: list[ToolArtifact] = []
                j = i + 1
                while j < n and _message_has_tool_artifact(ordered[j]):
                    card = _message_tool_artifact(ordered[j])
                    if card is not None:
                        tool_cards.append(card)
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
            if _message_has_tool_artifact(m):
                card = _message_tool_artifact(m)
                if card is None:
                    i += 1
                    continue
                tool_cards = [card]
                j = i + 1
                while j < n and _message_has_tool_artifact(ordered[j]):
                    next_card = _message_tool_artifact(ordered[j])
                    if next_card is not None:
                        tool_cards.append(next_card)
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
                self._trace_event("stream_started", turn=self._agent_turn)
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
                self._trace_event(
                    "stream_end",
                    turn=self._agent_turn,
                    finish_reason=ev.finish_reason,
                    finish_reason_reported=bool(getattr(ev, "finish_reason_reported", True)),
                    assistant_excerpt=_trace_clip_text(raw_content, limit=400),
                    reasoning_chars=self._stream_reasoning_chars,
                    native_tool_calls=[
                        _trace_tool_call_summary(tc) for tc in native_calls
                    ],
                    legacy_block_count=len(legacy_blocks),
                )

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
                    any_ran |= self._dispatch_native_tool_calls(
                        native_calls,
                        stream_finish_reason=ev.finish_reason,
                        stream_finish_reason_reported=bool(
                            getattr(ev, "finish_reason_reported", True)
                        ),
                    )
                if legacy_blocks:
                    any_ran |= self._dispatch_streamed_bash_blocks(legacy_blocks)
                self._trace_event(
                    "tool_dispatch_batch",
                    turn=self._agent_turn,
                    native_tool_count=len(native_calls),
                    legacy_block_count=len(legacy_blocks),
                    any_ran=any_ran,
                    runner_count=len(self._running_tools),
                )

                # The `_agent_turn > 0` guard is important: tests drive
                # `_pump_stream` directly with a pre-installed fake
                # stream WITHOUT going through `_submit`, so they never
                # increment the counter. Only user-initiated submissions
                # trigger continuation; synthetic test streams don't.
                if any_ran and self._agent_turn > 0:
                    # Async tools (bash runners) resume once the last
                    # runner finishes. Synchronous tools (like the
                    # subagent spawn path) have no runner to wait on,
                    # so continue immediately.
                    if self._running_tools:
                        self._trace_event(
                            "continuation_pending",
                            turn=self._agent_turn,
                            runner_count=len(self._running_tools),
                        )
                        self._pending_continuation = True
                    else:
                        self._pending_continuation = False
                        self._trace_event(
                            "continuation_immediate",
                            turn=self._agent_turn,
                        )
                        self._begin_agent_turn()
                    return

                if (
                    self._agent_turn > 0
                    and self._task_ledger.has_in_progress()
                    and not self._task_continue_nudged_this_turn
                ):
                    nudge = build_task_continue_nudge(self._task_ledger)
                    if nudge:
                        self._task_continue_nudged_this_turn = True
                        self._task_continue_nudge = nudge
                        active = self._task_ledger.in_progress_task()
                        self._trace_event(
                            "task_continue_nudge",
                            turn=self._agent_turn,
                            active_task=active.active_form if active else "",
                            assistant_excerpt=_trace_clip_text(raw_content, limit=320),
                        )
                        self._begin_agent_turn()
                        return

                # No tool calls OR nothing successfully ran — turn
                # ends here. Reset the agent-turn counter so the next
                # user submission starts fresh.
                self._trace_event(
                    "agent_turn_complete",
                    turn=self._agent_turn,
                    reason="plain_text_or_no_runnable_tools",
                )
                self._agent_turn = 0
            elif isinstance(ev, StreamError):
                partial = "".join(self._stream_content)
                self._trace_event(
                    "stream_error",
                    turn=self._agent_turn,
                    message=ev.message,
                    partial_excerpt=_trace_clip_text(partial, limit=400),
                )
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
                f"the URL above. Three ways to fix this:\n"
                f"\n"
                f"  1. Start a local llama.cpp server:\n"
                f"     llama-server -m <your-model.gguf> --host 0.0.0.0 --port 8080\n"
                f"\n"
                f"  2. Quit (Ctrl+C) and run `successor setup` to create\n"
                f"     a profile against OpenAI or OpenRouter instead.\n"
                f"\n"
                f"  3. Open /config and edit the active profile's\n"
                f"     provider.base_url and provider.api_key fields."
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
        from .bash.change_capture import begin_change_capture
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
            self._trace_event(
                "bash_refused",
                tool_call_id=resolved_call_id,
                risk=final_risk,
                reason=refused.reason,
                command=command,
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
            self._trace_event(
                "bash_refused",
                tool_call_id=resolved_call_id,
                risk=final_risk,
                reason=refused.reason,
                command=command,
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
        runner.change_capture = begin_change_capture(
            preview,
            cwd=bash_cfg.working_directory,
        )
        msg = _Message(
            "tool",
            "",
            tool_card=preview,
            running_tool=runner,
        )
        self.messages.append(msg)
        self._running_tools.append(msg)
        self._trace_event(
            "bash_spawn",
            tool_call_id=resolved_call_id,
            verb=preview.verb,
            risk=preview.risk,
            parser=preview.parser_name,
            cwd=bash_cfg.working_directory or os.getcwd(),
            timeout_s=bash_cfg.timeout_s,
            command=command,
        )
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

    def _spawn_subagent_task(
        self,
        prompt: str,
        *,
        name: str = "",
        tool_call_id: str | None = None,
    ) -> bool:
        """Spawn a background subagent and append a structured card."""
        cfg = self.profile.subagents
        if not cfg.enabled:
            self.messages.append(_Message(
                "successor",
                "subagent tool is disabled for this profile. Enable subagents in /config before delegating background work.",
                synthetic=True,
            ))
            return False
        if not cfg.notify_on_finish:
            self.messages.append(_Message(
                "successor",
                "subagent tool requires notify_on_finish=on so the parent chat can receive the result later.",
                synthetic=True,
            ))
            return False
        directive = prompt.strip()
        if not directive:
            self.messages.append(_Message(
                "successor",
                "subagent tool call had no prompt.",
                synthetic=True,
            ))
            return False

        from .bash.exec import _new_tool_call_id  # noqa: PLC0415

        task = self._subagent_manager.spawn_fork(
            directive=directive,
            name=name,
            context_snapshot=self._subagent_context_snapshot(),
            profile=self.profile,
            config=cfg,
        )
        card = SubagentToolCard(
            task_id=task.task_id,
            name=task.name,
            directive=directive,
            tool_call_id=tool_call_id or _new_tool_call_id(),
            spawn_result=build_spawn_result_payload(task),
        )
        self.messages.append(_Message(
            "tool",
            "",
            subagent_card=card,
            display_text=build_spawn_result_display(task),
        ))
        self._scroll_to_bottom()
        return True

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
        return ToolCard(
            verb=verb,
            params=params,
            risk=risk,
            raw_command=raw_command,
            confidence=1.0,
            parser_name=f"native-{tool_name}",
            stderr=message,
            exit_code=1,
            duration_ms=0.0,
            tool_name=tool_name,
            tool_arguments=tool_arguments,
            raw_label_prefix=raw_label_prefix,
            tool_call_id=tool_call_id,
        )

    def _spawn_skill_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        from .bash.exec import _new_tool_call_id  # noqa: PLC0415

        resolved_call_id = tool_call_id or _new_tool_call_id()
        requested_name = str(arguments.get("skill") or "").strip().lower()
        task = " ".join(str(arguments.get("task") or "").split()).strip()
        enabled_skills = {
            skill.name: skill
            for skill in self._enabled_skills_for_turn()
        }
        skill = enabled_skills.get(requested_name)
        if skill is None:
            card = self._tool_error_card(
                tool_name="skill",
                verb="load-skill",
                raw_command=requested_name or "skill",
                tool_call_id=resolved_call_id,
                params=(),
                tool_arguments={
                    "skill": requested_name,
                    **({"task": task} if task else {}),
                },
                raw_label_prefix="§",
                message=(
                    f"skill '{requested_name or '(missing)'}' is not enabled for "
                    "this profile, is missing on disk, or requires tools that are "
                    "not available in this turn."
                ),
            )
            self.messages.append(_Message("tool", "", tool_card=card))
            return False

        params: list[tuple[str, str]] = [("skill", skill.name)]
        if skill.allowed_tools:
            params.append(("tools", ", ".join(skill.allowed_tools)))
        if task:
            task_value = task if len(task) <= 64 else task[:63].rstrip() + "…"
            params.append(("task", task_value))
        raw_command = " ".join(bit for bit in (skill.name, task) if bit)
        preview = ToolCard(
            verb="load-skill",
            params=tuple(params),
            risk="safe",
            raw_command=raw_command,
            confidence=1.0,
            parser_name="native-skill",
            tool_name="skill",
            tool_arguments={
                "skill": skill.name,
                **({"task": task} if task else {}),
            },
            raw_label_prefix="§",
            tool_call_id=resolved_call_id,
        )

        source = "builtin"
        source_path = getattr(skill, "source_path", "")
        if "/.config/" in source_path:
            source = "user"

        if self._skill_already_loaded(skill.name):
            final_card = replace(
                preview,
                output=f"Skill `{skill.name}` is already loaded earlier in the conversation.",
                exit_code=0,
                duration_ms=0.0,
                api_content_override=build_skill_reuse_result(skill.name, task=task),
            )
            self.messages.append(_Message("tool", "", tool_card=final_card))
            self._scroll_to_bottom()
            return True

        final_card = replace(
            preview,
            output=build_skill_card_output(skill, task=task, source=source),
            exit_code=0,
            duration_ms=0.0,
            api_content_override=build_skill_tool_result(
                skill,
                task=task,
                source=source,
            ),
        )
        self._trace_event(
            "tool_spawn",
            tool_name="skill",
            tool_call_id=resolved_call_id,
            skill_name=skill.name,
            task=task,
        )
        self._trace_event(
            "tool_runner_finished",
            tool_name="skill",
            tool_call_id=resolved_call_id,
            exit_code=0,
            error="",
            duration_ms=0.0,
            stdout_excerpt=_trace_clip_text(final_card.output, limit=320),
            stderr_excerpt="",
            truncated=False,
        )
        self.messages.append(_Message("tool", "", tool_card=final_card))
        self._scroll_to_bottom()
        return True

    def _spawn_task_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        from .bash.exec import _new_tool_call_id  # noqa: PLC0415

        resolved_call_id = tool_call_id or _new_tool_call_id()
        try:
            items = parse_task_items(arguments.get("items"))
        except TaskLedgerError as exc:
            card = self._tool_error_card(
                tool_name="task",
                verb="task-ledger",
                raw_command="update tasks",
                tool_call_id=resolved_call_id,
                params=(),
                tool_arguments={"items": arguments.get("items")},
                raw_label_prefix="#",
                message=str(exc),
            )
            self.messages.append(_Message("tool", "", tool_card=card))
            return False

        self._task_ledger.replace(items)
        active = self._task_ledger.in_progress_task()
        params: list[tuple[str, str]] = [("tasks", str(len(items)))]
        if active is not None:
            active_value = active.content
            if len(active_value) > 64:
                active_value = active_value[:63].rstrip() + "…"
            params.append(("active", active_value))
        raw_command = "clear" if not items else f"update {len(items)} tasks"
        payload = {"items": task_items_to_payload(items)}
        preview = ToolCard(
            verb="task-ledger",
            params=tuple(params),
            risk="safe",
            raw_command=raw_command,
            confidence=1.0,
            parser_name="native-task",
            tool_name="task",
            tool_arguments=payload,
            raw_label_prefix="#",
            tool_call_id=resolved_call_id,
        )
        final_card = replace(
            preview,
            output=build_task_card_output(self._task_ledger),
            exit_code=0,
            duration_ms=0.0,
            api_content_override=build_task_tool_result(self._task_ledger),
        )
        self._trace_event(
            "tool_spawn",
            tool_name="task",
            tool_call_id=resolved_call_id,
            task_count=len(items),
            active_task=active.active_form if active else "",
        )
        self._trace_event(
            "task_ledger_updated",
            tool_call_id=resolved_call_id,
            task_count=len(items),
            open_count=self._task_ledger.open_count(),
            completed_count=self._task_ledger.completed_count(),
            active_task=active.active_form if active else "",
        )
        self._trace_event(
            "tool_runner_finished",
            tool_name="task",
            tool_call_id=resolved_call_id,
            exit_code=0,
            error="",
            duration_ms=0.0,
            stdout_excerpt=_trace_clip_text(final_card.output, limit=320),
            stderr_excerpt="",
            truncated=False,
        )
        self.messages.append(_Message("tool", "", tool_card=final_card))
        self._scroll_to_bottom()
        return True

    def _spawn_verify_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        from .bash.exec import _new_tool_call_id  # noqa: PLC0415

        resolved_call_id = tool_call_id or _new_tool_call_id()
        try:
            items = parse_verification_items(arguments.get("items"))
        except VerificationContractError as exc:
            card = self._tool_error_card(
                tool_name="verify",
                verb="verification",
                raw_command="update verification contract",
                tool_call_id=resolved_call_id,
                params=(),
                tool_arguments={"items": arguments.get("items")},
                raw_label_prefix="✓",
                message=str(exc),
            )
            self.messages.append(_Message("tool", "", tool_card=card))
            return False

        self._verification_ledger.replace(items)
        active = self._verification_ledger.in_progress_item()
        params: list[tuple[str, str]] = [("assertions", str(len(items)))]
        if active is not None:
            active_value = active.claim
            if len(active_value) > 64:
                active_value = active_value[:63].rstrip() + "…"
            params.append(("active", active_value))
        raw_command = (
            "clear"
            if not items
            else f"update {len(items)} assertions"
        )
        payload = {"items": verification_items_to_payload(items)}
        preview = ToolCard(
            verb="verification",
            params=tuple(params),
            risk="safe",
            raw_command=raw_command,
            confidence=1.0,
            parser_name="native-verify",
            tool_name="verify",
            tool_arguments=payload,
            raw_label_prefix="✓",
            tool_call_id=resolved_call_id,
        )
        final_card = replace(
            preview,
            output=build_verification_card_output(self._verification_ledger),
            exit_code=0,
            duration_ms=0.0,
            api_content_override=build_verification_tool_result(self._verification_ledger),
        )
        assertions_artifact = build_assertions_artifact(self._verification_ledger)
        self._trace_event(
            "tool_spawn",
            tool_name="verify",
            tool_call_id=resolved_call_id,
            assertion_count=len(items),
            active_claim=active.claim if active else "",
            active_evidence=active.evidence if active else "",
        )
        self._trace_event(
            "verification_contract_updated",
            tool_call_id=resolved_call_id,
            assertion_count=len(items),
            pending_count=self._verification_ledger.pending_count(),
            open_count=self._verification_ledger.open_count(),
            passed_count=self._verification_ledger.passed_count(),
            failed_count=self._verification_ledger.failed_count(),
            active_claim=active.claim if active else "",
            active_evidence=active.evidence if active else "",
            items=payload["items"],
            artifact=assertions_artifact,
        )
        self._trace_event(
            "tool_runner_finished",
            tool_name="verify",
            tool_call_id=resolved_call_id,
            exit_code=0,
            error="",
            duration_ms=0.0,
            stdout_excerpt=_trace_clip_text(final_card.output, limit=320),
            stderr_excerpt="",
            truncated=False,
        )
        self.messages.append(_Message("tool", "", tool_card=final_card))
        self._scroll_to_bottom()
        return True

    def _spawn_runbook_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        from .bash.exec import _new_tool_call_id  # noqa: PLC0415

        resolved_call_id = tool_call_id or _new_tool_call_id()
        try:
            state = parse_runbook_state(arguments)
            if state is None and arguments.get("attempt") not in (None, ""):
                raise RunbookError(
                    "runbook.attempt cannot be recorded in the same call that clears the runbook"
                )
            attempt = parse_experiment_attempt(
                arguments.get("attempt"),
                next_attempt_id=self._runbook_attempt_count + 1,
            )
        except RunbookError as exc:
            card = self._tool_error_card(
                tool_name="runbook",
                verb="runbook",
                raw_command="update runbook",
                tool_call_id=resolved_call_id,
                params=(),
                tool_arguments=dict(arguments),
                raw_label_prefix="◇",
                message=str(exc),
            )
            self.messages.append(_Message("tool", "", tool_card=card))
            return False

        if state is None:
            self._runbook.clear()
            self._runbook_attempt_count = 0
        else:
            self._runbook.replace(state)
        if attempt is not None:
            self._runbook_attempt_count = attempt.attempt_id

        state_payload = runbook_state_to_payload(self._runbook.state)
        payload: dict[str, Any] = dict(state_payload)
        if attempt is not None:
            payload["attempt"] = experiment_attempt_to_payload(attempt)
        params: list[tuple[str, str]] = []
        if self._runbook.state is not None:
            params.append(("status", self._runbook.state.status))
            params.append(("baseline", self._runbook.state.baseline_status))
            if self._runbook.state.evaluator:
                params.append(("eval", str(len(self._runbook.state.evaluator))))
        else:
            params.append(("state", "cleared"))
        if attempt is not None:
            params.append(("attempt", str(attempt.attempt_id)))
            params.append(("decision", attempt.decision))
        raw_command = "clear" if self._runbook.state is None else "update runbook"
        preview = ToolCard(
            verb="runbook",
            params=tuple(params),
            risk="safe",
            raw_command=raw_command,
            confidence=1.0,
            parser_name="native-runbook",
            tool_name="runbook",
            tool_arguments=payload,
            raw_label_prefix="◇",
            tool_call_id=resolved_call_id,
        )
        runbook_artifact = build_runbook_artifact(
            self._runbook.state,
            attempt_count=self._runbook_attempt_count,
            last_attempt=attempt,
        )
        final_card = replace(
            preview,
            output=build_runbook_card_output(self._runbook.state, attempt=attempt),
            exit_code=0,
            duration_ms=0.0,
            api_content_override=build_runbook_tool_result(
                self._runbook.state,
                attempt=attempt,
            ),
        )
        objective = self._runbook.state.objective if self._runbook.state is not None else ""
        self._trace_event(
            "tool_spawn",
            tool_name="runbook",
            tool_call_id=resolved_call_id,
            objective=objective,
            baseline_status=self._runbook.state.baseline_status if self._runbook.state is not None else "",
            attempt_id=attempt.attempt_id if attempt is not None else 0,
        )
        self._trace_event(
            "runbook_updated",
            tool_call_id=resolved_call_id,
            objective=objective,
            status=self._runbook.state.status if self._runbook.state is not None else "cleared",
            baseline_status=self._runbook.state.baseline_status if self._runbook.state is not None else "missing",
            active_hypothesis=self._runbook.state.active_hypothesis if self._runbook.state is not None else "",
            evaluator_count=len(self._runbook.state.evaluator) if self._runbook.state is not None else 0,
            attempt_count=self._runbook_attempt_count,
            runbook=state_payload,
            artifact=runbook_artifact,
        )
        if attempt is not None:
            self._trace_event(
                "experiment_attempt_recorded",
                tool_call_id=resolved_call_id,
                objective=objective,
                attempt=experiment_attempt_to_payload(attempt),
                baseline_status=self._runbook.state.baseline_status if self._runbook.state is not None else "missing",
            )
        self._trace_event(
            "tool_runner_finished",
            tool_name="runbook",
            tool_call_id=resolved_call_id,
            exit_code=0,
            error="",
            duration_ms=0.0,
            stdout_excerpt=_trace_clip_text(final_card.output, limit=320),
            stderr_excerpt="",
            truncated=False,
        )
        self.messages.append(_Message("tool", "", tool_card=final_card))
        self._scroll_to_bottom()
        return True

    def _spawn_read_file_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        from .bash.exec import _new_tool_call_id  # noqa: PLC0415

        resolved_call_id = tool_call_id or _new_tool_call_id()
        working_directory = self._tool_working_directory()
        requested_path = str(arguments.get("file_path") or "").strip()
        try:
            normalized_path = normalize_file_path(
                requested_path,
                working_directory=working_directory,
            )
        except Exception as exc:
            card = self._tool_error_card(
                tool_name="read_file",
                verb="read-file",
                raw_command=requested_path or "read_file",
                tool_call_id=resolved_call_id,
                params=(("path", requested_path),) if requested_path else (),
                tool_arguments=dict(arguments),
                raw_label_prefix="⟫",
                message=str(exc),
            )
            self.messages.append(_Message("tool", "", tool_card=card))
            return False

        normalized_args = dict(arguments)
        normalized_args["file_path"] = normalized_path
        preview = read_file_preview_card(normalized_args, tool_call_id=resolved_call_id)
        runner = CallableToolRunner(
            tool_call_id=resolved_call_id,
            worker=lambda progress: run_read_file(
                normalized_args,
                preview=preview,
                read_state=self._file_read_state,
                read_tracker=self._file_read_tracker,
                working_directory=working_directory,
                progress=progress,
            ),
        )
        msg = _Message("tool", "", tool_card=preview, running_tool=runner)
        self.messages.append(msg)
        self._running_tools.append(msg)
        self._trace_event(
            "tool_spawn",
            tool_name="read_file",
            tool_call_id=resolved_call_id,
            path=normalized_path,
            offset=normalized_args.get("offset"),
            limit=normalized_args.get("limit"),
        )
        runner.start()
        self._scroll_to_bottom()
        return True

    def _spawn_write_file_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        from .bash.exec import _new_tool_call_id  # noqa: PLC0415

        resolved_call_id = tool_call_id or _new_tool_call_id()
        working_directory = self._tool_working_directory()
        requested_path = str(arguments.get("file_path") or "").strip()
        try:
            normalized_path = normalize_file_path(
                requested_path,
                working_directory=working_directory,
            )
        except Exception as exc:
            card = self._tool_error_card(
                tool_name="write_file",
                verb="write-file",
                raw_command=requested_path or "write_file",
                tool_call_id=resolved_call_id,
                params=(("path", requested_path),) if requested_path else (),
                tool_arguments=dict(arguments),
                raw_label_prefix="✎",
                message=str(exc),
                risk="mutating",
            )
            self.messages.append(_Message("tool", "", tool_card=card))
            return False

        normalized_args = dict(arguments)
        normalized_args["file_path"] = normalized_path
        preview = write_file_preview_card(normalized_args, tool_call_id=resolved_call_id)
        runner = CallableToolRunner(
            tool_call_id=resolved_call_id,
            worker=lambda progress: run_write_file(
                normalized_args,
                preview=preview,
                read_state=self._file_read_state,
                working_directory=working_directory,
                progress=progress,
            ),
        )
        msg = _Message("tool", "", tool_card=preview, running_tool=runner)
        self.messages.append(msg)
        self._running_tools.append(msg)
        self._trace_event(
            "tool_spawn",
            tool_name="write_file",
            tool_call_id=resolved_call_id,
            path=normalized_path,
            content_length=len(str(normalized_args.get("content") or "")),
        )
        runner.start()
        self._scroll_to_bottom()
        return True

    def _spawn_edit_file_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        from .bash.exec import _new_tool_call_id  # noqa: PLC0415

        resolved_call_id = tool_call_id or _new_tool_call_id()
        working_directory = self._tool_working_directory()
        requested_path = str(arguments.get("file_path") or "").strip()
        try:
            normalized_path = normalize_file_path(
                requested_path,
                working_directory=working_directory,
            )
        except Exception as exc:
            card = self._tool_error_card(
                tool_name="edit_file",
                verb="edit-file",
                raw_command=requested_path or "edit_file",
                tool_call_id=resolved_call_id,
                params=(("path", requested_path),) if requested_path else (),
                tool_arguments=dict(arguments),
                raw_label_prefix="✎",
                message=str(exc),
                risk="mutating",
            )
            self.messages.append(_Message("tool", "", tool_card=card))
            return False

        normalized_args = dict(arguments)
        normalized_args["file_path"] = normalized_path
        preview = edit_file_preview_card(normalized_args, tool_call_id=resolved_call_id)
        runner = CallableToolRunner(
            tool_call_id=resolved_call_id,
            worker=lambda progress: run_edit_file(
                normalized_args,
                preview=preview,
                read_state=self._file_read_state,
                working_directory=working_directory,
                progress=progress,
            ),
        )
        msg = _Message("tool", "", tool_card=preview, running_tool=runner)
        self.messages.append(msg)
        self._running_tools.append(msg)
        self._trace_event(
            "tool_spawn",
            tool_name="edit_file",
            tool_call_id=resolved_call_id,
            path=normalized_path,
            replace_all=bool(normalized_args.get("replace_all")),
        )
        runner.start()
        self._scroll_to_bottom()
        return True

    def _spawn_holonet_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        from .bash.exec import _new_tool_call_id  # noqa: PLC0415

        resolved_call_id = tool_call_id or _new_tool_call_id()
        cfg = resolve_holonet_config(self.profile)
        try:
            route = resolve_holonet_route(arguments, cfg)
        except Exception as exc:
            card = self._tool_error_card(
                tool_name="holonet",
                verb="web-search",
                raw_command="holonet",
                tool_call_id=resolved_call_id,
                params=(),
                tool_arguments=dict(arguments),
                raw_label_prefix="≈",
                message=str(exc),
            )
            self.messages.append(_Message("tool", "", tool_card=card))
            return False

        preview = holonet_preview_card(route, tool_call_id=resolved_call_id)
        runner = CallableToolRunner(
            tool_call_id=resolved_call_id,
            worker=lambda progress: run_holonet(route, cfg, progress),
        )
        msg = _Message("tool", "", tool_card=preview, running_tool=runner)
        self.messages.append(msg)
        self._running_tools.append(msg)
        self._trace_event(
            "tool_spawn",
            tool_name="holonet",
            tool_call_id=resolved_call_id,
            provider=route.provider,
            query=route.query,
            url=route.url,
            count=route.count,
        )
        runner.start()
        self._scroll_to_bottom()
        return True

    def _spawn_browser_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        from .bash.exec import _new_tool_call_id  # noqa: PLC0415

        resolved_call_id = tool_call_id or _new_tool_call_id()
        preview = browser_preview_card(arguments, tool_call_id=resolved_call_id)
        browser_cfg = resolve_browser_config(self.profile)
        browser_status = browser_runtime_status(self.profile.name, browser_cfg)
        if not browser_status.package_available:
            card = self._tool_error_card(
                tool_name="browser",
                verb=preview.verb,
                raw_command=preview.raw_command,
                tool_call_id=resolved_call_id,
                params=preview.params,
                tool_arguments=preview.tool_arguments,
                raw_label_prefix=preview.raw_label_prefix,
                message=(
                    "Playwright is not available in the configured runtime. "
                    "Install with `pip install 'successor[browser]'`, or set "
                    "browser.python_executable to a Python interpreter that "
                    "already has Playwright installed."
                ),
            )
            self.messages.append(_Message("tool", "", tool_card=card))
            return False

        manager = self._browser_manager_for_profile()
        runner = CallableToolRunner(
            tool_call_id=resolved_call_id,
            worker=lambda progress: run_browser_action(
                arguments,
                manager=manager,
                progress=progress,
            ),
        )
        msg = _Message("tool", "", tool_card=preview, running_tool=runner)
        self.messages.append(msg)
        self._running_tools.append(msg)
        self._trace_event(
            "tool_spawn",
            tool_name="browser",
            tool_call_id=resolved_call_id,
            action=str(arguments.get("action") or ""),
            target=str(arguments.get("target") or arguments.get("url") or ""),
        )
        runner.start()
        self._scroll_to_bottom()
        return True

    def _spawn_vision_runner(
        self,
        arguments: dict[str, Any],
        *,
        tool_call_id: str | None = None,
    ) -> bool:
        from .bash.exec import _new_tool_call_id  # noqa: PLC0415

        resolved_call_id = tool_call_id or _new_tool_call_id()
        preview = vision_preview_card(arguments, tool_call_id=resolved_call_id)
        vision_cfg = resolve_vision_config(self.profile)
        status = vision_runtime_status(vision_cfg, client=self.client)
        if not status.tool_available:
            card = self._tool_error_card(
                tool_name="vision",
                verb=preview.verb,
                raw_command=preview.raw_command,
                tool_call_id=resolved_call_id,
                params=preview.params,
                tool_arguments=preview.tool_arguments,
                raw_label_prefix=preview.raw_label_prefix,
                message=status.reason,
            )
            self.messages.append(_Message("tool", "", tool_card=card))
            return False

        runner = CallableToolRunner(
            tool_call_id=resolved_call_id,
            worker=lambda progress: run_vision_analysis(
                arguments,
                vision_cfg,
                client=self.client,
                progress=progress,
            ),
        )
        msg = _Message("tool", "", tool_card=preview, running_tool=runner)
        self.messages.append(msg)
        self._running_tools.append(msg)
        self._trace_event(
            "tool_spawn",
            tool_name="vision",
            tool_call_id=resolved_call_id,
            path=str(arguments.get("path") or ""),
            prompt=str(arguments.get("prompt") or ""),
        )
        runner.start()
        self._scroll_to_bottom()
        return True

    def _dispatch_native_tool_calls(
        self,
        tool_calls: list[dict],
        *,
        stream_finish_reason: str = "stop",
        stream_finish_reason_reported: bool = True,
    ) -> bool:
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
                if name != "read_file":
                    note_non_read_tool_call(self._file_read_tracker)
                if name == "read_file":
                    if isinstance(args, dict) and self._spawn_read_file_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "write_file":
                    if isinstance(args, dict) and self._spawn_write_file_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "edit_file":
                    if isinstance(args, dict) and self._spawn_edit_file_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "task":
                    if isinstance(args, dict) and self._spawn_task_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "verify":
                    if isinstance(args, dict) and self._spawn_verify_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "runbook":
                    if isinstance(args, dict) and self._spawn_runbook_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "skill":
                    if isinstance(args, dict) and self._spawn_skill_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "subagent":
                    prompt = args.get("prompt") if isinstance(args, dict) else ""
                    label = args.get("name") if isinstance(args, dict) else ""
                    if self._spawn_subagent_task(
                        str(prompt or ""),
                        name=str(label or ""),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "holonet":
                    if isinstance(args, dict) and self._spawn_holonet_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "browser":
                    if isinstance(args, dict) and self._spawn_browser_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                if name == "vision":
                    if isinstance(args, dict) and self._spawn_vision_runner(
                        dict(args),
                        tool_call_id=call_id,
                    ):
                        any_ran = True
                    continue
                self.messages.append(_Message(
                    "successor",
                    f"unknown tool {name!r} — supported tools are read_file, write_file, edit_file, bash, task, verify, runbook, skill, subagent, holonet, browser, and vision",
                    synthetic=True,
                ))
                continue

            note_non_read_tool_call(self._file_read_tracker)
            command = args.get("command") if isinstance(args, dict) else ""
            if not command:
                self.messages.append(_Message(
                    "successor",
                    _native_tool_call_failure_message(
                        tc,
                        finish_reason=stream_finish_reason,
                        finish_reason_reported=stream_finish_reason_reported,
                    ),
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
        completed_tool_batch: list[tuple[str, ToolCard, dict[str, Any]]] = []
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
            for ev in runner.drain():
                if isinstance(ev, RunnerStarted):
                    tool_name = getattr(msg.tool_card, "tool_name", "bash")
                    if tool_name == "bash":
                        self._trace_event(
                            "bash_runner_started",
                            tool_call_id=runner.tool_call_id,
                            pid=runner.pid,
                        )
                    else:
                        self._trace_event(
                            "tool_runner_started",
                            tool_name=tool_name,
                            tool_call_id=runner.tool_call_id,
                        )
                elif isinstance(ev, RunnerErrored):
                    tool_name = getattr(msg.tool_card, "tool_name", "bash")
                    self._trace_event(
                        "tool_runner_errored" if tool_name != "bash" else "bash_runner_errored",
                        tool_name=tool_name,
                        tool_call_id=runner.tool_call_id,
                        message=ev.message,
                    )
            # Force a fresh paint each frame while running so the
            # spinner/border pulse and live output stream visibly.
            msg._card_rows_cache_key = None
            msg._card_rows_cache = None
            if runner.is_done():
                finalized = self._finalize_runner(msg)
                if finalized is not None:
                    completed_tool_batch.append(finalized)
                completed_msgs.append(msg)

        for msg in completed_msgs:
            try:
                self._running_tools.remove(msg)
            except ValueError:
                pass

        if completed_tool_batch:
            self._emit_completed_tool_batch_progress(completed_tool_batch)

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

    def _finalize_runner(
        self,
        msg: "_Message",
    ) -> tuple[str, ToolCard, dict[str, Any]] | None:
        """Replace the preview tool_card on `msg` with the final
        enriched card built from the runner's accumulated stdout,
        stderr, exit code, and duration. Clears running_tool so the
        renderer falls through to the static paint path.
        """
        from dataclasses import replace as _replace
        from .bash.change_capture import finalize_change_capture
        runner = msg.running_tool
        preview = msg.tool_card
        if runner is None or preview is None:
            return None
        build_final = getattr(runner, "build_final_card", None)
        if callable(build_final):
            final_card = build_final(preview)
        else:
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
            change_artifact = finalize_change_capture(
                getattr(runner, "change_capture", None),
            )
            if change_artifact is not None:
                final_card = _replace(final_card, change_artifact=change_artifact)
        metadata = dict(getattr(runner, "metadata", None) or {})
        msg.tool_card = final_card
        msg.running_tool = None
        msg._card_rows_cache_key = None
        msg._card_rows_cache = None
        tool_name = getattr(final_card, "tool_name", "bash")
        event_name = "bash_runner_finished" if tool_name == "bash" else "tool_runner_finished"
        self._trace_event(
            event_name,
            tool_name=tool_name,
            tool_call_id=runner.tool_call_id,
            exit_code=final_card.exit_code,
            error=runner.error,
            duration_ms=round(runner.elapsed() * 1000.0, 3),
            stdout_excerpt=_trace_clip_text(final_card.output, limit=320),
            stderr_excerpt=_trace_clip_text(final_card.stderr, limit=320),
            truncated=final_card.truncated,
        )
        if tool_name in {"write_file", "edit_file"} and final_card.exit_code != 0:
            recovery_nudge = build_file_tool_recovery_nudge(
                tool_name,
                final_card.stderr or final_card.output or runner.error or "",
            )
            if recovery_nudge and not self._file_tool_continue_nudged_this_turn:
                self._file_tool_continue_nudged_this_turn = True
                self._file_tool_continue_nudge = recovery_nudge
                self._trace_event(
                    "file_tool_recovery_nudge",
                    turn=self._agent_turn,
                    tool_name=tool_name,
                    tool_call_id=runner.tool_call_id,
                    message=recovery_nudge,
                )
        return tool_name, final_card, metadata

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
        if bottom <= top or width <= 2:
            return

        # Empty-state hero: when the user hasn't sent any real messages
        # yet AND the active profile defines chat_intro_art, paint the
        # ANSI art portrait + info panel instead of the normal message
        # rendering. The painter handles all sizing, theming, and
        # graceful fallback (info-only on narrow terminals, no panel
        # if the art file is missing). Once the user submits anything,
        # _is_empty_chat() returns False and we fall through to the
        # normal painter.
        if self._is_empty_chat() and self._has_intro_art():
            self._paint_empty_state(grid, top, bottom, width, theme)
            return

        density = self._current_density()
        geometry = compute_viewport_decision(
            width=width,
            top=top,
            bottom=bottom,
            density=density,
            committed_height=0,
            scroll_offset=self.scroll_offset,
            auto_scroll=self._auto_scroll,
            last_total_height=self._last_total_height,
        )

        # Build the flat list of committed-message lines.
        committed = self._build_message_lines(geometry.body_width, theme)
        committed_h = len(committed)
        viewport = compute_viewport_decision(
            width=width,
            top=top,
            bottom=bottom,
            density=density,
            committed_height=committed_h,
            scroll_offset=self.scroll_offset,
            auto_scroll=self._auto_scroll,
            last_total_height=self._last_total_height,
        )
        self.scroll_offset = viewport.scroll_offset
        self._auto_scroll = viewport.auto_scroll
        self._last_chat_h = viewport.last_chat_h
        self._last_chat_w = viewport.last_chat_w
        self._last_total_height = viewport.last_total_height

        # NB: there's no scroll override during compaction anymore.
        # The boundary + summary now live at the END of self.messages
        # (display order), so they naturally appear at the bottom of
        # the chat where the user is auto-scrolled. The materialize
        # animation plays in view without any forced snap.

        # Slice the committed lines for the current scroll position.
        visible = committed[viewport.start:viewport.end]

        # Streaming reply — only when anchored at bottom.
        if self._stream is not None and self.scroll_offset == 0:
            stream_lines = self._build_streaming_lines(viewport.body_width, theme)
            combined = visible + stream_lines
            if len(combined) > viewport.chat_height:
                combined = combined[-viewport.chat_height:]
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
            self._paint_chat_row(
                grid, viewport.body_x, y, viewport.body_width, row, theme
            )

        # NB: there is no centered "compacting" overlay anymore. The
        # chat content stays fully visible during compaction so the
        # user can scroll, search, and read freely while the model
        # generates the summary in the background. The "compaction
        # in progress" signal lives in the static footer instead.

    # ─── Empty-state hero panel ───

    def _is_empty_chat(self) -> bool:
        """True iff the chat has no visible content yet.

        Used by _paint_chat_area to decide whether to paint the empty-
        state hero + info panel or fall through to the normal message
        rendering. Counts ANY of the following as "real content" that
        should hide the hero:

          - Non-synthetic messages (user input, committed assistant
            replies)
          - Tool cards (technically synthetic for API serialization
            purposes, but visually they ARE real artifacts of a real
            tool run that the user expects to see)
          - Compaction boundaries and summaries (synthetic but
            structurally important — without them the user would
            see the hero come back AFTER a compaction, which would
            be wrong)
          - An in-flight stream (the chat is mid-response — show
            whatever's streaming, not the hero)
        """
        if self._stream is not None:
            return False
        for m in self.messages:
            if _message_has_tool_artifact(m):
                return False
            if getattr(m, "is_boundary", False):
                return False
            if getattr(m, "is_summary", False):
                return False
            if not m.synthetic:
                return False
        return True

    def _has_intro_art(self) -> bool:
        """True iff the active profile has loadable chat_intro_art.

        Calls _resolve_intro_art() lazily so disk I/O happens once,
        on the first frame the empty state is painted, and the result
        is cached on the chat instance. Switching profiles invalidates
        the cache via _resolve_intro_art's check against the resolved
        profile name.
        """
        return self._resolve_intro_art() is not None

    def _resolve_intro_art(self):
        """Lazy-load and cache the active profile's chat_intro_art.

        Returns the BrailleArt instance, or None if the profile has
        no art configured or the file failed to load. Cached on the
        instance until the active profile changes — set
        self._intro_art_resolved = False after a profile swap to
        force a reload.
        """
        if self._intro_art_resolved:
            return self._intro_art
        from .render.intro_art import load_intro_art
        if self.profile is None:
            self._intro_art = None
        else:
            self._intro_art = load_intro_art(self.profile.chat_intro_art)
        self._intro_art_resolved = True
        return self._intro_art

    def _paint_empty_state(
        self,
        grid: Grid,
        top: int,
        bottom: int,
        width: int,
        theme: ThemeVariant,
    ) -> None:
        paint_empty_state_surface(
            grid,
            top,
            bottom,
            width,
            theme,
            panel_lines=self._build_intro_panel_lines(),
            resolve_intro_art=self._resolve_intro_art,
        )

    def _build_intro_panel_lines(self) -> list[tuple[str, str, bool, bool]]:
        """Build the rows for the empty-state info panel.

        Returns a list of (label, value, is_header, is_hint) tuples.
        Headers paint as dim uppercase labels, value rows paint
        indented two cells. The last row is the hint, painted in
        accent_warm and centered.

        Built from the active profile + client state, NOT from any
        cached info — runs on every empty-state paint, so changes
        propagate immediately if the user swaps theme or density
        before sending their first message.
        """
        rows: list[tuple[str, str, bool, bool]] = []

        # PROFILE section
        rows.append(("profile", "", True, False))
        rows.append(("", self.profile.name if self.profile else "(none)", False, False))
        rows.append(("", "", False, False))

        # PROVIDER section
        provider_cfg = (self.profile.provider or {}) if self.profile else {}
        provider_type = provider_cfg.get("type") or "llamacpp"
        model = provider_cfg.get("model") or self.client.model
        # Strip leading "openai/" from openrouter slugs since the panel
        # already names openrouter as the provider type — keeps the
        # value column tight.
        if provider_type == "openai_compat":
            base_url = provider_cfg.get("base_url", "")
            if "openrouter" in base_url:
                provider_label = "openrouter"
            elif "openai.com" in base_url:
                provider_label = "openai"
            else:
                provider_label = "openai-compat"
        else:
            provider_label = provider_type
        rows.append(("provider", "", True, False))
        rows.append(("", provider_label, False, False))
        rows.append(("", model, False, False))
        # Resolved context window — use the chat's existing resolver
        # so the same number that drives compaction shows up here.
        try:
            window = self._resolve_context_window()
            rows.append(("", f"{window:,} tokens", False, False))
        except Exception:  # noqa: BLE001
            pass
        # Health: a one-word green/red signal so the user knows the
        # next message will work.
        if self._server_health_ok is True:
            rows.append(("", "● reachable", False, False))
        elif self._server_health_ok is False:
            rows.append(("", "○ unreachable", False, False))
        rows.append(("", "", False, False))

        # TOOLS section
        tools = list(self.profile.tools) if self.profile and self.profile.tools else []
        rows.append(("tools", "", True, False))
        if tools:
            for t in tools:
                rows.append(("", tool_label(t), False, False))
        else:
            rows.append(("", "(none enabled)", False, False))
        rows.append(("", "", False, False))

        # APPEARANCE section — read LIVE state, not the profile's
        # stored values, so the panel reflects mid-session changes
        # like Ctrl+T (cycle theme) or Alt+D (toggle dark/light) that
        # haven't been written back to the profile yet.
        theme_name = self.theme.name if self.theme else "steel"
        mode = self.display_mode if self.display_mode else "dark"
        density = self.density.name if self.density else "normal"
        rows.append(("appearance", "", True, False))
        rows.append(("", f"{theme_name} · {mode} · {density}", False, False))
        rows.append(("", "", False, False))

        # Hint at the bottom — the most actionable thing on the screen.
        rows.append(("type / for commands · press ? for help", "", False, True))

        return rows

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
        paint_chat_scene_row(
            grid,
            x,
            y,
            body_width,
            row,
            theme,
            prefix_width=_PREFIX_W,
            elapsed=self.elapsed,
        )

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
            if _message_has_tool_artifact(msg):
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
        return fade_prepainted_chat_rows(rows, bg_color, toward_bg_amount)

    def _render_tool_card_rows(
        self,
        msg: "_Message",
        body_width: int,
        theme: ThemeVariant,
    ) -> list[_RenderedRow]:
        return render_tool_chat_card_rows(msg, body_width, theme)

    def _render_running_tool_card_rows(
        self,
        msg: "_Message",
        body_width: int,
        theme: ThemeVariant,
        runner: BashRunner,
    ) -> list[_RenderedRow]:
        return render_running_chat_card_rows(msg, body_width, theme, runner)

    def _render_subagent_card_rows(
        self,
        msg: "_Message",
        body_width: int,
        theme: ThemeVariant,
    ) -> list[_RenderedRow]:
        return render_subagent_chat_card_rows(msg, body_width, theme)

    def _render_md_lines_with_search(
        self,
        md_lines: list[LaidOutLine],
        msg_raw_text: str,
        matches: list[tuple[int, int, int]],
        prefix: str,
        base_color: int,
    ) -> list[_RenderedRow]:
        query = self._search_query.lower() if self._search_active else ""
        return render_markdown_rows_with_search(
            md_lines,
            query,
            matches,
            prefix,
            base_color,
            prefix_width=_PREFIX_W,
        )

    def _highlight_spans(
        self,
        spans: list[LaidOutSpan],
        query: str,
    ) -> list[LaidOutSpan]:
        return highlight_row_spans(spans, query)

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
        display_text = _tool_preview_text(name, raw_arguments)

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
        inferred = _infer_tool_preview(display_text) if name == "bash" else None
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
