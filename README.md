# Successor

A terminal chat harness for local LLMs, built on a custom cell-based
renderer. Runs against llama.cpp (or any OpenAI-compatible endpoint)
with a bash tool the model can drive live.

## What it does

Streaming chat against a local model over llama.cpp's OpenAI-compatible
HTTP API. Qwen-style thinking content is rendered as a live scrolling
preview while the model is working, so the wait never looks like a hang.

Bash dispatch through an async subprocess runner. When the model emits
a tool call the harness spawns a background thread, streams stdout and
stderr into a live tool card with a pulsing border and elapsed-time
counter, then feeds the result back to the model for a continuation
turn. The card's verb and parameters are inferred from the partial
command as the arguments stream in, so the header resolves to
`write-file path: about.html` (or whatever it happens to be) while the
body is still arriving.

Compaction runs as a visible animation when the context budget
tightens. The chat stays responsive throughout; once the summary is
ready the kept turns slide back in under a materialized boundary.

Profiles, themes, and a three-pane config menu let you edit the active
profile without leaving the chat. A multi-line prompt editor with
soft-wrap, shift-arrow selection, and OSC 52 clipboard support is
used for the system prompt field so you can rewrite it inline.

The chat's scrollback is custom, not terminal-native. It survives
resize without flicker, supports search across history, and keeps
every past message mutable in memory so the renderer can re-color or
annotate after the fact.

## Architecture

The core is a five-layer terminal renderer where only one module,
`src/successor/render/diff.py`, is allowed to write to stdout.
Everything above that is a pure function over a cell grid. The chat
surface, the tool cards, the compaction animation, the running-state
pulse, the streaming preview, the setup wizard, and the config menu
all paint into the same grid and get diff-committed together every
frame.

See [`docs/rendering-superpowers.md`](docs/rendering-superpowers.md)
for the design rules and anti-patterns, and
[`docs/rendering-plan.md`](docs/rendering-plan.md) for the original
architectural decisions.

## Install

```
pip install -e .
```

Registers two binaries in `~/.local/bin`:

- `successor`, the canonical command
- `sx`, a short alias for daily use

Both point at the same entry. Python 3.11 or newer. No third-party
runtime dependencies. Pure stdlib for the renderer, chat, tool dispatch,
compaction, and everything shipped in the package.

## Run

```
successor                 show help
successor chat            chat interface
successor setup           profile creation wizard with a live preview pane
successor config          three-pane profile config menu
successor doctor          terminal capabilities report
successor skills          list loaded skills
successor tools           list registered tools
successor snapshot        headless render of a chat scenario
successor record          record an input session to JSONL
successor replay          replay a recorded session
successor bench           renderer benchmark, no TTY required
```

Inside the chat:

- `Ctrl+C` or `/quit` to exit
- `Ctrl+,` or `/config` to open the config menu
- `Ctrl+P` cycles profiles, `Ctrl+T` cycles themes, `Alt+D` toggles
  light/dark, `Ctrl+]` cycles density
- `Ctrl+G` interrupts an in-flight stream or running tool
- `/bash <command>` runs a bash command as a structured tool card
- `/budget` shows the current context fill and token estimate
- `/burn N` injects N synthetic tokens (for stress-testing compaction)
- `/compact` fires compaction against the current chat history

The chat expects a llama.cpp server at `http://localhost:8080` by
default. The base URL and model name come from the active profile's
`provider` field, so you can point Successor at any OpenAI-compatible
endpoint by editing the profile.

## Tests

```
pytest
```

The suite is hermetic. Each test gets its own `SUCCESSOR_CONFIG_DIR`,
and bash dispatch tests use real shell builtins (no mocks). There are
811 tests at the time of writing.

## Docs

- [`docs/rendering-superpowers.md`](docs/rendering-superpowers.md),
  the design rules and what the architecture enables
- [`docs/rendering-plan.md`](docs/rendering-plan.md), original
  architecture notes
- [`docs/concepts.md`](docs/concepts.md), speculative features the
  architecture can support with small additive changes
- [`docs/llamacpp-protocol.md`](docs/llamacpp-protocol.md), reference
  for the llama.cpp HTTP API we consume
- [`docs/changelog.md`](docs/changelog.md), running development history
- [`CLAUDE.md`](CLAUDE.md), repo orientation auto-loaded by Claude Code
  sessions working in this directory
