"""grep / rg / ag — search-content."""

from __future__ import annotations

from ..cards import ToolCard
from ..parser import bash_parser, clip_at_operators


def _parse_search(
    args: list[str],
    *,
    raw_command: str,
    parser_name: str,
) -> ToolCard:
    args = clip_at_operators(args)
    case_insensitive = False
    recursive = parser_name == "rg"  # rg defaults to recursive
    line_numbers = False
    files_only = False
    fixed_string = False
    pattern: str | None = None
    paths: list[str] = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            if arg == "--ignore-case":
                case_insensitive = True
            elif arg == "--recursive":
                recursive = True
            elif arg == "--line-number":
                line_numbers = True
            elif arg == "--files-with-matches":
                files_only = True
            elif arg == "--fixed-strings":
                fixed_string = True
        elif arg.startswith("-") and len(arg) > 1:
            # Bundled short flags like -ri, -rn, -lFi
            for ch in arg[1:]:
                if ch == "i":
                    case_insensitive = True
                elif ch in ("r", "R"):
                    recursive = True
                elif ch == "n":
                    line_numbers = True
                elif ch == "l":
                    files_only = True
                elif ch == "F":
                    fixed_string = True
        elif pattern is None:
            pattern = arg
        else:
            paths.append(arg)
        i += 1

    params: list[tuple[str, str]] = []
    if pattern is not None:
        params.append(("pattern", pattern))
    if paths:
        params.append(("path", ", ".join(paths) if len(paths) > 1 else paths[0]))
    elif recursive:
        params.append(("path", "."))
    if case_insensitive:
        params.append(("case", "insensitive"))
    if recursive:
        params.append(("recursive", "yes"))
    if line_numbers:
        params.append(("line-numbers", "yes"))
    if files_only:
        params.append(("output", "files-only"))
    if fixed_string:
        params.append(("mode", "literal"))

    return ToolCard(
        verb="search-content",
        params=tuple(params),
        risk="safe",
        raw_command=raw_command,
        confidence=0.9,
        parser_name=parser_name,
    )


@bash_parser("grep")
def parse_grep(args: list[str], *, raw_command: str) -> ToolCard:
    return _parse_search(args, raw_command=raw_command, parser_name="grep")


@bash_parser("rg", "ripgrep")
def parse_rg(args: list[str], *, raw_command: str) -> ToolCard:
    return _parse_search(args, raw_command=raw_command, parser_name="rg")
