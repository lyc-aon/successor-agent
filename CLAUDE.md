# Successor — Notes for Claude Sessions

You're working in the Successor agent harness. This file is auto-loaded by
Claude Code when you open a session in this directory. It's a tight
orientation; the deeper architectural docs live in `docs/`.

## What is Successor

Custom Python agent harness for locally-run mid-grade models (Qwen 3.5 27B
primary). Pure-stdlib Python 3.11+, zero deps. Replaces the previous
attempt at `~/dev/ai/hk13/` which got stuck in Rich + prompt_toolkit +
patch_stdout coexistence wars.

Phase 0 + framework infra status (2026-04-06):
  - terminal renderer + chat interface complete
  - extension framework (loader pattern, themes, profiles, providers,
    skills, tools) complete as scaffolding — see "Framework infra" below
  - agent loop and tool dispatch intentionally not built yet
  - successor setup wizard (the showcase) is the next planned piece

## The One Rule (read before touching the renderer)

**`src/successor/render/diff.py` is the only module in the entire codebase
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

src/successor/profiles/      Profile dataclass + JSON loader + active-profile resolver
src/successor/providers/     ChatProvider protocol + factory + llamacpp/openai_compat
src/successor/skills/        Skill dataclass + frontmatter parser + registry (loader-only)
src/successor/tools/         @tool decorator + ToolRegistry (Python imports, gated user dir)
src/successor/bash/          bash-masking subsystem — parse model bash → structured cards
  cards.py               ToolCard frozen dataclass (verb/params/risk/raw/output/exit_code)
  parser.py              @bash_parser registry, parse_bash(), clip_at_operators
  risk.py                independent risk classifier (safe/mutating/dangerous)
  exec.py                dispatch_bash() — parse + classify + run, refuse dangerous
  render.py              paint_tool_card() — pure paint function for the cards
  patterns/              one file per command family (ls, cat, grep, find, git, ...)
src/successor/agent/         agent loop + compaction subsystem
  log.py                 ApiRound + MessageLog + BoundaryMarker (compaction-ready data shape)
  events.py              ChatEvent ADT (StreamStarted, Compacted, ToolCompleted, ...)
  tokens.py              TokenCounter — /tokenize endpoint + char heuristic + LRU cache
  budget.py              ContextBudget + CircuitBreaker + RecompactChain + BudgetTracker
  microcompact.py        time-based stale tool result clearing (pure function)
  compact.py             autocompact via llama.cpp summarization + PTL retry loop
  bash_stream.py         BashStreamDetector — fenced ```bash detection across stream chunks
  loop.py                QueryLoop — tick-driven state machine, the agent loop core
src/successor/wizard/        successor setup wizard with live preview pane (the showcase)

src/successor/builtin/       package-shipped data files loaded by the registries
  themes/steel.json                       cool blue instrument-panel oklch (default)
  themes/forge.json                       warm red blacksmith-by-the-fire (alternate)
  profiles/default.json                   general-purpose profile
  profiles/successor-dev.json             harness-development profile (uses successor intro)
  skills/successor-rendering-pattern.md   the One Rule + five-layer architecture
  tools/read_file.py                      example built-in tool
  intros/successor/00..10-*.txt           11 braille frames for the bundled emergence intro

src/successor/chat.py        SuccessorChat — chat interface (profile-aware, real llama.cpp streaming)
src/successor/intros/        intro animations played before the chat opens
  successor.py           SuccessorIntro App — emerge then hold the title portrait

src/successor/snapshot.py    headless render via chat_demo_snapshot() / wizard_demo_snapshot() / config_demo_snapshot()
src/successor/recorder.py    record/replay session traces
src/successor/cli.py         argparse subcommand dispatch (`successor` binary)
src/successor/__main__.py    `python -m successor` entry point

tests/                   pytest suite — 652 tests, hermetic via SUCCESSOR_CONFIG_DIR
  conftest.py            temp_config_dir fixture
  test_loader.py         Registry pattern tests
  test_theme.py          color parsing, variant resolver, blend math, registry
  test_config.py         load/save, v1→v2 migration, atomic write
  test_snapshot_themes.py  visual regression matrix (scenario × theme × mode)
  test_providers.py      protocol conformance, factory dispatch
  test_profiles.py       loader, registry, active-profile resolver
  test_chat_profiles.py  SuccessorChat ↔ Profile integration, hot swap
  test_skills.py         frontmatter parser, registry
  test_tools.py          @tool decorator, ToolRegistry, user gating
  test_bash_parser.py    parse_bash + 12 pattern parsers + classify_risk
  test_bash_exec.py      dispatch_bash, refusal, truncation, timeout, preview
  test_bash_render.py    paint_tool_card per-risk visual + measure consistency
  test_chat_bash.py      /bash slash command + tool message integration
  test_agent_log.py      MessageLog/ApiRound/BoundaryMarker shapes
  test_agent_tokens.py   TokenCounter heuristic + endpoint paths + LRU
  test_agent_budget.py   thresholds + circuit breaker + recompact chain
  test_agent_microcompact.py  count + time-based stale tool result clearing
  test_agent_compact.py  full compaction with mocked stream + PTL retry
  test_bash_stream.py    fenced ```bash detection across stream fragments
  test_agent_loop.py     QueryLoop state machine + transitions + error paths
  test_compaction_animation.py  5-phase animation state machine + paint_horizontal_divider primitive
  test_context_fill_bar.py      static footer threshold transitions + badges + window scaling

