"""Frame loop with double-buffered diffing, input, and resize handling.

The App owns two grids — front and back. Each frame:

    1. Check the resize flag. If set (or first frame), reallocate both
       grids to the current terminal size and mark first_frame.
    2. Clear the back buffer.
    3. Call self.on_tick(back) so user code paints into it.
    4. diff_frames(front, back) — emit minimal ANSI deltas (or full
       redraw on first_frame / resize).
    5. Swap front <-> back.
    6. Sleep until the next frame deadline, but wake on input.

Subclass App and override on_tick / on_key. Then call .run().

Why double-buffered: it lets the diff layer compare two equally-shaped
grids without per-frame allocation. The "previous frame" stays alive as
the front buffer for the duration of one tick, then becomes the back for
the next tick (cleared first).
"""

from __future__ import annotations

import os
import select
import sys
import time

from .cells import Grid
from .diff import diff_frames
from .terminal import Terminal


class App:
    """Frame-budgeted terminal app loop.

    target_fps:   upper bound on redraws per second
    quit_keys:    bytes whose receipt ends the loop (default q, Q, Ctrl+C)
    """

    def __init__(
        self,
        *,
        target_fps: float = 30.0,
        quit_keys: bytes = b"qQ\x03",
    ) -> None:
        self.target_fps = target_fps
        self.quit_keys = quit_keys
        self.term = Terminal()
        self._front: Grid | None = None
        self._back: Grid | None = None
        self._first_frame = True
        self._t0 = 0.0
        self._frame = 0
        self._running = False

    # ─── public state ───

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    @property
    def frame(self) -> int:
        return self._frame

    def stop(self) -> None:
        self._running = False

    # ─── overridable ───

    def on_tick(self, grid: Grid) -> None:
        """Override: paint into `grid` to produce the next frame."""
        ...

    def on_key(self, byte: int) -> None:
        """Override: react to a single input byte. Default does nothing."""
        ...

    # ─── loop ───

    def _allocate_grids(self) -> None:
        rows, cols = self.term.get_size()
        self._front = Grid(rows, cols)
        self._back = Grid(rows, cols)
        self._first_frame = True

    def run(self) -> None:
        self._t0 = time.monotonic()
        self._frame = 0
        self._running = True
        frame_dt = 1.0 / max(1.0, self.target_fps)
        with self.term:
            while self._running:
                # Resize check (also catches the initial allocation because
                # Terminal._resize_pending starts True).
                if self.term.consume_resize() or self._back is None:
                    self._allocate_grids()
                assert self._back is not None and self._front is not None

                back = self._back
                back.clear()
                try:
                    self.on_tick(back)
                except Exception:
                    self._running = False
                    raise

                prev = None if self._first_frame else self._front
                payload = diff_frames(prev, back)
                if payload:
                    self.term.write(payload)
                self._first_frame = False

                # Swap buffers.
                self._front, self._back = self._back, self._front
                self._frame += 1

                # Frame budget — sleep until the next frame, but wake on input.
                deadline = self._t0 + self._frame * frame_dt
                while self._running:
                    timeout = max(0.0, deadline - time.monotonic())
                    try:
                        rl, _, _ = select.select([sys.stdin], [], [], timeout)
                    except (OSError, ValueError):
                        rl = []
                    if not rl:
                        break
                    try:
                        data = os.read(sys.stdin.fileno(), 64)
                    except (OSError, BlockingIOError):
                        break
                    if not data:
                        break
                    for b in data:
                        if b in self.quit_keys:
                            self._running = False
                            break
                        self.on_key(b)
                    # If a SIGWINCH arrived during input handling, break out
                    # of the input drain so the next iteration can react.
                    if self.term.consume_resize():
                        # Put it back — consume_resize() can only return True
                        # once and we want the main loop to handle it next.
                        self.term._resize_pending = True  # type: ignore[attr-defined]
                        break
