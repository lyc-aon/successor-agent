# Ronin — Notes for Claude Sessions

You're working in the Ronin agent harness. This file is auto-loaded by
Claude Code when you open a session in this directory. It's a tight
orientation; the deeper architectural docs live in `docs/`.

## What is Ronin

Custom Python agent harness for locally-run mid-grade models (Qwen 3.5 27B
primary). Pure-stdlib Python 3.11+, zero deps. Replaces the previous
attempt at `~/dev/ai/hk13/` which got stuck in Rich + prompt_toolkit +
patch_stdout coexistence wars.

Phase 0 + framework infra status (2026-04-06):
  - terminal renderer + chat interface complete
  - extension framework (loader pattern, themes, profiles, providers,
    skills, tools) complete as scaffolding — see "Framework infra" below
  - agent loop and tool dispatch intentionally not built yet
  - rn setup wizard (the showcase) is the next planned piece

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
  theme.py               Theme bundle, ThemeVariant, blend_variants, oklch parser

src/ronin/loader.py      generic Registry[T] pattern shared by every kind
src/ronin/config.py      ~/.config/ronin/chat.json load/save + v1→v2 migration

src/ronin/profiles/      Profile dataclass + JSON loader + active-profile resolver
src/ronin/providers/     ChatProvider protocol + factory + llamacpp/openai_compat
src/ronin/skills/        Skill dataclass + frontmatter parser + registry (loader-only)
src/ronin/tools/         @tool decorator + ToolRegistry (Python imports, gated user dir)

src/ronin/builtin/       package-shipped data files loaded by the registries
  themes/steel.json      the default theme — instrument-panel oklch
  profiles/default.json  general-purpose profile
  profiles/ronin-dev.json  harness-development profile (uses nusamurai intro)
  skills/ronin-rendering-pattern.md   the One Rule + five-layer architecture
  tools/read_file.py     example built-in tool

src/ronin/demos/         runnable scenes
  braille.py             RoninDemo (animation, supports intro_mode + max_duration_s)
  chat.py                RoninChat — v0 chat interface (now profile-aware)

src/ronin/snapshot.py    headless render via chat_demo_snapshot()
src/ronin/recorder.py    record/replay session traces
src/ronin/cli.py         argparse subcommand dispatch (`rn` binary)
src/ronin/__main__.py    `python -m ronin` entry point

docs/example-themes/     copy these into ~/.config/ronin/themes/ to install
  forge.json             warm samurai red, hand-tuned hex palette

assets/nusamurai/pos-th30/   9 braille keyframes (the intro animation source)

tests/                   pytest suite — 187 tests, hermetic via RONIN_CONFIG_DIR
  conftest.py            temp_config_dir fixture
  test_loader.py         Registry pattern tests
  test_theme.py          color parsing, variant resolver, blend math, registry
  test_config.py         load/save, v1→v2 migration, atomic write
  test_snapshot_themes.py  visual regression matrix (scenario × theme × mode)
  test_providers.py      protocol conformance, factory dispatch
  test_profiles.py       loader, registry, active-profile resolver
  test_chat_profiles.py  RoninChat ↔ Profile integration, hot swap
  test_skills.py         frontmatter parser, registry
  test_tools.py          @tool decorator, ToolRegistry, user gating

docs/                    architectural docs (read these)
  rendering-plan.md      original five-layer architecture decisions
  rendering-superpowers.md   READ FIRST — what the architecture buys us
  concepts.md            features enabled by the architecture
  llamacpp-protocol.md   what we send / what we get back from llama.cpp
  changelog.md           per-phase notes for the framework infra
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

## Framework infra (added 2026-04-06, phases 1–6)

The harness now has the loader pattern + four customizable axes:

- **Themes** (`src/ronin/render/theme.py`): `Theme(name, icon, dark, light)`
  bundles dark and light variants of the same visual identity. Display
  mode is now ORTHOGONAL to theme — Ctrl+T cycles theme, Alt+D toggles
  mode, both transition smoothly via `blend_variants`. The bundled
  `steel` theme is the default; user themes drop into
  `~/.config/ronin/themes/*.json`.

- **Profiles** (`src/ronin/profiles/`): `Profile` bundles theme +
  display_mode + density + system_prompt + provider config + skill
  refs + tool refs + intro_animation. Switching a profile is one
  user-facing action that swaps everything coherently. Built-in
  profiles: `default` (general purpose) and `ronin-dev` (harness work,
  uses the nusamurai braille intro). Slash command: `/profile <name>`.
  Keybind: Ctrl+P cycles. Title bar shows the active profile name.

- **Providers** (`src/ronin/providers/`): `ChatProvider` Protocol +
  `LlamaCppClient` + `OpenAICompatClient` + `make_provider(config)`
  factory. Profiles reference a provider config dict; the factory
  constructs the right class.

- **Skills** (`src/ronin/skills/`): SCAFFOLD only. Markdown +
  frontmatter parser, `~/.config/ronin/skills/*.md` loader, `rn skills`
  inventory command. NOT yet wired into the chat — invocation strategy
  (always-on prepend vs on-demand tool) deferred until hands-on time
  with the local model.

- **Tools** (`src/ronin/tools/`): SCAFFOLD only. `@tool` decorator
  registers functions in `TOOL_REGISTRY`. Built-in tools live in
  `src/ronin/builtin/tools/*.py` (one example: `read_file`). User
  tools in `~/.config/ronin/tools/*.py` are GATED behind
  `allow_user_tools` config (default OFF, audited to stderr). NOT yet
  wired into the chat — agent loop comes later after we study request/
  response patterns more deliberately.

- **Loader pattern** (`src/ronin/loader.py`): generic `Registry[T]`
  reused by themes, profiles, skills (tools have their own
  Python-import variant). Built-in dir + user dir, user wins on name
  collision, broken files skipped to stderr. Hermetic-testable via
  `RONIN_CONFIG_DIR` env var (already supported by `config.py`).

- **Config schema v2**: `chat.json` gained `version`, `display_mode`,
  `active_profile`, `allow_user_tools` slots. v1 configs are migrated
  transparently on load. Migration is idempotent and tested.

The intro animation feature uses the existing `RoninDemo` with two new
parameters (`max_duration_s`, `intro_mode`) so a profile's
`intro_animation: "nusamurai"` plays the bundled braille keyframes for
4 seconds before the chat opens. Any keypress skips ahead.

**What's NOT yet built**: `rn setup` wizard (the showcase — coming
next), skill invocation strategy, agent loop, tool dispatch,
framework docs.

See [`docs/changelog.md`](docs/changelog.md) for the per-phase notes.

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