scripts/                 manual-run scripts (no auto-execution)
  swap_to_a3b.sh         swap qwopus → A3B at 50K context for compaction stress test
  swap_to_qwopus.sh      restore qwopus at 262K context (Nyx + ChetGPT depend on it)

docs/                    architectural docs (read these)
  rendering-plan.md      original five-layer architecture decisions
  rendering-superpowers.md   READ FIRST — what the architecture buys us
  concepts.md            features enabled by the architecture
  llamacpp-protocol.md   what we send / what we get back from llama.cpp
  changelog.md           per-phase notes for the framework infra
```

## Commands

The binary is `successor` (full word — typing the brand reinforces it).
A 2-letter alias `sx` is also installed for daily ergonomics. Both
point at the same entry. Installed via:

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
                       — inside chat: /bash <cmd>     run bash, render as tool card
                                      /budget         show context fill % + thresholds
                                      /burn N         inject N synthetic tokens (test compaction)
                                      /compact        manually fire compaction
successor setup        profile creation wizard with live preview
successor config       three-pane profile config menu (browse + edit + live preview)
successor doctor       terminal capabilities + measure samples
successor skills       list loaded skills
successor tools        list registered tools
successor snapshot     headless render of a chat scenario
successor record       record an input session to JSONL
successor replay       replay a recorded session
successor bench        renderer benchmark (no TTY needed)
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

Two places it pays off in Successor today, both validated:

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
  `SuccessorChat._paint_chat_area` slices a flat list of all message lines
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

- **Themes** (`src/successor/render/theme.py`): `Theme(name, icon, dark, light)`
  bundles dark and light variants of the same visual identity. Display
  mode is now ORTHOGONAL to theme — Ctrl+T cycles theme, Alt+D toggles
  mode, both transition smoothly via `blend_variants`. The bundled
  `steel` theme is the default; user themes drop into
  `~/.config/successor/themes/*.json`.

- **Profiles** (`src/successor/profiles/`): `Profile` bundles theme +
  display_mode + density + system_prompt + provider config + skill
  refs + tool refs + intro_animation. Switching a profile is one
  user-facing action that swaps everything coherently. Built-in
  profiles: `default` (general purpose) and `successor-dev` (harness work,
  uses the bundled successor emergence intro). Slash command:
  `/profile <name>`. Keybind: Ctrl+P cycles. Title bar shows the
  active profile name.

- **Providers** (`src/successor/providers/`): `ChatProvider` Protocol +
  `LlamaCppClient` + `OpenAICompatClient` + `make_provider(config)`
  factory. Profiles reference a provider config dict; the factory
  constructs the right class.

- **Skills** (`src/successor/skills/`): SCAFFOLD only. Markdown +
  frontmatter parser, `~/.config/successor/skills/*.md` loader, `successor skills`
  inventory command. NOT yet wired into the chat — invocation strategy
  (always-on prepend vs on-demand tool) deferred until hands-on time
  with the local model.

- **Tools** (`src/successor/tools/`): SCAFFOLD only. `@tool` decorator
  registers functions in `TOOL_REGISTRY`. Built-in tools live in
  `src/successor/builtin/tools/*.py` (one example: `read_file`). User
  tools in `~/.config/successor/tools/*.py` are GATED behind
  `allow_user_tools` config (default OFF, audited to stderr). NOT yet
  wired into the chat — agent loop comes later after we study request/
  response patterns more deliberately.

- **Loader pattern** (`src/successor/loader.py`): generic `Registry[T]`
  reused by themes, profiles, skills (tools have their own
  Python-import variant). Built-in dir + user dir, user wins on name
  collision, broken files skipped to stderr. Hermetic-testable via
  `SUCCESSOR_CONFIG_DIR` env var (already supported by `config.py`).

- **Config schema v2**: `chat.json` gained `version`, `display_mode`,
  `active_profile`, `allow_user_tools` slots. v1 configs are migrated
  transparently on load. Migration is idempotent and tested.

- **Intros** (`src/successor/intros/`): standalone intro animations
  played before the chat opens. Currently bundles one: `successor` —
  an 11-frame braille emergence sequence (Bayer-dot interpolation
  between adjacent frames) that morphs through the emerge stages and
  holds the final title portrait for ~2 seconds. Total ~5 seconds,
  any keypress skips. Profiles reference it via
  `intro_animation: "successor"`. Theme-aware — paints in the active
  profile's accent color.

**`successor setup` wizard** (`src/successor/wizard/setup.py`): multi-region App
with a LIVE preview pane that's a real SuccessorChat instance the wizard
mutates as the user picks options. Eight steps (welcome, name, theme,
mode, density, intro, review, saved) with sidebar progress, footer
keybinds + colored progress bar, validation glow on bad input, toast
notification on save. The preview pane uses the chat's existing
`_set_theme`/`_set_display_mode`/`_set_density` machinery so the
smooth blend transitions run for free — no animation code in the
wizard at all. **The wizard is the proof that the harness can build
itself**: writing it required ZERO new primitives.

**`successor config` menu** (`src/successor/wizard/config.py`): three-pane App
for ongoing tweaks. Profiles list (left) | settings tree (middle) |
live preview (right). Tab cycles focus, ↑↓ navigates within the
focused pane, Enter edits the selected setting.

Field kinds (FieldKind enum):
  - **CYCLE** — inline overlay with ↑↓ pick (theme, density,
    provider_type)
  - **TOGGLE** — immediate flip on Enter (display_mode, intro_animation)
  - **TEXT** — inline single-line editor with cursor model
    (provider_model, provider_base_url)
  - **NUMBER** — TEXT with int/float validation
    (provider_temperature, provider_max_tokens)
  - **SECRET** — TEXT but value displays as ••• when not editing,
    plaintext while editing (provider_api_key)
  - **MULTILINE** — opens a full-screen `_PromptEditor` overlay with
    full text editor cursor model (system_prompt)
  - READONLY — only `skills` and `tools` (those aren't wired)

Dirty fields get a `*` marker, dirty profiles get a `*` next to their
name in the left pane, the title bar shows a "N unsaved" pill. **S**
saves all dirty profiles to disk + reloads the registry, **R** reverts.
Esc with unsaved changes shows a warning toast first; second Esc
discards. From inside the chat, `Ctrl+,` and `/config` open the menu,
and the cli main loop handles the chat → config → chat re-entry
seamlessly.

**Delete profile** — capital `D` from the profiles pane opens a
centered confirmation modal (theme-aware accent_warn border, 200ms
ease-out fade-in via `lerp_rgb`). Two modes: **delete** unlinks a
pure user JSON file from disk; **revert** unlinks a user override
of a built-in, letting the built-in re-emerge in its place. Refusal
cases all show a warning toast and never open the modal: pure
built-ins (nothing to remove), the active profile per chat.json
(would orphan the chat), the last remaining profile (no fallback
left). Safe-default keys: only `Y` confirms; N/Enter/Esc all cancel.

**The system prompt editor** (`src/successor/wizard/prompt_editor.py`) is
a real text editor with the full feature set you'd expect:

- **Pretext-shaped soft word wrap** — per-source-line cache invalidates
  only the edited line; resize re-wraps everything (rare). Greedy
  word-boundary breaks via `_wrap_source_line`.
- **Visible-row cursor navigation** — UP/DOWN navigate visible chunks
  (not source rows), so a long source line that wraps to 3 visible
  lines lets you arrow through them naturally.
- **Selection** — `Shift+arrows` extend, `Ctrl+A` selects all, `Esc`
  clears (or cancels editor on second press).
- **Selection-aware editing** — typing/Backspace/Delete with active
  selection replaces the range. Multi-line selection deletes spliced
  correctly.
- **OSC 52 clipboard** — `Ctrl+C` copies selection via the existing
  `Terminal.copy_to_clipboard` (passed in as a callback at editor
  construction time). `Ctrl+X` cuts (copy + delete).
- **Full-row selection highlight** — multi-line selection highlights
  extend across the entire text area width for fully-selected interior
  rows, matching VS Code / Notepad / every modern text editor.
- **Standard keybinds** — `Ctrl+S` save, `Esc` cancel, line number
  gutter, char count + selection size in the title bar.

No agent CLI has ever let you edit your system prompt directly inside
the TUI before, with proper word wrap and selection.

## Bash-masking subsystem (added 2026-04-07, phase 5.0)

The premise: mid-grade local models (Qwen 3.5 27B distilled and friends)
are *fluent* in bash because they've eaten millions of bash commands in
pretraining. They are *unreliable* with structured tool-call schemas
(`docs/llamacpp-protocol.md` calls this out explicitly). So we don't
ask them to learn a tool schema. We let them write bash in fenced code
blocks, parse the bash CLIENT-SIDE, and render it as a structured tool
card with verb + params + risk + output.

The renderer is uniquely positioned for this — the diff layer can
rewrite cells AFTER the fact, so we can intercept a bash command, paint
a structured card with the parser's interpretation, and stream the
output beneath. The model emits raw bash; the user sees a clean
structured action card with risk classification and exit status.

**`src/successor/bash/`** is the whole subsystem:

- **`cards.py`** — `ToolCard` frozen dataclass: verb, params, risk
  ("safe"/"mutating"/"dangerous"), raw_command, confidence (0-1),
  parser_name, output, stderr, exit_code, duration_ms, truncated.
  Cards are immutable; the executor builds enriched cards via
  `dataclasses.replace()`.

- **`parser.py`** — `@bash_parser("name")` decorator that registers
  pattern parsers in `_PARSERS`. `parse_bash(cmd)` shlex-splits and
  dispatches; unknown commands fall back to a generic "bash ?" card
  with confidence 0.5. **`clip_at_operators(args)`** truncates argv
  at the first shell operator (`|`, `||`, `&&`, `;`, `>`, `2>`, ...)
  so parsers don't bleed into pipelined commands.

- **`risk.py`** — `classify_risk(cmd)` runs IN ADDITION to the parser
  on the raw command string. Detects rm-rf-on-system-paths, sudo,
  curl|sh, eval, chmod 777, redirect-into-system-path, dd-to-block-device,
  fork bombs, mkfs/fdisk, shutdown/reboot, kill PID 1, iptables flush.
  Returns `(Risk, reason)`. The dispatch layer takes
  `max_risk(parser_risk, classifier_risk)` so either layer can escalate.

- **`exec.py`** — `dispatch_bash(cmd, *, allow_dangerous=False)` is
  the public entry point. Parse + classify + run via
  `subprocess.run(shell=True, executable="/bin/bash")` with timeout
  (default 30s) and 8KB output truncation. Dangerous commands raise
  `DangerousCommandRefused` (carrying the refused card so the UI
  can show what was blocked). `preview_bash(cmd)` is the parse-only
  path used by the renderer to show the card BEFORE execution.

- **`render.py`** — `paint_tool_card(grid, card, *, x, y, w, theme)`
  pure paint function. Top section: verb header pill + key/value
  param table inside a rounded box. Bottom border: the raw command
  prefixed with `$ ` (dim italic — always preserved so users can
  spot parser misses). Below the box: command output with code-tinted
  bg + status footer (`✓ exit 0 in 12ms` or `✗ exit 1`). Risk-tinted
  border + verb glyph: `▸` safe (theme.accent), `✎` mutating
  (theme.accent_warm), `⚠` dangerous (theme.accent_warn). Confidence
  < 0.7 adds a `?` badge after the verb. `measure_tool_card_height()`
  is the matching pure measurer for callers that need to lay out
  cards before painting.

- **`patterns/`** — one file per command family. v0 ships parsers
  for: ls, cat, head/tail, grep/rg/ripgrep, find/fd/fdfind, pwd,
  echo, true/false, mkdir, touch, rm, cp/mv, git (with per-subcommand
  risk + git-push-force escalation), python/python3, which/type. The
  package `__init__` imports them all so the decorators run at
  registry-init time.

**Chat integration** (`src/successor/chat.py`): `_Message` gained an
optional `tool_card: ToolCard | None` field — non-None marks the message
as a tool card and forces synthetic=True (never sent to the model).
`_RenderedRow` gained `prepainted_cells: tuple[Cell, ...]` for the
fast path. `_render_tool_card_rows()` paints the card into a sub-grid
once and snapshots each row's cells; `_paint_chat_row` short-circuits
to copy them verbatim. The chat's flat-row scroll model is preserved.

**`/bash <command>` slash command** is the v0 proof: type
`/bash ls -la /etc` in the chat and you get a structured `list-directory`
card with the parsed params, the raw command on the bottom border, the
real `ls` output beneath, and the exit code + duration in the status
footer. When the agent loop lands, the SAME `dispatch_bash()` becomes
the tool dispatch entry point — no rework.

**Why this is the right architecture for local models**:
1. The model's `tools`/`tool_choice` API path is never used (Qwen 3.5
   distill is unreliable at it per `docs/llamacpp-protocol.md`)
2. The model emits bash in its strongest mode (fenced code blocks)
3. Risk classification is a render-time concern, not a prompting concern
4. Every level of the pipeline degrades gracefully — unknown command
   becomes a generic "bash ?" card that still executes
5. The user always sees the raw command on the bottom border so they
   can verify the parser's interpretation

## Agent loop + compaction (added 2026-04-07, phase 5.1)

The agent loop, the four-tier compaction pipeline (just two of the
four are mandatory in our build), and the burn-tested-against-A3B
proof that semantic continuity survives a 96.9% context reduction.
Lives in `src/successor/agent/`.

**Architectural decisions cross-checked against free-code's actual
source** at `~/dev/ai/free-code-main/`:

- Generator-based loop (free-code) → tick-driven state machine (us),
  because our chat is a sync frame-driven `App` and async generators
  don't compose with `on_tick` cleanly. Same flow, different plumbing.
- 4-layer compaction pipeline → 2 layers. Free-code's snipCompact and
  contextCollapse are feature-gated and compiled-out in external
  builds; only microcompact + autocompact are mandatory. We mirror
  the two that matter and skip the optimization layers.
- CacheSafeParams + cache_edits microcompact → SKIPPED entirely.
  llama.cpp's KV cache is local and free; there's no remote prompt
  cache to keep warm.
- StreamingToolExecutor (concurrent + streaming-during-model-stream)
  → SYNC tool dispatch in v0. The shape is right, the concurrency
  isn't wired yet because we don't have an asyncio event loop.
- FallbackTriggeredError → SKIPPED. We have one model.
- Reactive compact on prompt-too-long → KEPT. Same trigger semantics.
- Token thresholds → ours scaled for llama.cpp's larger contexts:
  `autocompact_buffer = max(4_000, window // 32)` (vs free-code's
  flat 13K).

**The agent package** (`src/successor/agent/`):

- **`log.py`** — `LogMessage` + `ApiRound` + `MessageLog` +
  `BoundaryMarker` + `AttachmentRegistry`. Compaction-ready data
  shape: PTL truncation drops whole `ApiRound`s so the API never
  sees orphaned tool_results. Boundary markers are first-class
  entities the renderer can paint as visible dividers. **Critical
  detail**: `LogMessage.to_api_dict()` converts boundary/summary
  messages to `role=user` with explicit `[summary…]` prefix because
  Qwen 3.5's chat template enforces "system message must be at the
  beginning" and rejects any non-leading system message with a
  Jinja exception.

- **`events.py`** — frozen `ChatEvent` ADT for everything the loop
  yields (`StreamStarted`, `BashBlockDetected`, `Compacted`,
  `BlockingLimitReached`, etc.). The chat consumes via callback;
  tests assert isinstance + count.

- **`tokens.py`** — `TokenCounter` with two paths: (1) llama.cpp's
  `POST /tokenize` endpoint for ground-truth counts (verified
  against the live server: `"hello world"` → 2 tokens), (2) char
  heuristic at 3.5 chars/token (deliberately conservative to
  overestimate slightly so compaction fires early). LRU per-string
  cache (default 1024 entries). Auto-disables endpoint after 3
  consecutive HTTP failures.

- **`budget.py`** — `ContextBudget` (window + warning/autocompact/
  blocking buffers + threshold predicates), `CircuitBreaker` (trips
  after 3 consecutive failures), `RecompactChain` (blocks two
  compactions within 30s + 3 turns), `BudgetTracker` bundle.
  `should_attempt_compaction(used, turn)` returns `(decision, reason)`
  so refusals are diagnostic.

- **`microcompact.py`** — pure stateless function that clears stale
  tool result content via count-based and time-based triggers. Replaces
  `tool_card.output` with placeholder while preserving the structural
  card so chat history stays navigable.

- **`compact.py`** — `compact(log, client, *, counter, ...)`
  summarizes older rounds into one block via the LLM, keeps the most
  recent N rounds verbatim. **PTL retry loop**: on prompt-too-long,
  drops oldest 3 rounds-to-summarize per attempt, up to 3 retries.
  Default summarization instructions are tuned for Qwen 3.5 distill
  (explicit about preserving facts/paths/decisions, explicit about
  discarding reasoning chains).

- **`bash_stream.py`** — `BashStreamDetector`, a state machine that
  consumes streamed model content character-by-character and detects
  fenced ```` ```bash ```` blocks even when fence markers split
  across chunk boundaries. Verified by one-character-at-a-time drip
  test. Handles `bash` / `sh` / `shell` / `zsh` / `fish` / `console`
  / `terminal` aliases, comments, blank lines, backslash continuation,
  multi-block accumulation. `flush()` at end-of-stream to resolve
  any in-progress fence with no trailing newline.

- **`loop.py`** — `QueryLoop`, the agent loop as a tick-driven state
  machine. Phases: `IDLE → COMPACTING → STREAMING → EXECUTING_TOOLS
  → IDLE`. Owns the message log, budget, token counter, current
  stream, bash detector, pending tool queue. **Reactive compact**
  fires on PTL stream errors. Events flow OUT through `on_event`
  callback. Synchronous tool dispatch in v0.

**Chat integration**: rather than rewriting the chat to use
`MessageLog`, the chat keeps its `_Message` list and uses two
adapter methods (`_to_agent_log` / `_from_agent_log`) to bridge.
Three new slash commands wire the agent in:

- **`/budget`** — show context fill % + token counts + threshold
  state. Calls `_to_agent_log()`, runs `TokenCounter.count_log`,
  reports stats including `warn @ N`, `autocompact @ N`, `blocking @ N`.

- **`/burn N`** — inject N synthetic tokens of varied content
  (code blocks, lorem-ipsum padding, fake file paths) so compaction
  can be tested without burning real model calls. Each round gets
  realistic timestamps so microcompact's idle logic doesn't fire.

- **`/compact`** — manually fire `compact()` against the chat's
  current history. Builds the agent log, runs compaction against
  the live client, writes the result back. Reports the boundary
  stats as a synthetic message ("✓ compacted: N → M tokens, X%
  reduction").

**Burn rig results** (live A3B at 50K context, 165 rounds, 4 key
facts seeded in round 1):
```
Pre:   40,052 tokens   165 rounds
Post:   1,259 tokens     6 rounds
Reduction: 96.9%  (saved 38,793 tokens)
Wall time: 40.2s
Semantic recall: 100% (4/4 facts retrieved correctly)
```

**The two bugs the burn rig caught** (would have shipped silently
under unit-test-only discipline):

1. `providers/llama.py` `urlopen(req, timeout=connect_timeout)`
   sets the socket timeout for ALL I/O on the connection, not just
   connect. A3B processing a 38K-token prompt took 30+ seconds to
   emit the first byte; the 5s connect_timeout fired the read.
   Fixed by `max(timeout, connect_timeout)`.
2. Boundary + summary messages were emitted with `role=system` but
   Qwen 3.5's chat template enforces "system message must be at the
   beginning" and raised a Jinja exception. Fixed by emitting them
   as `role=user` with explicit `[summary…]` prefix.

**The compact-test profile** (`builtin/profiles/compact-test.json`)
points at A3B with `context_window: 50000`. Forge theme so the
chrome visually signals "stress-test mode". Switch in via
`/profile compact-test` after running `scripts/swap_to_a3b.sh`.

**`scripts/swap_to_a3b.sh` and `scripts/swap_to_qwopus.sh`** —
manual-run scripts that swap the running llama-server between
qwopus (production) and A3B (compaction stress testing). Both
scripts export `LD_LIBRARY_PATH` to the llama.cpp build dir
because the binary at `/usr/local/bin/llama-server` has unresolved
shared lib references. Both wait for `/health` before returning.
Documented Nyx-impact in the script header — qwopus is what the
Nyx Telegram Relay uses, so swapping to A3B takes it down briefly.

## Compaction animation (added 2026-04-07, phase 5.2)

The compaction event becomes the harness's signature visual moment.
Five phases over ~5 seconds, every cell driven by `(state, time) →
cells` math through the existing renderer primitives. Lives entirely
in `chat.py` + a new `paint_horizontal_divider` primitive.

**The arc**:

```
T=0     compaction completes → snapshot pre-compact messages
T=0-300   ANTICIPATION  base_color tinted toward accent_warm by 35%
                        (the rounds-to-be-summarized get a subtle glow)
T=300-1500 FOLD         per-row fade_alpha lerps fg → bg via
                        ease_out_cubic. Old rounds dissolve into the
                        void; chars stay but their fg matches bg
T=1500-1900 MATERIALIZE the boundary divider draws in from CENTER
                        outward via paint_horizontal_divider(t).
                        Pill snaps in at t > 0.6 with its own
                        alpha fade-in over the remaining 0.6 → 1.0
T=1900-2500 REVEAL      summary message fades in from theme.bg →
                        theme.fg_dim via lerp_rgb on its rows
T=2500-5000 SETTLED     boundary stays as a permanent visible artifact
                        with a subtle 0.4 Hz pulse via lerp_rgb
                        toward theme.accent — "living artifact"
```

**Why this is unique to Successor**: Other agent harnesses can't
animate compaction because once they `print()` a message line, it
belongs to the terminal and they can't reach it. Our diff layer owns
every cell, so we can fade them out, draw a divider through them,
and replace them with a summary artifact — frame by frame,
deterministic, no flicker.

**Architecture**:

- **`render/paint.py:paint_horizontal_divider`** — generic primitive
  that draws a horizontal line growing from the center outward at
  progress `t` (0 → 1). Returns cells drawn. Useful for any "divider
  draws in" effect, not just compaction.

- **`_Message`** gained `is_boundary` / `is_summary` / `boundary_meta`
  fields. Boundary messages route to `_paint_compaction_boundary`
  (the divider painter), summary messages get a dim/italic treatment
  with a `▼` prefix. Both flags force `synthetic=True` (never sent
  to the model).

- **`_RenderedRow`** gained `is_boundary`, `boundary_meta`,
  `materialize_t`, `is_summary`, `fade_alpha`. The `_faded` inner
  helper in `_paint_chat_row` applies fade_alpha uniformly to
  leading text, blockquote borders, and body spans.

- **`_CompactionAnimation` dataclass** with the 5-phase state machine.
  `phase_at(now)` returns `(phase_name, t)` where t is 0-1 progress
  within the current phase. Phase names: `pending → anticipation →
  fold → materialize → reveal → toast → done`.

- **`_handle_compact_cmd`** snapshots `self.messages` BEFORE running
  compaction, swaps to the post-compact state immediately after,
  arms the animation. The painter then drives the visible transition.
  No "compacting…" status message — the animation IS the status.

- **`_build_message_lines`** routes through `_build_rows_from_messages`
  with animation-phase-aware overrides:
  - `fold` / `anticipation` → paint the snapshot
  - `materialize` / `reveal` / `toast` → paint `self.messages` with
    overrides on boundary materialize_t and summary fade_alpha
  - `done` → clear `_compaction_anim`, paint normally

- **`_paint_chat_area`** has a scroll override during materialize/
  reveal/toast that finds the boundary row and pins `effective_scroll`
  so the divider sits in the upper-sixth of the visible chat region.
  This guarantees materialization is IN VIEW regardless of where
  the user was scrolled when /compact fired.

- **`_paint_compaction_boundary`** is the divider painter:
  ```
  ━━━━━━━━━━━━━━━━━━━━━━━━━┤ ▼ 6 rounds · 3k → 2k · 37% saved ▼ ├━━━━━━━━━━━━━━━━━━━━━━━━━
  ```
  Risk-tinted via `theme.accent_warm`. Subtle 0.4 Hz pulse via
  `lerp_rgb` toward `theme.accent` after materialize completes.

**The fade-reads-as-bg trick**: chars stay in the grid during fold,
but their fg color converges to `theme.bg` exactly. Verified by
reading actual cell colors at multiple elapsed times — at t=1.5s
(end of fold) the channel distance from bg is 0, meaning the chars
are perfectly invisible. No char deletion, just color transitions.

**Live E2E results** (qwopus, /burn 3000 → /compact, 10.8s wall time):
all 7 captured animation phases render correctly. The boundary
materializes from center outward, the pill snaps in, the summary
appears below, the kept rounds stay anchored at the bottom.

**Context fill bar overhaul** (also phase 5.2): the static-footer
ctx bar now reads its count from the agent's TokenCounter (when
cached) and its window from `profile.provider.context_window`
(replacing the hardcoded `CONTEXT_MAX`). Adds threshold-state
classification mirroring `agent.budget.ContextBudget`:
- `◉ COMPACT` badge at autocompact threshold
- `⚠ BLOCKED` badge at blocking threshold
- Continuous 0.5 Hz pulse via `math.sin` blending the bar color
  toward fg when in autocompact/blocking state

**What's NOT yet built**: visible toast notification that slides
in after the boundary settles (currently the boundary's continuous
pulse handles the "look at me" signal), wiring `/profile compact-test`
end-to-end for the burn rig to run from inside the chat without
scripts, concurrent tool execution, streaming tool execution (tools
start AFTER stream commits in v0), framework docs.

See [`docs/changelog.md`](docs/changelog.md) for the per-phase notes.

## Things deliberately deferred

These are known limits, all waiting on the same upcoming "real key
parser" piece:

- ASCII-only typed input (no UTF-8 multi-byte input)
- No arrow-key cursor navigation in the input box
- No bracketed paste in the chat
- No interrupt during successor response (Ctrl+C still quits)
- History recall (Up/Down in input)

When the real key parser lands, all of these get fixed simultaneously.
See [`docs/concepts.md`](docs/concepts.md) for the broader roadmap.

## Reference repos in `~/dev/ai/`

For architectural comparison:
- `codex-reference/` — OpenAI Codex CLI (Rust, ~80 crates, ~595K LOC)
- `hermes-reference/` — Nous Research Hermes Agent (Python, ~297K LOC)
- `opencode-reference/` — sst/opencode (TypeScript, ~59K LOC)
- `hk13/` — the deprecated agent harness Successor replaces

## Validated by user

> "this renders better than every other agent harness already. game
> changer rendering method." — 2026-04-06

Don't break the architecture. Don't reach for libraries. Don't add
side effects to `on_tick`. Read `rendering-superpowers.md` if any
doubt.
