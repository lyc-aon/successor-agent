"""Bash parser registry — map raw bash commands to structured ToolCards.

The contract is intentionally narrow: a parser is a pure function

    (args: list[str], *, raw_command: str) -> ToolCard

registered against one or more command names via @bash_parser. The
parse() entry point shlex-splits the command, looks up the first
token in the registry, and dispatches. If no parser matches, a
generic fallback card is returned with confidence ~0.3 so the
renderer can mark it as "we don't know what this does."

A parser MUST NOT execute the command, touch the filesystem, or
import anything heavy. Parsers run on every keystroke during the
streaming pass, so they must be cheap.

Risk classification is the responsibility of `risk.py`, NOT the
parser. The parser declares its OWN best guess at risk in the
returned card, but the dispatch layer will overwrite it with the
risk classifier's verdict if the classifier finds something the
parser missed (e.g., a redirect to /etc/passwd that the parser
ignored).
"""

from __future__ import annotations

import re
import shlex
from typing import Callable

from .cards import ToolCard


# ─── Heredoc body stripping ───
#
# Before shlex.split runs we remove any heredoc bodies from the command
# string. The body can contain arbitrary characters (HTML apostrophes,
# unclosed quotes in shell scripts, backticks, whatever the model is
# writing to disk) which shlex's posix-quote tokenizer cannot handle —
# it raises ValueError on the first unbalanced quote and the whole
# command falls through to the generic "bash ?" fallback. The opener
# line alone is enough to classify the command (`cat > target <<'EOF'`
# has every signal we need: the `>` redirect, the target, the heredoc
# marker), so stripping the body lets the cat/tee/etc. parsers do
# their job regardless of what's inside.
#
# Supported opener forms:
#   cmd << EOF         (unquoted delimiter)
#   cmd << 'EOF'       (single-quoted — literal body)
#   cmd << "EOF"       (double-quoted — variable expansion in body)
#   cmd <<- EOF        (dash for tab stripping)
#   cmd <<-'EOF'       (dash + quoted delim)
#
# The closer is the delimiter on its own line, optionally with leading
# whitespace (for the `<<-` form). If no closer is found — the heredoc
# is still streaming in — we truncate at the opener line, leaving a
# well-formed short command for shlex.

_HEREDOC_OPENER_RE = re.compile(
    r"""
    <<                  # heredoc operator
    -?                  # optional dash (tab stripping)
    \s*                 # optional whitespace
    (['"]?)             # optional opening quote (group 1)
    (\w+)               # delimiter word (group 2)
    \1                  # matching closing quote (backref to group 1)
    [^\n]*              # rest of the opener line (trailing command text)
    \n                  # newline ends the opener
    """,
    re.VERBOSE,
)


