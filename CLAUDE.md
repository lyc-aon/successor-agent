# Ronin — Notes for Claude Sessions

You're working in the Ronin agent harness. This file is auto-loaded by
Claude Code when you open a session in this directory. It's a tight
orientation; the deeper architectural docs live in `docs/`.

## What is Ronin

Custom Python agent harness for locally-run mid-grade models (Qwen 3.5 27B
primary). Pure-stdlib Python 3.11+, zero deps. Replaces the previous
attempt at `~/dev/ai/hk13/` which got stuck in Rich + prompt_toolkit +
patch_stdout coexistence wars.

Phase 0 status: terminal renderer + chat interface complete. Agent loop
and model adapter intentionally not built yet.

## The One Rule (read before touching the renderer)

**`src/ronin/render/diff.py` is the only module in the entire codebase
allowed to write to stdout.** Not Rich. Not prompt_toolkit. Not `print()`.
Not your own one-off escape sequences from somewhere convenient.

If you find yourself wanting to write to stdout from outside `diff.py`,
the answer is: **paint into the Grid via `paint.py` instead**. The
renderer was designed so that's always possible.

**Read [`docs/rendering-superpowers.md`](docs/rendering-superpowers.md)
in full** before:
- Adding any rendering library (Rich, prompt_toolkit, Textual, blessed,
  urwid, etc.)
- Adding `print()` anywhere outside `diff.py`
- Hardcoding terminal coordinates
- Doing anything in `on_tick` that has side effects

## What's where

```
src/ronin/render/        the rendering engine
  measure.py             Layer 1 — grapheme width, ANSI strip
  cells.py               Cell, Style, Grid (data layers operate on)
  paint.py               Layers 2-4 — text, lines, fills, centering
  diff.py                Layer 5 — minimal ANSI commit (ONLY stdout writer)
  terminal.py            alt-screen, raw mode, SIGWINCH, signal-safe restore
  app.py                 double-buffered frame loop with input + resize
  braille.py             BrailleArt — Pretext-shaped resampling, Bayer interp
  text.py                PreparedText, hard_wrap, lerp_rgb, ease_out_cubic

src/ronin/demos/         runnable scenes
  braille.py             RoninDemo (animation), RoninShow (static)
  chat.py                RoninChat — v0 chat interface

src/ronin/cli.py         argparse subcommand dispatch (`rn` binary)
src/ronin/__main__.py    `python -m ronin` entry point

assets/nusamurai/pos-th30/   9 braille keyframes
docs/                    architectural docs (read these)
  rendering-plan.md      original five-layer architecture decisions
  rendering-superpowers.md   READ FIRST — what the architecture buys us
  concepts.md            features enabled by the architecture
```

## Commands

The binary is `rn` (the `ronin` name is too contested in OSS — see
`docs/rendering-superpowers.md` for context). Installed via:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
ln -sf "$PWD/.venv/bin/rn" ~/.local/bin/rn
```

Available subcommands:

```
rn               help
rn -V            version
rn chat          v0 chat (scripted ronin responses)
rn demo          braille animation cycle
rn show <name>   single static braille frame
rn frames        list 9 nusamurai keyframes
rn doctor        terminal capabilities + measure samples
rn bench         renderer benchmark (no TTY needed)
```

## The five layers

Understand these before touching anything in `render/`:

```
Layer 5 — diff.py        the ONLY module that writes to stdout
Layer 4 — paint.py       compose into a virtual cell grid
Layer 3 — paint.py       layout (text/art → grid mutations at width W)
Layer 2 — text/braille   prepare (parse source ONCE, cache by target size)
Layer 1 — measure.py     grapheme width, ANSI strip, EAW table
```

Everything above Layer 5 is pure. Nothing above Layer 5 ever touches the
terminal. The renderer is testable by inspecting Grid contents directly.

## Pretext-shaped primitives (the cache pattern)

Two places it pays off in Ronin today, both validated:

| Primitive | Cache hit | Miss | Speedup |
|---|---|---|---|
| `BrailleArt.layout(cells_w, cells_h)` | 0.4 ms | 6.2 ms | 16× |
| `PreparedText.lines(width)` | 0.15 µs | 77.83 µs | 519× |

When adding new visual elements that take expensive prepare work, follow
this pattern:
1. Parse source ONCE in `__init__`
2. Expose a `layout(target_size)` method
3. Cache the result, keyed by target size, single-entry

## Common gotchas

- **Chat scrolling is custom-built**, not native terminal. We're in
  alt-screen mode so terminal scrollback can't see our paint. The
  `RoninChat._paint_chat_area` slices a flat list of all message lines
  by `scroll_offset`.
- **`on_tick` clears the ESC accumulator** (`self._esc_buf = None`) at
  the start so bare ESC presses don't get stuck.
- **Bracketed paste is OFF in the chat** because the v0 input handler
  doesn't parse the `CSI 200~ ... 201~` wrapper. Re-enable when the
  real key parser lands.
- **The streaming reply is NOT in `self.messages`** until it commits.
  It's rendered as a "virtual" trailing block. This keeps the chat
  height stable during the typewriter.
- **Layout coordinates always come from `grid.rows / cols`** plus
  computed offsets, never magic numbers. This is why resize works.
- **Auto-anchor on scroll**: when content grows while user is scrolled
  up (`auto_scroll == False`), `scroll_offset` advances by the new
  content's height so the user's view of history doesn't jerk.

## How to extend the renderer

When you want to add a new visual feature:

1. Identify the layer it belongs to (see `rendering-superpowers.md`)
2. Make it a pure function — no I/O, no global state, no `print()`
3. Cache the prepare step if expensive (single-entry width-keyed cache)
4. Wire it into a paint method with one line at the call site
5. Add a headless render test (no PTY needed)

If your new feature doesn't fit through this recipe, **the recipe is
not wrong** — your feature has a hidden side effect. Find and remove
it before continuing.

## Things deliberately deferred

These are known limits, all waiting on the same upcoming "real key
parser" piece:

- ASCII-only typed input (no UTF-8 multi-byte input)
- No arrow-key cursor navigation in the input box
- No bracketed paste in the chat
- No interrupt during ronin response (Ctrl+C still quits)
- History recall (Up/Down in input)

When the real key parser lands, all of these get fixed simultaneously.
See [`docs/concepts.md`](docs/concepts.md) for the broader roadmap.

## Reference repos in `~/dev/ai/`

For architectural comparison:
- `codex-reference/` — OpenAI Codex CLI (Rust, ~80 crates, ~595K LOC)
- `hermes-reference/` — Nous Research Hermes Agent (Python, ~297K LOC)
- `opencode-reference/` — sst/opencode (TypeScript, ~59K LOC)
- `hk13/` — the deprecated agent harness Ronin replaces

## Validated by user

> "this renders better than every other agent harness already. game
> changer rendering method." — 2026-04-06

Don't break the architecture. Don't reach for libraries. Don't add
side effects to `on_tick`. Read `rendering-superpowers.md` if any
doubt.
