# Changelog

User-facing release notes. The internal per-phase development log
lives in [`docs/changelog.md`](docs/changelog.md).

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
