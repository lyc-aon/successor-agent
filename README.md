# Successor

[![tests](https://github.com/lyc-aon/successor-agent/actions/workflows/test.yml/badge.svg)](https://github.com/lyc-aon/successor-agent/actions/workflows/test.yml)
[![license](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

Successor is a terminal chat harness for local language models and
OpenAI-compatible endpoints. The renderer is a five-layer cell-based
pipeline where one module owns the screen end to end, the agent loop
drives native file tools and shell tools in real time, the setup
wizard walks first-time users through provider configuration with a
live preview pane, and the autocompactor keeps your context window healthy with
percentage-based thresholds you can tune per profile.

The operating assumption is local-model reality, not one-shot benchmark
theater. Long runs are acceptable when they stay productive, inspectable,
and evidence-backed. Successor is optimized for coherent workflow:
native file work, real runtime/browser verification, compact control
ledgers, and durable recordings that make it obvious what happened.

![Successor running multi-tool dispatch in agentic mode](https://github.com/lyc-aon/successor-agent/releases/download/v0.1.3/tool_dispatch.gif)

## Quick start

```bash
git clone https://github.com/lyc-aon/successor-agent
cd successor-agent
pip install -e .
successor setup
```

The install provides `successor` and the `sx` two-letter alias in the
current environment's `bin` directory. Python 3.11 or newer. The base
install has zero third-party runtime dependencies. The renderer, the
chat surface, the native file-tool path, the bash dispatch, the
autocompactor, and the wizard are all pure stdlib.

If you also want the optional Playwright browser tool, install the
extra instead:

```bash
pip install -e ".[browser]"
```

That only adds the Python package. You can either point Successor at an
existing browser install through the profile's `browser.channel` or
`browser.executable_path` settings, or install Playwright-managed
browsers with:

```bash
python -m playwright install chromium
```

The `vision` tool does not need an extra Python package. It needs a
multimodal model endpoint. For local `llama.cpp`, that usually means a
VL model plus an `mmproj` projector, often on a separate sidecar port:

```bash
llama-server -m /path/to/Qwen3-VL.gguf --mmproj /path/to/mmproj.gguf --port 8090
```

`successor setup` plays the SUCCESSOR emergence animation and walks
you through 10 interactive wizard steps with a live preview pane:
welcome, name, theme, dark/light, density, intro animation, provider,
tools, autocompact, review. The provider step gives you three choices out of the
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
token counts without you having to set anything manually. Local
`llama.cpp` profiles also default `provider.max_tokens` to `0`, which
Successor resolves to the detected local context window so generation
is not artificially capped unless you pin a smaller ceiling yourself.

The tools step auto-discovers the built-in native tool registry.
`read`, `write`, `edit`, and `bash` are on by default; `subagent`,
`holonet`, `browser`, and `vision` are opt-in. If you want bare chat,
uncheck everything. If you want API-backed web research, a live
browser session, or screenshot-based visual
inspection, enable them there or later in `/config`. Runtime also
auto-exposes two internal control tools when tools are enabled:
`task` for the session task ledger and `verify` for a compact
evidence-bearing verification contract. Long iterative runs now also
get `runbook`, an internal experiment contract for objective,
baseline, evaluator, and attempt decisions. For broad, stateful, or
multi-step requests, the runtime now nudges the model to adopt the
task ledger before the first substantive mutation, process-management
step, or browser loop instead of rawdogging straight into writes.

If you skip the wizard and run `successor chat` directly on a fresh install, you get the
bundled default profile pointed at `http://localhost:8080`. The default
tool surface includes native `read_file`, `write_file`, `edit_file`,
and `bash`, so the model can inspect files, make controlled edits, run
quick shell checks, and verify its work as part of any reply. If your first message
reports `[no server at http://localhost:8080]`, the local server
is not running yet. The hint message lists three concrete remediation
paths: start a local server, run `successor setup` to switch
providers, or open `/config` to edit the profile inline.

The bundled default profile does not enable `holonet`, `browser`, or
`vision` for the model automatically. Those tools are included in the
harness, but they only appear in the runtime when you opt in through
the wizard's tools step or the config menu.

## Core Workflows

These are the commands that matter most day to day:

```bash
successor setup
successor doctor
successor chat
successor record
successor playback --library --open
successor playback --open
```

- `successor setup` is the first-run path and the easiest way to turn
  on `holonet`, `browser`, `vision`, theming, and auto-record.
- `successor doctor` is the first troubleshooting command when the
  model cannot reach its provider, the browser tool is unavailable, or
  a profile's web/vision configuration looks wrong.
- `successor chat` is the normal interactive path once a profile is
  configured.
- `successor playback --library --open` opens the recordings manager
  for all accumulated local bundles.
- `successor record` and `successor playback --open` are the normal
  debugging path when you want a durable, reviewable artifact instead
  of trying to remember what happened in a long session.

## Visuals

The braille intro animation that plays before the chat opens, and
the SUCCESSOR emergence in the chat itself once the wizard saves a
profile:

| Custom braille intro | SUCCESSOR emergence (paper theme) |
|---|---|
| ![intro animation](https://github.com/lyc-aon/successor-agent/releases/download/v0.1.3/intro_braille.gif) | ![SUCCESSOR braille in the paper theme](https://github.com/lyc-aon/successor-agent/releases/download/v0.1.3/braille_red.gif) |

The 10-step setup wizard, with live theme cycling between steel and
paper, and the chat in agentic mode running multi-tool dispatch:

| Wizard theme cycling | Multi-tool dispatch |
|---|---|
| ![wizard theme step](https://github.com/lyc-aon/successor-agent/releases/download/v0.1.3/wizard_theme.gif) | ![heredoc moneyshot](https://github.com/lyc-aon/successor-agent/releases/download/v0.1.3/tool_dispatch.gif) |

Conversation search with highlighted matches, and the streaming
chat surface with the live thinking indicator + a `read-file` tool
dispatch:

| Conversation search | Streaming + tool dispatch |
|---|---|
| ![search demo](https://github.com/lyc-aon/successor-agent/releases/download/v0.1.3/search_demo.gif) | ![streaming chat](https://github.com/lyc-aon/successor-agent/releases/download/v0.1.3/chat_streaming.gif) |

The new recordings surface, with the dense local recordings manager and
the bounded terminal-artboard reviewer:

| Session Manager Reveal | Session Manager Focus |
|---|---|
| ![session manager reveal](https://github.com/lyc-aon/successor-agent/releases/download/v0.1.3/session_manager_reveal.gif) | ![session manager focus](https://github.com/lyc-aon/successor-agent/releases/download/v0.1.3/session_manager_focus.gif) |

All nine GIFs are attached to the [v0.1.3 release](https://github.com/lyc-aon/successor-agent/releases/tag/v0.1.3)
as downloadable assets.

## Inside the chat

The chat interface keeps every command discoverable from the
keyboard. Press `?` for the full help overlay (it lists every
keybinding *and* every slash command), or type `/` to open the inline
command palette. `/mouse off` leaves wheel scrolling and text
selection to the terminal. `/mouse on` gives Successor ownership of
wheel scroll plus clickable title-bar widgets; hold Shift to use
native drag selection while it is on.

```
type / to see commands         press ? for the full help overlay
type ? for help                 press Ctrl+, to open the config menu

editing            scroll                 look & feel        commands
  Enter             ↑ ↓ scroll one line     Ctrl+P profiles    /bash <cmd>
  Backspace         PgUp/PgDn page          Ctrl+T themes      /budget
  Ctrl+C quit       Home/End top/bottom     Alt+D dark/light   /compact
  Ctrl+G interrupt  Ctrl+F search history   Ctrl+] density     /config
```

Common runtime commands not shown in the compact grid:
`/recording`, `/playback`, `/fork`, `/tasks`, and `/task-cancel`.

`/fork <directive>` spawns a background subagent against the current
chat context. `/tasks` lists queued/running/completed background
tasks, and `/task-cancel <id|all>` requests cancellation. Scheduling,
queue width, timeout, and notifications live in `/config`, under the
`subagents` section.

If the current profile also has the `subagent` tool enabled in its
tool list, the model can fork background workers on its own. That path
depends on `notify_on_finish=on`, because the result comes back later
as a background-task notification. The bundled `successor-dev` profile
ships with the model-visible tool on; the plain `default` profile keeps
manual `/fork` available but leaves model delegation off by default.

Multi-step agentic runs also get an internal session-local `task`
ledger. It is not a profile toggle and is never written to disk. The
model uses it to keep one explicit `in_progress` task during longer
jobs, playback bundles show each ledger update as a normal tool card,
and the runtime can use that structured state to continue one more turn
when the model stops too early.

Profiles now also carry `max_agent_turns`, the hard cap for one user
submission's model loop. The default is `999`, and both the setup wizard
review screen and `/config` expose it so long local runs are not stuck
with the old tiny ceiling.

Raw turn count is not treated as failure by itself. For local GPU-backed
workflows, the important question is whether the model is still making
real progress toward a verified result. Successor's control plane is
meant to catch unproductive loops, not punish productive iteration.

Browser-heavy QA turns now also have a real runtime controller behind
them. When the model is in verification mode, repeated failed clicks,
same-page reopen loops, and stagnant browser state get turned into
structured continuation reminders instead of more blind retrying. Those
controller decisions are recorded in the session trace and surfaced in
the reviewer.

Verification mode now also injects tighter browser execution guidance
when the task is explicitly about visible behavior. The runtime tells
the model to keep browser work bounded, check `console_errors` after
runtime-sensitive steps, and use `screenshot` plus `vision` before
passing layout, spacing, clipping, or other visual claims. If the
profile has the built-in `browser-verifier` skill available, the system
prompt nudges the model to load it before the first browser action.

Native file-tool guards now also recover more cleanly. If `write_file`
or `edit_file` is refused because the file was never fully read, was
only partially read, changed since the last read, or the edit target is
ambiguous, Successor injects one deterministic recovery reminder on the
next turn so the model re-reads and retries the native tool instead of
falling back to bash mutation.

## Web, Browser, And Vision Tools

Successor now ships three more built-in tool families alongside `bash`
and `subagent`.

`holonet` is the API-backed web and research path. It stays in the
same zero-dependency stdlib runtime as the rest of the harness and is
the first choice when you need search or retrieval but do not need a
live page session. The current providers are:

- `brave_search`
- `brave_news`
- `firecrawl_search`
- `firecrawl_scrape`
- `europe_pmc`
- `clinicaltrials`
- `biomedical_research`

Brave and Firecrawl need API keys. Europe PMC and ClinicalTrials.gov
work keyless. The composite `biomedical_research` route fans out to the
paper and trial APIs together and merges the result into one tool call.

`browser` is the live Playwright path. It is intentionally optional and
does not vendor its own browser bundle into the base install. When the
tool is enabled, Successor uses the Playwright Python package plus the
profile's configured `channel` or `executable_path` to attach to a real
Chromium-family browser. One persistent session is kept per profile so
local app verification, login state, clicks, typing, screenshots, and
console-error checks all happen in one place. Browser `type` is now
deliberately human-like: it behaves like real keyboard input, so inline
edit bugs still surface during verification unless the model
explicitly asks to replace the existing field value first.

`vision` is the screenshot and image-inspection path. It lets a text
chat model call out to a multimodal endpoint for layout, clipping,
contrast, hierarchy, and other visibly grounded questions. It works
well with `browser screenshot`, but it can also inspect any local image
path the harness can read. The config supports two modes:

- `inherit`: reuse the active chat provider if it is multimodal
- `endpoint`: point at a dedicated multimodal endpoint, such as a local
  `llama.cpp` sidecar launched with `--mmproj`

All three tools are configured under `/config` once enabled.
Profiles created through `successor setup` live under
`~/.config/successor/profiles/<name>.json`, so any tool config you save
there is local-only and outside the git repo. `holonet` has
per-provider toggles plus inline key / key-file fields.
`browser` has `headless`, `channel`, `python_executable`,
`executable_path`, `user_data_dir`, viewport, timeout, and
`screenshot_on_error`. `vision` has `mode`, provider type, base URL,
model, optional API key / key file, timeout, max tokens, and detail
level. The full reference is in
[`docs/web-tools.md`](docs/web-tools.md).

Recommended local secret path:

- keep provider keys in `~/.config/successor/secrets/`
- point the profile at those files via `/config`
- Successor writes `chat.json` and profile JSON with user-only file
  permissions, so inline keys stay local too, but key files are the
  cleaner default
- supported env fallbacks:
  `SUCCESSOR_BRAVE_API_KEY` / `BRAVE_API_KEY`,
  `SUCCESSOR_FIRECRAWL_API_KEY` / `FIRECRAWL_API_KEY`,
  and `SUCCESSOR_VISION_API_KEY` / `OPENAI_API_KEY`

Example local secret-file setup:

```bash
mkdir -p ~/.config/successor/secrets
chmod 700 ~/.config/successor ~/.config/successor/secrets
printf '%s' "$BRAVE_API_KEY" > ~/.config/successor/secrets/brave-api-key
printf '%s' "$FIRECRAWL_API_KEY" > ~/.config/successor/secrets/firecrawl-api-key
chmod 600 ~/.config/successor/secrets/brave-api-key ~/.config/successor/secrets/firecrawl-api-key
```

Those files live outside the repo and are never tracked by git.

Successor also ships focused helper skills for these tools:

- `holonet-research`
- `biomedical-research`
- `browser-operator`
- `browser-verifier`
- `vision-inspector`

Profiles created through `successor setup` auto-seed the matching
built-in skills when you enable `holonet`, `browser`, or `vision`.
Existing profiles can edit the skill list later in `/config` under the
`extensions` section.

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
arriving. When the command is an explicit unified diff (`git diff`,
`git show`, `diff -u`) or a deterministic file mutation the parser
understands (`write-file`, `rm`, `cp`, `mv`, `mkdir`, `touch`), the
settled card renders semantic file headers and hunk lines so the user
sees `+` / `-` changes directly instead of a raw output blob.

Subagents reuse the same runtime shape through isolated headless child
chats. Manual `/fork` and the model-visible `subagent` tool both
create background tasks with transcript files, a title-bar task badge,
an inline spawn card, and a later completion notification injected back
into the parent chat.

Holonet adds deterministic API-backed web retrieval without opening a
browser. The harness resolves an explicit or inferred provider, renders
the call as a native tool card, and returns structured text for general
search, news, article scraping, biomedical papers, clinical studies, or
the combined biomedical route.

The optional Playwright browser tool handles the opposite case: work
that actually needs a real page session. A persistent browser manager
owns one session per profile and exposes navigation, clicking, typing,
waiting, text extraction, screenshots, and console-error checks through
the same native tool-call path the rest of the chat already uses.

Browser and holonet usage can now be taught on demand instead of being
hardcoded into every turn's base prompt. The system prompt gets a
compact available-skills list, and the model loads the full skill body
through the internal `skill` tool only when the task clearly matches
it. That keeps the base prompt lean while still giving the model
focused browser/research workflows when those tools are enabled.

Longer jobs no longer have to rely entirely on free-form prose memory.
The internal `task` tool maintains a compact session ledger for
pending, in-progress, and completed work. If a turn ends while a task
is still explicitly marked `in_progress`, the loop can issue one
guarded continuation reminder instead of silently dropping momentum or
spinning forever.

Background scheduling is now explicit per profile: `serial` keeps one
background model lane, `slots` uses llama.cpp's reported slot count
with one slot reserved for the parent chat, and `manual` trusts the
configured width directly. The default remains `serial`, because local
multi-slot generation can improve responsiveness and isolation while
still reducing total throughput on a saturated box.

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

Input history recall works the way it does in any shell. Up arrow on
an empty input loads the most recent submitted message. Up walks
older, Down walks newer, Down past the newest restores any draft you
were working on before you started recalling. Esc bails out of recall
mode and brings the draft back. Any editing key turns the recalled
text into a fresh draft you can edit normally.

## Commands

```
successor                 show help
successor chat            streaming chat with the active profile
successor setup           10-step profile creation wizard
successor config          three-pane profile config menu
successor doctor          terminal + active profile health check
successor skills          list loaded skills
successor tools           list native chat tools and plugin tools
successor snapshot        headless render of a chat scenario
successor record          record a session to a playback bundle
successor replay          replay a recorded input session
successor playback        reopen or open a playback bundle reviewer
successor review          alias for playback
successor bench           renderer benchmark, no TTY required
```

`successor doctor` is the troubleshooting command. It dumps your
terminal capabilities, lists the active profile's provider and model,
probes the configured `base_url` to see if it is reachable, and
reports the resolved context window. On llama.cpp it also reports the
visible slot count and whether the server advertises parallel tool-call
support. It also prints the active profile's tool surface. When
`holonet`, `browser`, or `vision` are enabled on the active profile,
doctor also reports the enabled provider set, Playwright package
readiness, the browser channel/executable path, the persistent
user-data directory, and the configured vision runtime status. It now
also reports whether local session auto-recording is on and, when
enabled, which directory receives the bundles. Run it first when
something is not working.

`successor tools` now shows both sections clearly:

- native chat tools from the built-in profile tool picker
- plugin tools from the Python-import registry under `src/successor/tools/`

The plugin section is separate from the native chat loop unless a
plugin is explicitly wired into runtime dispatch.

## File Tools

Successor's default authoring path is now native file IO:

- `read_file` for reading local text files with deterministic line numbers
- `edit_file` for exact-string changes to an existing file
- `write_file` for new files or full-file replacements
- `bash` for shell/system work like tests, builds, git, serving, or process inspection

Existing-file writes and edits require a prior full read and fail
cleanly if the file changed in the meantime. That keeps stale writes
from silently clobbering user edits or linter rewrites.

After each native `write_file` or `edit_file`, Successor also runs one
fast syntax/lint sanity check when it can do so deterministically in the
current workspace. Python files prefer `ruff check` when the repo
advertises Ruff and fall back to `py_compile`; JSON files use
`python -m json.tool`; JS files use `node --check` when Node is
available. These checks do not silently roll back the edit, but their
status is surfaced in the tool output, progress summaries, traces, and
playback so the model sees the failure immediately.

Repeated unchanged full-file reads are also compressed down to a short
stub instead of re-sending the same file content over and over, and the
runtime now warns then blocks obvious identical read loops with no
intervening non-read tool call.

When tools are enabled, the system prompt also adds a small execution-
discipline layer: if the model says it will inspect, run, edit, or
verify something, it should make the tool call in the same response,
keep going while tool work would materially improve the result, and only
finish after verification.

For longer or stateful runs, Successor also keeps a session-local
verification contract alongside the task ledger. The model can update
explicit claims, the concrete evidence that should prove them, and the
observed outcome once the evidence exists. That keeps "looks done" and
"is actually verified" separate.

For local-model workflows especially, `verify` is not about forcing the
model into ceremony. It is there to make the final-mile proof compact and
legible: what claim is being checked, what evidence should prove it, and
what was actually observed.

For stateful or realtime work like games, canvas loops, timing-sensitive
animations, or fast browser interactions, the runtime now pushes the
model one step further: set up the proof path early, name a deterministic
driver or autoplay harness when manual play is weak, and pair it with an
observable debug surface such as a HUD value, runtime log, or state
accessor. The goal is not fewer turns for their own sake. The goal is
fewer fake finishes and stronger runtime proof.

The same discipline now applies to serving local apps during those runs.
The bash guidance explicitly tells the model to pick another free high
port instead of blindly reclaiming `8080`, and the runtime hard-refuses
obvious kill/reclaim commands when they target the active local provider
endpoint. That keeps the model from shooting down its own llama.cpp
server while trying to launch a preview app.

For genuinely iterative runs, Successor can also keep a runbook: a
small session-local contract for the objective, success definition,
baseline status, active hypothesis, and stable evaluator steps. This is
paired with an append-only attempt ledger so the model can stop
retrying failed ideas blindly.

When the model needs a fresh checker instead of more implementation
context, the same `subagent` tool now supports `role="verification"`.
That launches a stricter read-only worker: no `write_file`, no
`edit_file`, no nested delegation, and non-mutating bash only. The
verification worker is meant to run the repo contract first, then prove
behavior directly with runtime evidence.

For the full contract, see [docs/file-tools.md](docs/file-tools.md).

Normal `successor chat` sessions also leave a bounded local runtime
trace under `~/.config/successor/logs/`. These JSONL files record user
submissions, model turn boundaries, tool spawns, runner completion, and
shutdown cancellation so hangs can be debugged after the chat exits.
They now also record browser-verification interventions, compact
progress summaries, and bounded subagent follow-up nudges.

## Recording Bundles

`successor record` is now the obvious debugger path, not just an
input-byte dump. With no arguments it writes a timestamped bundle under
`~/.local/share/successor/recordings/` containing:

- `input.jsonl` with the raw input stream
- `timeline.json` with captured rendered frames
- `runbook.json` when the run used a structured experiment runbook
- `experiments.jsonl` with attempt-ledger rows reconstructed from trace
- `session_trace.jsonl` and `session_trace.json` with runtime events
- `assertions.json` when the run recorded an explicit verification contract
- `playback.html`, a self-contained browser session reviewer with
  playback controls, turn cards, trace explorer, artifact links, and
  screenshot galleries when a bundle contains still images

Use it like this:

```bash
successor record
successor record ~/incoming/hang-debug
successor playback --library --open
successor playback
successor playback ~/incoming/hang-debug --open
successor review ~/incoming/hang-debug --open
```

For ordinary debugging, the shortest loop is:

```bash
successor chat
successor playback --open
```

Leave auto-record on, do the run, exit, and immediately reopen the
latest bundle in the reviewer.

Normal `successor chat` sessions also auto-record to that same local
bundle format by default. This is a user preference, not a profile
trait:

- fresh installs default to `autorecord = true`
- the setup wizard review screen shows the toggle before first save
- `/recording on|off|toggle` controls it later from the chat
- bundles stay on local disk only
- if you intentionally point a bundle inside a git repo, Successor adds
  that bundle path to the repo's local `.git/info/exclude` so it stays
  uncommitted by default

The reviewer is interactive, not a video. Open `playback.html`
directly or use `successor playback --open` / `successor review --open`
and scrub frame-by-frame with trace events, turn summaries, artifact
links, and event detail alongside it. Keyboard shortcuts are built in:
Space play/pause, Left/Right step, Home/End jump.

The current browser reviewer is shaped like a real workbench rather
than a screenshot gallery: recorded terminal frames are centered on a
bounded artboard, the event browser lives in a dedicated dock, and the
right rail keeps evidence and payload detail out of the viewport. When
a run used the verification contract, that same rail also shows the
latest proof state and links it to the recorded trace. When a run used
the experiment runbook, the same rail also shows the objective,
baseline, active hypothesis, and recent keep/discard attempt history.

There is also a recordings manager on top of the per-bundle reviewer:

- `successor playback --library --open`
- `successor review --library --open`
- `/playback recordings` from inside chat
- `/playback` from inside chat to open the current live session reviewer
  when auto-record is active, otherwise the latest finished bundle

That manager regenerates stale bundle viewers from `timeline.json`
before opening them, so older local recordings pick up the current
reviewer instead of trapping you in obsolete `playback.html` shells.

The manager itself is intentionally operational: one dense recordings
grid, one inspector, real theme parity with the harness, and no
separate "dashboard mode" to drift out of sync.

Recorded traces now make the control layer visible too: browser
verification interventions, progress-summary rows, and subagent
follow-through events all appear in the same reviewer timeline as the
rest of the runtime.

The shipped reviewer UI is now frontend-backed. The source lives in
[`reviewer-app/`](reviewer-app), and the built static assets are
vendored into `src/successor/builtin/reviewer_app/` for packaging. If
you edit the frontend, rebuild it from the repo root with:

```bash
npm --prefix reviewer-app install
npm --prefix reviewer-app run build
```

If you want the old minimal repro path, pass a `.jsonl` output or use
`--input-only`:

```bash
successor record repro.jsonl --input-only
successor replay repro.jsonl --speed 2
```

For agent handoff or postmortems, the bundle already packages the
machine-friendly pieces too:

- `summary.json` gives the top-level artifact map
- `session_trace.json` is the parsed runtime log
- `timeline.json` is the full rendered-frame sequence
- `index.md` explains the recommended read order

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
ruff check src tests
pytest -q
```

The suite is hermetic. Each test gets its own
`SUCCESSOR_CONFIG_DIR`, and bash dispatch tests use real shell
builtins (no mocks). 1234 tests at the time of writing. Run them
with `pytest -q` for a clean dot view, or `pytest -xvs` to follow
individual tests.

For harness work, treat Ruff as part of the repo contract rather than
an optional cleanup step. After touching Python files, run `ruff check`
on the files you changed or `ruff check src tests` before you report the
work complete.

## Docs

- [`docs/rendering-superpowers.md`](docs/rendering-superpowers.md):
  the design rules and what the architecture enables. Read this first.
- [`docs/rendering-plan.md`](docs/rendering-plan.md): original
  architecture notes and the reasoning behind the layer split
- [`docs/chat-render-refactor-plan.md`](docs/chat-render-refactor-plan.md):
  the behavior-preserving extraction of chat scene composition out of
  `chat.py`, plus the verification record for the refactor
- [`docs/chat-runtime-refactor-plan.md`](docs/chat-runtime-refactor-plan.md):
  the follow-on extraction of native tool/runtime orchestration out of
  `chat.py`, plus the live E2E verification record for that seam
- [`docs/concepts.md`](docs/concepts.md): features the architecture
  can support with small additive changes
- [`docs/llamacpp-protocol.md`](docs/llamacpp-protocol.md): what we
  send to and receive from llama.cpp's HTTP server
- [`docs/compaction.md`](docs/compaction.md): autocompactor reference,
  threshold configuration, and the post-compact assertion
- [`docs/file-tools.md`](docs/file-tools.md): native file-tool contract,
  guarded write flow, and how verification pairs with authoring
- [`docs/changelog.md`](docs/changelog.md): running development history
- [`reviewer-app/README.md`](reviewer-app/README.md): how the recordings
  manager and session reviewer frontend is built and packaged
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
