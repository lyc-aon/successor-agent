# Implementation Concepts, What Our Renderer Enables

This document is the running list of features and behaviors that Successor's
rendering architecture makes possible. None of these are "blocked by the
renderer", they're all small additive changes to pure-function modules.
Some are flashy, some are foundational, all of them are doable without
breaking [the One Rule](rendering-superpowers.md#the-one-rule).

The list is organized by **capability category** (what underlying renderer
property makes the idea possible) rather than by feature, because once you
understand the categories, you can invent your own.

---

## Effort scale

Throughout this doc:

| Tag | Meaning |
|---|---|
| **XS** | One paint function, ~50 lines, an afternoon |
| **S** | New module + integration, ~150 lines, a day |
| **M** | Multiple modules + state mutation, ~400 lines, a few days |
| **L** | Architectural addition + multiple new primitives, ~1000+ lines |

## Value scale

| Tag | Meaning |
|---|---|
| **F** | Foundational, unblocks other features |
| **U** | Unique, no other agent harness can do this |
| **V** | Vibe, it's flashy, makes people go "oh" |
| **T** | Tool, useful for our own development |

---

## Category 1, Cells we paint are mutable forever

The rendered chat history is just data in `self.messages` + paint
operations. Until the message scrolls off-screen *and* we stop tracking
it (which we never do), every cell of every message can be re-painted
on the next frame.

### Inline collapsible tool calls, `S, U, V`

When the agent calls a tool, render it inline as `▸ read_file('/path')
· 1.2s · 47 lines`. Press a hotkey (`o`?) on the focused message to
expand → animated row-height growth → reveals the full tool output.
Press again to collapse.

- New state: per-message `expanded: bool`
- New paint logic in `_build_message_lines`: if expanded, append the
  tool output lines after the message body
- Effort: ~150 lines, mostly in `chat.py`
- Why no other harness can do this: their tool output is committed to
  scrollback and immutable. Ours is a paint function.

### Edit-and-fix the previous message, `XS, U`

Slash command `/edit` reopens the last user message in the input area
with its content prefilled. Edit and resubmit → the old message is
replaced in `self.messages` (not duplicated) and successor re-responds.

- New state: `self.editing_index: int | None`
- New on_key handling for the `/edit` command
- Re-paint reflects the new content automatically
- Effort: ~80 lines

### Strikethrough retracted content, `XS, U`

When successor says something like "actually, that's wrong, the answer is..."
the renderer detects the retraction phrase and renders the wrong portion
with `ATTR_STRIKE` styling. Optional, opt-in via a slash command on
successor's behalf.

- New paint rule in `_build_message_lines`: if a message contains a
  retraction marker, slice the body and apply different styles to the
  before/after sections
- Effort: ~50 lines

### Annotate messages with metadata glyphs, `XS, U`

A small left-gutter column on each message shows status glyphs:
- `★` starred
- `📌` pinned (we'd use `▌` or `■` to avoid emoji)
- `✓` verified
- `!` error
- `↻` regenerating

Cells in the gutter are mutable per-frame, so glyphs update instantly
when the metadata changes.

- New state: per-message `tags: set[str]`
- Paint a 2-cell gutter before each message body
- Effort: ~80 lines

### Re-color old messages based on later evidence, `XS, U`

When a tool call later in the conversation contradicts an earlier
assistant message, dim the now-questionable message. The render rule
just checks "has any later message marked this as 'contradicted'" and
adjusts the fg color. Other harnesses can't do this, once they print
the message, they can't reach back to color it differently.

- New state: `_Message.contradicted: bool`
- Render rule in `_build_message_lines`: if contradicted, lerp the
  base color toward `INK_DUST`
- Effort: ~30 lines (the hard part is detecting contradictions, which
  is an agent-loop problem, not a renderer problem)

---

## Category 2, Smooth animation on anything

The frame loop ticks at 30 fps with `time.monotonic()` driving every
animation. Any visual effect can be expressed as `(state, time) → cells`.

### Live token-rate gauge, `XS, V`

In the title row (right side, near the scroll indicator), paint a
small horizontal sparkline showing tokens-per-second over the last
30 frames. Updates every frame, color-shifts from cool blue to warm
red as throughput rises.

- New state: ring buffer of (time, token_count) samples
- New paint helper: `paint_sparkline(grid, x, y, w, samples, color_lerp_fn)`
- Effort: ~60 lines

### Animated tool spinner that morphs into a checkmark, `S, V`

When a tool starts, render a braille spinner in the message gutter.
When it completes, the spinner cells animate over 200ms into a `✓`
glyph via cell-by-cell transition. We already have all the primitives:
braille spinner from `chat.py`, lerp from `text.py`, frame timing from
the App loop.

- New state: per-tool-call `started_at: float`, `completed_at: float | None`
- Paint rule: pick the cell content based on (now - started_at) and
  (now - completed_at) timing windows
- Effort: ~120 lines

### Toast notifications that slide in and fade out, `XS, V`

A `toast(message, duration=3.0)` API that paints a small box in the
top-right corner over the existing UI. Slides in from the right over
200ms, fades to dim over the last 500ms of its lifetime, disappears.

- New state: `self.toasts: list[Toast]` where Toast = (text, started_at,
  duration)
- New paint pass after the chat area, before the input/footer
- Effort: ~80 lines

### Spotlight effect, `S, V, U`

Agent says "look at the URL on line 3 of my last response." The renderer
draws an animated bracket or glow around the relevant cells, fading
in over 300ms and slowly pulsing while the agent explains. Other
harnesses can't highlight existing committed text at all.

- New state: `self.spotlight: SpotlightRegion | None` with start time
  and lifetime
- New paint rule that overlays a styled border around the target cells
- Effort: ~150 lines

### Section reveal animations, `XS, V`

When a long successor response commits, instead of dumping the whole thing
at once, the message reveals one paragraph at a time with a 100ms
delay between paragraphs. Each paragraph fades in via the same lerp
machinery the message-level fade uses, but at sub-message granularity.

- Refactor `_Message` to have paragraph-level created_at offsets
- Effort: ~60 lines

---

## Category 3, Multi-region UI

The compositor doesn't care about regions. They're all rectangles in
the same Grid. We can stack as many as we want.

### Left sidebar with active sessions, `M, U`

A 25-column sidebar on the left listing all open chat sessions /
projects / conversations. Clickable (when we have mouse mode) or
keybindable. Selected session is highlighted; others are dim.

- New layout math: chat_top, chat_left = sidebar_width, etc.
- New `Session` data type that wraps the existing chat state
- The chat App becomes a multi-session shell
- Effort: ~400 lines, mostly state restructuring

### Inline diff between two responses, `S, U, V`

"Show me my previous answer compared to this one" → split the chat
area horizontally, render two responses side-by-side with cell-level
diff highlighting (added cells in green, removed in red). Press a
hotkey to swap left/right or toggle which is "current".

- New paint helper: `paint_diff(grid, region, lines_a, lines_b)`
- Effort: ~200 lines

### Floating popup palette (Cmd-K style), `M, V, U`

Press a hotkey (e.g. `Ctrl+K`), a centered overlay box appears with a
fuzzy-search input. Type to filter slash commands / past messages /
saved snippets. Up/Down to select, Enter to invoke. Esc to close.

- New paint pass that draws a centered box with shadow on top of the
  existing UI
- Reuses existing input handling pattern
- Effort: ~300 lines

### Right sidebar showing current tool execution state, `S, V, U`

When the agent is running tools, a 30-column right sidebar shows
each tool call as it's happening: tool name, args (truncated), status
(spinner/check/cross), elapsed time, output preview. Real-time updates
without disturbing the chat scroll.

- New layout math + new region painter
- Effort: ~250 lines

### Bottom drawer for "agent thinking" output, `S, V`

A pull-up drawer (toggleable with hotkey) that shows the agent's
internal reasoning, hidden tool chatter, or live state. Slides up
from the bottom over the input area. The chat area shrinks to make
room, smoothly.

- New state: `self.drawer_height: int`, `_drawer_target_height: int`
- Animate the height transition by lerping `drawer_height` toward
  the target each tick
- Effort: ~200 lines

---

## Category 4, Search and navigation

The conversation lives in `self.messages` as Python objects. We can
search it, jump around in it, and re-render with style overlays.

### Search across history with live highlights, `S, U, V`

Press `/` to open a search bar (replaces the input area or appears
above it). As the user types, every cell in past messages that
matches gets re-painted with a highlight bg color. `n` and `N`
jump between matches with smooth scroll-to-position.

- New state: `self.search_query: str | None`, `self.search_matches: list[Match]`
- New paint rule in `_build_message_lines`: if a span matches, paint
  it with `ATTR_REVERSE` or a different bg
- New scroll behavior: jump to the line containing the next/prev match
- Effort: ~250 lines
- **No other agent harness can do this** because their past content
  is in terminal scrollback that they can't re-style.

### Jump to message N, `XS, T`

Slash command `/g 5` jumps the scroll position so message 5 is at
the top of the chat area. Smooth animated scroll over ~200ms.

- New scroll method: `_scroll_to_message_index(idx, animate=True)`
- Effort: ~50 lines

### "Go to definition" inside a chat message, `M, V, U`

The agent's response references "the function I wrote two messages
ago." A hotkey resolves the reference and scrolls + spotlights the
target. Combines the spotlight effect from category 2 with the
message indexing from this category.

- Effort: ~300 lines (most of which is the reference resolution, not
  the renderer)

### Filter mode, `S, U`

Press a hotkey to filter the chat to only messages matching a query
or only messages from a specific role or only messages with errors.
Filtered-out messages collapse into a "... 5 hidden messages ..."
divider. Toggle filter off to expand back.

- New state: `self.filter_predicate: Callable[[Message], bool] | None`
- `_build_message_lines` walks the filtered list with collapse markers
- Effort: ~150 lines

---

## Category 5, Replayable, deterministic rendering

`on_tick(grid)` is a pure function of `(state, time, grid_size)`.
This unlocks development tooling that no other harness can build.

### Session recording and playback, `M, T`

Record every key event with its timestamp. To replay: feed the
recorded events back into a fresh App with simulated time. The
playback is pixel-identical to the original session because the
renderer is deterministic.

- New `Recorder` class that wraps `App.on_key` and the time source
- New `Player` class that drives an App with recorded events at
  configurable speed
- Effort: ~300 lines
- **Killer use case**: bug reports become reproducible with one file.
  Crash trace + the recording = exact reproduction.

### Headless screenshot generation, `XS, T`

Given a session state, render `on_tick` into a Grid, walk the cells,
output as ANSI text or as an image (via PIL). Useful for documentation,
release notes, marketing.

- New helper: `render_to_ansi(app)` and `render_to_png(app, font)`
- Effort: ~100 lines for ANSI, ~250 for PNG (needs font handling)

### Frame-by-frame debugger, `M, T, V`

A "step mode" hotkey that pauses the frame loop. Subsequent presses
advance one frame at a time. Each frame, dump the Grid contents to
a side panel for inspection. Useful for finding rendering bugs.

- New App state: `self.paused: bool`, `self.step_requested: bool`
- Frame loop respects pause flag
- Effort: ~150 lines

### Session forking, `M, T, U`

At any point in a chat, press `Ctrl+Shift+F` to fork the session.
Now you have two independent conversations diverging from the same
prefix. Useful for exploring "what if I'd asked differently" without
losing the original thread.

- Deep-copy `self.messages` and the rest of state into a new App
- Multi-session UI from category 3 to view both
- Effort: ~200 lines on top of multi-session sidebar

---

## Category 6, Inline media

Our cell grid is a 2D array of styled glyphs. Anything that can be
turned into glyphs can be rendered inline.

### Inline braille image previews, `S, V, U`

Tool reads or generates an image → renderer converts it to a braille
bitmap (using our existing `parse_dots` / `pack_dots` / `resample_dots`
machinery) and renders it inline at the appropriate viewport size.
Image scales with terminal resize for free, since `BrailleArt` is
already viewport-aware.

- New helper: `image_to_braille_art(path) → BrailleArt`. Use PIL or
  raw bytes to read pixels, threshold to dots, build a `BrailleArt`.
- Inline render in `_build_message_lines` when a message has an
  attached image
- Effort: ~250 lines (most of which is image loading)

### Inline sparkline charts, `XS, V`

Agent computes some metric over time. Render it inline as a 1-row
unicode block sparkline (`▁▂▃▄▅▆▇█` characters). Works at any width
because the chars are width-1 monospace.

- New helper: `paint_sparkline(grid, x, y, w, values, color)`
- Effort: ~50 lines

### Inline progress bars, `XS, V`

Agent runs a long task with measurable progress. Render an inline
progress bar in the message body using `█░` block characters with
color lerp from blood → ember → gold (same as the ctx bar).

- New helper: `paint_progress_bar(grid, x, y, w, pct, color_fn)`
- Effort: ~50 lines

### Inline tables, `S, V`

Agent returns tabular data. Render it as a real table with borders,
column alignment, and overflow handling. Reuses paint primitives.

- New helper: `paint_table(grid, x, y, w, headers, rows)`
- Width-aware: each column gets a fraction of the available width
- Effort: ~200 lines

### ASCII art via codepoint tables, `XS, V`

Render banner text using a glyph table (one row per character of
banner text, expanded to multi-row block letters). The braille intro
animation already shows that frame-shaped art renders cleanly through
the existing paint pipeline; this is the same idea applied to ASCII
banner fonts.

- New asset: a banner font as a dict of `char` to `list[str]`
- New paint helper: `paint_banner(grid, x, y, text, font)`
- Effort: ~100 lines

---

## Category 7, The agent can drive the UI

Once we have the event bus and the agent loop, the agent can issue
state mutations that the renderer responds to. This is the bridge
between "agent computes things" and "user sees things."

### Programmatic scrolling, `XS, U`

Agent: "Scroll back to where I mentioned the API endpoint." Renderer:
`_scroll_to_message_index(target_idx, animate=True)`. The user sees
a smooth animated scroll to the right place.

- Effort: ~30 lines (depends on `_scroll_to_message_index` from
  category 4)

### Programmatic spotlights, `XS, U`

Agent: "Look at the third bullet point in my response." Renderer:
spotlight that range of cells with a fade-in pulse.

- Reuses category 2 spotlight effect
- Effort: ~30 lines

### Programmatic toast notifications, `XS, U`

Agent emits an event saying "I'm starting a long operation." Renderer
shows a toast. Agent emits "done." Renderer dismisses the toast or
replaces it with a result.

- Reuses category 2 toast system
- Effort: ~30 lines

### Live status banner, `XS, U`

A small banner row above the input shows the agent's current activity:
"thinking", "running tool: read_file", "compacting context", "idle."
Updates frame-by-frame from the agent event stream.

- New region in the layout, between chat area and input
- Effort: ~80 lines

### Mid-message correction stream, `S, U, V`

Agent realizes mid-stream that it's saying something wrong. Instead
of continuing past the error, it issues a `RetractFrom(char_index=120)`
event. The renderer animates the wrong portion fading out (color lerp
to dim, then to bg) over 300ms, then continues streaming from char 120.

- New state on the streaming reply: `retracted_until: int`
- Render rule: chars before `retracted_until` paint dim, chars after
  paint normal
- Animate the transition with `ease_out_cubic`
- Effort: ~120 lines

---

## Category 8, Long-shot ideas

Things that are possible but speculative.

### Two-user collaborative session over IPC, `L, U`

Two users on the same machine attach to the same `SuccessorChat` instance
via Unix socket. Both see the same messages, both can scroll
independently because each maintains their own `scroll_offset`. Like
Google Docs collab but in the terminal.

- New state model: chat state is shared, view state is per-user
- New IPC layer: socket protocol for state sync + key event forwarding
- Effort: ~1500+ lines

### Remote session over the diff stream, `L, U`

Run the renderer on a remote machine. Stream only the diff bytes
(typical 50-300/frame) to a local "thin client" terminal. Compared
to Rich's full-line redraws (which produce KB per change), this would
be the only agent harness that runs comfortably over a 1990s dial-up
modem. ~10 KB/s steady state for a fully animated chat.

- New `RemoteApp` that sends diffs over a socket instead of stdout
- New thin-client binary that receives diffs and writes them to local
  stdout
- Effort: ~800 lines

### Animated SVG / GIF export, `M, T, V`

Given a session recording, render every frame to an SVG snapshot,
then either output them as an animated SVG or compose them into an
MP4/GIF. Perfect for documentation and marketing screencasts.

- New helper: `grid_to_svg(grid, font_metrics)`
- Optional ffmpeg integration for video output
- Effort: ~400 lines

### Frame-rate scrubbing in replay mode, `M, T`

In playback mode, scroll-wheel-style scrub through the session timeline
with frame-perfect granularity. Pause, advance, rewind, jump to any
frame. Like Final Cut for terminal recordings.

- Combines session recording (5) with frame-by-frame debugger (5)
- Effort: ~500 lines on top of those

---

## Priority sort (my honest take)

For pure forward motion, I'd build them in roughly this order:

1. **Real key parser** (foundational, currently in chat.py inline)
2. **Event bus + AgentEvent types** (foundational)
3. **Inline collapsible tool calls** (small, high value, exercises the
   per-message metadata pattern)
4. **Search with highlights** (medium, U+V, shows off the architecture)
5. **Animated tool spinner → checkmark** (small, V, makes the agent
   feel alive)
6. **Programmatic scrolling + spotlights** (small, agent-drives-UI,
   unlocks new agent UX patterns)
7. **Session recording + replay** (medium, T, becomes our debugging
   superpower for everything else)
8. **Multi-session sidebar** (medium, U, unlocks tabs/forks)
9. **Inline braille image previews** (small after PIL is in)

Everything else is gravy. We can pick from this menu based on whatever's
most valuable when we get there.

---

## The meta-pattern

Every concept above follows the same shape:

1. Define a new piece of state (or extend existing state)
2. Write a new paint helper as a pure function in `render/`
3. Wire it into the appropriate `_paint_*` method in the App
4. The diff layer handles the rest for free

That's the entire pattern. Every feature is "add state, add a paint
function, call it from `on_tick`." Nothing more.

Compare to other harnesses where every feature requires:

1. Find a place in the print stream that won't break Rich's auto-wrap
2. Or rewire prompt_toolkit's layout to make room for the new region
3. Or fight patch_stdout to let the new content through
4. Or accept flicker because the redraw can't be partial
5. Or give up on resize handling

This is why our renderer is going to keep being able to do things
nobody else can. Not because it's clever, because it doesn't fight.
