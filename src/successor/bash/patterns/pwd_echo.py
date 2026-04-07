"""pwd / echo / true / false — trivial introspection commands."""

from __future__ import annotations

from ..cards import ToolCard
from ..parser import bash_parser, clip_at_operators


@bash_parser("pwd")
def parse_pwd(args: list[str], *, raw_command: str) -> ToolCard:
    return ToolCard(
        verb="working-directory",
        params=(),
        risk="safe",
        raw_command=raw_command,
        confidence=1.0,
        parser_name="pwd",
    )


@bash_parser("echo")
def parse_echo(args: list[str], *, raw_command: str) -> ToolCard:
    args = clip_at_operators(args)
    """Echo is rarely meaningful as an action, but the model uses it
    to print things back to itself for verification. We capture the
    text being echoed as a single param so the card stays small."""
    suppress_newline = False
    interpret_escapes = False
    text_args: list[str] = []
    for arg in args:
        if arg == "-n":
            suppress_newline = True
        elif arg == "-e":
            interpret_escapes = True
        elif arg == "-E":
            interpret_escapes = False
        else:
            text_args.append(arg)
    text = " ".join(text_args)
    truncated = len(text) > 60
    display_text = (text[:57] + "…") if truncated else text

    params: list[tuple[str, str]] = [("text", display_text)]
    if suppress_newline:
        params.append(("newline", "no"))
    if interpret_escapes:
        params.append(("escapes", "yes"))

    return ToolCard(
        verb="print-text",
        params=tuple(params),
        risk="safe",
        raw_command=raw_command,
        confidence=0.95,
        parser_name="echo",
    )


@bash_parser("true")
def parse_true(args: list[str], *, raw_command: str) -> ToolCard:
    return ToolCard(
        verb="noop",
        params=(("value", "true"),),
        risk="safe",
        raw_command=raw_command,
        confidence=1.0,
        parser_name="true",
    )


@bash_parser("false")
def parse_false(args: list[str], *, raw_command: str) -> ToolCard:
    return ToolCard(
        verb="noop",
        params=(("value", "false"),),
        risk="safe",
        raw_command=raw_command,
        confidence=1.0,
        parser_name="false",
    )
