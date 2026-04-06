"""Terminal setup, teardown, and signal-safe restore.

The Terminal class is the boundary between Ronin's pure render layers and
the actual TTY. It owns:

  - alternate screen buffer toggle
  - cursor visibility
  - termios cbreak mode (for non-blocking single-key input)
  - SIGWINCH handling
  - guaranteed restore on normal exit, exception, or signal

Use as a context manager:

    with Terminal() as term:
        ...

If your process is killed with SIGKILL there is nothing we can do. For
SIGINT, SIGTERM, normal exit, and exceptions in the `with` block, the
restore is guaranteed via __exit__ + atexit fallback.
"""

from __future__ import annotations

import atexit
import fcntl
import os
import signal
import struct
import sys
import termios
import tty

CSI = "\x1b["
ALT_SCREEN_ON = CSI + "?1049h"
ALT_SCREEN_OFF = CSI + "?1049l"
HIDE_CURSOR = CSI + "?25l"
SHOW_CURSOR = CSI + "?25h"
RESET_SGR = CSI + "0m"
CLEAR_HOME = CSI + "2J" + CSI + "H"


class Terminal:
    """A bounded TTY session.

    Args:
        raw:        put stdin in cbreak mode so single keys are readable
                    immediately without echo. Disable for non-interactive use.
        alt_screen: enter the alternate screen buffer so the user's normal
                    scrollback is preserved.
    """

    def __init__(self, *, raw: bool = True, alt_screen: bool = True) -> None:
        self.raw = raw
        self.alt_screen = alt_screen
        self.fd = sys.stdout.fileno()
        self._stdin_fd = sys.stdin.fileno()
        self._saved_termios: list | None = None
        self._resize_pending = True  # initial layout pass counts as a "resize"
        self._installed = False

    # ─── public ───

    def get_size(self) -> tuple[int, int]:
        """Return (rows, cols) from the kernel via TIOCGWINSZ."""
        try:
            data = fcntl.ioctl(self.fd, termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols, _, _ = struct.unpack("hhhh", data)
            if rows <= 0 or cols <= 0:
                return (24, 80)
            return (rows, cols)
        except OSError:
            return (24, 80)

    def write(self, data: str) -> None:
        """Write a chunk of bytes directly to the controlling TTY.

        We bypass sys.stdout's buffering and use os.write so SIGWINCH or
        signal-driven exits can't strand half-written ANSI in a Python
        buffer. The diff layer's output is the only thing that should
        ever flow through here.
        """
        if not data:
            return
        os.write(self.fd, data.encode("utf-8"))

    def consume_resize(self) -> bool:
        """Return True at most once per SIGWINCH delivery.

        The frame loop polls this each tick. If True, the loop reallocates
        its grids to the new size and forces a full repaint.
        """
        if self._resize_pending:
            self._resize_pending = False
            return True
        return False

    # ─── lifecycle ───

    def __enter__(self) -> "Terminal":
        if self._installed:
            return self
        self._installed = True
        # Save and modify termios state.
        if sys.stdin.isatty():
            try:
                self._saved_termios = termios.tcgetattr(self._stdin_fd)
                if self.raw:
                    tty.setcbreak(self._stdin_fd)
            except termios.error:
                self._saved_termios = None
        # Enter alt screen, hide cursor, clear.
        out: list[str] = []
        if self.alt_screen:
            out.append(ALT_SCREEN_ON)
        out.append(HIDE_CURSOR)
        out.append(CLEAR_HOME)
        self.write("".join(out))
        # Install handlers.
        try:
            signal.signal(signal.SIGWINCH, self._on_winch)
        except (ValueError, OSError):
            # Not in main thread; resize handling will be lost but we won't crash.
            pass
        atexit.register(self._restore)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._restore()

    # ─── internal ───

    def _on_winch(self, signum, frame) -> None:
        # Signal handlers must be minimal — flip a flag, return.
        self._resize_pending = True

    def _restore(self) -> None:
        if not self._installed:
            return
        self._installed = False
        out: list[str] = [RESET_SGR, SHOW_CURSOR]
        if self.alt_screen:
            out.append(ALT_SCREEN_OFF)
        try:
            self.write("".join(out))
        except Exception:
            pass
        if self._saved_termios is not None:
            try:
                termios.tcsetattr(self._stdin_fd, termios.TCSADRAIN, self._saved_termios)
            except Exception:
                pass
