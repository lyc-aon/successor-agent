"""cp / mv — copy-files and move-files (mutating)."""

from __future__ import annotations

from ..cards import ToolCard
from ..parser import bash_parser, clip_at_operators


def _parse_two_path_op(
    args: list[str],
    *,
    raw_command: str,
    verb: str,
    parser_name: str,
) -> ToolCard:
    args = clip_at_operators(args)
    recursive = False
    force = False
    positional: list[str] = []

    for arg in args:
        if arg.startswith("--"):
            if arg == "--recursive":
                recursive = True
            elif arg == "--force":
                force = True
        elif arg.startswith("-"):
            for ch in arg[1:]:
                if ch in ("r", "R"):
                    recursive = True
                elif ch == "f":
                    force = True
        else:
            positional.append(arg)

    if len(positional) < 2:
        return ToolCard(
            verb=verb,
            params=(("source", positional[0] if positional else "(missing)"),),
            risk="mutating",
            raw_command=raw_command,
            confidence=0.4,
            parser_name=parser_name,
        )

    *sources, dest = positional
    src_text = sources[0] if len(sources) == 1 else ", ".join(sources)
    params: list[tuple[str, str]] = [
        ("source", src_text),
        ("destination", dest),
    ]
    if recursive:
        params.append(("recursive", "yes"))
    if force:
        params.append(("force", "yes"))

    return ToolCard(
        verb=verb,
        params=tuple(params),
        risk="mutating",
        raw_command=raw_command,
        confidence=0.95,
        parser_name=parser_name,
    )


@bash_parser("cp")
def parse_cp(args: list[str], *, raw_command: str) -> ToolCard:
    return _parse_two_path_op(
        args, raw_command=raw_command, verb="copy-files", parser_name="cp",
    )


@bash_parser("mv")
def parse_mv(args: list[str], *, raw_command: str) -> ToolCard:
    return _parse_two_path_op(
        args, raw_command=raw_command, verb="move-files", parser_name="mv",
    )
