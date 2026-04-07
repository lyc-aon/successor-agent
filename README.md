# Successor

A terminal chat harness for local LLMs, built on a custom cell-based
renderer where one module owns the screen end to end. Runs against
llama.cpp (or any OpenAI-compatible endpoint) with bash tool dispatch
the model can drive live.

```
                                          successor · chat     successor-dev   normal   ☾   ◆ steel




 successor ▸ I am successor. Speak freely. Ctrl+C, /quit, or ? for help.

 you ▸ show me what's in this directory

 successor ▸ Here you go.

 ╭── ☰ list-directory ────────────────────────────────────────────────────────────────────────────╮
 │    path  /tmp                                                                                  │
 │  hidden  yes                                                                                   │
 │  format  long                                                                                  │
 ╰── $ ls -la /tmp ───────────────────────────────────────────────────────────────────────────────╯
    total 738664
    drwxrwxrwt  574 root     294980  Apr  7 14:38  ▸ .
    drwxr-xr-x  20 root       4096  Mar 26 19:37  ▸ ..
    -rw-rw-r--   1 user          0  Apr  2 10:15  · .session-cache
    ⋯ +105 more lines ⋯
      ↳ ✓ exit 0 in 53ms  · output truncated
▍
 ctx      36/ 262144  ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   0.01%  local
```

## The architectural premise

`src/successor/render/diff.py` is the only module in the entire
codebase allowed to write to stdout. Not Rich, not prompt_toolkit, not
`print()`, not your own one-off escape sequences from somewhere
convenient. Every visible cell, every animation, every tool card, the
streaming preview, the compaction sequence, the setup wizard, the
config menu — all of it paints into one virtual cell grid that gets
diff-committed once per frame.

This is the single decision that lets Successor:

- edit any cell of any past message in-place at frame rate
- animate compaction as the rounds dissolve into a summary boundary
- stream tool stdout into a card with a pulsing border while it runs
- search and re-style scrollback after the fact
- survive resize without flicker, and run headless without a TTY

Other harnesses can't do most of these because once they `print()` a
line, it belongs to the terminal scrollback and they can't reach it
anymore. Read [`docs/rendering-superpowers.md`](docs/rendering-superpowers.md)
for the full list of what the architecture buys you.

## What it does

**Streaming chat against a local model.** Qwen-style thinking content
is rendered as a live scrolling preview while the model is working,
so the wait never looks like a hang.

**Bash dispatch through an async subprocess runner.** When the model
emits a tool call, the harness spawns a background thread, streams
stdout and stderr into a live tool card with a pulsing border and
elapsed-time counter, then feeds the result back to the model for a
continuation turn. The card's verb and parameters are inferred from
the partial command as the arguments stream in, so the header
resolves to `write-file path: about.html` while the body is still
arriving.

**Compaction runs as a visible animation** when the context budget
tightens. The chat stays responsive throughout; once the summary is
ready the kept turns slide back in under a materialized boundary
divider.

**Profiles, themes, and a three-pane config menu** let you edit the
active profile without leaving the chat. A multi-line prompt editor
with soft-wrap, shift-arrow selection, and OSC 52 clipboard support
is used for the system prompt field so you can rewrite it inline.

**The chat's scrollback is custom**, not terminal-native. It survives
resize without flicker, supports search across history, and keeps
every past message mutable in memory so the renderer can re-color or
annotate after the fact.

## Quick start

```bash
git clone https://github.com/lyc-aon/successor-agent
cd successor-agent
pip install -e .
successor setup
```

The install registers `successor` and the `sx` two-letter alias in
`~/.local/bin`. Python 3.11 or newer. No third-party runtime
dependencies — pure stdlib for the renderer, chat, tool dispatch,
compaction, and everything shipped in the package.

`successor setup` plays the SUCCESSOR emergence animation, then walks
you through a 9-step profile creation wizard with a live preview pane:
name, theme, dark/light, density, intro animation, **provider**,
tools, review, save. The provider step gives you three choices:

