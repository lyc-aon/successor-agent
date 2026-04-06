# Rendering Superpowers — Read Me First

> If you're about to add `import rich`, `import prompt_toolkit`, `import textual`,
> or any other "let me just import a TUI library" to Ronin: **read this entire
> document first**. Then decide whether you still need to. The answer should
> almost always be no.

---

## The One Rule

**`src/ronin/render/diff.py` is the only module in the entire codebase
allowed to write to stdout.**

Not Rich. Not prompt_toolkit. Not `print()`. Not `sys.stdout.write()`. Not
your own one-off escape sequences from somewhere convenient.

The diff layer holds the contract with the terminal. Nothing else gets to
break it. If you find yourself wanting to write to stdout from outside
`diff.py`, the answer is: **paint into the Grid via `paint.py` instead**.
The renderer was designed so that's always possible. Every feature in
Ronin so far has fit through this hole, and that is not a coincidence.

---

## Why this exists

The previous attempt at this harness (`~/dev/ai/hk13/`) ended up with
**nine non-negotiable rules** in its rendering reference doc, just to keep
Rich + prompt_toolkit + patch_stdout from corrupting each other's screen
state. Every rule was a treaty clause between two libraries that each
assumed they owned stdout. The visible output looked OK; the architecture
was a war.

Ronin's design eliminates that war by having one screen owner. Not "fewer
fights," not "better isolation" — *zero* fights, by construction. The diff
layer is the only stdout writer, so there is nothing for it to fight with.

This single decision is why Ronin's chat already renders better than every
other agent harness — and it's also why the codebase is small, why resize
doesn't flicker, why the renderer is testable without a terminal, and why
every feature is a one-line addition somewhere.

---

## The five layers

```
┌─────────────────────────────────────────────────────────┐
│ Layer 5: COMMITTER  (diff.py)                           │
│   Diffs current frame vs previous frame.                │
│   Emits minimal ANSI (cursor moves + SGR + chars).      │
│   THE ONLY MODULE THAT WRITES TO STDOUT.                │
├─────────────────────────────────────────────────────────┤
│ Layer 4: COMPOSITOR  (paint.py)                         │
│   Stacks layout output into a virtual cell grid.        │
│   Knows about regions, fills, centering, clipping.      │
├─────────────────────────────────────────────────────────┤
│ Layer 3: LAYOUT  (paint.py + text.py + braille.py)     │
│   Pure: (text/art, target_width) → grid mutations.      │
│   Re-runnable on resize with no re-parse.               │
├─────────────────────────────────────────────────────────┤
│ Layer 2: PREPARE  (text.py + braille.py)                │
│   Tokenize / parse the source ONCE into a measured     │
│   structure. Cache the result. Pretext-shaped.          │
├─────────────────────────────────────────────────────────┤
│ Layer 1: MEASURE  (measure.py)                          │
│   char_width(ch) → 0 | 1 | 2                            │
│   strip_ansi(s)  → text without CSI sequences           │
│   Pure functions. East Asian Width tables.              │
└─────────────────────────────────────────────────────────┘

Plus three support modules:
  cells.py     Cell, Style, Grid (the data layers operate on)
  terminal.py  TTY setup/teardown, alt-screen, SIGWINCH, restore
  app.py       Frame loop with double-buffered diffing + input
```

**Layers 1–4 are pure.** They don't touch the terminal, never block,
never depend on signal state, never allocate file descriptors. Only
Layer 5 emits bytes. This is what makes the renderer testable, replayable,
crash-safe, and immune to library coexistence bugs.

---

## Validated speedups (the cache pattern)

The "prepare once, layout many times with width-keyed cache" pattern from
Cheng Lou's Pretext maps directly onto terminal rendering. Two places it
pays off in Ronin today:

| Primitive | Cache hit | Cache miss | Speedup |
|---|---|---|---|
| `BrailleArt.layout(cells_w, cells_h)` | 0.4 ms | 6.2 ms | **16×** |
| `PreparedText.lines(width)` | 0.15 µs | 77.83 µs | **519×** |

A 100-message conversation re-flowing on resize at the new width: every
visible message hits the cache after the first paint at that width. The
chat scrollback re-laying out is essentially free.

---

## What this rule unlocks

These are the things our renderer can do that other agent harnesses
either can't or have to fight their stack to attempt. They split into
"genuinely impossible elsewhere" and "massively easier here."

### Genuinely impossible in Rich+pt-style harnesses

