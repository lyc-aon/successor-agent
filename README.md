# Successor

Successor is a terminal chat harness for local language models and
OpenAI-compatible endpoints. The renderer is a five-layer cell-based
pipeline where one module owns the screen end to end, the agent loop
drives bash dispatch in real time, the setup wizard walks first-time
users through provider configuration with a live preview pane, and
the autocompactor keeps your context window healthy with
percentage-based thresholds you can tune per profile.

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
`~/.local/bin`. Python 3.11 or newer. Zero third-party runtime
dependencies. The renderer, the chat surface, the bash dispatch, the
autocompactor, and the wizard are all pure stdlib.

`successor setup` plays the SUCCESSOR emergence animation and walks
you through 10 wizard steps with a live preview pane: name, theme,
dark/light, density, intro animation, provider, tools, autocompact,
review, save. The provider step gives you three choices out of the
box:

| Provider | Auth | Notes |
|---|---|---|
| **local llama.cpp** | none | free + private, needs `llama-server` running |
| **openai** | API key | pay-per-use against your OpenAI credits |
| **openrouter** | API key | free models available, no card needed |

When you save, the wizard writes the profile to
`~/.config/successor/profiles/<name>.json` and drops straight into the
chat. The context window is auto-detected from the provider on first
use, so the autocompactor's percentage thresholds resolve to actual
token counts without you having to set anything manually.

If you skip the wizard and run `successor chat` directly, you get the
bundled default profile pointed at `http://localhost:8080`. Bash is
enabled by default, so the model can read files, run quick checks,
and verify its work as part of any reply. If your first message
reports `[no server at http://localhost:8080]`, the local server
is not running yet. The hint message lists three concrete remediation
paths: start a local server, run `successor setup` to switch
providers, or open `/config` to edit the profile inline.

## Inside the chat

The chat interface keeps every command discoverable from the
keyboard. Press `?` for the full help overlay (it lists every
keybinding *and* every slash command), or type `/` to open the inline
command palette.

```
type / to see commands         press ? for the full help overlay
type ? for help                 press Ctrl+, to open the config menu

editing            scroll                 look & feel        commands
  Enter             ↑ ↓ scroll one line     Ctrl+P profiles    /bash <cmd>
  Backspace         PgUp/PgDn page          Ctrl+T themes      /budget
  Ctrl+C quit       Home/End top/bottom     Alt+D dark/light   /compact
  Ctrl+G interrupt  Ctrl+F search history   Ctrl+] density     /config
```

`/budget` shows the live token fill, the warning / autocompact /
blocking thresholds derived from the active profile, and the round
count. `/burn N` injects synthetic context for stress-testing the
autocompactor without spending real model time. `/compact` fires the
summarizer manually if you want to reset the window before a long
turn.

## What it does

Successor streams chat against local or hosted models. Live preview
of Qwen-style thinking content while the model is working, so the
wait never looks like a hang. Same code path against llama.cpp,
OpenAI, OpenRouter, or any other OpenAI-compatible endpoint.

Bash dispatch goes through an async subprocess runner. When the model
emits a tool call, the harness spawns a background thread, streams
stdout and stderr into a live tool card with a pulsing border and an
elapsed-time counter, then feeds the result back to the model for a
continuation turn. The card's verb and parameters are inferred from
the partial command as the arguments stream in, so the header
resolves to `write-file path: about.html` while the body is still
arriving.

Compaction runs as a visible animation when the context budget
tightens. The chat stays responsive throughout. Once the summary is
ready the kept turns slide back in under a materialized boundary
divider.

The autocompactor is configurable per profile via percentage
thresholds. The defaults trip warning at 12.5% headroom, autocompact
at 6.25%, and refuse the API call at 1.5%. Hard token floors keep
tiny context windows from collapsing to a zero-token buffer. The
setup wizard exposes four presets (default, aggressive, lazy, off),
and the config menu has a full per-field editor with live preview of
where each threshold would fire on a 200K reference window. See
[`docs/compaction.md`](docs/compaction.md) for the full reference.

Profiles, themes, and a three-pane config menu let you edit the
active profile without leaving the chat. A multi-line prompt editor
with soft-wrap, shift-arrow selection, and OSC 52 clipboard support
is wired into the system prompt field so you can rewrite it inline.

The chat's scrollback is custom, not terminal-native. It survives
resize without flicker, supports search across history (`Ctrl+F`),
and keeps every past message mutable in memory so the renderer can
re-color or annotate after the fact.

Multi-line paste handling normalizes CRLF to `\n`, expands tabs to
4 spaces, and strips orphan focus tails. When a paste exceeds the
visible input rows you get an `↑ N more lines` overflow indicator so
you know your content is still there.

## Commands

```
successor                 show help
successor chat            streaming chat with the active profile
successor setup           10-step profile creation wizard
successor config          three-pane profile config menu
successor doctor          terminal + active profile health check
successor skills          list loaded skills
successor tools           list registered tools
successor snapshot        headless render of a chat scenario
successor record          record an input session to JSONL
successor replay          replay a recorded session
successor bench           renderer benchmark, no TTY required
```

