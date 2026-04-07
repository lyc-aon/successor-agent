"""mkdir / touch — create-directory and create-file (mutating)."""

from __future__ import annotations

from ..cards import ToolCard
from ..parser import bash_parser, clip_at_operators


@bash_parser("mkdir")
def parse_mkdir(args: list[str], *, raw_command: str) -> ToolCard:
    args = clip_at_operators(args)
    parents = False
    paths: list[str] = []
    for arg in args:
        if arg in ("-p", "--parents"):
            parents = True
        elif arg.startswith("-"):
            continue
        else:
            paths.append(arg)

    if not paths:
        return ToolCard(
            verb="create-directory",
            params=(("path", "(missing)"),),
            risk="mutating",
            raw_command=raw_command,
            confidence=0.4,
            parser_name="mkdir",
        )

    params: list[tuple[str, str]] = [
        ("path", paths[0] if len(paths) == 1 else ", ".join(paths))
    ]
    if parents:
        params.append(("parents", "yes"))

    return ToolCard(
        verb="create-directory",
        params=tuple(params),
        risk="mutating",
        raw_command=raw_command,
        confidence=0.95,
        parser_name="mkdir",
    )


@bash_parser("touch")
def parse_touch(args: list[str], *, raw_command: str) -> ToolCard:
    args = clip_at_operators(args)
    paths = [a for a in args if not a.startswith("-")]
    if not paths:
        return ToolCard(
            verb="create-file",
            params=(("path", "(missing)"),),
            risk="mutating",
            raw_command=raw_command,
            confidence=0.4,
            parser_name="touch",
        )
    return ToolCard(
        verb="create-file",
        params=(("path", paths[0] if len(paths) == 1 else ", ".join(paths)),),
        risk="mutating",
        raw_command=raw_command,
        confidence=0.95,
        parser_name="touch",
    )
