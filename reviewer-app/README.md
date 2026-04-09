# Successor Reviewer App

This is the frontend source for Successor's recordings manager and
session reviewer.

It is a small React + Vite app that renders two payload shapes owned by
the Python runtime:

- `kind: "library"` for the recordings manager
- `kind: "session"` for a single bundle reviewer

The Python side keeps ownership of:

- bundle discovery
- trace / timeline parsing
- theme export from the real Successor theme registry
- generated `playback.html` and `recordings.html` files

The frontend side owns:

- layout
- interaction patterns
- paper/steel theme switching with light/dark modes
- bundle and library presentation

## Build

From the repo root:

```bash
npm --prefix reviewer-app install
npm --prefix reviewer-app run build
```

The build output is written directly into:

- `src/successor/builtin/reviewer_app/`

Those files are vendored into the Python package and inlined by
`src/successor/reviewer.py`.

## Design direction

This app is intentionally split into two UX families:

- `library` uses a dense operational recordings grid plus inspector
- `session` uses a monochrome review workbench with a real terminal artboard

The goal is to stop treating recordings as "dashboard cards with a scrubber"
and instead make them feel like:

- a dense run inventory when browsing many sessions
- a purpose-built workbench when reviewing one session in depth

## Fidelity checklist

Use this as a hard cross-check before calling the design direction complete.

### Airtable library base

- The primary browsing surface is a grid/table, not a card board
- Rows should be scannable and dense before they are decorative
- The selected run should reveal detail in an inspector, not another dashboard
- Controls should feel operational and precise, not playful

### Session workbench

- Chrome should stay fundamentally black/white with minimal accent bleed
- The main stage should read like an artboard, not a centered card
- Playback should render direct terminal frames, not a repaint-heavy emulator
- The trace browser should live in a dedicated dock, not an endless side column
- The layout should feel like a tool workspace with rails, inspectors, selection, and a bounded viewport

### Global checks

- Must work from generated local HTML opened directly in a browser
- Must preserve the existing bundle data contract
- Must use the real Successor paper/steel catalog for light/dark + theme names
- Must look good in screenshots and promo footage at `1920x1080`
- If the result still reads as a generic three-panel dashboard, reset and try again
