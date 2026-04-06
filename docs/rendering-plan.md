# Ronin Rendering Plan

Date: 2026-04-06
Status: Phase 0 prototype landed.

## Purpose

This document describes the design of Ronin's terminal renderer and the
reasoning behind the choices. It exists so we never have to relitigate
the architecture once the agent loop and tool system land on top of it.

It is the first piece of Ronin we're building because **rendering bugs
are the single most expensive class of issue to retrofit into a TUI**.
Get this layer wrong and every later feature pays the cost.

## Goals

1. **No fight between subsystems.** One pipeline owns the screen. Nothing
   else (no Rich, no prompt_toolkit, no `print()`) writes to stdout.
2. **Brutal-resize survivability.** Dragging the corner of the terminal
   never causes flicker, garbled output, or stale chrome.
3. **Frame-budgeted animations.** Braille animations and spinners run
   smoothly without saturating CPU or producing per-frame allocation
   storms.
4. **Pure layers, testable in isolation.** Every layer except the bottom
   one is a pure function of `(input data, grid)`. The bottom layer is the
   only thing that touches the TTY.
5. **Pure stdlib.** No `wcwidth`, no `grapheme`, no `rich`, no
   `prompt_toolkit`. Phase 0 must run on a vanilla Python 3.11 install.

## Non-goals (for Phase 0)

- IME / bidi / RTL text
- Sixel / Kitty graphics protocol
- Mouse input
- Hyperlink escape sequences (OSC 8)
- Multi-window or split-pane layout
- Markdown / syntax-highlight rendering primitives

These are deferred to Phase 1+ once the foundation is proven.

## The five layers

```
┌─────────────────────────────────────────────────────────┐
│ Layer 5: COMMITTER  (diff.py)                           │
│   Diffs current frame vs previous frame.                │
│   Emits minimal ANSI (cursor moves + SGR + chars).      │
│   THE ONLY MODULE THAT WRITES TO STDOUT.                │
├─────────────────────────────────────────────────────────┤
│ Layer 4: COMPOSITOR  (paint.py)                         │
│   Stacks laid-out blocks into a virtual cell grid.      │
│   Knows about: regions, fills, centering, clipping.     │
├─────────────────────────────────────────────────────────┤
│ Layer 3: LAYOUT  (paint.py)                             │
│   Pure: (text, x, y, width, wrap_mode) → grid mutation. │
│   Re-runnable on resize without re-measuring.           │
├─────────────────────────────────────────────────────────┤
│ Layer 2: PREPARE  (paint.py + measure.py)               │
│   Walks input text. For each grapheme: width, attach    │
│   combining marks, classify zero-width.                 │
├─────────────────────────────────────────────────────────┤
│ Layer 1: MEASURE  (measure.py)                          │
│   char_width(ch) → 0 | 1 | 2                            │
│   strip_ansi(s)  → text without CSI sequences           │
│   Pure functions. East Asian Width tables.              │
└─────────────────────────────────────────────────────────┘
```

The crucial property: **layers 1–4 are pure**. They don't touch the
terminal, never block, never allocate file descriptors, never depend on
signal state. Only Layer 5 ever writes a byte to stdout.

This is what makes the renderer:
- testable (assert against Grid contents directly)
- replayable (a Grid is just data)
- deterministic (same input → same bytes out)
- crash-safe (no half-written ANSI on exception)

## Support modules

| File | Role |
|---|---|
| `cells.py` | `Cell`, `Style`, `Grid` — the data layers operate on |
| `terminal.py` | TTY setup/teardown, alt-screen, cursor, raw mode, SIGWINCH, restore |
| `app.py` | Frame loop with double-buffered diffing and input handling |
| `braille.py` | Braille codec + Bayer dot interpolation (Phase 0 demo asset) |

## Data model

```python
@dataclass(frozen=True, slots=True)
class Style:
    fg: int | None = None    # 24-bit packed RGB or None
    bg: int | None = None
    attrs: int = 0           # ATTR_BOLD | ATTR_DIM | ATTR_ITALIC | ...

@dataclass(slots=True)
class Cell:
    char: str = ' '
    style: Style = DEFAULT_STYLE
    wide_tail: bool = False  # trailing half of a width-2 grapheme

class Grid:
    rows: int
    cols: int
    _cells: list[list[Cell]]
```

Frozen `Style` makes it cheap to compare and use as a dict key. Mutable
`Cell` lets paint operations replace cells in place without re-allocating
the row list.

## Frame loop

```python
with Terminal() as term:                # alt screen, raw mode, hide cursor
    while running:
        if term.consume_resize():       # one-shot SIGWINCH flag
            allocate_grids()            # new front + back at new size

        back.clear()
        on_tick(back)                   # user code paints here

        prev = None if first_frame else front
        payload = diff_frames(prev, back)
        term.write(payload)             # the ONLY write to stdout
        first_frame = False

        front, back = back, front       # swap

        sleep_until(next_frame_deadline)# but wake on input
```

Double-buffering means we allocate two grids on resize and never again
inside the loop. The "previous frame" stays alive as `front` while
on_tick paints into `back`, then they swap. Zero per-frame Grid
allocation in steady state.

## Resize handling

1. `signal.signal(SIGWINCH, handler)` flips a single boolean. That's the
   entire signal handler — no allocation, no I/O.
