# Successor

A terminal chat harness for local LLMs and OpenAI-compatible endpoints,
built on a custom cell-based renderer where one module owns the screen
end to end. Async bash tool dispatch the model can drive live, visible
compaction animation when the context budget tightens, and a 9-step
profile wizard that walks first-time users through provider setup.

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

`successor setup` plays the SUCCESSOR emergence animation and walks
you through a 9-step profile creation wizard with a live preview pane:
name, theme, dark/light, density, intro animation, **provider**,
tools, review, save. The provider step gives you three choices out of
the box:

| Provider | Auth | Notes |
|---|---|---|
| **local llama.cpp** | none | free + private, needs `llama-server` running |
| **openai** | API key | pay-per-use against your OpenAI credits |
| **openrouter** | API key | free models available, no card needed |

When you save, the wizard writes the profile to
`~/.config/successor/profiles/<name>.json` and drops straight into the
chat. The context window is auto-detected from each provider on first
use, so the harness's compaction thresholds and the live fill bar in
the chat footer are correct without you having to set anything
manually.

If you skip the wizard and run `successor chat` directly, you get the
default profile pointed at `http://localhost:8080`. If your first
message reports `[no server at http://localhost:8080]`, the local
server isn't running yet — the hint message lists three concrete
remediation paths (start a local server, run `successor setup` to
switch providers, or open `/config` to edit the profile inline).

## Inside the chat

The chat interface keeps every command discoverable from the keyboard.
Press `?` for the full help overlay (it lists every keybinding *and*
every slash command), or type `/` to open the inline command palette.

```
type / to see commands         press ? for the full help overlay
type ? for help                 press Ctrl+, to open the config menu

editing            scroll                 look & feel        commands
  Enter             ↑ ↓ scroll one line     Ctrl+P profiles    /bash <cmd>
  Backspace         PgUp/PgDn page          Ctrl+T themes      /budget
  Ctrl+C quit       Home/End top/bottom     Alt+D dark/light   /compact
  Ctrl+G interrupt  Ctrl+F search history   Ctrl+] density     /config
```

## What it does

**Streaming chat against local or hosted models.** Live preview of
Qwen-style thinking content while the model is working, so the wait
never looks like a hang. Same code path against llama.cpp, OpenAI,
OpenRouter, or any other OpenAI-compatible endpoint.

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
resize without flicker, supports search across history (`Ctrl+F`),
and keeps every past message mutable in memory so the renderer can
re-color or annotate after the fact.

**Multi-line paste handling.** CRLF normalizes to `\n`, tabs expand to
4 spaces, orphan focus tails get stripped. When a paste exceeds the
visible input rows you get an `↑ N more lines` overflow indicator so
you know your content didn't get truncated.

## Commands

```
successor                 show help
successor chat            streaming chat with the active profile
successor setup           9-step profile creation wizard
successor config          three-pane profile config menu
successor doctor          terminal + active profile health check
successor skills          list loaded skills
successor tools           list registered tools
successor snapshot        headless render of a chat scenario
successor record          record an input session to JSONL
successor replay          replay a recorded session
successor bench           renderer benchmark, no TTY required
```

`successor doctor` is the troubleshooting command — it dumps your
terminal capabilities, lists the active profile's provider and model,
probes the configured base_url to see if it's reachable, and reports
the resolved context window. Run it first when something isn't
working.

## Tests

```bash
pytest
```

The suite is hermetic. Each test gets its own `SUCCESSOR_CONFIG_DIR`,
and bash dispatch tests use real shell builtins (no mocks). There are
881 tests at the time of writing — run them with `pytest -q` for a
clean dot view, or `pytest -xvs` to follow individual tests.

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

The renderer is five layers, only the bottom one writes to stdout:

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
the test suite validates the full visual output of the wizard, the
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

## Author

Built by **Lycaon LLC** (Colorado).

- Twitter: [@lyc_aon](https://twitter.com/lyc_aon)
- Email: michael@lycaon.wtf

If you build something cool with Successor, I'd love to hear about it.
PRs and issues welcome at [github.com/lyc-aon/successor-agent](https://github.com/lyc-aon/successor-agent).

## License

Apache 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

Short version: anyone can use, fork, modify, sell, or embed Successor
in their own products (commercial or otherwise) — they just have to
ship the LICENSE and NOTICE files alongside, and they can't strip the
attribution. See [`NOTICE`](NOTICE) for the credit line that needs to
travel with derivative work.
