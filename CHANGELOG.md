# Changelog

User-facing release notes. The internal per-phase development log
lives in [`docs/changelog.md`](docs/changelog.md).

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

### Known limits (deferred)

- ASCII-only typed input (no UTF-8 multi-byte input)
- No arrow-key cursor navigation in the input box
- History recall (Up/Down in input)
- Streaming tool execution (tools start AFTER the stream commits)
- Concurrent tool execution

When the real key parser lands, several of these get fixed together.
See [`docs/concepts.md`](docs/concepts.md) for the broader roadmap.