2. Each tick the loop calls `term.consume_resize()` which returns the
   flag and resets it.
3. On True: reallocate both grids at the new size, set `first_frame =
   True`. Next `diff_frames` call sees `prev=None` and falls back to
   `render_full`, which clears the screen and writes everything fresh.

This handles drag-resize naturally because we coalesce SIGWINCH at frame
boundaries — no matter how many SIGWINCHes arrive between two ticks,
we only react once.

The signal handler is also re-entered safely in the input drain loop:
if SIGWINCH arrives while we're reading bytes from stdin, we break out
of the input drain so the main loop sees the resize on its next pass.

## Input handling

`Terminal.__enter__` puts stdin in cbreak mode so single keypresses are
delivered without waiting for newline and without local echo. The frame
loop polls stdin via `select.select` with a timeout that fits the frame
budget — wake on input or wake on deadline, whichever comes first.

Default `quit_keys = b'qQ\x03'` covers q, Q, and Ctrl+C. Subclasses can
override `on_key(byte)` to react to other input.

Escape sequences (arrow keys, function keys) arrive as multi-byte
sequences and are passed to `on_key` byte-by-byte. Phase 0 doesn't try
to be clever about this; Phase 1 will add a key parser.

## Why no Rich, no prompt_toolkit

Three reasons, each independently sufficient:

1. **Two screen owners cannot coexist.** Rich assumes it owns stdout.
   prompt_toolkit assumes it owns the screen. `patch_stdout()` is a
   peace treaty between them. Every rendering bug in hk13 traces back
   to a treaty violation. We're not adopting that pattern.

2. **The thing they each do best is small.** Rich's strength is
   styling individual blocks (markdown, syntax, tables). We can absorb
   that later by calling Rich's `Console.render()` to produce a
   `list[Segment]` and translating segments into our cell grid — no
   Rich byte ever reaches stdout. prompt_toolkit's strength is its
   input parser; we can use the parser without instantiating the
   Application. Both libraries become *libraries*, not frameworks.

3. **The cost of writing it ourselves is small.** The Phase 0 renderer
   is ~1000 lines of pure Python with zero dependencies. We pay that
   cost once. We pay the "fight prompt_toolkit + Rich" cost every time
   we touch the renderer for the rest of the project.

## Pretext correspondence

Cheng Lou's Pretext (https://www.pretext.cool/) makes one core move:
**separate slow measurement from fast layout, do everything in
userland, commit once.** That same move maps onto a terminal:

| Pretext (browser) | Ronin (terminal) |
|---|---|
| Slow side: `getBoundingClientRect` (DOM reflow) | Slow side: grapheme width, ANSI strip, EAW table lookup |
| `prepare()` measures via off-screen canvas, caches | `measure.char_width` + paint walks each grapheme once |
| `layout(width)` is pure arithmetic | `paint_*` are pure grid mutations, re-runnable on resize |
| Commits to DOM via dedicated render pass | `diff.diff_frames` commits via cursor moves + SGR |

The terminal version is in some ways *easier* than the browser one
because the target is a discrete cell grid, not subpixels — but the
discipline is the same: never let your slow path be in the hot loop,
never let side effects leak across layer boundaries.

## Performance budgets

- **Steady-state animation**: 30 FPS frame budget, ~33ms/frame.
- **Allocation per frame**: zero new `Grid` instances in steady state
  (double-buffered). Replacement `Cell` instances are unavoidable but
  use `__slots__`.
- **Diff cost**: O(rows × cols) cell comparison per frame; the inner
  loop is plain Python list indexing. For an 80×40 grid that's 3200
  comparisons — well under 1ms in CPython.
- **Wire cost**: a static animation produces ~zero bytes per frame
  after initial paint. A full braille frame change produces ~6KB
  worst case.

## What this does NOT solve yet

- **Style coalescing across frame writes.** We track "current style"
  inside one diff pass but reset it implicitly between frames. Phase 1
  could carry the style across frames to save SGR resets.
- **Dirty region tracking.** We compare every cell every frame. Cheap
  but not optimal. Phase 1 could ask the user code to declare dirty
  regions if profiling shows the diff loop dominating.
- **Hardware emoji width quirks.** The EAW table is conservative.
  Some terminals render certain emoji as width 1 even though Unicode
  says width 2. Live with it for now.
- **Color quantization.** We always emit truecolor. Terminals that
  only support 256-color will get the closest available color from
  their renderer's quantizer. Phase 1 could add a degrade path.

## Acceptance criteria for Phase 0

- [x] Demo runs without crashing.
- [x] Resize during animation does not flicker or corrupt output.
- [x] Ctrl+C / q exit cleanly with the terminal restored.
- [x] No `print()` calls anywhere in the renderer.
- [x] Renderer is pure-stdlib.
- [x] Braille interpolation matches the Bayer ordering of the web reference.

## Phase 1 (after Phase 0 ships)

- Layout primitives: `Box`, `HStack`, `VStack`, `Spacer`
- Rich-segment importer (so we can render Markdown / syntax via Rich
  but never let Rich touch stdout)
- Key parser (escape sequences → key events)
- Mouse support
- Basic theme/skin module
- Plumbing for the agent event stream → renderer (turn boxes, tool
  lines, status bar, footer activity lane)
