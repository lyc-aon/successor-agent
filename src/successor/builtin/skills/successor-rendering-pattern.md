---
name: successor-rendering-pattern
description: Use this skill when extending or debugging the Successor terminal renderer, when adding any visual feature to a chat-shaped TUI, or when tempted to import Rich, prompt_toolkit, Textual, or any other library that wants to own stdout.
---

# Successor Rendering Pattern

The validated approach for terminal UIs in this codebase. The harness
that runs your chat is built on it.

## The One Rule

**One module owns stdout.** In Successor, that's `src/successor/render/diff.py`.
Not Rich, not prompt_toolkit, not Textual, not your own ad-hoc `print()`
calls. Exactly one place in the codebase emits bytes to the terminal,
and everything else paints into a virtual cell grid that the one place
diffs and commits.

A previous Python TUI harness this codebase replaces ended up with
**nine non-negotiable rules** in its rendering reference doc just to
keep Rich + prompt_toolkit + patch_stdout from corrupting each other's
screen state. Every rule was a treaty clause between two libraries
that each assumed they owned stdout. Successor eliminates that war by
having one screen owner. Not "fewer fights" — *zero* fights, by
construction.

## The five layers

```
Layer 5: COMMITTER     diff.py — the ONLY stdout writer
Layer 4: COMPOSITOR    paint.py — stack regions into virtual grid
Layer 3: LAYOUT        paint.py — wrap text/art at target width
Layer 2: PREPARE       text.py / braille.py — parse source ONCE,
                       cache by target size (Pretext-shaped)
Layer 1: MEASURE       measure.py — char width, ANSI strip
```

Layers 1–4 are pure functions of `(input data, grid) → grid mutations`.
They never touch the terminal, never block, never have side effects.
Layer 5 is the only thing that emits bytes.

## The Pretext cache pattern

For anything expensive to parse (text wrapping, image resampling,
syntax highlighting), parse the source ONCE in `__init__` and cache
the layout result keyed by target size. Resize re-runs layout from
cache, never re-parses. Validated speedups in Successor: 16× for
`BrailleArt.layout`, 519× for `PreparedText.lines`.

## Anti-patterns

- **`import rich` as a screen owner.** Rich is fine as a pure function
  for styled segments — call `Console.render(renderable, options)` to
  get a `list[Segment]` and translate to cells. Never `Console.print()`.
- **`import prompt_toolkit` as an Application.** The input parser is
  fine as a pure function. The Application gives pt screen ownership.
- **`from textual.app import App`.** Textual is heavy and wants to own
  the event loop. We don't need it.
- **`print()` from anywhere except diff.py.** Even debug logging goes
  through a sidebar log region painted into the grid.
- **Magic-number coordinates.** Always compute from `grid.rows`,
  `grid.cols`, and chrome heights so resize works.
- **Side effects in `on_tick`.** It paints into the grid. That's all.
  No file I/O, no network, no print, no global mutation. The "every
  frame is a pure function of state" guarantee is what makes replay,
  testing, and determinism work.

## Recipe for adding a new visual feature

1. Identify the layer it belongs to (measure / prepare / layout / composite).
2. Make it a pure function. Inputs: data + style + region. Output:
   mutations to a Grid.
3. Cache the prepare step if expensive (single-entry width-keyed cache).
4. Wire it into a paint method with one line at the call site.
5. Add a headless render test asserting against grid contents.

If your new feature doesn't fit through this recipe, the recipe is
**not** wrong — your feature has a hidden side effect. Find and remove
it before continuing.

## Reference

- `~/dev/ai/successor/docs/rendering-superpowers.md` — full architectural rules
- `~/dev/ai/successor/docs/concepts.md` — features the architecture enables
- `~/dev/ai/successor/docs/rendering-plan.md` — original five-layer decisions
