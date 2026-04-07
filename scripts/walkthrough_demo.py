#!/usr/bin/env python3
"""Scripted walkthrough of the Successor harness for video recording.

Drives a real `successor chat` subprocess via a pseudoterminal,
injects keystrokes at human pace, lets animations breathe between
sections, and writes a timestamped milestone log to a file so you
know exactly where to cut your video afterward.

## Prerequisites

  1. successor installed in editable mode (you should be able to run
     `successor chat` from anywhere)
  2. A working chat profile pointed at a reachable model. The script
     uses whatever profile is currently active in your config — it
     does NOT touch your settings. The fastest path is a local
     llama.cpp server on http://localhost:8080.
  3. A terminal at least 120 cols × 32 rows so the empty-state hero
     panel renders in its two-column layout. The script verifies
     this at startup.

## Usage

  python scripts/walkthrough_demo.py

The script:

  1. Sanity-checks your terminal size and the active profile's
     provider connectivity.
  2. Tells you the timestamp log file path.
  3. Waits for you to press Enter — START YOUR SCREEN RECORDING NOW.
  4. Spawns `successor chat` and runs the scripted walkthrough
     end-to-end (~80-100 seconds total runtime).
  5. Exits cleanly. Your terminal returns to normal.
  6. Tells you the log file path again so you can find your cut points.

The log file is plain text, one milestone per line, with elapsed
time in seconds since the recording started:

    [  0.00s] section 1: empty state hero panel
    [  4.50s] section 2: typing first user message
    [ 10.20s] section 3: response streaming
    ...

## Tweaking the script

The walkthrough is the `WALKTHROUGH` constant near the bottom of
this file. Each entry is a (label, action, post_delay) tuple. To
add a section, add an entry. To change pacing, change the
post_delay values. To send custom keystrokes, see the `send_*`
helpers — they handle Ctrl/Alt modifiers and named keys correctly.
"""

from __future__ import annotations

import fcntl
import os
import pty
import select
import struct
import sys
import termios
import time
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


# ─── Configuration ───

LOG_DIR = Path("/tmp")
MIN_COLS = 120
MIN_ROWS = 32
PROBE_URL = "http://localhost:8080/health"
PROBE_TIMEOUT_S = 1.0

# Typing speed for the helper that sends a string char-by-char.
# 60ms/char ≈ 16 cps which reads as a fast but human typist.
TYPE_CHAR_DELAY_S = 0.06

# Delay between an action firing and the next action being scheduled
# when no explicit post_delay is given. Generous so animations land
# fully before the next thing happens.
DEFAULT_POST_DELAY_S = 1.5


# ─── State carried through the run ───


class Driver:
    """Wraps the pty + child + timestamp log so the section helpers
    don't have to thread state through every call."""

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.log_file = log_path.open("w", buffering=1)  # line-buffered
        self.t0 = 0.0
        self.fd = -1
        self.pid = -1

    def start(self, t0: float, fd: int, pid: int) -> None:
        self.t0 = t0
        self.fd = fd
        self.pid = pid

    def log(self, label: str) -> None:
        """Write one timestamped milestone to the log file. Never
        prints to stdout/stderr — the visible terminal stays clean
        for the screen recording."""
        elapsed = time.monotonic() - self.t0
        line = f"[{elapsed:7.2f}s] {label}\n"
        self.log_file.write(line)
        self.log_file.flush()

    def section(self, n: int, name: str) -> None:
        """Mark the start of a numbered walkthrough section."""
        self.log("")
        self.log(f"━━━ section {n}: {name}")

    def pump_for(self, duration_s: float) -> None:
        """Forward child output to the user's terminal for `duration_s`
        seconds. Used between actions to let the chat catch up,
        animations finish, and the model stream its reply.
        """
        deadline = time.monotonic() + duration_s
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                return
            r, _, _ = select.select([self.fd], [], [], min(0.05, remaining))
            if self.fd in r:
                try:
                    data = os.read(self.fd, 4096)
                except OSError:
                    return
                if not data:
                    return
                os.write(1, data)

    def send(self, payload: bytes, label: str | None = None) -> None:
        """Inject keystrokes into the child. Logs a milestone if a
        label is given. Drains any pending child output first so the
        keystroke happens visually after the previous frame lands.
        """
        # Drain any output that came in while we were sleeping
        self.pump_for(0.0)
        if label is not None:
            self.log(label)
        os.write(self.fd, payload)

    def close(self) -> None:
        try:
            self.log_file.flush()
            self.log_file.close()
        except Exception:
            pass
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass


