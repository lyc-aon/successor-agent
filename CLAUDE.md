# Successor — Notes for Claude Sessions

You're working in the Successor agent harness. This file is auto-loaded
by Claude Code when you open a session in this directory. It's a tight
orientation; the deeper architectural docs live in `docs/`.

## What is Successor

Custom Python agent harness for local models and OpenAI-compatible
endpoints. The base install is pure Python 3.11+ with zero runtime
dependencies. The renderer is a five-layer cell-based pipeline where
one module owns the screen end to end.

## The One Rule (read before touching the renderer)

**`src/successor/render/diff.py` is the only module in the entire
codebase allowed to write to stdout.** Not Rich, not prompt_toolkit,
not `print()`, not your own one-off escape sequences from somewhere
convenient.

If you find yourself wanting to write to stdout from outside `diff.py`,
the answer is: paint into the Grid via `paint.py` instead. The renderer
was designed so that's always possible.

**Read [`docs/rendering-superpowers.md`](docs/rendering-superpowers.md)
in full** before:

- Adding any rendering library (Rich, prompt_toolkit, Textual, blessed,
  urwid, etc.)
- Adding `print()` anywhere outside `diff.py`
- Hardcoding terminal coordinates
- Doing anything in `on_tick` that has side effects

## What's where

```
src/successor/render/        the rendering engine
  measure.py             Layer 1 — grapheme width, ANSI strip
  cells.py               Cell, Style, Grid (data layers operate on)
  paint.py               Layers 2-4 — text, lines, fills, centering
  diff.py                Layer 5 — minimal ANSI commit (ONLY stdout writer)
  terminal.py            alt-screen, raw mode, SIGWINCH, signal-safe restore
  app.py                 double-buffered frame loop with input + resize
  braille.py             BrailleArt — Pretext-shaped resampling, Bayer interp
  text.py                PreparedText, hard_wrap, lerp_rgb, ease_out_cubic
  theme.py               Theme bundle, ThemeVariant, blend_variants, oklch parser

src/successor/loader.py      generic Registry[T] pattern shared by every kind
src/successor/config.py      ~/.config/successor/chat.json load/save + v1→v2 migration
src/successor/tool_runner.py generic native-tool runner for non-bash tools

src/successor/profiles/      Profile dataclass + JSON loader + active-profile resolver
src/successor/providers/     ChatProvider protocol + factory + llamacpp/openai_compat
src/successor/skills/        Skill dataclass + frontmatter parser + registry
src/successor/tools/         @tool decorator + ToolRegistry (Python imports, gated user dir)
src/successor/web/           optional API/web tooling
  config.py              holonet/browser profile config resolution
  holonet.py             API-backed web routes (Brave, Firecrawl, Europe PMC, ClinicalTrials)
  browser.py             Playwright browser manager + native browser actions
src/successor/bash/          bash-masking subsystem — parse model bash → structured cards
  cards.py               ToolCard frozen dataclass (verb/params/risk/raw/output/exit_code)
  parser.py              @bash_parser registry, parse_bash(), clip_at_operators
  risk.py                independent risk classifier (safe/mutating/dangerous)
  diff_artifact.py       structured file-diff artifacts + unified-diff parser
  change_capture.py      deterministic before/after capture for known file mutations
  exec.py                dispatch_bash() — parse + classify + run, refuse dangerous
  runner.py              BashRunner — async subprocess worker (background threads)
  render.py              paint_tool_card() — pure paint function for the cards
  patterns/              one file per command family (ls, cat, grep, find, git, ...)
src/successor/agent/         agent loop + compaction subsystem
  log.py                 ApiRound + MessageLog + BoundaryMarker
  events.py              ChatEvent ADT (StreamStarted, Compacted, ToolCompleted, ...)
  tokens.py              TokenCounter — /tokenize endpoint + char heuristic + LRU cache
  budget.py              ContextBudget + CircuitBreaker + RecompactChain + BudgetTracker
  microcompact.py        time-based stale tool result clearing (pure function)
  compact.py             autocompact via llama.cpp summarization + PTL retry loop
  bash_stream.py         BashStreamDetector — fenced ```bash detection across stream chunks
  loop.py                QueryLoop — tick-driven state machine

src/successor/wizard/        setup wizard + config menu + system prompt editor
src/successor/intros/        intro animations played before the chat opens
src/successor/builtin/       package-shipped data files loaded by the registries
  themes/steel.json                       cool blue instrument-panel oklch (default)
  profiles/default.json                   general-purpose profile
  profiles/successor-dev.json             harness-development profile
  skills/successor-rendering-pattern.md   the One Rule + five-layer architecture
  tools/read_file.py                      example built-in tool
  intros/successor/00..10-*.txt + hero.txt  11 intro frames + dedicated empty-state hero art

