# Ronin

An omni-agent harness for locally-run mid-grade models, focused on local
tools, configurability, and a terminal renderer that doesn't fight you.

## Status

Phase 0 — terminal renderer prototype only. The agent loop, tool system,
and configuration are intentionally not built yet. We are validating the
rendering foundation first because rendering bugs are the single most
expensive class of issue to retrofit.

## Layout

```
ronin/
├── docs/
│   └── rendering-plan.md       # The five-layer architecture
├── assets/
│   └── nusamurai/pos-th30/     # Braille keyframes for the demo
├── src/ronin/
│   └── render/
│       ├── measure.py          # Layer 1 — grapheme width
│       ├── cells.py            # Cell, Style, Grid
│       ├── paint.py            # Layers 2-4 — paint into grid
│       ├── diff.py             # Layer 5 — minimal ANSI commit
│       ├── terminal.py         # Term setup/teardown, signals
│       ├── app.py              # Frame loop with input + resize
│       └── braille.py          # Braille codec + Bayer interp
└── examples/
    └── demo_ronin.py           # Full-screen braille animation
```

## Install

The project is named **Ronin** but the installed binary is **`rn`**
(the `ronin` name is heavily contested in the open-source ecosystem
— Node CLI framework, npm `@ronin/cli`, ronin-rb, axieinfinity
blockchain, etc).

```
pip install -e .
```

This registers `rn` in `~/.local/bin` so it's available from anywhere.

## Use

```
rn               show help
rn -V            version
rn demo          braille animation
rn show <name>   render a single static braille frame
rn frames        list available frames with dimensions
rn doctor        terminal capabilities + renderer info
rn bench         renderer benchmark (no TTY required)
```

Press **q** or **Ctrl+C** to exit any TUI command. Drag the terminal
corner aggressively to test resize handling.

## Why a custom renderer

See `docs/rendering-plan.md` for the design and the cost/benefit of
*not* using Rich + prompt_toolkit + patch_stdout.
