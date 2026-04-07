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
import base64
import fcntl
import os
import signal
import struct
import sys
import termios
import tty

CSI = "\x1b["
OSC = "\x1b]"
BEL = "\x07"

ALT_SCREEN_ON = CSI + "?1049h"
ALT_SCREEN_OFF = CSI + "?1049l"
HIDE_CURSOR = CSI + "?25l"
SHOW_CURSOR = CSI + "?25h"
RESET_SGR = CSI + "0m"
CLEAR_HOME = CSI + "2J" + CSI + "H"

# Bracketed paste mode (DEC mode 2004) — when on, the terminal wraps
# pasted content in CSI 200 ~ ... CSI 201 ~ so the input parser can
# distinguish keystrokes from pastes. Cheap to enable, expensive to
# retrofit later when the input handler is wrong about it.
BRACKETED_PASTE_ON = CSI + "?2004h"
BRACKETED_PASTE_OFF = CSI + "?2004l"

# Mouse reporting:
#   ?1000  base X11 mouse reporting (button press + release)
#   ?1006  SGR extended mouse format (modern, supports columns > 223)
#
# Both must be enabled together for SGR mouse to work. Disabling them
# is the inverse. While these are on, the terminal forwards mouse events
# to the application instead of using its own selection layer — users
# need to hold Shift to drag-select.
MOUSE_ON = CSI + "?1000h" + CSI + "?1006h"
MOUSE_OFF = CSI + "?1006l" + CSI + "?1000l"


class Terminal:
    """A bounded TTY session.

    Args:
        raw:        put stdin in cbreak mode so single keys are readable
                    immediately without echo. Disable for non-interactive use.
        alt_screen: enter the alternate screen buffer so the user's normal
                    scrollback is preserved.
    """

    def __init__(
        self,
        *,
        raw: bool = True,
        alt_screen: bool = True,
        bracketed_paste: bool = True,
        mouse_reporting: bool = False,
    ) -> None:
        self.raw = raw
        self.alt_screen = alt_screen
        self.bracketed_paste = bracketed_paste
        self.mouse_reporting = mouse_reporting
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

    def set_mouse_reporting(self, enabled: bool) -> None:
        """Toggle SGR mouse reporting at runtime.

        Sends the enable / disable escape sequences immediately. After
        calling this with True, the input stream will start carrying SGR
        mouse events (CSI < button ; col ; row M/m) — the KeyDecoder will
        emit MouseEvent objects for them.

        While mouse reporting is on, the terminal stops handling click-
        drag selection itself. Users need to hold Shift to override and
        use native selection. This is honored by Ghostty / kitty / iTerm2 /
        alacritty / modern xterm.
        """
        if enabled and not self.mouse_reporting:
            self.write(MOUSE_ON)
            self.mouse_reporting = True
        elif not enabled and self.mouse_reporting:
            self.write(MOUSE_OFF)
            self.mouse_reporting = False

    def copy_to_clipboard(self, text: str) -> None:
        """Programmatically copy text to the system clipboard via OSC 52.

        Supported by Ghostty, kitty, iTerm2, alacritty, modern xterm.
        Inside tmux, requires `set -g set-clipboard on` (or external mode).
        Bytes per call are bounded by terminal-specific limits — most
        accept up to ~100 KB; xterm caps lower. Truncates silently if
        the terminal rejects the sequence.
        """
        if not text:
            return
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        # OSC 52 ; c (clipboard) ; <base64> BEL
        self.write(f"{OSC}52;c;{encoded}{BEL}")

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
        # Enter alt screen, hide cursor, enable bracketed paste, clear.
        out: list[str] = []
        if self.alt_screen:
            out.append(ALT_SCREEN_ON)
        out.append(HIDE_CURSOR)
        if self.bracketed_paste:
            out.append(BRACKETED_PASTE_ON)
        if self.mouse_reporting:
            out.append(MOUSE_ON)
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
        if self.mouse_reporting:
            out.append(MOUSE_OFF)
        if self.bracketed_paste:
            out.append(BRACKETED_PASTE_OFF)
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
