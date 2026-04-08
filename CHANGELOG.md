# Changelog

User-facing release notes. The internal per-phase development log
lives in [`docs/changelog.md`](docs/changelog.md).

## v0.1.11 — 2026-04-08

Corrective follow-up for mouse ownership semantics.

### What changed

- `mouse off` is restored to the intended behavior:
  the terminal owns wheel scrolling and native click-drag selection
- `mouse on` still enables Successor-owned wheel scrolling plus
  clickable title-bar widgets
- `src/successor/config.py` still uses schema v3, but v2 → v3 now
  preserves the stored `mouse` value instead of forcing old installs
  into mouse-on behavior

### Verification

- focused regressions: `116 passed`
- full local suite: `1074 passed`
- real local config probe confirmed the existing
  `~/.config/successor/chat.json` now starts with `mouse: false`
  and `term.mouse_reporting = False`

## v0.1.10 — 2026-04-08

Tool-card light-theme cleanup plus mouse ownership fix.

### What changed

- `src/successor/bash/render.py` now owns the full row background for
  settled and running tool-card output/status rows, which removes the
  leaked default-black side rails and footer tails that were visible in
  light themes
- the mouse split is restored to the intended behavior:
  `mouse off` leaves wheel/selection to the terminal, `mouse on`
  gives Successor clickable widgets plus in-chat wheel scroll
- `src/successor/config.py` still stamps v3, but v2 → v3 now preserves
  the stored `mouse` value exactly instead of forcing old installs into
  mouse-on behavior

### Verification

- focused regressions: `114 passed`
- full local suite: `1072 passed`
- direct light-theme render probe confirmed output/status row edges now
  carry theme backgrounds instead of default black cells
- real local config probe confirmed the existing
  `~/.config/successor/chat.json` now preserves `mouse: false` at
  startup, so terminal-native mouse ownership is restored

## v0.1.9 — 2026-04-08

Semantic diff cards for file changes.

### What changed

- deterministic mutating bash cards now settle with structured change
  artifacts when the target shape is known ahead of time:
  `write-file`, `create-file`, `delete-file`, `delete-tree`,
  `create-directory`, single-target `copy-files`, and single-target
  `move-files`
- explicit unified diff commands now render semantically too:
  `git diff`, `git show`, and generic unified-diff stdout with
  `---` / `+++` / `@@` hunk markers
- tool cards now show file headers, hunk headers, context lines, added
  lines, removed lines, and note rows through the existing prepared
  output cache instead of falling back to plain wrapped stdout
- `scripts/e2e_chat_driver.py` gained `assert_turn_plain_contains` plus
  a live `rewrite_diff` scenario so the E2E harness can verify the
  rendered transcript, not just the final filesystem state

### Verification

- targeted bash/chat regression slice: `317 passed`
- full local suite: `1066 passed`
- live local llama.cpp/Qwopus E2E:
  `scripts/e2e_chat_driver.py --scenario rewrite_diff --runs 3`
- live artifact review confirmed:
  - deterministic `write-file` cards show `note.txt [added]` and
    `note.txt [modified]`
  - modified cards show real hunk lines including `-beta` and `+gamma`
  - explicit `git diff -- note.txt` also renders as a semantic diff card

## v0.1.7 — 2026-04-08

Local subagent/runtime follow-on: slot-aware scheduling is now a real
profile feature instead of a design note, and the local diagnostics now
surface the server capabilities that actually matter for running worker
tasks well.

### Local scheduling + diagnostics

- `SubagentConfig` now has an explicit `strategy` field:
  `serial`, `slots`, or `manual`
- `src/successor/chat.py` now resolves the live background-model width
  from that strategy and the active provider, and `/tasks` reports the
  effective scheduler shape
- `src/successor/wizard/config.py` now exposes the scheduling strategy
  in the `subagents` section
- `src/successor/providers/llama.py` now surfaces runtime capabilities
  from `/props`: context window, slot count, `/slots` availability, and
  parallel-tool-call support
- `successor doctor` now reports llama.cpp slot capacity and whether
  the server advertises parallel tool calls

### Parallel read guidance

When the active provider advertises parallel native tool calls,
`src/successor/chat.py` now tells the model to fan out only independent
read-only bash work in the same assistant turn, while keeping writes
and dependent steps serialized.

### Verification

