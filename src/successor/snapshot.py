"""Headless snapshot — render a chat state to text or ANSI without a TTY.

The renderer is deterministic and pure, so we can drive it without
ever entering an alt screen. This module provides:

  - render_grid_to_plain(grid): walk a Grid and return its visible text
                                (no styles, just chars). Useful for
                                tests and rough previews.

  - render_grid_to_ansi(grid):  produce a full ANSI dump of a Grid that
                                can be `cat`ed into a terminal to
                                replay the rendered frame.

  - chat_demo_snapshot(...):    construct a fresh SuccessorChat with a
                                scripted set of messages and return a
                                single rendered frame.

  - wizard_demo_snapshot(...):  construct a fresh SuccessorSetup wizard at
                                a chosen step with chosen state and
                                return a single rendered frame.

The snapshot helpers are used both by tests (visual regression) and
by `successor snapshot` (marketing material, documentation images, bug-repro
screenshots).
"""

from __future__ import annotations

import time

from .render.cells import Grid
from .render.diff import render_full


def render_grid_to_plain(grid: Grid) -> str:
    """Walk a Grid and return its visible text (no styling).

    Each row becomes a line. Trailing whitespace is stripped from each
    row. Wide-char trailing cells are skipped.
    """
    lines: list[str] = []
    for r in range(grid.rows):
        row_chars: list[str] = []
        for c in range(grid.cols):
            cell = grid.at(r, c)
            if cell.wide_tail:
                continue
            row_chars.append(cell.char if cell.char else " ")
        lines.append("".join(row_chars).rstrip())
    return "\n".join(lines)


def render_grid_to_ansi(grid: Grid) -> str:
    """Render a Grid as full ANSI output suitable for `cat`ing.

    Wraps render_full() (the diff layer's first-frame path) and adds
    a trailing reset + newline.
    """
    payload = render_full(grid)
    return payload + "\x1b[0m\n"


def chat_demo_snapshot(
    *,
    rows: int = 30,
    cols: int = 100,
    theme_name: str = "steel",
    display_mode: str = "dark",
    density_name: str = "normal",
    scenario: str = "showcase",
) -> Grid:
    """Build a Grid showing the chat in a chosen state.

    scenario:
      "showcase"   default — markdown sampler + bullet/code/quote
      "blank"      empty chat with the greeting
      "thinking"   a streaming reply mid-thinking-phase with reasoning
      "search"     search mode active over a sample conversation
      "help"       help overlay open
      "autocomplete"  the slash command palette open at /

    Returns a fresh Grid containing one fully-rendered frame. The
    chat App is constructed without entering its terminal context, so
    no TTY is required. Useful for screenshots, documentation, and
    headless tests.

    The (theme_name, display_mode) pair is now ORTHOGONAL — theme picks
    the visual identity, display_mode picks dark vs light. Same theme
    in both modes is the typical "switch dark/light" snapshot pair.
    """
    # Imports here to avoid pulling chat into module-level when only
    # snapshot helpers are needed.
    from .chat import (
        SuccessorChat,
        _Message,
        SLASH_COMMANDS,
        find_slash_command,
    )
    from .render.theme import get_theme, normalize_display_mode
    from .chat import find_density

    chat = SuccessorChat()

    target_theme = get_theme(theme_name)
    if target_theme is not None:
        chat.theme = target_theme
        chat._theme_from = None

    chat.display_mode = normalize_display_mode(display_mode)
    chat._mode_from = None

    target_density = find_density(density_name)
    if target_density is not None:
        chat.density = target_density
        chat._density_from = None

    if scenario == "blank":
        pass  # just the greeting
    elif scenario == "showcase":
        chat.messages.append(_Message("user", "show me what you can do"))
        chat.messages.append(
            _Message(
                "successor",
                "# What I can render\n\n"
                "**Bold**, *italic*, ~~strikethrough~~, and `inline code`.\n\n"
                "- Bullet lists work\n"
                "- With multiple items\n"
                "- And **bold inside** them\n\n"
                "1. Numbered lists too\n"
                "2. With proper alignment\n\n"
                "```python\n"
                "def meditate():\n"
                "    return 'stillness'\n"
                "```\n\n"
                "> A quote, of course. Brevity is honor.\n\n"
                "Plus [linked text](https://example.com).",
            )
        )
    elif scenario == "thinking":
        chat.messages.append(_Message("user", "what is the way of the blade"))

        class _FakeStream:
            done = False
            reasoning_so_far = (
                "Let me think through this carefully. The user is asking "
                "about the way of the blade. I should respond as a thoughtful "
                "voice. Maybe something about how the breath is the seed "
                "of motion."
            )
            def drain(self): return []

        chat._stream = _FakeStream()
        chat._stream_content = []
        chat._stream_reasoning_chars = len(_FakeStream.reasoning_so_far)
        chat.streaming = {"phase": "think", "phase_start": time.monotonic()}
    elif scenario == "search":
        chat.messages.append(_Message("user", "tell me about the blade"))
        chat.messages.append(
            _Message(
                "successor",
                "The blade is not steel. The blade is the silence between heartbeats.",
            )
        )
        chat.messages.append(_Message("user", "and the path of the blade?"))
        chat.messages.append(
            _Message(
                "successor",
                "Walk the path until the blade walks you. The blade leads where the will follows.",
            )
        )
        chat._search_active = True
        chat._search_query = "blade"
        chat._search_recompute()
    elif scenario == "help":
        chat._help_open = True
        chat._help_opened_at = time.monotonic() - 1.0
    elif scenario == "autocomplete":
        chat.input_buffer = "/"

    g = Grid(rows, cols)
    chat.on_tick(g)
    return g