1. **Edit any cell of any past message in-place, at frame rate.** The
   chat history is mutable in-memory data, not committed terminal output.
   Want to fix a typo in a previous assistant message? Strike through a
   retracted section? Add a "(verified)" annotation next to a tool result?
   Re-color a message after a tool error? Paint the new state, diff
   updates only the changed cells, the user sees the update with no
   flicker. Other harnesses can't do this *at all* — once they `print()`
   a line, it belongs to the terminal and they can't reach it.

2. **Multi-region UI without framework fights.** Sidebar + chat + input +
   popup + status bar all in the same frame. They're all just regions in
   the grid. There is no z-ordering library, no widget tree, no event
   bubbling — just paint operations into rectangles. The diff layer
   handles all of it identically.

3. **Search across the entire conversation history with live highlights.**
   Our messages are in `self.messages` as Python objects. We can grep,
   find matches, and re-render past content with highlighted styling on
   the next frame. Other harnesses can't highlight what's already in
   terminal scrollback because they don't own it anymore.

4. **Replayable, deterministic rendering.** Every frame is a pure function
   of `(state, time, terminal_size)`. Given the same inputs, `on_tick`
   produces the same Grid. We can record a session as `(timestamp,
   key_event, state_snapshot)` triples and replay it perfectly. The
   headless render tests we run during development are exactly this.
   The renderer **runs without a terminal**.

5. **Per-message styling based on metadata that arrived later.** Apply
   any visual treatment to any past message based on any metadata. Other
   harnesses can only apply ANSI escapes inline at print time; they can't
   re-style old content.

6. **The agent can drive scrollback.** "Scroll back to the place I
   mentioned the deployment URL" → agent issues a state mutation →
   renderer scrolls there with an animated transition. Most harnesses
   can't do this because they don't own the scroll position.

7. **Frame-perfect timing for everything.** Every animation, every
   blink, every fade is keyed to `time.monotonic()` per frame. There's
   no race condition with terminal output, no waiting for `print()` to
   flush, no jitter. The renderer ticks at a known frequency and every
   visual effect is interpolated deterministically.

### Massively easier here than elsewhere

8. **Smooth visual transitions on anything.** Fade, slide, scale,
   dissolve, color cycle. Express the effect as a per-frame function of
   `(state, time) → cells` and you're done. Examples already shipped:
   message fade-in via `lerp_rgb`, typewriter via character-slice,
   thinking spinner via braille frame cycling, cursor blink via time
   modulo.

9. **Resize without flicker, ever.** Every frame is computed from cached
   PreparedText / BrailleArt structures. Resize triggers re-layout from
   cache (cheap), recompose, diff, commit. No half-drawn frames, no
   scrollback corruption, no input area jumping.

10. **Selectable text that survives an animated UI.** We don't enable
    mouse mode, so the terminal's native click-drag selection works on
    every cell. Other harnesses that enable mouse reporting for
    "scroll wheel support" lose this entirely.

11. **No double-buffering drama.** Two grids, swap each tick, diff
    against last frame, emit minimal cells. Steady-state animation
    produces ~50–300 bytes/frame on a 200-col terminal — small enough
    to ship over a slow remote link if we wanted.

For the full list of specific applied ideas (inline collapsible tools,
search-with-highlights, inline diffs, conversation timeline, etc.), see
[`concepts.md`](concepts.md).

---

## Anti-patterns

If you catch yourself doing any of these, stop and ask why.

### `import rich` (as a screen owner)

Rich is great at one thing: turning a Python object into styled segments.
We can use Rich for **that** by calling `Console.render(renderable, options)`
to get a `list[Segment]` and translating those segments into our cell grid.
**No Rich byte ever reaches stdout.**

What we never do: instantiate a `Console` and call `.print()`. That's
giving Rich screen ownership and re-creating the hk13 problem.

### `import prompt_toolkit` (as an Application)

prompt_toolkit's strength is its input parser. We can use that *part*
without instantiating its `Application`. We're not using it yet because
we wrote our own ESC parser, but if we ever import it, it stays a parser,
never an Application.

What we never do: `Application(...).run()`. That gives pt screen
ownership.

### `from textual.app import App`

Textual is the most Pretext-aligned thing in Python and would actually
work, but it's a heavyweight reactive framework with widgets, CSS-like
styling, and an event loop that wants to own everything. We don't need
its model and we don't want its bulk.

### `print()` from anywhere except diff.py