Local verification for v0.1.7:

- full pytest suite: 1059 passing
- `successor doctor` on the local llama.cpp server reports:
  - `ctx window 262144 tokens`
  - `slots 4 total (/slots on)`
  - `tool calls parallel supported`
- live Qwopus subagent overlap check with `strategy=slots` showed two
  `/fork` tasks running concurrently and completing cleanly
- live Qwopus two-file read probe still returned the correct answer,
  but the model continued to serialize the two bash reads; the runtime
  is ready for same-turn parallel reads, the current model still needs
  more steering before that behavior is reliable

## v0.1.8 — 2026-04-08

Terminal input fix: mouse-wheel scrolling in some terminals was being
translated into fake Up/Down cursor keys while Successor was in the
alternate screen, which made wheel-up look like "recall previous
prompt" instead of scroll behavior.

### What changed

- `src/successor/render/terminal.py` now saves and disables xterm
  alternate-scroll mode (`?1007`) for the duration of the TTY session,
  then restores it on exit
- added `tests/test_terminal.py` to lock the terminal enter/exit escape
  sequence contract in place

### Verification

- targeted regression: `tests/test_terminal.py` + `tests/test_input_history.py`
- full local suite: `1060 passed`

## v0.1.6 — 2026-04-08

Subagent tool pass: the background-worker foundation from v0.1.5 is
now a real model-visible capability, with scheduler hardening and live
llama.cpp verification.

### Model-visible subagents

The `subagent` tool is now part of the native tool surface. When a
profile enables that tool, the model can fork a background worker that
inherits the current conversation context, runs in a headless child
chat, and later reports back through a structured completion
notification.

The runtime keeps the contract tight:

- child chats strip the `subagent` tool to prevent recursive forks
- completion notifications are injected back as user-role API events
- the tool is hidden from the model when `notify_on_finish` is off,
  because that configuration would otherwise create workers the parent
  could never hear back from
- queue-width changes made while tasks are active now defer cleanly
  until the manager goes idle, then apply safely

### Verification

Local verification for v0.1.6:

- full pytest suite: 1051 passing
- live llama.cpp/Qwopus E2E: manual `/fork` path passed
- live llama.cpp/Qwopus E2E: model-visible `subagent` path passed
- CLI surfaces: `successor -V` reports `0.1.6`

## v0.1.5 — 2026-04-08

Unicode input audit + grapheme-aware editing pass.

Late in the same release line, v0.1.5 also picked up the first
shipping subagent foundation: manual `/fork`, `/tasks`, and
`/task-cancel`, per-profile subagent settings in the config menu,
background task transcripts, and a title-bar task badge. The runtime
uses isolated headless child chats so the background path reuses the
real tool-calling + continuation loop instead of a toy executor.

### Grapheme-aware deletion

Typed input was already UTF-8-capable once the real key decoder
landed, but text editing still deleted one Python codepoint at a
time. That showed up most clearly with decomposed accents (`e` +
combining acute), and it also affected emoji modifiers, ZWJ emoji,
and flag pairs.

Backspace/delete now operate on whole grapheme clusters in the chat
input, the search bar, the config menu's inline text editors, the
setup wizard's text fields, and the multiline prompt editor. The
config menu and prompt editor also move LEFT/RIGHT by grapheme
boundary, so the common edit path no longer strands the cursor
inside a visible character.

### Audit cleanup

`CLAUDE.md` no longer claims typed input is ASCII-only. The current
deferred list now reflects the real remaining gaps instead of a
stale byte-decoding limitation that was already gone.

### Tests

1014 → 1027. 13 new tests across three files:

- `tests/test_key_decoder.py` (4 tests): mixed ASCII + UTF-8 decode,
  byte-by-byte reassembly, invalid-sequence recovery, and
  bracketed-paste Unicode coalescing
- `tests/test_unicode_editing.py` (7 tests): grapheme-aware
  backspace/delete coverage for chat input, search, config inline
  editing, the prompt editor, and the setup wizard
- `tests/test_intro_sequence.py` (2 tests): startup intro frame
  selection excludes `hero.txt` and preserves numbered ordering

## v0.1.4 — 2026-04-08

Tier-1 polish pass: closes the most visible UX gaps for cold visitors
landing on the public repo, adds two new themes, and ships
GitHub Actions CI.

### Input history recall

