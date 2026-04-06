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

## Run the demo

```
python3 examples/demo_ronin.py
```

Press **q** or **Ctrl+C** to exit. Drag the terminal corner aggressively
to test resize handling.

## Why a custom renderer

See `docs/rendering-plan.md` for the design and the cost/benefit of
*not* using Rich + prompt_toolkit + patch_stdout.