| Provider | Auth | Notes |
|---|---|---|
| **local llama.cpp** | none | run `llama-server -m <model.gguf> --host 0.0.0.0 --port 8080` first |
| **openai** | API key | uses `https://api.openai.com/v1`, default model `gpt-4o-mini` |
| **openrouter** | API key | uses `https://openrouter.ai/api/v1`, default model `openai/gpt-oss-20b:free` |

The wizard saves your profile to `~/.config/successor/profiles/<name>.json`
and drops straight into the chat. The context window is auto-detected
from each provider on first use:

- **llama.cpp** probes `/props` and reads the `n_ctx` the server was
  launched with (your `-c` flag)
- **OpenRouter** reads per-model `context_length` from `/v1/models`
- **OpenAI** falls back to a hardcoded prefix-matched table covering
  GPT-5, GPT-4.1, GPT-4o, GPT-4-turbo, GPT-4, GPT-3.5, and the
  o1/o3/o4 reasoning families (because OpenAI's `/v1/models` doesn't
  expose context lengths)

The detected window drives the compaction thresholds and the live
fill bar in the chat footer, so the harness knows when to compact
without you having to set anything manually.

If you skip the wizard and run `successor chat` directly, you get the
default profile pointed at `http://localhost:8080`. If your first
message reports `[no server at http://localhost:8080]`, the local
server isn't running yet — the hint message names the URL the chat
tried so you can verify or open `/config` to point elsewhere.

## Commands

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

## Slash command palette

Typing `/` opens an inline command palette with arrow-key navigation
and ghost-text argument hints:

```
  ╭────────────────────────────────────────────────────────────────────────────╮
  │ /bash     run a bash command and render it as a structured tool card       │
  │ /budget   show current context fill % + token usage stats                  │
  │ /burn     inject N synthetic tokens to stress-test compaction              │
  │ /compact  manually trigger compaction of the current chat history          │
  │ /config   open the profile config menu                                     │
  │ /density  adjust layout density                                            │
  │ /mode     switch display mode                                              │
  │ /mouse    toggle mouse reporting                                           │
  │ /profile  switch active profile (theme + prompt + provider)                │
  │ /quit     leave the chat                                                   │
  │ /theme    switch color theme                                               │
  ╰────────────────────────────────────────────────────────────────────────────╯
```

## Tests

```bash
pytest
```

The suite is hermetic. Each test gets its own `SUCCESSOR_CONFIG_DIR`,
and bash dispatch tests use real shell builtins (no mocks). There are
864 tests at the time of writing.

## Architecture

Five layers, only the bottom one writes to stdout:

```
Layer 5 - diff.py        the ONLY module that writes to stdout
Layer 4 - paint.py       compose into a virtual cell grid
Layer 3 - paint.py       layout (text/art -> grid mutations at width W)
Layer 2 - text/braille   prepare (parse source ONCE, cache by target size)
Layer 1 - measure.py     grapheme width, ANSI strip, EAW table
```

Layers 1 through 4 are pure functions over a cell grid. Nothing above
Layer 5 ever touches the terminal. The renderer is testable by
inspecting Grid contents directly, with no PTY required, which is why
the test suite can validate the full visual output of the wizard, the
config menu, the compaction animation, and every tool card without
spawning a subprocess.

## Docs

- [`docs/rendering-superpowers.md`](docs/rendering-superpowers.md) —
  the design rules and what the architecture enables. Read this first.
- [`docs/rendering-plan.md`](docs/rendering-plan.md) — original
  architecture notes and the reasoning behind the layer split
- [`docs/concepts.md`](docs/concepts.md) — features the architecture
  can support with small additive changes
- [`docs/llamacpp-protocol.md`](docs/llamacpp-protocol.md) — what we
  send to and receive from llama.cpp's HTTP server
- [`docs/changelog.md`](docs/changelog.md) — running development history
- [`CLAUDE.md`](CLAUDE.md) — repo orientation auto-loaded by Claude
  Code sessions working in this directory

## License

Apache 2.0. See [`LICENSE`](LICENSE).
