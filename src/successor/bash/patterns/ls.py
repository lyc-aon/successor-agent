"""ls — list-directory."""

from __future__ import annotations

from ..cards import ToolCard
from ..parser import bash_parser, clip_at_operators


@bash_parser("ls")
def parse_ls(args: list[str], *, raw_command: str) -> ToolCard:
    args = clip_at_operators(args)
    """Parse `ls [-alh...] [path]` into a list-directory card.

    Recognizes the common flag bundle (-a, -l, -h, -1, -R, --color)
    and a single positional path. Multiple positional paths get
    joined; the verb stays singular ("list-directory") because the
    user reads the path as a hint, not a contract.
    """
    flags: set[str] = set()
    paths: list[str] = []
    for arg in args:
        if arg.startswith("-") and not arg.startswith("--"):
            for ch in arg[1:]:
                flags.add(ch)
        elif arg.startswith("--"):
            flags.add(arg)
        else:
            paths.append(arg)

    long_format = "l" in flags
    show_hidden = "a" in flags or "A" in flags
    recursive = "R" in flags
    human = "h" in flags

    # Take only the first path; multiple positional paths are rare and
    # the raw_command always preserves the full intent on the bottom border
    path = paths[0] if paths else "."

    params: list[tuple[str, str]] = [("path", path)]
    if show_hidden:
        params.append(("hidden", "yes"))
    if long_format:
        params.append(("format", "long"))
    if recursive:
        params.append(("recursive", "yes"))
    if human:
        params.append(("human-sizes", "yes"))

    return ToolCard(
        verb="list-directory",
        params=tuple(params),
        risk="safe",
        raw_command=raw_command,
        confidence=0.95,
        parser_name="ls",
    )
