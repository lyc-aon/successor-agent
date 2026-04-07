"""Bash executor — parse + classify + run, return an enriched ToolCard.

This is the public dispatch entry point used by the chat (`/bash`)
and, when the agent loop lands, by the tool dispatch path. It runs
the command synchronously via subprocess.run() and captures stdout/
stderr/exit_code/duration into a new ToolCard built from the parser's
output.

Key safety properties:

  1. Risk classification runs INDEPENDENTLY from the parser. The
     final risk is the max of (parser risk, classifier risk), so a
     pattern parser that's too lenient gets corrected.
  2. "dangerous" commands are REFUSED unless allow_dangerous=True.
     The refusal is communicated by raising DangerousCommandRefused;
     the caller decides whether to surface a confirmation modal or
     just show the toast.
  3. Output is truncated at MAX_OUTPUT_BYTES so a runaway command
     can't fill the chat with megabytes.
  4. Timeout is enforced via subprocess.run(timeout=...). On timeout,
     the partial output we have is preserved and the card is marked
     with exit_code=-1.
  5. The shell IS used (shell=True) because the whole point is to
     execute what the model wrote verbatim — including pipes,
     redirects, and substitutions. The risk classifier is the
     safety layer, NOT shell escaping.

This module never imports anything from chat.py / render/ — it must
remain testable in isolation.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import replace

from .cards import Risk, ToolCard
from .parser import parse_bash
from .risk import classify_risk, max_risk


# ─── Constants ───

DEFAULT_TIMEOUT_S: float = 30.0
MAX_OUTPUT_BYTES: int = 8192


# ─── Refusal exception ───


class DangerousCommandRefused(Exception):
    """Raised when a dangerous command is dispatched without
    allow_dangerous=True. Carries the card so the caller can show
    the user what was refused and why.
    """

    def __init__(self, card: ToolCard, reason: str) -> None:
        self.card = card
        self.reason = reason
        super().__init__(f"refused dangerous command: {reason}")


# ─── Output truncation ───


def _truncate_output(text: str, *, max_bytes: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    """Trim text to fit max_bytes, returning (text, was_truncated).

    Operates on bytes so we don't break in the middle of a multi-byte
    character. Falls back to char-trim if the encoding fails."""
    if not text:
        return ("", False)
    try:
        b = text.encode("utf-8")
    except UnicodeError:
        return (text[:max_bytes], len(text) > max_bytes)
    if len(b) <= max_bytes:
        return (text, False)
    # Find a UTF-8 boundary at or below max_bytes
    cut = max_bytes
    while cut > 0 and (b[cut] & 0xC0) == 0x80:
        cut -= 1
    return (b[:cut].decode("utf-8", errors="replace") + "\n…", True)


# ─── Public dispatch ───


def dispatch_bash(
    command: str,
    *,
    allow_dangerous: bool = False,
    timeout: float = DEFAULT_TIMEOUT_S,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> ToolCard:
    """Parse, classify, and execute a bash command. Return enriched card.

    Args:
        command: the raw bash command string. Pipes / redirects /
            substitutions all work because we run with shell=True.
        allow_dangerous: if False (default), commands classified as
            dangerous raise DangerousCommandRefused before execution.
        timeout: subprocess timeout in seconds. On timeout the
            returned card has exit_code=-1 and partial output.
        cwd: working directory. None means the current process's cwd.
        env: subprocess environment. None means inherit from parent.

    Returns:
        A new ToolCard with output, stderr, exit_code, duration_ms,
        and truncated set. The verb/params/risk fields come from the
        parser (with risk possibly escalated by the classifier).
    """
    # 1. Parse the command structurally
    parsed = parse_bash(command)

    # 2. Independent risk classification
    classifier_risk, classifier_reason = classify_risk(command)

    # 3. Take the more cautious of the two risks
    final_risk: Risk = max_risk(parsed.risk, classifier_risk)

    # Build a card with the elevated risk in case we refuse
    gated_card = replace(parsed, risk=final_risk)

    # 4. Refuse dangerous commands unless explicitly allowed
    if final_risk == "dangerous" and not allow_dangerous:
        raise DangerousCommandRefused(
            gated_card,
            classifier_reason or "command pattern flagged as dangerous",
        )

    # 5. Run the command. shell=True is intentional — we WANT pipes,
    # redirects, etc. to work. The classifier is our safety net.
    start = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout.decode("utf-8", errors="replace") if isinstance(e.stdout, bytes)
                  else (e.stdout or ""))
        stderr = (e.stderr.decode("utf-8", errors="replace") if isinstance(e.stderr, bytes)
                  else (e.stderr or ""))
        stderr += f"\n[timed out after {timeout:.1f}s]"
        exit_code = -1
    except FileNotFoundError as e:
        stdout = ""
        stderr = f"[command not found: {e}]"
        exit_code = 127

    duration_ms = (time.monotonic() - start) * 1000.0

    # 6. Truncate output so a runaway command doesn't blow up the chat
    truncated_out, was_truncated_out = _truncate_output(stdout)
    truncated_err, was_truncated_err = _truncate_output(stderr)

    return replace(
        gated_card,
        output=truncated_out,
        stderr=truncated_err,
        exit_code=exit_code,
        duration_ms=duration_ms,
        truncated=was_truncated_out or was_truncated_err,
    )


# ─── Convenience: parse-only dry-run ───


def preview_bash(command: str) -> ToolCard:
    """Run the parser + risk classifier WITHOUT executing.

    Used by the renderer to show what a command WOULD do as the user
    is typing it (or as the model is streaming it). The returned
    card has no output and exit_code=None.
    """
    parsed = parse_bash(command)
    classifier_risk, _ = classify_risk(command)
    final_risk: Risk = max_risk(parsed.risk, classifier_risk)
    return replace(parsed, risk=final_risk)