def config_demo_snapshot(
    *,
    rows: int = 30,
    cols: int = 120,
    focus: str = "settings",
    profile_cursor: int = 0,
    settings_cursor: int = 0,
    editing: bool = False,
    dirty: tuple[tuple[str, str], ...] = (),
    elapsed: float = 0.5,
) -> Grid:
    """Build a Grid showing the config menu in a chosen state.

    focus: which pane has keyboard focus — "profiles" or "settings".

    profile_cursor: index of the profile under the left-pane cursor.

    settings_cursor: row index in the settings tree (skipping read-only
        rows isn't enforced here — caller passes the actual index from
        _SETTINGS_TREE).

    editing: when True, the inline edit overlay is shown for the
        current settings_cursor field.

    dirty: tuple of (profile_name, field_name) pairs to mark as
        unsaved-changes for visual testing.

    elapsed: simulated runtime, used for animation states.
    """
    from .wizard.config import SuccessorConfig, Focus

    menu = SuccessorConfig()
    menu._focus = Focus.PROFILES if focus == "profiles" else Focus.SETTINGS
    if menu._working_profiles:
        menu._profile_cursor = max(0, min(profile_cursor, len(menu._working_profiles) - 1))
    menu._settings_cursor = settings_cursor
    if editing:
        menu._editing_field = settings_cursor
        menu._editing_cursor = 0

    for entry in dirty:
        menu._dirty.add(entry)

    menu._sync_preview()
    menu._t0 = time.monotonic() - elapsed
    menu._section_reveal_at = max(0.0, elapsed - 0.5)

    g = Grid(rows, cols)
    menu.on_tick(g)
    return g


def wizard_demo_snapshot(
    *,
    rows: int = 30,
    cols: int = 100,
    step: str = "welcome",
    name: str = "",
    theme_name: str = "steel",
    display_mode: str = "dark",
    density: str = "normal",
    intro_animation: str | None = None,
    enabled_tools: tuple[str, ...] | None = None,
    elapsed: float = 0.5,
) -> Grid:
    """Build a Grid showing the setup wizard at a chosen step.

    step: which Step enum value to render. Accepts the lowercase enum
        name (welcome / name / theme / mode / density / intro / review /
        saved). Defaults to "welcome".

    name, theme_name, display_mode, density, intro_animation: the
        wizard's in-progress state when the snapshot is taken. Lets
        tests assert that the renderer reflects the chosen values
        (e.g. the title bar shows the entered name, the live preview
        uses the chosen theme).

    elapsed: simulated time since the wizard started, used to advance
        animations (typewriter, pulse, transitions). Defaults to 0.5s
        which is past most reveal animations but before the welcome
        typewriter completes.

    Returns a fresh Grid containing one fully-rendered frame. No TTY
    required. Used by both the test suite (visual regression) and
    `successor snapshot` for marketing material.
    """
    from .wizard.setup import SuccessorSetup, Step, _WizardState
    from .tools_registry import default_enabled_tools

    wizard = SuccessorSetup()

    # Apply state directly. The wizard's _sync_preview_to_state takes
    # care of pushing into the preview chat.
    tools_tuple = (
        tuple(enabled_tools) if enabled_tools is not None
        else default_enabled_tools()
    )
    wizard.state = _WizardState(
        name=name,
        theme_name=theme_name,
        display_mode=display_mode,
        density=density,
        intro_animation=intro_animation,
        enabled_tools=tools_tuple,
    )
    wizard._sync_preview_to_state()

    # Resolve the step name to its enum value
    step_lookup = {s.name.lower(): s for s in Step}
    target_step = step_lookup.get(step.lower(), Step.WELCOME)
    wizard._enter_step(target_step)

    # Pin self.elapsed by setting _t0 retroactively. App.elapsed is
    # `time.monotonic() - self._t0`, so picking _t0 = now - elapsed
    # gives the wizard the appearance of having been running for
    # `elapsed` seconds.
    wizard._t0 = time.monotonic() - elapsed
    wizard._step_entered_at = max(0.0, elapsed - 0.5)
    if target_step == Step.WELCOME:
        wizard._welcome_started_at = wizard._step_entered_at

    g = Grid(rows, cols)
    wizard.on_tick(g)
    return g