Up arrow on an empty input buffer enters recall mode and loads the
most recent submitted message, the same way bash and zsh handle
history. Up walks older, Down walks newer, Down past newest
restores any draft you were typing before you started recalling.
Esc bails out of recall mode and brings the draft back. Any
editing key (typing, backspace) exits recall mode and lets you
edit the recalled text as a fresh draft.

The Up/Down handlers are layered: autocomplete dropdown wins
first, then history recall, then chat scroll. Up never clobbers
an in-progress draft because the recall gate requires an empty
input buffer to trigger. The history is in-memory only and capped
at 100 entries; consecutive duplicates are deduped so `/profile
cycle` spam does not pollute the buffer.

### Two new builtin themes

- `paper`: warm cream and sepia palette with fountain-pen accents,
  designed for daytime reading. Both dark (warm low-blue surface)
  and light (cream document surface) modes.
- `cobalt`: deep saturated cobalt blue dark theme with cool white
  text plus a complementary warm accent. Most users will pick this
  for night sessions.

Successor now ships **four** builtin themes (steel, forge, paper,
cobalt). All four use oklch color space for accurate perceptual
blending between themes and dark/light modes.

### Chat empty-state hero swap

The chat empty-state hero panel was loading the final frame of the
emergence animation, which has the SUCCESSOR title text painted
across the top. Reading the title text once you are already in the
chat felt redundant. Swapped to a new `hero.txt` file (the soldier
portrait without the title overlaid). The wizard welcome screen
still loads the title frame because the welcome screen wants the
"welcome to SUCCESSOR" framing.

The loader prefers `hero.txt` and falls back to `10-title.txt` for
any custom intros that have not added a hero file yet.

### CONTRIBUTING.md + GitHub Actions CI

- New `CONTRIBUTING.md` covers dev setup, test commands, the One
  Rule reference, the anti-slop discipline for new docs, and a
  PR process.
- New `.github/workflows/test.yml` runs pytest on push to master
  and every PR, against Python 3.11 / 3.12 / 3.13 in matrix.
  Caught a real Python 3.13-only `from copy import replace`
  import on the very first run that broke 3.11 / 3.12 (the import
  was dead code, fixed in the same release).
- README picks up CI status, Apache 2.0 license, and Python 3.11+
  badges at the top.

### pyproject.toml description

The package description on PyPI / `pip show` / GitHub social
previews used to say "for local llama.cpp models" which understated
the harness. Updated to mention OpenAI + OpenRouter alongside
llama.cpp, the renderer architecture, and the autocompactor.

### Tests

974 → 1014. 40 new tests across three files:

- `tests/test_input_history.py` (24 tests): the full recall state
  machine including ring buffer, dedupe, cap, Up/Down navigation,
  in-recall edits, Esc restore, and submit interactions
- `tests/test_tier1_polish.py` (13 tests): both new themes load,
  full palettes, distinct bg colors, chat construction with each
  theme, pyproject description regex check, CI workflow YAML
  validation, README badge presence
- `tests/test_intro_art.py` picked up 3 new tests for the hero.txt
  loader preference + the legacy 10-title.txt fallback + a density
  check confirming the bundled hero is the no-text variant

## v0.1.3 — 2026-04-07

Configurable autocompactor + first-launch polish.

### Configurable autocompactor

The chat now ships an end-to-end autocompact gate at
`SuccessorChat._begin_agent_turn`. Before each agent turn the gate
checks the current token count against percentage-based thresholds
derived from the active profile's new `compaction` block, and (when
the threshold is crossed) defers the turn behind a background
compaction worker that resumes the turn against the freshly compacted
log when it finishes.

- New `CompactionConfig` frozen dataclass with 9 fields:
  `warning_pct` / `autocompact_pct` / `blocking_pct` (each a fraction
  of the resolved context window), matching `*_floor` token minimums
  so tiny windows still get usable headroom, plus `enabled`,
  `keep_recent_rounds`, and `summary_max_tokens`. Validation enforces
  the threshold ordering invariant + range checks at construction
  time.
- The wizard has a new `compaction` step (now 10 steps total) with
  four presets: **default** (12.5% / 6.25% / 1.5%), **aggressive**
  (25% / 12.5% / 3%), **lazy** (5% / 2% / 0.5%), **off**. Each preset
  is rendered with a description and a live preview panel showing
  the resolved buffer thresholds against a 200K reference window.