Self-explanatory. Even debug print statements should go through a
"sidebar log region" painted into the grid, not stdout.

### Fixed coordinates

Never write `paint_text(grid, "x", 5, 12, ...)` with magic numbers.
Compute coordinates from `grid.rows / cols / chrome heights` so the
layout adapts to resize.

### Side effects in `on_tick`

`on_tick(grid)` should only paint into the grid. No file I/O, no network,
no print statements, no global mutation. Side effects break the "every
frame is a pure function of state" guarantee that makes replay /
testing / determinism work.

### Per-frame allocation in steady state

Each frame should not allocate a new Grid. The double-buffer pattern
in `App` avoids this. If you find yourself building per-frame data
structures inside `on_tick`, see if you can hoist them into `__init__`.

### Library-stacking to solve a UX problem

If you want richer chat formatting, more UI elements, animated widgets,
or any other "real TUI features" — write a small new pure-function
primitive in `src/ronin/render/`. Don't reach for a library to do it.

The renderer is designed to grow in pure-function modules, not by
stacking opinionated frameworks on top.

---

## How to extend the renderer correctly

When you want to add a new visual element (a chart, a popup, a sidebar,
a syntax-highlighted code block, an inline image), the recipe is:

1. **Identify the layer it belongs to.**
   - Pure measurement (a new width function)? Layer 1, `measure.py`.
   - Source → cached structure (a new prepared type)? Layer 2, new
     module under `render/` or extension to `text.py` / `braille.py`.
   - Cached structure → grid mutation (a new paint function)? Layer 3/4,
     `paint.py` or a new helper.
   - Anything that emits bytes? **No.** That's diff.py's job and it
     already does it.

2. **Make it a pure function.** Inputs: data + style + region. Output:
   mutations to a Grid. No I/O, no global state, no `print()`.

3. **Cache the prepare step if it's expensive.** Use the same pattern as
   `BrailleArt.layout()` and `PreparedText.lines()`: keyed by target
   width / size, single-entry cache, recompute on miss.

4. **Wire it into a demo or App with one line of paint code.** If it
   requires more than `paint_thing(grid, data, x, y, style=...)` in
   the call site, you've made it too complicated.

5. **Add a headless render test that asserts grid contents.** Because
   the renderer is pure, you can construct a Grid, call your new paint
   function, and walk the cells in a unit test. No PTY required.

If your new feature doesn't fit through this recipe, **the recipe is
not wrong** — your feature design has a hidden side effect. Find it
and remove it.

---

## When this rule has cost us nothing

Phase 0 evidence (commits in `git log`):

| Feature | Net new lines | Touched layers | Required new lib |
|---|---|---|---|
| Five-layer renderer + braille demo | ~1100 | all | none |
| Viewport scaling (Pretext-shaped) | ~250 | braille.py, demos/braille.py | none |
| OSC 52 clipboard + bracketed paste | ~30 | terminal.py | none |
| Pause animation + space-to-pause | ~30 | demos/braille.py | none |
| Full chat interface (`rn chat`) | ~400 | new files | none |
| Move ctx bar below input | ~5 | demos/chat.py | none |
| Custom scrollback navigation | ~250 | demos/chat.py | none |

Every single feature was a small additive change to pure-function
modules. Zero treaty negotiation. Zero "fight Library X" debugging.
Zero "this works in tmux but not Ghostty" surprises. Zero rendering
flicker bugs.

This is the architecture working. Don't break it.

---

## TL;DR for Claude sessions visiting this codebase

- The diff layer is the only thing that writes to stdout. Period.
- Don't import Rich, prompt_toolkit, Textual, blessed, urwid, or any
  other TUI library as a screen owner. If you need styled segments
  from one of them, call it as a pure function and translate to cells.
- New visual features go in as new pure functions in `src/ronin/render/`.
- Layout coordinates come from `grid.rows / cols`, never magic numbers.
- `on_tick` has no side effects. It paints into the grid. That's all.
- Caching expensive prepares is the pattern. Use single-entry width-
  keyed caches like `BrailleArt._layout_cache` and `PreparedText._cache_w`.
- Read this whole doc again if you're tempted to break any of the above.

See also:
- [`concepts.md`](concepts.md) — features enabled by this architecture
  that we haven't built yet
- [`rendering-plan.md`](rendering-plan.md) — the original architectural
  decisions and why they were made
- The repo-level `CLAUDE.md` — auto-loaded reminder of these rules
