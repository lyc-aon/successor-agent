#!/usr/bin/env python3
"""Scripted first-time setup walkthrough for video recording.

Drives a real `successor setup` subprocess via a pseudoterminal,
injects keystrokes at human pace, lets animations breathe between
sections, and writes a timestamped milestone log so you know
exactly where to cut your video afterward.

The walkthrough simulates a brand-new user running `successor setup`
for the first time: SUCCESSOR intro animation → 9-step wizard
(name, theme, mode, density, intro, provider, tools, review,
save) → wizard drops into the chat → empty-state hero panel
renders → /quit. The complete first-run arc.

## What gets demoed

  - The SUCCESSOR emergence animation playing on `successor setup`
  - The welcome screen with its typewriter
  - Typing a profile name at human pace
  - Cycling between themes (forge ↔ steel) with smooth blend
  - Toggling dark/light mode with smooth blend
  - Cycling layout density
  - The intro animation step (already on "successor" by default)
  - The provider picker — cycling through llama.cpp / openai /
    openrouter so the viewer sees all three options
  - The tools step (bash enabled by default)
  - The review screen with the full profile summary
  - Hitting Enter to save → wizard drops into the chat
  - The chat's empty-state hero panel (SUCCESSOR portrait + info
    panel) painted against the just-saved profile
  - Clean /quit exit

Total runtime: about 90 seconds. No model required — the chat
tail just shows the empty state, no streaming.

## Prerequisites

  1. successor installed (you should be able to run `successor` from
     anywhere — the install via `pip install -e .` registers
     ~/.local/bin/successor and the sx alias)
  2. A terminal at least 120 cols × 36 rows. The wizard sidebar +
     content + live preview need horizontal room, and the chat tail
     needs vertical room for the empty-state hero panel to fit
     comfortably. The script checks this at startup.

## What the script touches

  - Creates a TEMP config dir under /tmp/successor-walkthrough-<ts>/
    so the wizard saves its profile there, not in your real
    ~/.config/successor/. Your existing profiles stay untouched.
  - Writes a timestamped milestone log to /tmp/successor-walkthrough-
    <ts>.log with one line per section start.
  - Does NOT modify your shell, your real config, or any file
    outside /tmp.

## Usage

  python scripts/walkthrough_demo.py

The script will:

  1. Sanity-check your terminal size
  2. Print the planned timeline so you know what's coming
  3. Wait for you to press Enter — START YOUR SCREEN RECORDING NOW
  4. Spawn `successor setup` with SUCCESSOR_CONFIG_DIR pointed at
     the temp dir, and run the scripted walkthrough end-to-end
  5. Exit cleanly when the demo finishes
  6. Tell you the log file path again so you can find cut points

## Tweaking the script

The walkthrough is `run_walkthrough()` near the bottom. Each
section is a `driver.section(N, "name")` line followed by some
combination of `driver.send(...)`, `type_string(...)`, and
`driver.pump_for(seconds)` calls. The planned-timeline preview is
computed by static-analyzing that function, so if you tweak a
duration or add a section the preview updates automatically.
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
import shutil
import tempfile


# ─── Configuration ───

LOG_DIR = Path("/tmp")
MIN_COLS = 120
MIN_ROWS = 36

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


def press_up(driver: Driver) -> None:
    """Up arrow — CSI A. Decoded by KeyDecoder as Key.UP."""
    driver.send(b"\x1b[A")


def press_down(driver: Driver) -> None:
    """Down arrow — CSI B. Decoded by KeyDecoder as Key.DOWN."""
    driver.send(b"\x1b[B")


def press_right(driver: Driver) -> None:
    """Right arrow — CSI C. Decoded by KeyDecoder as Key.RIGHT.
    The wizard uses this to advance to the next step."""
    driver.send(b"\x1b[C")


def press_left(driver: Driver) -> None:
    """Left arrow — CSI D. Decoded by KeyDecoder as Key.LEFT.
    The wizard uses this to retreat to the previous step."""
    driver.send(b"\x1b[D")


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


def preflight() -> bool:
    """Run the sanity checks. Returns True iff everything looks ready."""
    cols, rows = get_terminal_size()
    print(f"terminal size: {cols} cols × {rows} rows")
    if cols < MIN_COLS or rows < MIN_ROWS:
        print(
            f"  ⚠ need at least {MIN_COLS} cols × {MIN_ROWS} rows for the"
        )
        print(
            f"    wizard sidebar + content + live preview to fit. Resize"
        )
        print(f"    your terminal first.")
        return False
    print(f"  ✓ wide enough for the wizard layout")

    # Confirm the successor binary is on PATH
    binary = shutil.which("successor")
    if binary is None:
        print()
        print("  ⚠ `successor` not found in PATH.")
        print("    Install with: pip install -e . from the repo root.")
        return False
    print(f"\n  ✓ successor binary: {binary}")
    return True


# ─── The walkthrough scripts ───


def run_walkthrough_setup(driver: Driver) -> None:
    """First-time setup walkthrough.

    Drives `successor setup` from cold-boot through the 9-step
    profile creation wizard, into the chat with the freshly-saved
    profile, pauses on the empty-state hero panel, and quits
    cleanly. Edit pump durations or add/remove sections as needed —
    the planned-timeline preview at startup auto-updates from this
    function's source.

    The wizard is driven entirely with arrow keys + Enter. The only
    text input is the profile name in the NAME step. The provider
    step cycles through all three options (llama.cpp / openai /
    openrouter) with Space so the viewer sees them all, then leaves
    llama.cpp selected to advance without needing to type fake
    api keys on camera.
    """

    # Section 1: SUCCESSOR emergence animation + welcome screen
    # cmd_setup() plays the bundled intro before the wizard opens,
    # so we let the full animation play through. The animation is
    # ~5 seconds; add a buffer for boot.
    driver.section(1, "SUCCESSOR emergence animation")
    driver.pump_for(6.0)

    # Section 2: welcome screen — typewriter intro
    # The wizard's first frame is the welcome panel with a
    # left-to-right typewriter. Let it complete fully so the viewer
    # sees the brand portrait + tagline.
    driver.section(2, "welcome screen — typewriter")
    driver.pump_for(4.0)

    # Section 3: advance to NAME step
    driver.section(3, "advance to NAME step")
    press_right(driver)
    driver.pump_for(1.5)

    # Section 4: type the profile name
    # "demo" is short, easy to read, and clearly a placeholder.
    driver.section(4, "type profile name")
    type_string(driver, "demo")
    driver.pump_for(0.8)

    # Section 5: submit name → THEME step
    driver.section(5, "submit name → THEME step")
    press_enter(driver)
    driver.pump_for(2.0)

    # Section 6: cycle to forge theme — show the live preview blend
    # The theme step has a live preview pane that smoothly blends
    # between palettes via lerp_rgb. This is one of the showpiece
    # moments — make sure the viewer has time to see it.
    driver.section(6, "theme cycle — Down to forge")
    press_down(driver)
    driver.pump_for(2.5)

    # Section 7: cycle back to steel
    driver.section(7, "theme cycle — Up to steel")
    press_up(driver)
    driver.pump_for(2.0)

    # Section 8: advance to MODE step
    driver.section(8, "advance to MODE step")
    press_right(driver)
    driver.pump_for(1.5)

    # Section 9: toggle to light mode — another smooth blend
    driver.section(9, "mode — Down to light")
    press_down(driver)
    driver.pump_for(2.5)

    # Section 10: toggle back to dark
    driver.section(10, "mode — Up to dark")
    press_up(driver)
    driver.pump_for(2.0)

    # Section 11: advance to DENSITY step
    driver.section(11, "advance to DENSITY step")
    press_right(driver)
    driver.pump_for(1.5)

    # Section 12: cycle density — compact, normal, spacious
    # Default cursor is at "normal" (index 1). Down → spacious,
    # Up Up → compact, Down → normal. Each move triggers the
    # density layout transition in the live preview.
    driver.section(12, "density — Down to spacious")
    press_down(driver)
    driver.pump_for(2.0)

    driver.section(13, "density — Up Up to compact")
    press_up(driver)
    driver.pump_for(0.5)
    press_up(driver)
    driver.pump_for(2.0)

    driver.section(14, "density — Down to normal")
    press_down(driver)
    driver.pump_for(1.5)

    # Section 15: advance to INTRO step
    driver.section(15, "advance to INTRO step")
    press_right(driver)
    driver.pump_for(2.0)

    # Section 16: INTRO is already on "successor" by default — no
    # change needed, just advance. The viewer sees the option list
    # with successor highlighted.
    driver.section(16, "INTRO step — successor preselected, advance")
    press_right(driver)
    driver.pump_for(1.5)

    # Section 17: PROVIDER step — cycle through all three options
    # so the viewer sees the matrix of choices. Space cycles
    # llamacpp → openai → openrouter → llamacpp.
    driver.section(17, "PROVIDER step — llamacpp default")
    driver.pump_for(2.5)

    driver.section(18, "PROVIDER — Space to openai")
    press_space(driver)
    driver.pump_for(2.5)

    driver.section(19, "PROVIDER — Space to openrouter")
    press_space(driver)
    driver.pump_for(2.5)

    driver.section(20, "PROVIDER — Space back to llamacpp")
    press_space(driver)
    driver.pump_for(1.5)

    # Section 21: advance to TOOLS step
    driver.section(21, "advance to TOOLS step")
    press_right(driver)
    driver.pump_for(2.0)

    # Section 22: TOOLS step — bash is enabled by default, so we
    # just let the viewer see the checkbox state and advance.
    driver.section(22, "TOOLS step — bash preselected, advance")
    press_right(driver)
    driver.pump_for(1.5)

    # Section 23: REVIEW step — show the full profile summary
    driver.section(23, "REVIEW step — summary visible")
    driver.pump_for(4.0)

    # Section 24: save the profile — Enter triggers the SAVED
    # screen which shows a "profile saved" toast and auto-advances
    # into the chat after a brief pause.
    driver.section(24, "save profile — Enter")
    press_enter(driver)
    driver.pump_for(2.5)

    # Section 25: chat opens with the new profile active
    # The chat constructor builds the empty-state hero panel
    # because the wizard-saved profile has chat_intro_art="successor"
    # by default. The panel renders immediately on the first frame.
    driver.section(25, "chat opens — empty-state hero panel")
    driver.pump_for(6.0)

    # Section 26: clean /quit exit
    # /quit doesn't go through autocomplete here because we want
    # the keystrokes to be visible — type it slowly.
    driver.section(26, "type /quit")
    type_string(driver, "/quit")
    driver.pump_for(0.4)

    driver.section(27, "submit /quit → exit")
    press_enter(driver)
    driver.pump_for(1.5)

    driver.section(28, "walkthrough complete")


def run_walkthrough_chat(driver: Driver) -> None:
    """Live chat walkthrough against the user's active profile.

    Drives `successor chat` against the active profile (whatever's
    set in the user's real config — usually llama.cpp pointing at
    a local model). Shows the empty-state hero panel, types a real
    user message, lets the model stream its reply, runs a bash
    tool through the structured card, cycles theme + mode + density
    to show smooth blends, opens the help overlay, and demonstrates
    the compaction animation by burning synthetic tokens then
    triggering /compact.

    Pump durations assume a snappy local model (~50 tok/sec
    generation, ~5-15 sec reasoning phase per query). If your
    model is slower, bump the pump durations on sections 6, 11,
    20, and 23. If faster, you can shrink them.
    """

    # Section 1: empty-state hero panel
    # The chat just opened with the user's active profile. Let
    # the SUCCESSOR portrait + info panel sit so the viewer can
    # read the panel contents (profile name, provider, model,
    # context window, tools, theme).
    driver.section(1, "empty-state hero panel")
    driver.pump_for(5.0)

    # Section 2: type a short real prompt
    # Pick something the local qwen model can answer well in a
    # short paragraph — keeps the streaming reply manageable for
    # the recording.
    driver.section(2, "first message — typing")
    type_string(
        driver,
        "Explain how a binary search tree works in two short sentences.",
    )
    driver.pump_for(0.5)

    driver.section(3, "first message — submit")
    press_enter(driver)
    driver.pump_for(0.3)

    # Section 4: streaming reply — reasoning + content
    # Qwen3.5 thinking model: ~5-15 sec reasoning, then content
    # streams at ~50 tok/sec. Pump generously so the full reply
    # lands. The viewer sees the live thinking spinner with char
    # counter, then the content typewriter.
    driver.section(4, "streaming reply — reasoning + content")
    driver.pump_for(18.0)

    # Section 5: open the slash command palette
    driver.section(5, "slash command palette — type /")
    driver.send(b"/")
    driver.pump_for(2.5)

    driver.section(6, "dismiss palette")
    press_esc(driver)
    driver.pump_for(0.4)
    # Backspace to clear the leading slash from the input
    driver.send(b"\x7f")
    driver.pump_for(0.5)

    # Section 7: bash tool execution
    # /bash ls -la /tmp is universal, predictable, and shows the
    # tool card with verb classification, parameter parsing, and
    # output streaming.
    driver.section(7, "bash tool — type /bash ls -la /tmp")
    type_string(driver, "/bash ls -la /tmp")
    driver.pump_for(0.4)

    driver.section(8, "bash tool — submit + watch the card")
    press_enter(driver)
    driver.pump_for(4.0)

    # Section 9: theme cycle — show the smooth blend
    driver.section(9, "theme cycle — Ctrl+T to forge")
    press_ctrl(driver, "t")
    driver.pump_for(2.5)

    driver.section(10, "theme cycle — Ctrl+T back to steel")
    press_ctrl(driver, "t")
    driver.pump_for(2.0)

    # Section 11: dark/light toggle
    driver.section(11, "mode toggle — Alt+D to light")
    press_alt(driver, "d")
    driver.pump_for(2.5)

    driver.section(12, "mode toggle — Alt+D back to dark")
    press_alt(driver, "d")
    driver.pump_for(2.0)

    # Section 13: density cycle
    driver.section(13, "density cycle — Ctrl+]")
    press_ctrl(driver, "]")
    driver.pump_for(1.8)

    driver.section(14, "density cycle — Ctrl+] again")
    press_ctrl(driver, "]")
    driver.pump_for(1.8)

    driver.section(15, "density cycle — Ctrl+] back to normal")
    press_ctrl(driver, "]")
    driver.pump_for(1.5)

    # Section 16: help overlay
    driver.section(16, "help overlay — press ?")
    press_question(driver)
    driver.pump_for(5.0)

    driver.section(17, "dismiss help overlay")
    press_space(driver)
    driver.pump_for(1.0)

    # Section 18: /budget — show the live token count
    driver.section(18, "/budget — type")
    type_string(driver, "/budget")
    driver.pump_for(0.3)

    driver.section(19, "/budget — submit, see token usage")
    press_enter(driver)
    driver.pump_for(3.0)

    # Section 20: /burn synthetic tokens to set up compaction
    driver.section(20, "/burn 5000 — type")
    type_string(driver, "/burn 5000")
    driver.pump_for(0.3)

    driver.section(21, "/burn 5000 — submit")
    press_enter(driver)
    driver.pump_for(1.5)

    # Section 22: trigger compaction — the harness's signature
    # visible animation. The 5-phase animation runs over ~5 sec,
    # then the model summarizes the burned content (~10-15 sec on
    # a snappy local model). Total ~20 sec.
    driver.section(22, "/compact — type")
    type_string(driver, "/compact")
    driver.pump_for(0.3)

    driver.section(23, "/compact — submit + animation + summary")
    press_enter(driver)
    driver.pump_for(22.0)

    driver.section(24, "settled boundary — pulse + summary visible")
    driver.pump_for(3.0)

    # Section 25: clean exit
    driver.section(25, "type /quit")
    type_string(driver, "/quit")
    driver.pump_for(0.4)

    driver.section(26, "submit /quit → exit")
    press_enter(driver)
    driver.pump_for(1.0)

    driver.section(27, "walkthrough complete")


def run_walkthrough_showcase(driver: Driver) -> None:
    """Model-driven feature showcase against the live qwen model.

    This mode is the one to record for a feature video. The script
    just types carefully-chosen prompts and waits — the MODEL drives
    everything: thinking phase with the live spinner, tool_call
    streaming with verb inference, animated tool cards with output
    pumping, agent loop continuation where the model talks about
    what it just ran.

    Each prompt is designed to:

      - Be safe (read-only or writes only to /tmp)
      - Naturally pull the model toward the bash tool (imperative
        phrasing like "use bash to ...")
      - Generate enough output to be visually impressive on camera
      - Demo a different bash verb so the verb classifier shows
        its full range (list-directory, read-file, search-text,
        find-files, git, write-file)

    The last prompt is the moneyshot: a heredoc-based file write
    followed by a read-back. This triggers the live verb-inference-
    from-partial-streaming-arguments magic AND the agent loop
    continuation across two tool calls.

    ## Prerequisites

      - Active profile must have bash tool enabled (the default
        and successor-dev profiles both have it on)
      - Local model must be reachable (this is a live model
        showcase — there's no graceful fallback)
      - Run from the successor repo root so the model's relative
        paths (README.md, src/successor/) resolve correctly

    Pump durations assume a snappy local model (~50 tok/sec
    generation, ~5-15s reasoning per prompt). Bump them if your
    model is slower; shrink them if it's faster.
    """

    # Section 1: empty-state hero panel — let the viewer see the
    # active profile / provider / context window before anything
    # starts happening.
    driver.section(1, "empty-state hero panel")
    driver.pump_for(5.0)

    # ─── Prompt 1: list-directory verb ───
    # `ls -la /tmp` is safe, universal, and produces enough output
    # to make the tool card look full. Asking the model to also
    # count items forces a continuation turn after the bash result
    # comes back.
    driver.section(2, "PROMPT 1: list /tmp — type")
    type_string(
        driver,
        "Use bash to list everything in /tmp with `ls -la`, then tell me in one short sentence how many things are there.",
    )
    driver.pump_for(0.5)

    driver.section(3, "PROMPT 1: submit + thinking + tool card + continuation")
    press_enter(driver)
    driver.pump_for(20.0)

    # ─── Prompt 2: read-file verb ───
    # README.md is in the repo root so the model can use a relative
    # path. The continuation summarizes the file contents — shows
    # the model actually reading the tool output.
    driver.section(4, "PROMPT 2: read README — type")
    type_string(
        driver,
        "Use bash to read this repo's README.md file, then summarize what successor is in two short sentences.",
    )
    driver.pump_for(0.5)

    driver.section(5, "PROMPT 2: submit + read-file card + summary")
    press_enter(driver)
    driver.pump_for(25.0)

    # ─── Prompt 3: search-text verb ───
    # grep -r against the source tree. BrailleArt is a clean,
    # specific symbol that returns 8-12 hits — enough to fill the
    # tool card without overflowing.
    driver.section(6, "PROMPT 3: grep BrailleArt — type")
    type_string(
        driver,
        "Use bash to grep for 'BrailleArt' in src/successor/, then tell me in one sentence what it's used for.",
    )
    driver.pump_for(0.5)

    driver.section(7, "PROMPT 3: submit + search-text card + answer")
    press_enter(driver)
    driver.pump_for(22.0)

    # ─── Prompt 4: find-files verb ───
    # `find` with structured output — different verb glyph than ls.
    # Limiting to render/ keeps the output bounded.
    driver.section(8, "PROMPT 4: find python files — type")
    type_string(
        driver,
        "Use bash to find all Python files in src/successor/render/, then tell me how many you found.",
    )
    driver.pump_for(0.5)

    driver.section(9, "PROMPT 4: submit + find-files card + count")
    press_enter(driver)
    driver.pump_for(18.0)

    # ─── Prompt 5: git verb ───
    # git log shows the per-commit verb glyph and the commit-line
    # parsing in the output pipeline.
    driver.section(10, "PROMPT 5: git log — type")
    type_string(
        driver,
        "Use bash to show the last 5 commits in this repo with `git log --oneline -5`, then tell me what the most recent change was about.",
    )
    driver.pump_for(0.5)

    driver.section(11, "PROMPT 5: submit + git card + summary")
    press_enter(driver)
    driver.pump_for(22.0)

    # ─── Prompt 6: write-file via heredoc + read-back (MONEYSHOT) ───
    # This is THE one to highlight in the video. The model emits
    # `cat > /tmp/showcase.txt << 'EOF'` and the parser infers
    # write-file from the partial command DURING streaming, before
    # the heredoc body even arrives. Then the model runs cat to
    # read it back — two tool calls in one prompt, continuation
    # turn between them.
    driver.section(12, "PROMPT 6: heredoc write + read-back — type")
    type_string(
        driver,
        "Create a file at /tmp/successor-walkthrough.txt containing exactly three lines: first 'Hello from successor', second 'Generated by the walkthrough', third 'Safe to delete'. Then read the file back to confirm what you wrote.",
    )
    driver.pump_for(0.5)

    driver.section(13, "PROMPT 6: submit + write card + read card + answer")
    press_enter(driver)
    driver.pump_for(35.0)

    # ─── Settle + clean exit ───
    driver.section(14, "settle — final card visible")
    driver.pump_for(3.0)

    driver.section(15, "type /quit")
    type_string(driver, "/quit")
    driver.pump_for(0.4)

    driver.section(16, "submit /quit → exit")
    press_enter(driver)
    driver.pump_for(1.0)

    driver.section(17, "walkthrough complete")


# ─── Timeline preview ───


def compute_planned_timeline(func) -> list[tuple[float, str]]:
    """Statically analyze a walkthrough function and return the
    planned cumulative timestamps for each section. Used to print
    a preview before the recording starts so the user knows the
    runtime AND the cut points without having to run the demo first.

    Parses the function's source line-by-line for `driver.section(...)`,
    `driver.pump_for(...)`, and `type_string(driver, "...")` calls.
    Each typed string adds TYPE_CHAR_DELAY_S per char. Each press_*
    call adds zero time (single keystroke, instant). Stays in sync
    with the actual script because it's reading the actual
    function source.
    """
    import inspect
    import re

    src = inspect.getsource(func)
    timeline: list[tuple[float, str]] = []
    t = 0.0

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
    import argparse

    parser = argparse.ArgumentParser(
        prog="walkthrough_demo",
        description=(
            "Scripted walkthrough driver for Successor. Three modes: "
            "`setup` (drives the wizard, no model required), `chat` "
            "(drives the chat UI showing slash commands and theme "
            "cycling), and `showcase` (model-driven feature demo "
            "where qwen does the cool stuff and the script just "
            "feeds carefully-chosen prompts)."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["setup", "chat", "showcase"],
        default="setup",
        help=(
            "which walkthrough to run. setup = first-time profile "
            "creation wizard arc (no model required). chat = live "
            "chat with manual UI feature drives. showcase = the one "
            "to record for a feature video — model drives every "
            "frame, script just types prompts and waits."
        ),
    )
    args = parser.parse_args()

    print("=" * 64)
    print(f"Successor walkthrough demo — mode: {args.mode}")
    print("=" * 64)
    print()
    if not preflight():
        return 1

    # Mode-specific config
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"successor-walkthrough-{args.mode}-{timestamp}.log"
    temp_config: Path | None = None
    if args.mode == "setup":
        # Temp config dir so the wizard saves its profile there, NOT
        # in the user's real ~/.config/successor/. Cleaned up at exit.
        temp_config = Path(tempfile.mkdtemp(
            prefix=f"successor-walkthrough-{timestamp}-",
            dir="/tmp",
        ))
        run_walkthrough = run_walkthrough_setup
        successor_argv = ["successor", "setup"]
    elif args.mode == "chat":
        # Chat mode: use the user's REAL active profile so the live
        # model is reachable. No temp config dir.
        run_walkthrough = run_walkthrough_chat
        successor_argv = ["successor", "chat"]
    else:
        # Showcase mode: same as chat (real config, real model) but
        # the walkthrough is purely prompts that ask the model to do
        # things. The script types and waits; the model drives.
        run_walkthrough = run_walkthrough_showcase
        successor_argv = ["successor", "chat"]

    print()

    # Preview the planned timeline so the user knows what's coming.
    # Computed by static analysis of the chosen run function so it
    # stays in sync if you edit pump_for / type_string / sections.
    print("Planned timeline:")
    timeline = compute_planned_timeline(run_walkthrough)
    for ts, label in timeline:
        print(f"  [{ts:6.1f}s]  {label}")
    total = timeline[-1][0] if timeline else 0.0
    print(f"\nTotal runtime: ~{total:.0f} seconds (~{total/60:.1f} min)")
    print()
    print(f"timestamp log: {log_path}")
    if temp_config is not None:
        print(f"temp config:   {temp_config}  (cleaned up at exit)")
    else:
        print(f"using your real ~/.config/successor/ (active profile)")
    print()
    print("When you press Enter, the script will:")
    if args.mode == "setup":
        print("  1. Spawn `successor setup` against the temp config dir")
        print("  2. Run the scripted first-time setup walkthrough")
    else:
        print("  1. Spawn `successor chat` against your active profile")
        print("  2. Run the scripted live-model walkthrough")
    print("  3. Exit cleanly")
    print()
    if args.mode == "chat":
        print("⚠ Chat mode needs a reachable model on your active")
        print("  profile. If your local llama-server isn't running,")
        print("  the streaming sections will hit the friendly error")
        print("  message instead of a real reply.")
        print()
    if args.mode == "showcase":
        print("⚠ Showcase mode needs:")
        print("  - A reachable model on your active profile")
        print("  - Bash tool enabled on the profile (default + dev")
        print("    profiles both have it on)")
        print("  - Run from the successor repo root so the model's")
        print("    relative paths (README.md, src/successor/, etc.)")
        print("    resolve correctly")
        print()
    print("START YOUR SCREEN RECORDING NOW, then press Enter to begin.")
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print("aborted")
        if temp_config is not None:
            shutil.rmtree(temp_config, ignore_errors=True)
        return 0

    # Save terminal mode so we can restore it on exit
    saved_mode = None
    try:
        saved_mode = termios.tcgetattr(0)
    except Exception:
        pass

    cols, rows = get_terminal_size()
    driver = Driver(log_path)

    pid = -1
    try:
        # Fork the child
        pid, fd = pty.fork()
        if pid == 0:
            # Child: setup mode points at the temp config dir;
            # chat mode inherits the user's environment so the
            # active profile is loaded normally.
            if temp_config is not None:
                os.environ["SUCCESSOR_CONFIG_DIR"] = str(temp_config)
            try:
                os.execvp(successor_argv[0], successor_argv)
            except FileNotFoundError:
                os.write(2, b"successor binary not found in PATH\n")
                os._exit(1)

        # Match the child's window size to ours
        set_winsize(fd, cols, rows)

        t0 = time.monotonic()
        driver.start(t0, fd, pid)
        driver.log(f"walkthrough started — mode: {args.mode}")
        driver.log(f"log: {log_path}")
        driver.log(f"terminal: {cols} cols × {rows} rows")
        if temp_config is not None:
            driver.log(f"temp config: {temp_config}")
        else:
            driver.log("using user's real config dir")

        # Run the scripted demo. Section 1 is whatever the chosen
        # walkthrough's first section is — for setup it's the
        # SUCCESSOR emergence animation, for chat it's the empty-
        # state hero panel.
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
        if pid > 0:
            try:
                os.waitpid(pid, os.WNOHANG)
            except Exception:
                pass
        # Clean up the temp config dir if we created one
        if temp_config is not None:
            shutil.rmtree(temp_config, ignore_errors=True)

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
