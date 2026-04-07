"""Headless snapshot — render a chat state to text or ANSI without a TTY.

The renderer is deterministic and pure, so we can drive it without
ever entering an alt screen. This module provides:

  - render_grid_to_plain(grid): walk a Grid and return its visible text
                                (no styles, just chars). Useful for
                                tests and rough previews.

  - render_grid_to_ansi(grid):  produce a full ANSI dump of a Grid that
                                can be `cat`ed into a terminal to
                                replay the rendered frame.

  - rn snapshot subcommand:     load a chat scenario, render one frame
                                at a chosen size, output text or ANSI
                                to stdout or a file.

The snapshot subcommand is powered by `chat_demo_snapshot()` which
constructs a fresh RoninChat with a scripted set of messages and
returns a single rendered frame. Marketing material, documentation
images, and bug-repro screenshots all flow through here.
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
    from .demos.chat import (
        RoninChat,
        _Message,
        SLASH_COMMANDS,
        find_slash_command,
    )
    from .render.theme import get_theme, normalize_display_mode
    from .demos.chat import find_density

    chat = RoninChat()

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
                "ronin",
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
                "about the way of the blade. I should respond in character "
                "as a samurai sage. Maybe something about how the breath is "
                "the seed of motion."
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
                "ronin",
                "The blade is not steel. The blade is the silence between heartbeats.",
            )
        )
        chat.messages.append(_Message("user", "and the path of the blade?"))
        chat.messages.append(
            _Message(
                "ronin",
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