- The config menu (`Ctrl+,`) has a new `compaction` section with
  per-field editors for all 9 knobs. Percentages are entered and
  displayed as percent (e.g. type `6.25` for 6.25%); the conversion
  to fraction happens at commit time.
- Profile JSON gained a `compaction` block. Lenient parsing applies:
  missing fields use defaults, partial fields merge with defaults,
  malformed values silently fall back so the profile still loads.
- New post-compact size assertion: if the new log is at least 90% of
  the original size, `compact()` stamps a `warning` field on the
  `BoundaryMarker` and the boundary message in the chat picks up a
  `⚠ underperformed` annotation. Non-fatal, but visible.
- Per-turn guard prevents the gate from firing twice on the same
  user message. In-flight worker guard prevents stacking workers.
  `Ctrl+G` cancellation clears the deferred-resume flag so a
  cancelled compaction does not silently resume the deferred turn.
- New `docs/compaction.md` covers the full schema, threshold math,
  the gate flow, and the failure modes the post-compact assertion
  catches.

### Profile + default behavior

- The bundled `default` profile now ships with `bash` enabled out of
  the box. New users see the agentic loop on their very first turn.
- Both `default` and `successor-dev` ship with explicit `compaction`
  blocks so the JSON files document the configuration surface.
  `successor-dev` ships with the **aggressive** preset to keep dev
  sessions responsive at the edge of the context window.

### Tests

881 → 974. New coverage for `CompactionConfig` (33 tests),
percentage scaling at multiple window sizes (12 tests), the
post-compact assertion (10 tests), the chat-layer autocompact gate
(11 tests), the edge cases (12 tests), the wizard compaction step
visual rendering (8 snapshot tests), and the config menu compaction
section visual rendering (7 snapshot tests).

## v0.1.2 — 2026-04-07

Usage clarity pass. Every touch point a new user hits now points
them at the next useful step.

### Empty-state hero panel

The chat opens to a SUCCESSOR title portrait on the left and an
info panel on the right showing the active profile, provider,
model, resolved context window, server reachability, enabled
tools, theme/mode/density, and an actionable bottom hint
(`type / for commands · press ? for help`). Theme/dark/light
aware, gracefully degrades on narrow terminals to info-panel-only.
Once the user submits their first message the empty state hides
and normal chat painting takes over.

Per-profile customizable via the new `chat_intro_art` field:

- `"successor"` (default) loads the bundled title portrait
- Drop a braille frame at `~/.config/successor/art/<name>.txt`
  and reference it as `<name>`
- Or pass an absolute path to any braille text file

The default profile, the dev profile, and wizard-created profiles
all ship with `chat_intro_art="successor"` and `intro_animation="successor"`.
Users who want a quieter open can clear either field via `/config`.

### Discoverability fixes

- **Help overlay (`?`) lists every slash command.** The new
  "available commands" section is built from the live
  `SLASH_COMMANDS` registry at paint time, so any future command
  shows up automatically. Also fixed a duplicate `Ctrl+P` keybind
  (was listed in both vim-scroll and look & feel sections).
- **`successor doctor` runs a connectivity check.** New "active
  profile" section after the terminal capability dump shows the
  configured provider, base_url, model, api_key status,
  reachability (probes `/health` or `/v1/models`), and the
  resolved context window. The first command to run when something
  isn't working.
- **`successor` no-args text refreshed.** Dropped stale labels
  ("v0, scripted", "phase 6 scaffold — not yet wired"), updated
  the tagline to mention OpenAI-compatible endpoints, added a
  "First time? Run `successor setup`" footer.

### Friendlier errors

- The connection-refused / DNS / unreachable / timeout error now
  lists three numbered remediation paths: start a local
  llama-server, run `successor setup` to switch providers, or
  open `/config` to edit the profile inline. Previously it only
  mentioned the local-server path, which left users without
  llama.cpp installed dead in the water.

### Wizard polish

- **PROVIDER step hints are motivating, not just functional.**
  "free + private, needs llama-server running" / "pay-per-use
  against your OpenAI credits" / "free models available, no
  card needed" instead of the old description-only text.

### README rewrite

- Leads with the user journey (what you can do in 30 seconds)
  instead of the architectural premise. The premise is still
  there, but lower down where engineering-minded readers find
  it after they've decided to install.