# ─── Keystroke helpers ───


def type_string(driver: Driver, text: str, char_delay: float = TYPE_CHAR_DELAY_S) -> None:
    """Type a string char-by-char at human pace, pumping output
    between every character so the typewriter effect is visible."""
    for ch in text:
        driver.send(ch.encode("utf-8"))
        driver.pump_for(char_delay)


def press_enter(driver: Driver) -> None:
    driver.send(b"\r")


def press_esc(driver: Driver) -> None:
    driver.send(b"\x1b")


def press_question(driver: Driver) -> None:
    driver.send(b"?")


def press_space(driver: Driver) -> None:
    driver.send(b" ")


def press_ctrl(driver: Driver, letter: str) -> None:
    """Send a Ctrl+letter combination. letter must be a-z."""
    code = ord(letter.lower()) - ord("a") + 1
    driver.send(bytes([code]))


def press_alt(driver: Driver, letter: str) -> None:
    """Send Alt+letter as ESC then the letter — the standard xterm
    encoding the input parser already understands."""
    driver.send(b"\x1b" + letter.encode("utf-8"))


def press_ctrl_comma(driver: Driver) -> None:
    """Ctrl+, doesn't have a single ASCII code; the chat parses it
    via the modifyOtherKeys CSI extension. We send the literal
    sequence the input decoder accepts: ESC [ 4 4 ; 5 u (CSI key
    44=',' modifier 5=Ctrl)."""
    driver.send(b"\x1b[44;5u")


# ─── Sanity checks ───


def get_terminal_size() -> tuple[int, int]:
    """Return (cols, rows) of the current TTY."""
    try:
        data = struct.unpack(
            "hhhh", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\x00" * 8)
        )
        rows, cols = data[0], data[1]
        return cols, rows
    except Exception:
        # Fallback for environments without TIOCGWINSZ
        return 80, 24


def set_winsize(fd: int, cols: int, rows: int) -> None:
    """Tell the child pty its window size so the chat lays out
    against the same dimensions the user sees."""
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("hhhh", rows, cols, 0, 0))
    except Exception:
        pass


