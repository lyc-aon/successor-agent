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
import secrets
import subprocess
import time
from dataclasses import dataclass, replace
from typing import Any

from .cards import Risk, ToolCard
from .change_capture import begin_change_capture, finalize_change_capture
from .parser import parse_bash
from .risk import classify_risk, max_risk


# ─── Constants ───

DEFAULT_TIMEOUT_S: float = 30.0
MAX_OUTPUT_BYTES: int = 8192


# ─── Per-profile bash configuration ───
#
# Lives in `profile.tool_config["bash"]` as a plain dict so profiles
# stay JSON-round-trippable. `resolve_bash_config(profile)` folds the
# raw dict over the defaults below and returns a frozen dataclass
# the executor can consume directly.
#
# Three axes of control:
#
#   allow_dangerous  — OFF by default. Flip on for yolo mode. When
#                      True, the classifier's "dangerous" refusal is
#                      SKIPPED — rm -rf /, sudo, curl|sh, etc. will
#                      all run. There is no middle ground: if this
#                      is on, the user has explicitly opted in to
#                      running whatever the model emits.
#
#   allow_mutating   — ON by default. Flip off for READ-ONLY mode,
#                      which refuses anything the classifier tags
#                      as "mutating" (mkdir, touch, rm in cwd, mv,
#                      cp, git add, sed -i, package-manager installs,
#                      file redirects). Useful for letting the agent
#                      explore a repo without touching it.
#
#   timeout_s        — subprocess timeout. Default 30s.
#   max_output_bytes — output truncation limit. Default 8KB.
#
# New flags always default to the existing hard-coded behavior so
# old profiles without tool_config["bash"] keep working unchanged.


@dataclass(frozen=True, slots=True)
class BashConfig:
    """Resolved per-profile bash execution configuration.

    Built by `resolve_bash_config(profile)` — callers should not
    construct this directly from raw dict data. The frozen dataclass
    guarantees the executor sees consistent defaults regardless of
    what was in the profile JSON.
    """

    allow_dangerous: bool = False
    allow_mutating: bool = True
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_output_bytes: int = MAX_OUTPUT_BYTES
    # Working directory for every dispatched command. None means
    # "inherit from the Python process". Profile writers can set
    # this to a project directory so the model can write files
    # without prefixing every path with an absolute path, and the
    # harness doesn't dump them wherever the binary was launched.
    # Supports ~/home expansion and environment variables.
    working_directory: str | None = None


def resolve_bash_config(profile: Any) -> BashConfig:
    """Fold a profile's `tool_config["bash"]` dict over the defaults.

    `profile` is typed Any to avoid a circular import with profiles.
    A None/missing profile or a profile with no tool_config entry for
    bash returns the pure defaults. Extra keys in the dict are ignored
    so future additions stay backwards-compatible.
    """
    if profile is None:
        return BashConfig()
    tool_config = getattr(profile, "tool_config", None) or {}
    raw = tool_config.get("bash") or {}
    try:
        working_dir_raw = raw.get("working_directory")
        working_dir: str | None = None
        if working_dir_raw:
            # Expand ~ and env vars so the profile JSON stays portable
            expanded = os.path.expandvars(os.path.expanduser(str(working_dir_raw)))
            if os.path.isdir(expanded):
                working_dir = expanded
            # Silently drop invalid paths — dispatch falls back to
            # the process cwd rather than crashing
        return BashConfig(
            allow_dangerous=bool(raw.get("allow_dangerous", False)),
            allow_mutating=bool(raw.get("allow_mutating", True)),
            timeout_s=float(raw.get("timeout_s", DEFAULT_TIMEOUT_S)),
            max_output_bytes=int(raw.get("max_output_bytes", MAX_OUTPUT_BYTES)),
            working_directory=working_dir,
        )
    except (TypeError, ValueError):
        # Malformed JSON — fall back to pure defaults rather than
        # crash the chat. The config menu validates on write but a
        # hand-edited profile could still land bad types.
        return BashConfig()


# ─── Refusal exceptions ───


class RefusedCommand(Exception):
    """Base for any command refused before execution. Carries the
    pre-execution card so the UI can show what was blocked and why."""

    def __init__(self, card: ToolCard, reason: str) -> None:
        self.card = card
        self.reason = reason
        super().__init__(f"refused command: {reason}")


