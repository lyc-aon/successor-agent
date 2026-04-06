"""Ronin terminal renderer.

Five layers, top to bottom:

  Layer 5 — diff.py       — the only module that writes bytes to stdout
  Layer 4 — paint.py      — composition into a virtual cell grid
  Layer 3 — paint.py      — layout (text → grid rows, given a target width)
  Layer 2 — paint.py      — prepare (rich text → measured cells)
  Layer 1 — measure.py    — pure grapheme width inspection

Plus three support modules:

  cells.py    — Cell, Style, Grid (the data the layers operate on)
  terminal.py — TTY setup/teardown, alt-screen, SIGWINCH, raw mode, restore
  app.py      — frame loop with double-buffered diffing and input handling

The whole point of the layering is that nothing above Layer 5 ever touches
the terminal. The renderer can be unit-tested by inspecting Grid contents.
The terminal is only ever the destination for the bytes that Layer 5 emits.
"""
