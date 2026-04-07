"""python / python3 — run-python (script or inline)."""

from __future__ import annotations

from ..cards import ToolCard
from ..parser import bash_parser, clip_at_operators


def _parse_python(
    args: list[str],
    *,
    raw_command: str,
    parser_name: str,
) -> ToolCard:
    args = clip_at_operators(args)
    inline_code: str | None = None
    module: str | None = None
    script: str | None = None
    flags: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-c" and i + 1 < len(args):
            inline_code = args[i + 1]
            i += 2
            continue
        if arg == "-m" and i + 1 < len(args):
            module = args[i + 1]
            i += 2
            continue
        if arg.startswith("-"):
            flags.append(arg)
        elif script is None:
            script = arg
        i += 1

    if inline_code is not None:
        snippet = inline_code if len(inline_code) <= 50 else inline_code[:47] + "…"
        return ToolCard(
            verb="run-python-inline",
            params=(("code", snippet),),
            risk="mutating",  # arbitrary code execution
            raw_command=raw_command,
            confidence=0.9,
            parser_name=parser_name,
        )

    if module is not None:
        return ToolCard(
            verb="run-python-module",
            params=(("module", module),),
            risk="mutating",
            raw_command=raw_command,
            confidence=0.9,
            parser_name=parser_name,
        )

    if script is not None:
        return ToolCard(
            verb="run-python-script",
            params=(("script", script),),
            risk="mutating",
            raw_command=raw_command,
            confidence=0.9,
            parser_name=parser_name,
        )

    # `python` with no args = REPL — we don't run that, model shouldn't either
    return ToolCard(
        verb="python-repl",
        params=(),
        risk="safe",
        raw_command=raw_command,
        confidence=0.6,
        parser_name=parser_name,
    )


@bash_parser("python")
def parse_python(args: list[str], *, raw_command: str) -> ToolCard:
    return _parse_python(args, raw_command=raw_command, parser_name="python")


@bash_parser("python3")
def parse_python3(args: list[str], *, raw_command: str) -> ToolCard:
    return _parse_python(args, raw_command=raw_command, parser_name="python3")