def probe_server(url: str = PROBE_URL, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """Quick reachability probe. Returns True iff the URL responds
    with any HTTP status — we just want to know the socket opens."""
    try:
        with urlopen(url, timeout=timeout) as resp:
            return resp.status >= 200
    except (URLError, OSError):
        return False


def preflight() -> bool:
    """Run the sanity checks. Returns True iff everything looks ready."""
    cols, rows = get_terminal_size()
    print(f"terminal size: {cols} cols × {rows} rows")
    if cols < MIN_COLS or rows < MIN_ROWS:
        print(
            f"  ⚠ need at least {MIN_COLS} × {MIN_ROWS} for the empty-state",
            "hero panel to render in two-column layout. Resize first."
        )
        return False
    print(f"  ✓ wide enough for the empty-state hero panel")

    print(f"\nllama.cpp probe: {PROBE_URL}")
    if probe_server():
        print(f"  ✓ reachable")
    else:
        print(
            "  ⚠ no response. The walkthrough's `/bash`, `/budget`, and",
            "model-reply sections need a working provider on the active",
            "profile. If you're using a non-llama.cpp profile, this is",
            "fine — the probe is just a heuristic for the default setup.",
        )
        # Don't bail; the user might be on OpenAI/OpenRouter
    return True


# ─── The walkthrough script ───


def run_walkthrough(driver: Driver) -> None:
    """The actual scripted demo. Edit this to add/remove sections
    or change pacing. Each section logs its start to the milestone
    file so you can find cut points in your recording afterward."""

    # Section 1: empty-state hero panel
    # The chat just opened. Let the SUCCESSOR portrait + info panel
    # sit on screen for a moment so the viewer can read the panel
    # contents (profile name, provider, model, context window, tools).
    driver.section(1, "empty-state hero panel")
    driver.pump_for(5.0)

    # Section 2: type the first user message
    driver.section(2, "first user message — typing")
    type_string(driver, "Hello! Tell me about yourself in one short paragraph.")
    driver.pump_for(0.4)

    driver.section(3, "first user message — submit")
    press_enter(driver)
    driver.pump_for(0.3)

    # Section 4: model streams its reply
    driver.section(4, "streaming reply")
    driver.pump_for(12.0)

    # Section 5: open the slash command palette
    driver.section(5, "slash command palette")
    driver.send(b"/")
    driver.pump_for(2.5)

    driver.section(6, "dismiss the palette")
    press_esc(driver)
    driver.pump_for(0.5)
    # Clear the leading slash from the input buffer
    driver.send(b"\x7f")  # backspace
    driver.pump_for(0.5)

    # Section 7: bash tool execution
    driver.section(7, "bash tool — type the command")
    type_string(driver, "/bash ls -la /tmp")
    driver.pump_for(0.4)

    driver.section(8, "bash tool — submit and watch the card")
    press_enter(driver)
    driver.pump_for(4.0)

    # Section 9: theme cycling — show the smooth blend transition
    driver.section(9, "theme cycle — Ctrl+T")
    press_ctrl(driver, "t")
    driver.pump_for(2.5)

    driver.section(10, "theme cycle — Ctrl+T again")
    press_ctrl(driver, "t")
    driver.pump_for(2.5)

    # Section 11: dark / light toggle
    driver.section(11, "dark/light toggle — Alt+D")
    press_alt(driver, "d")
    driver.pump_for(2.5)

    driver.section(12, "dark/light toggle back — Alt+D")
    press_alt(driver, "d")
    driver.pump_for(2.0)

    # Section 13: help overlay
    driver.section(13, "help overlay — press ?")
    press_question(driver)
    driver.pump_for(5.0)

    driver.section(14, "dismiss help overlay")
    press_space(driver)
    driver.pump_for(1.0)

    # Section 15: budget command — show the live token count
    driver.section(15, "/budget — type")
    type_string(driver, "/budget")
    driver.pump_for(0.3)

    driver.section(16, "/budget — submit")
    press_enter(driver)
    driver.pump_for(2.5)

    # Section 17: burn synthetic tokens to set up compaction
    driver.section(17, "/burn 5000 — type")
    type_string(driver, "/burn 5000")
    driver.pump_for(0.3)

    driver.section(18, "/burn 5000 — submit")
    press_enter(driver)
    driver.pump_for(1.5)

    # Section 18b: trigger compaction — the harness's signature
    # visible animation. Long pump because the model has to
    # summarize the burned content.
    driver.section(19, "/compact — type")
    type_string(driver, "/compact")
    driver.pump_for(0.3)

    driver.section(20, "/compact — submit + animation + summary")
    press_enter(driver)
    driver.pump_for(20.0)  # animation + LLM summary

    driver.section(21, "settled boundary — pulse + summary visible")
    driver.pump_for(3.0)

    # Section 22: clean exit
    driver.section(22, "/quit — type and submit")
    type_string(driver, "/quit")
    press_enter(driver)
    driver.pump_for(1.0)

    driver.section(23, "walkthrough complete")


# ─── Timeline preview ───


def compute_planned_timeline() -> list[tuple[float, str]]:
    """Statically analyze run_walkthrough() and return the planned
    cumulative timestamps for each section. Used to print a preview
    before the recording starts so the user knows the runtime AND
    the cut points without having to run the demo first.

    Parses the function's source line-by-line for `driver.section(...)`,
    `driver.pump_for(...)`, and `type_string(driver, "...")` calls.
    Adds 6.5s of boot+intro head start and TYPE_CHAR_DELAY_S per
    char for typing sections. Stays in sync with the actual script
    because it's reading the actual function source.
    """
    import inspect
    import re

    src = inspect.getsource(run_walkthrough)
    timeline: list[tuple[float, str]] = []
    t = 6.5  # boot + intro animation, matches the section 0 pump
    timeline.append((t, "section 0: boot + intro animation"))

    for line in src.splitlines():
        m = re.search(r"driver\.section\((\d+),\s*\"([^\"]+)\"\)", line)
        if m:
            timeline.append((t, f"section {m.group(1)}: {m.group(2)}"))
            continue
        m = re.search(r"driver\.pump_for\(([\d.]+)\)", line)
        if m:
            t += float(m.group(1))
            continue
        m = re.search(r'type_string\(driver, "([^"]+)"\)', line)
        if m:
            t += len(m.group(1)) * TYPE_CHAR_DELAY_S
            continue
    return timeline


# ─── Entry point ───


def main() -> int:
    print("=" * 64)
    print("Successor walkthrough demo — scripted recording driver")
    print("=" * 64)
    print()
    if not preflight():
        return 1

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"successor-walkthrough-{timestamp}.log"
    print()

    # Preview the planned timeline so the user knows what's coming
    # and roughly how long the recording will be. The numbers below
    # are computed from the WALKTHROUGH script's pump_for + typing
    # delays so they stay in sync if you edit the script.
    print("Planned timeline:")
    timeline = compute_planned_timeline()
    for ts, label in timeline:
        print(f"  [{ts:6.1f}s]  {label}")
    total = timeline[-1][0] if timeline else 0.0
    print(f"\nTotal runtime: ~{total:.0f} seconds (~{total/60:.1f} min)")
    print()
    print(f"timestamp log: {log_path}")
    print()
    print("When you press Enter, the script will:")
    print("  1. Spawn `successor chat` as a child")
    print("  2. Run the scripted walkthrough above")
    print("  3. Exit cleanly")
    print()
    print("START YOUR SCREEN RECORDING NOW, then press Enter to begin.")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print("aborted")
        return 0

    # Save terminal mode so we can restore it on exit
    saved_mode = None
    try:
        saved_mode = termios.tcgetattr(0)
    except Exception:
        pass

    cols, rows = get_terminal_size()
    driver = Driver(log_path)

    try:
        # Fork into the chat
        pid, fd = pty.fork()
        if pid == 0:
            # Child: exec successor
            try:
                os.execvp("successor", ["successor", "chat"])
            except FileNotFoundError:
                os.write(2, b"successor binary not found in PATH\n")
                os._exit(1)

        # Match the child's window size to ours
        set_winsize(fd, cols, rows)

        t0 = time.monotonic()
        driver.start(t0, fd, pid)
        driver.log(f"walkthrough started — log: {log_path}")
        driver.log(f"terminal: {cols} cols × {rows} rows")

        # Let the chat fully boot (alt-screen, intro animation, etc.)
        # The intro plays for ~5s, plus boot overhead.
        driver.section(0, "boot + intro animation")
        driver.pump_for(6.5)

        # Run the scripted demo
        run_walkthrough(driver)

        # Drain any remaining output from the child
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.1)
            if fd in r:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if data:
                    os.write(1, data)

    except KeyboardInterrupt:
        driver.log("INTERRUPTED by user (Ctrl+C)")
    finally:
        driver.close()
        if saved_mode is not None:
            try:
                termios.tcsetattr(0, termios.TCSADRAIN, saved_mode)
            except Exception:
                pass
        # Reap the child if still around
        try:
            os.waitpid(pid, os.WNOHANG)
        except Exception:
            pass

    print()
    print("=" * 64)
    print(f"walkthrough complete")
    print(f"timestamp log: {log_path}")
    print("=" * 64)
    print()
    print("Cut points are in the log. Open with:")
    print(f"    cat {log_path}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