class DangerousCommandRefused(RefusedCommand):
    """Raised when a dangerous command is dispatched without
    allow_dangerous=True. Existing callers catch this specifically;
    new refusal types inherit from RefusedCommand so a single catch
    can handle them all if desired.
    """


class MutatingCommandRefused(RefusedCommand):
    """Raised when a mutating command is dispatched against a profile
    running in read-only mode (allow_mutating=False). Used when the
    user wants the agent to explore a repo without touching it.
    """


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


def _new_tool_call_id() -> str:
    """Generate a synthetic tool_call_id for cards that don't get one
    from the model (legacy fenced-bash detection, /bash slash command,
    direct test invocations). Format mirrors what llama.cpp's
    tool-call streaming produces — opaque alphanumeric, ~32 chars.
    """
    return "call_" + secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:24]


def dispatch_bash(
    command: str,
    *,
    allow_dangerous: bool = False,
    allow_mutating: bool = True,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    tool_call_id: str | None = None,
) -> ToolCard:
    """Parse, classify, and execute a bash command. Return enriched card.

    Args:
        command: the raw bash command string. Pipes / redirects /
            substitutions all work because we run with shell=True.
        allow_dangerous: if False (default), commands classified as
            dangerous raise DangerousCommandRefused before execution.
            Flip to True in the profile's tool_config to enable yolo
            mode (rm -rf /, sudo, curl|sh, etc. will all run).
        allow_mutating: if False, commands classified as mutating
            raise MutatingCommandRefused before execution. Default
            True. Flip to False in the profile's tool_config for a
            read-only mode.
        timeout: subprocess timeout in seconds. On timeout the
            returned card has exit_code=-1 and partial output.
        max_output_bytes: stdout+stderr truncation ceiling. Output
            beyond this is replaced with an ellipsis and the card's
            truncated flag is set.
        cwd: working directory. None means the current process's cwd.
        env: subprocess environment. None means inherit from parent.
        tool_call_id: optional id from the model's structured tool
            call. When dispatching as a result of a streamed
            `tool_calls` block, the chat passes the model's id here
            so the resulting card can be linked back to the assistant
            turn that originated it. When omitted (legacy bash-block
            detection, /bash slash command, tests), a synthetic id
            is generated so the field is always populated.

    Returns:
        A new ToolCard with output, stderr, exit_code, duration_ms,
        truncated, and tool_call_id set. The verb/params/risk fields
        come from the parser (with risk possibly escalated by the
        classifier).
    """
    # 1. Parse the command structurally
    parsed = parse_bash(command)

    # 2. Independent risk classification
    classifier_risk, classifier_reason = classify_risk(command)

    # 3. Take the more cautious of the two risks
    final_risk: Risk = max_risk(parsed.risk, classifier_risk)

    # Resolve the tool call id once so refusal cards and execution
    # cards both carry the same value.
    resolved_call_id = tool_call_id or _new_tool_call_id()

    # Build a card with the elevated risk + call id in case we refuse
    gated_card = replace(parsed, risk=final_risk, tool_call_id=resolved_call_id)

    # 4a. Refuse dangerous commands unless explicitly allowed
    if final_risk == "dangerous" and not allow_dangerous:
        raise DangerousCommandRefused(
            gated_card,
            classifier_reason or "command pattern flagged as dangerous",
        )

    # 4b. Refuse mutating commands when the profile is in read-only mode
    if final_risk == "mutating" and not allow_mutating:
        raise MutatingCommandRefused(
            gated_card,
            classifier_reason or "mutating command refused in read-only mode",
        )

    # 5. Run the command. shell=True is intentional — we WANT pipes,
    # redirects, etc. to work. The classifier is our safety net.
    change_capture = begin_change_capture(gated_card, cwd=cwd)
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
    truncated_out, was_truncated_out = _truncate_output(stdout, max_bytes=max_output_bytes)
    truncated_err, was_truncated_err = _truncate_output(stderr, max_bytes=max_output_bytes)

    final_card = replace(
        gated_card,
        output=truncated_out,
        stderr=truncated_err,
        exit_code=exit_code,
        duration_ms=duration_ms,
        truncated=was_truncated_out or was_truncated_err,
    )
    change_artifact = finalize_change_capture(change_capture)
    if change_artifact is not None:
        final_card = replace(final_card, change_artifact=change_artifact)
    return final_card


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
