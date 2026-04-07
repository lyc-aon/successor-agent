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

import shlex
from typing import Callable

from .cards import ToolCard

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
      1. Strip and shlex-split. Empty input returns an empty generic card.
      2. Look up the first token in _PARSERS.
      3. If found, call it with the remaining args. Catch any parser
         exception and fall through to the generic path so a buggy
         parser never crashes the chat.
      4. If not found, return a generic "bash" card with confidence 0.5
         (we know it's a bash command, just don't recognize the verb).
      5. The parser's risk is honored as-is here. The risk classifier
         in `risk.py` runs separately at execution time and can
         escalate but not de-escalate.
    """
    raw = command.strip()
    if not raw:
        return _empty_card(raw)

    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        # Unbalanced quotes, etc. — shlex couldn't tokenize.
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