src/successor/chat.py        SuccessorChat — chat interface (profile-aware, real llama.cpp streaming)
src/successor/snapshot.py    headless render via *_demo_snapshot()
src/successor/recorder.py    input-byte record/replay sessions
src/successor/session_trace.py normal chat runtime JSONL traces for postmortem debugging
src/successor/cli.py         argparse subcommand dispatch (`successor` binary)
src/successor/__main__.py    `python -m successor` entry point

tests/                       pytest suite  1097+ tests, hermetic via SUCCESSOR_CONFIG_DIR

scripts/                     manual-run scripts (no auto-execution)
  e2e_chat_driver.py     scripted scenarios that drive a real chat against
                         llama.cpp and snapshot every turn for review

docs/                        architectural docs (read these)
  rendering-superpowers.md   READ FIRST — what the architecture buys us
  rendering-plan.md          original five-layer architecture decisions
  concepts.md                features enabled by the architecture
  llamacpp-protocol.md       what we send / what we get back from llama.cpp
  web-tools.md               holonet + Playwright browser install/config guide
  changelog.md               per-phase notes
```

## Commands

The binary is `successor` (canonical) plus `sx` as a 2-letter alias.
Both point at the same entry. Installed via:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
ln -sf "$PWD/.venv/bin/successor" ~/.local/bin/successor
ln -sf "$PWD/.venv/bin/sx" ~/.local/bin/sx
```

Available subcommands (use either `successor` or `sx`):

```
successor              help
successor -V           version
successor chat         chat interface (real llama.cpp streaming, intro plays first)
                        - inside chat:
                          /bash <cmd>     run bash, render as tool card
                          /budget         show context fill % + thresholds
                          /burn N         inject N synthetic tokens (test compaction)
                          /compact        manually fire compaction
                          /fork <text>    spawn a background subagent
                          /tasks          list background task state
                          /task-cancel    cancel a queued/running task
                          Ctrl+G          interrupt an in-flight stream or running tool
                          Ctrl+,          open config menu
                          Ctrl+P          cycle profiles
                          Ctrl+T          cycle themes
                          Alt+D           toggle dark/light
                          Ctrl+]          cycle density
successor setup        profile creation wizard with live preview
successor config       three-pane profile config menu
successor doctor       terminal capabilities + measure samples
successor skills       list loaded skills
successor tools        list import-registered Python tools
successor snapshot     headless render of a chat scenario
successor record       record an input session to JSONL
successor replay       replay a recorded session
successor bench        renderer benchmark (no TTY needed)
```

Background subagents now have two paths:
- manual `/fork`, which works whenever the profile's `subagents.enabled`
  setting is on
- model-visible `subagent`, which requires the tool to be enabled in
  the profile's tool list and also requires `subagents.notify_on_finish`
  so the parent chat receives the later completion event

Holonet and the Playwright browser are now real model-visible tools too,
but only when explicitly enabled on the active profile. `holonet` is the
API-backed path for search/research retrieval; `browser` is the live
page path for local apps, clicks, screenshots, and JS-heavy pages.

`successor tools` is separate from the native profile tool picker. It
lists Python import-registered tools from `src/successor/tools/`, not
the built-in native tool surface (`bash`, `subagent`, `holonet`,
`browser`).

Subagent scheduling is now profile-driven: `serial` keeps one
background model lane, `slots` uses llama.cpp slot capacity with one
slot reserved for the parent chat, and `manual` trusts the configured
width directly. The default remains `serial`.

## The five layers

Understand these before touching anything in `render/`:

```
Layer 5 - diff.py        the ONLY module that writes to stdout
Layer 4 - paint.py       compose into a virtual cell grid
Layer 3 - paint.py       layout (text/art -> grid mutations at width W)
Layer 2 - text/braille   prepare (parse source ONCE, cache by target size)
Layer 1 - measure.py     grapheme width, ANSI strip, EAW table
```

Everything above Layer 5 is pure. Nothing above Layer 5 ever touches
the terminal. The renderer is testable by inspecting Grid contents
directly, with no PTY required.

## Pretext-shaped primitives (the cache pattern)

Two places it pays off in Successor today, both validated:

| Primitive | Cache hit | Miss | Speedup |
|---|---|---|---|
| `BrailleArt.layout(cells_w, cells_h)` | 0.4 ms | 6.2 ms | 16x |
| `PreparedText.lines(width)` | 0.15 µs | 77.83 µs | 519x |

When adding new visual elements that take expensive prepare work, follow
this pattern:

1. Parse source ONCE in `__init__`
2. Expose a `layout(target_size)` method
3. Cache the result, keyed by target size, single-entry