`successor doctor` is the troubleshooting command. It dumps your
terminal capabilities, lists the active profile's provider and model,
probes the configured `base_url` to see if it is reachable, and
reports the resolved context window. Run it first when something is
not working.

## The architectural premise

`src/successor/render/diff.py` is the only module in the entire
codebase allowed to write to stdout. Not Rich, not prompt_toolkit,
not `print()`, not your own one-off escape sequences from somewhere
convenient. Every visible cell, every animation, every tool card,
the streaming preview, the compaction sequence, the setup wizard,
and the config menu, all of it paints into one virtual cell grid
that gets diff-committed once per frame.

This is the single decision that lets Successor:

- edit any cell of any past message in-place at frame rate
- animate compaction as the rounds dissolve into a summary boundary
- stream tool stdout into a card with a pulsing border while it runs
- search and re-style scrollback after the fact
- survive resize without flicker, and run headless without a TTY

Other harnesses cannot do most of these because once they `print()` a
line, it belongs to the terminal scrollback and they cannot reach it
anymore. Read [`docs/rendering-superpowers.md`](docs/rendering-superpowers.md)
for the full list of what the architecture buys you.

The renderer is five layers. Only the bottom one writes to stdout:

```
Layer 5 - diff.py        the ONLY module that writes to stdout
Layer 4 - paint.py       compose into a virtual cell grid
Layer 3 - paint.py       layout (text/art -> grid mutations at width W)
Layer 2 - text/braille   prepare (parse source ONCE, cache by target size)
Layer 1 - measure.py     grapheme width, ANSI strip, EAW table
```

Layers 1 through 4 are pure functions over a cell grid. Nothing
above Layer 5 ever touches the terminal. The renderer is testable by
inspecting Grid contents directly with no PTY required, which is why
the test suite validates the full visual output of the wizard, the
config menu, the compaction animation, and every tool card without
spawning a subprocess.

## Inspirations

Successor stands on a lot of other people's ideas.

**[Cheng Lou's Pretext](https://github.com/chenglou)** is the source
of the prepare-once / cache-by-target-size pattern that powers
`BrailleArt.layout()` and `PreparedText.lines()` in the renderer. Both
primitives parse their source representation exactly once and then
serve every subsequent layout request from a single-entry cache keyed
on the target size. `BrailleArt.layout()` measures 16x faster on cache
hit; `PreparedText.lines()` measures 519x faster. The pattern shows up
all over the codebase wherever expensive prepare work meets variable
target sizes.

**Hermes Agent** and the broader open-source agent harness ecosystem
shaped the agent loop's continuation pattern: stream → detect tool
call → execute → feed result back as a new turn → repeat until the
model commits a final reply. The native Qwen `tool_calls` format (vs
the legacy fenced-bash fallback) is the same shape every modern
agentic harness converged on, and it works because the underlying
chat templates were trained for it.

**The open-source AI community** is the reason any of this is even
buildable in 2026. llama.cpp provides the OpenAI-compatible HTTP
surface that local model serving was waiting for. Qwen, Llama, and
the rest of the open-weight model families make local agentic chat
useful in the first place. The dozens of contributors writing
inference servers, tokenizers, and quantization tools turned what
used to be a cloud-only toy into something a single developer can
run on their laptop.

If your project deserves credit and is missing from this list, open
an issue. I would rather over-credit than under-credit.

## Tests

```bash
pytest
```

The suite is hermetic. Each test gets its own
`SUCCESSOR_CONFIG_DIR`, and bash dispatch tests use real shell
builtins (no mocks). 974 tests at the time of writing. Run them
with `pytest -q` for a clean dot view, or `pytest -xvs` to follow
individual tests.

## Docs

- [`docs/rendering-superpowers.md`](docs/rendering-superpowers.md):
  the design rules and what the architecture enables. Read this first.
- [`docs/rendering-plan.md`](docs/rendering-plan.md): original
  architecture notes and the reasoning behind the layer split
- [`docs/concepts.md`](docs/concepts.md): features the architecture
  can support with small additive changes
- [`docs/llamacpp-protocol.md`](docs/llamacpp-protocol.md): what we
  send to and receive from llama.cpp's HTTP server
- [`docs/compaction.md`](docs/compaction.md): autocompactor reference,
  threshold configuration, and the post-compact assertion
- [`docs/changelog.md`](docs/changelog.md): running development history
- [`CLAUDE.md`](CLAUDE.md): repo orientation auto-loaded by Claude
  Code sessions working in this directory

## Author

Built by **Lycaon LLC** (Colorado).

- Twitter: [@lyc_aon](https://twitter.com/lyc_aon)
- Email: michael@lycaon.wtf

If you build something cool with Successor, I would love to hear
about it. PRs and issues welcome at
[github.com/lyc-aon/successor-agent](https://github.com/lyc-aon/successor-agent).

## License

Apache 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

Anyone can use, fork, modify, sell, or embed Successor in their own
products (commercial or otherwise). They just have to ship the
LICENSE and NOTICE files alongside, and they cannot strip the
attribution. See [`NOTICE`](NOTICE) for the credit line that needs
to travel with derivative work.