def _strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc bodies from a multi-line command so shlex can
    tokenize the rest without tripping on the body's quoting.

    Returns a single-line (or near-single-line) command string that
    shlex.split can consume cleanly. Idempotent: commands without
    heredocs pass through unchanged.
    """
    out = command
    max_iterations = 16  # safety bail against pathological nesting
    for _ in range(max_iterations):
        m = _HEREDOC_OPENER_RE.search(out)
        if m is None:
            break
        delim = m.group(2)
        # Look for the closer — the delimiter on its own line. Allow
        # leading whitespace (for <<- heredocs) and trailing whitespace.
        close_re = re.compile(
            r"\n\s*" + re.escape(delim) + r"\s*(?:\n|$)",
        )
        close_m = close_re.search(out, pos=m.end())
        if close_m is None:
            # Open-ended heredoc — the body is still streaming in.
            # Truncate at the end of the opener line so shlex has
            # a complete command to work with.
            out = out[: m.end()]
            break
        # Drop the body (everything between the opener's newline and
        # the closer's end). Leave the opener line intact so the
        # cat/tee/etc. parsers can still see the `>` redirect.
        out = out[: m.end()] + out[close_m.end() :]
    return out

# A parser takes the post-command-name args and the original raw
# command string. The raw_command is passed in as a kwarg so parsers
# can preserve it on the returned card without re-joining.
BashParser = Callable[..., ToolCard]


# ─── Registry ───
#
# Module-level dict mapping command name -> parser function. The
# decorator populates it at import time. Pattern files in
# `bash/patterns/` are imported by `bash/__init__.py` so their
# decorators run.

_PARSERS: dict[str, BashParser] = {}


def bash_parser(*command_names: str) -> Callable[[BashParser], BashParser]:
    """Register a parser for one or more command names.

    Multi-name registration is for command families that share a
    parser (head/tail, cp/mv, etc.). Re-registration is allowed but
    emits no warning — the user (in `~/.config/successor/bash/`,
    eventually) wins on collision via load order.

    Usage:
        @bash_parser("ls")
        def parse_ls(args, *, raw_command):
            ...
            return ToolCard(verb="list-directory", ...)
    """
    def decorator(fn: BashParser) -> BashParser:
        for name in command_names:
            _PARSERS[name] = fn
        return fn
    return decorator


def registered_commands() -> list[str]:
    """All command names with a registered parser. Used by tests and
    the `successor doctor` introspection command."""
    return sorted(_PARSERS.keys())


def has_parser(command_name: str) -> bool:
    """Cheap check used by the renderer to mark unknown-command cards."""
    return command_name in _PARSERS


def get_parser(command_name: str) -> BashParser | None:
    """Look up a parser by command name. Returns None if unregistered."""
    return _PARSERS.get(command_name)


# ─── Shell operator detection ───
#
# shlex tokenizes by quoting rules but knows nothing about shell
# grammar. A command like `ls foo | grep bar` becomes
# ['ls', 'foo', '|', 'grep', 'bar'] — the parser must stop at the
# pipe or it'll absorb every following token as a "path".
#
# Pattern parsers all call `clip_at_operators(args)` to get a clean
# arg list scoped to just their own command segment.

_SHELL_OPERATORS = frozenset({
    "|", "||", "&", "&&", ";", ";;",
    ">", ">>", "<", "<<", "<<<",
    "2>", "2>>", "&>", "&>>", "|&",
})


def clip_at_operators(args: list[str]) -> list[str]:
    """Truncate args at the first shell operator token.

    `shlex.split('ls foo | grep bar', posix=True)` produces
    `['ls', 'foo', '|', 'grep', 'bar']`. Pattern parsers should call
    `clip_at_operators(args)` so their argument list is scoped to
    `['foo']` instead of bleeding into `['foo', '|', 'grep', 'bar']`.

    Returns a new list — the original is not mutated.
    """
    out: list[str] = []
    for arg in args:
        if arg in _SHELL_OPERATORS:
            break
        # Also catch redirect-with-target-glued tokens like ">/dev/null"
        if arg.startswith((">", "<")) and len(arg) > 1:
            break
        if arg.startswith("2>") or arg.startswith("&>"):
            break
        out.append(arg)
    return out


# ─── Public entry point ───


def parse_bash(command: str) -> ToolCard:
    """Parse a bash command into a ToolCard.

    Strategy:
      1. Strip heredoc bodies — their contents are arbitrary (HTML
         apostrophes, shell scripts with unclosed quotes, etc.) and
         would otherwise crash shlex's quote-aware tokenizer. The
         opener line alone carries every signal the parser needs.
      2. Strip and shlex-split. Empty input returns an empty generic card.
      3. Look up the first token in _PARSERS.
      4. If found, call it with the remaining args. Catch any parser
         exception and fall through to the generic path so a buggy
         parser never crashes the chat.
      5. If not found, return a generic "bash" card with confidence 0.5
         (we know it's a bash command, just don't recognize the verb).
      6. The parser's risk is honored as-is here. The risk classifier
         in `risk.py` runs separately at execution time and can
         escalate but not de-escalate.

    The returned card's raw_command is the ORIGINAL command text,
    not the heredoc-stripped variant — we want the card to display
    and re-dispatch exactly what the user / model typed.
    """
    raw = command.strip()
    if not raw:
        return _empty_card(raw)

    # Strip heredoc bodies so the shlex tokenizer sees a command
    # with only the opener line's metacharacters. The original raw
    # text is still carried on the returned card below.
    tokenizable = _strip_heredoc_bodies(raw)

    try:
        tokens = shlex.split(tokenizable, posix=True)
    except ValueError:
        # Unbalanced quotes remain even after heredoc stripping —
        # probably a genuinely malformed command.
        return _generic_card(raw, confidence=0.2)

    if not tokens:
        return _empty_card(raw)

    name = tokens[0]
    parser = _PARSERS.get(name)
    if parser is None:
        return _generic_card(raw, confidence=0.5, command_name=name)

    try:
        return parser(tokens[1:], raw_command=raw)
    except Exception:
        # Defensive — a buggy pattern file shouldn't take down the chat.
        return _generic_card(raw, confidence=0.3, command_name=name)


# ─── Generic fallback cards ───


def _empty_card(raw: str) -> ToolCard:
    """Empty input — used for blank lines and stripped-to-nothing commands."""
    return ToolCard(
        verb="(empty)",
        params=(),
        risk="safe",
        raw_command=raw,
        confidence=0.0,
        parser_name="generic",
    )


def _generic_card(
    raw: str,
    *,
    confidence: float,
    command_name: str = "",
) -> ToolCard:
    """Catch-all card for commands without a registered parser.

    The verb is just "bash" so the renderer treats it visually as a
    raw passthrough. confidence < 0.7 will get a "?" badge in the
    card painter to mark it as low-trust.
    """
    return ToolCard(
        verb="bash",
        params=(("command", command_name),) if command_name else (),
        risk="safe",  # risk classifier runs separately and can escalate
        raw_command=raw,
        confidence=confidence,
        parser_name="generic",
    )