### Tests

864 → 881 (17 new for the empty-state painter, the loader's
4-tier resolution, the `_is_empty_chat` predicate, narrow-terminal
fallback, and the chat_intro_art unset path).

## v0.1.1 — 2026-04-07

Post-release polish around hosted providers and the first-run experience.

### Provider support

- **OpenAI as a first-class option.** Wizard PROVIDER step now offers
  three choices: local llama.cpp, OpenAI, OpenRouter. OpenAI uses
  `https://api.openai.com/v1` and the default `gpt-4o-mini` model.
  Same `OpenAICompatClient` as OpenRouter — verified live against
  api.openai.com.
- **Auto-detected context window.** No more manual `context_window`
  in profile JSON. The chat probes the provider on first use:
  - llama.cpp `/props` → `default_generation_settings.n_ctx`
  - OpenRouter `/v1/models` → per-model `context_length`
  - OpenAI `/v1/models` → no context_length field, fall back to a
    hardcoded prefix table covering GPT-5, GPT-4.1, GPT-4o,
    GPT-4-turbo, GPT-4, GPT-3.5, and o1/o3/o4 reasoning families
- **`/v1` URL handling.** Both `LlamaCppClient` and `OpenAICompatClient`
  tolerate `base_url` with or without `/v1`, matching the OpenAI SDK
  convention. Earlier you'd get a 404 if you typed `https://openrouter.ai/api/v1`
  because the client appended `/v1` again.
- **Friendly error rendering.** Connection refused, DNS failures,
  timeouts, HTTP 401 (unauthorized), 402 (out of credits), and 429
  (rate limited) all translate into actionable hints that name the
  active profile's `base_url` instead of leaking raw urllib stack
  traces.

### First-run experience

- **SUCCESSOR emergence animation plays at the start of `successor setup`**,
  before the wizard opens. Skippable with any keypress. First-time users
  see the harness's signature visual moment within seconds of installing.
- **Wizard PROVIDER step** with a 3-way picker, inline api_key field
  with bullet display, model field with smart defaults that auto-swap
  when toggling between hosted providers (gpt-4o-mini for openai,
  openai/gpt-oss-20b:free for openrouter), and validation glow if
  required fields are missing on advance.
- **Default + dev system prompts rewritten.** Default prompt is now
  model-agnostic (no Qwen-specific suppression rules), tells the model
  it's running in a TUI with full markdown support, and establishes
  bash tool usage expectations. Dev prompt reflects the current
  architecture (bash subsystem, agent loop, async runner, native Qwen
  tool calls, compaction animation, provider auto-detection) so a
  fresh model knows what's actually in the codebase.

### Paste handling

- **Multi-line paste with overflow indicator.** CRLF/CR normalize to
  `\n`, tabs expand to 4 spaces, orphan focus tails (`[I` / `[O`) get
  stripped, control chars below 0x20 dropped. When a paste exceeds the
  visible input rows, an `↑ N more lines` badge appears on the topmost
  visible row so the user knows the content didn't get truncated.
- **`hard_wrap` newline fix.** Found while writing the overflow tests:
  `\n` was being short-circuited by the zero-width character branch
  and never producing line breaks, so multi-line input rendered as one
  long visual row. Long-standing latent bug, now fixed.

### Tests

826 → 864 passing. New coverage for paste handling, stream errors,
provider URL handling, context window detection (mocked HTTP), and
the wizard's openai/openrouter full flows.

## v0.1.0 — 2026-04-07

First public cut. Everything below works against any OpenAI-compatible
HTTP endpoint, with llama.cpp as the primary target.

### Renderer

- Five-layer cell grid pipeline. `src/successor/render/diff.py` is
  the only module that writes to stdout.
- Pure-stdlib Python 3.11+, zero runtime dependencies.
- Double-buffered frame loop with SIGWINCH-safe resize handling.
- 24-bit truecolor, oklch theme parsing, smooth blend transitions
  between themes and dark/light modes.
- Pretext-shaped prepare-and-cache primitives: `BrailleArt.layout()`
  (16x cache hit speedup), `PreparedText.lines()` (519x speedup).
- OSC 52 clipboard, bracketed paste, alt-screen.

### Chat

