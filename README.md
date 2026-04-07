# Successor Agent

An omni-agent harness for locally-run mid-grade models, focused on local
tools, configurability, and a terminal renderer that doesn't fight you.

## Status

Phase 0 + framework infra (loader pattern, themes, profiles, providers,
skills/tools scaffolding, setup wizard, three-pane config menu, multi-line
prompt editor with soft wrap + selection + OSC 52 clipboard, bundled
emergence intro) + Phase 5.0 bash-masking subsystem (parser + risk
classifier + executor + structured tool card renderer + `/bash` slash
command) + Phase 5.1 agent loop + compaction subsystem (tick-driven
QueryLoop state machine, token counter via llama.cpp `/tokenize`,
context budget tracker + circuit breaker + recompact chain detection,
microcompact + autocompact via LLM summarization with PTL retry,
streaming bash block detector, `/budget` `/burn` `/compact` slash
commands, burn-tested live against A3B at 50K context with 96.9%
reduction and 100% semantic recall). 602 tests passing, all hermetic.

## Layout

```
src/successor/
  render/                # Five-layer renderer
    measure.py           # Layer 1 — grapheme width, ANSI strip
    cells.py             # Cell, Style, Grid
    paint.py             # Layers 2-4 — paint into grid
    diff.py              # Layer 5 — minimal ANSI commit (ONLY stdout writer)
    terminal.py          # Term setup/teardown, signals, OSC 52
    app.py               # Frame loop with input + resize
    braille.py           # Braille codec + Bayer interp + BrailleArt
    text.py              # PreparedText, hard_wrap, lerp, easing
    theme.py             # Theme bundle, ThemeVariant, blend, oklch parser
  loader.py              # Generic Registry[T] for themes/profiles/skills
  config.py              # ~/.config/successor/chat.json + v1→v2 migration
  chat.py                # SuccessorChat — chat interface (real llama.cpp)
  bash/                  # Bash-masking: parse model bash → structured cards
    cards.py             # ToolCard frozen dataclass
    parser.py            # @bash_parser registry + parse_bash + clip_at_operators
    risk.py              # Independent risk classifier (safe/mutating/dangerous)
    exec.py              # dispatch_bash — parse + classify + run + truncate
    render.py            # paint_tool_card pure paint function
    patterns/            # 12 pattern files (ls, cat, grep, find, git, ...)
  agent/                 # Agent loop + compaction subsystem
    log.py               # ApiRound + MessageLog + BoundaryMarker + AttachmentRegistry
    events.py            # ChatEvent ADT (StreamStarted, Compacted, ToolCompleted, ...)
    tokens.py            # TokenCounter — /tokenize endpoint + char heuristic + LRU
    budget.py            # ContextBudget + CircuitBreaker + RecompactChain + BudgetTracker
    microcompact.py      # time-based stale tool result clearing
    compact.py           # autocompact via llama.cpp summarization + PTL retry
    bash_stream.py       # BashStreamDetector — fenced ```bash detection across stream chunks
    loop.py              # QueryLoop — tick-driven state machine, the agent loop
scripts/                 # Manual swap scripts (qwopus ↔ A3B for compaction stress test)
  intros/                # Intro animations played before chat opens
    successor.py         # 11-frame braille emergence with held title portrait
  profiles/              # Profile dataclass + JSON loader + active resolver
  providers/             # ChatProvider Protocol + factory + llama/openai_compat
  skills/                # Skill markdown frontmatter parser + registry (loader-only)
  tools/                 # @tool decorator + ToolRegistry (loader-only, gated)
  wizard/                # Setup wizard + config menu + prompt editor
  builtin/               # Package-shipped data
    themes/steel.json
    profiles/{default,successor-dev}.json
    skills/successor-rendering-pattern.md
    tools/read_file.py
    intros/successor/{00..10}-*.txt
docs/                    # rendering-superpowers, concepts, plan, llamacpp, changelog
tests/                   # 602 tests, hermetic via SUCCESSOR_CONFIG_DIR
```

## Install

```
pip install -e .
```

This registers two binaries in `~/.local/bin`:
- `successor` — canonical command, full word for brand reinforcement
- `sx` — 2-letter alias for daily ergonomics

Both point at the same entry.

## Use

```
successor              show help
successor -V           version
successor chat         chat interface (real llama.cpp streaming)
successor setup        profile creation wizard with live preview
successor config       three-pane profile config menu
successor doctor       terminal capabilities + measure samples
successor skills       list loaded skills
successor tools        list registered tools
successor snapshot     headless render of a chat scenario
successor record       record an input session to JSONL
successor replay       replay a recorded session
successor bench        renderer benchmark (no TTY required)
```

Inside `successor chat`:
- `Ctrl+C` or `/quit` — exit
- `Ctrl+,` or `/config` — open the three-pane config menu
- `Ctrl+P` — cycle profile · `Ctrl+T` cycle theme · `Alt+D` toggle dark/light · `Ctrl+]` cycle density
- `/bash <command>` — run a bash command and render it as a structured tool card
  (parse + risk classify + execute + paint, dangerous commands refused with explanation)
- `/budget` — show current context fill % + token usage stats
- `/burn N` — inject N synthetic tokens (stress-test the compaction pipeline)
- `/compact` — manually fire compaction against the current chat history

## Why a custom renderer

The renderer is the foundation — everything else hangs off it. Read
these in order:

- **[`docs/rendering-superpowers.md`](docs/rendering-superpowers.md)** —
  what the architecture buys us, the One Rule (only `diff.py` writes
  to stdout), the anti-patterns to avoid, and how to extend the
  renderer without breaking it. **Read this first**.
- [`docs/rendering-plan.md`](docs/rendering-plan.md) — the original
  five-layer architecture decisions and the cost/benefit of *not*
  using Rich + prompt_toolkit + patch_stdout.
- [`docs/concepts.md`](docs/concepts.md) — features enabled by the
  rendering architecture, organized by capability category, with
  rough effort estimates.
- [`docs/changelog.md`](docs/changelog.md) — per-phase notes for the
  framework infra (loader, themes, profiles, providers, wizard,
  config menu, prompt editor, intros).
- [`CLAUDE.md`](CLAUDE.md) — repo-level notes auto-loaded by Claude
  Code sessions in this directory.