## Common gotchas

- **Chat scrolling is custom-built**, not native terminal. We're in
  alt-screen mode so terminal scrollback can't see our paint. The
  `SuccessorChat._paint_chat_area` slices a flat list of all message
  lines by `scroll_offset`.
- **`on_tick` clears the ESC accumulator** at the start so bare ESC
  presses don't get stuck.
- **The streaming reply is NOT in `self.messages`** until it commits.
  It's rendered as a "virtual" trailing block. This keeps the chat
  height stable during the typewriter.
- **Layout coordinates always come from `grid.rows / cols`** plus
  computed offsets, never magic numbers. This is why resize works.
- **Auto-anchor on scroll**: when content grows while user is scrolled
  up, `scroll_offset` advances by the new content's height so the
  user's view of history doesn't jerk.
- **`hard_wrap` checks `\n` BEFORE `cw == 0`** because newlines have
  zero display width and would otherwise attach as literal control
  chars. If you write a new wrapper, do the same.
- **Provider URL handling**: both `LlamaCppClient` and
  `OpenAICompatClient` accept `base_url` with or without `/v1`. The
  `_api_root()` helper detects which form was passed. If you build a
  new endpoint method, use `_api_root()` for OpenAI-compat endpoints
  and the bare `base_url` (or `_server_root()` on llama.cpp) for
  server-root endpoints like `/props`, `/tokenize`, `/health`.
- **Context window auto-detection**: don't read
  `profile.provider.context_window` directly. Call
  `chat._resolve_context_window()` which consults profile override →
  `client.detect_context_window()` → `CONTEXT_MAX` and caches the
  result on the chat. The detection paths probe `/props` (llama.cpp)
  or `/v1/models` + a hardcoded OpenAI prefix table (openai_compat).

## How to extend the renderer

When you want to add a new visual feature:

1. Identify the layer it belongs to (see `rendering-superpowers.md`)
2. Make it a pure function: no I/O, no global state, no `print()`
3. Cache the prepare step if expensive (single-entry width-keyed cache)
4. Wire it into a paint method with one line at the call site
5. Add a headless render test (no PTY needed)

If your new feature doesn't fit through this recipe, **the recipe is
not wrong**. Your feature has a hidden side effect. Find and remove
it before continuing.

## Architectural decisions cross-checked against free-code

The agent loop, compaction pipeline, and bash-masking subsystem were
designed against `~/dev/ai/free-code-main/`'s actual source as a
reference implementation. Where we diverged:

- Generator-based loop (free-code) → tick-driven state machine (us),
  because our chat is a sync frame-driven `App` and async generators
  don't compose with `on_tick` cleanly. Same flow, different plumbing.
- 4-layer compaction pipeline → 2 layers. Free-code's snipCompact and
  contextCollapse are feature-gated and compiled-out in external
  builds; only microcompact + autocompact are mandatory. We mirror
  the two that matter.
- CacheSafeParams + cache_edits microcompact → SKIPPED. llama.cpp's
  KV cache is local and free; there's no remote prompt cache to
  keep warm.
- StreamingToolExecutor (concurrent + streaming-during-model-stream)
  → async-runner tool dispatch. Single-stream, bash runners spawn
  in background threads, the chat tick loop pumps them.
- FallbackTriggeredError → SKIPPED. We have one provider.
- Reactive compact on prompt-too-long → KEPT.
- Token thresholds → percentage-based per profile via
  `CompactionConfig.warning_pct / autocompact_pct / blocking_pct`,
  with hard floors. Defaults are 12.5% / 6.25% / 1.5625% with 8K / 4K
  / 1K floors. The chat's `_agent_budget()` constructs a
  `ContextBudget` from `profile.compaction.buffers_for_window(window)`
  on every read so swapping profiles or changing percentages takes
  effect immediately. See `docs/compaction.md` for the full reference
  and the JSON schema.

## Things deliberately deferred

- No arrow-key cursor navigation in the input box
- No interrupt during successor response other than Ctrl+G
- Streaming tool execution (tools start AFTER stream commits)
- Concurrent tool execution

History recall (Up/Down arrows recall previous user messages,
shell-style) shipped in v0.1.4. The empty-input + Up flow + the
recall state machine live in `chat.py`; see `tests/test_input_history.py`
for the contract.

UTF-8 typed input shipped with the real key parser, and v0.1.5 makes
backspace/delete grapheme-aware across the chat, search, wizard, and
config editors via `graphemes.py`.

The remaining items are now chat/runtime gaps, not byte-decoding gaps.
See [`docs/concepts.md`](docs/concepts.md) for the broader roadmap.