- Streaming chat against llama.cpp's OpenAI-compatible HTTP API.
- Live preview of Qwen-style thinking content during the reasoning
  phase so the wait never looks like a hang.
- Custom scrollback (not terminal-native): survives resize without
  flicker, supports search across history (Ctrl+F), keeps every
  past message mutable in memory so the renderer can re-color or
  annotate after the fact.
- Multi-line input with bracketed paste, tab expansion, CRLF
  normalization, and a "↑ N more lines" overflow indicator when a
  paste exceeds the visible input rows.
- Slash command palette with arrow-key navigation, ghost-text
  argument hints, and tab completion.
- Friendly error message when the llama.cpp server is unreachable
  on the first request, with the expected `llama-server` quickstart
  embedded in the hint.

### Bash tool dispatch

- Async subprocess runner: tool execution happens in a background
  thread, the chat tick loop pumps stdout/stderr into a live tool
  card with a pulsing border, an elapsed-time counter, and a
  scrolling output window.
- Verb inference: the card header resolves to `write-file path:
  about.html` (or whatever the parser determines) while the bash
  arguments are still streaming in.
- 13 built-in pattern parsers covering ls, cat, head/tail, grep/rg,
  find/fd, git, python, mkdir/touch/rm, cp/mv, echo, pwd, and
  which/type. Unknown commands fall back to a generic "bash" card
  that still runs.
- Risk classification: `safe` / `mutating` / `dangerous` with a
  separate classifier independent of the parser. Refused commands
  render as a card with the refusal reason.
- Heredoc body stripping before tokenization (so apostrophes inside
  heredoc strings don't crash the parser).
- Output capped at 8KB by the executor, displayed with a head + tail
  scrolling window so a directory listing doesn't drown out the
  cards above it.

### Agent loop

- Tick-driven state machine with continuation: after a tool batch
  finishes, the harness automatically restarts the stream so the
  model sees its own tool output and can react.
- Native Qwen `tool_calls` format via the chat template's
  `<tool_call>` / `<tool_response>` tags.
- Bounded turn cap to catch infinite loops, with a synthetic
  "turn limit reached" message if the cap fires.

### Compaction

- Two-tier pipeline: time-based microcompact for stale tool results,
  full autocompact via LLM summarization for the older rounds.
- PTL retry loop: on prompt-too-long, drop the oldest 3 rounds per
  attempt, up to 3 retries.
- Five-phase visible animation when compaction fires: anticipation,
  fold (the old rounds dissolve into the bg color), materialize
  (the boundary divider draws in from center outward), reveal (the
  summary fades in), settled (the boundary stays as a permanent
  artifact with a subtle pulse).
- Cache pre-warmer: after compaction completes, the next user
  message lands without paying the cache-miss tax.

### Profiles, themes, customization

- Profile bundles: theme, display mode, density, system prompt,
  provider config, skill refs, tool refs, intro animation. Hot-swap
  via `/profile` or Ctrl+P.
- Built-in themes: `steel` (cool blue instrument-panel oklch).
  User themes drop into `~/.config/successor/themes/*.json`.
- Three-pane config menu (`successor config` or Ctrl+, in chat):
  profiles list, settings tree, live preview pane that's a real
  chat instance the menu mutates as you pick options.
- Multi-line system prompt editor with Pretext-shaped soft word
  wrap, visible-row cursor navigation, Shift+arrow selection,
  full-row selection highlight, OSC 52 clipboard via Ctrl+C/Ctrl+X.
- Setup wizard (`successor setup`) with eight steps and a live
  preview pane the user mutates by picking options.

### Tests

- 826 tests, hermetic via `SUCCESSOR_CONFIG_DIR`. Bash dispatch
  tests use real shell builtins (no mocks). The test suite runs
  fully without a TTY because the renderer is pure functions over
  a cell grid.

### Known limits (historical, at the time of this release)

- Typed input still lacked UTF-8 decoding (historical: the real key
  parser landed later, and v0.1.5 also makes backspace/delete
  grapheme-aware)
- No arrow-key cursor navigation in the input box
- History recall (historical: shipped in v0.1.4)
- Streaming tool execution (tools start AFTER the stream commits)
- Concurrent tool execution

The real key parser later cleared the UTF-8/history items; see
v0.1.4 and v0.1.5 above plus [`docs/concepts.md`](docs/concepts.md)
for the broader roadmap.
