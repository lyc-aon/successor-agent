"""ToolCard — the structured representation of a parsed bash command.

A ToolCard is what the renderer paints in place of (or alongside) a raw
bash command line. It's the *cosmetic* layer over rawdog bash: the
model emits `cat README.md`, the parser produces a card with
`verb="read-file"`, `params=(("path", "README.md"),)`, and the
renderer paints a clean structured action card.

The card carries enough metadata for:
  - the renderer to draw a verb header + key/value param table
  - the executor to attach output and an exit code after the command runs
  - the risk gate to refuse execution at "dangerous" risk levels
  - the user to verify the parser's interpretation by reading the
    raw_command field, which always preserves what the model actually
    typed

Cards are immutable. The executor builds a NEW card with output filled
in via dataclasses.replace(); it never mutates an existing card.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .diff_artifact import ChangeArtifact

# ─── Risk classes ───
#
# Three levels deliberately kept few — more granularity invites bikeshedding.
#
#   safe       read-only operations on the user's own files (ls, cat, grep,
#              find, pwd, echo, git status, python -c). Auto-allowed.
#   mutating   writes to the user's own filesystem (touch, mkdir, rm in
#              cwd, mv, cp, git commit, sed -i). Allowed unless the user
#              has flipped the gate off.
#   dangerous  network downloads, sudo, recursive deletes outside cwd,
#              chmod 777, curl|sh, eval, system-path mutation. Refused
#              by default; the user must explicitly opt in per-command
#              via a confirmation modal (modal pattern not in v0).

Risk = Literal["safe", "mutating", "dangerous"]


# ─── ToolCard ───


@dataclass(frozen=True, slots=True)
class ToolCard:
    """Structured representation of a parsed bash command.

    Built by the parser registry from a raw command string. The
    executor enriches it post-run with output, exit_code, and
    duration_ms.

    Fields:
        verb            short hyphenated action name ("read-file",
                        "list-directory", "git-status"). Renders as
                        the card's headline.
        params          ordered (key, value) tuples — what the parser
                        extracted from the command's flags and args.
                        Renders as a key/value table inside the card.
        risk            "safe" | "mutating" | "dangerous". Drives both
                        execution gating and the card's border color.
        raw_command     the original bash command verbatim. Always
                        present so the user can spot parser misses.
        confidence      0.0-1.0. The parser's self-assessment of how
                        well it matched the command. Generic fallbacks
                        return ~0.3; pattern parsers return 0.9+.
                        Renders as a "?" badge if below 0.7.
        parser_name     which @bash_parser produced this card. Used in
                        diagnostics and tests.
        output          stdout from execution, "" if not yet executed.
                        Truncated by the executor to avoid runaway
                        cards (8KB default).
        stderr          stderr from execution, "" if none.
        exit_code       process exit code, None if not yet executed.
        duration_ms     wall-clock execution time, None if not yet
                        executed.
        truncated       True if the executor clipped output for size.
        change_artifact optional user-facing structured diff / change
                        summary shown in the rendered card. This is
                        NOT substituted into the model-facing tool
                        result message; stdout/stderr remain the source
                        of truth for API history.
    """

    verb: str
    params: tuple[tuple[str, str], ...] = ()
    risk: Risk = "safe"
    raw_command: str = ""
    confidence: float = 1.0
    parser_name: str = ""
    output: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_ms: float | None = None
    truncated: bool = False
    change_artifact: ChangeArtifact | None = None
    # Tool-call linkage for native Qwen tool calling. When the model
    # emits a structured tool_call, the harness propagates the model's
    # call id here so the corresponding `role: "tool"` message can
    # link back via `tool_call_id`. For cards created from legacy
    # paths (the /bash slash command, the bash-block detector
    # fallback, tests), the executor synthesizes a uuid so the
    # field is always populated and api_messages stays consistent.
    tool_call_id: str = ""

    @property
    def executed(self) -> bool:
        """True once the executor has filled in exit_code."""
        return self.exit_code is not None

    @property
    def succeeded(self) -> bool:
        """True if executed AND exit_code == 0."""
        return self.exit_code == 0
