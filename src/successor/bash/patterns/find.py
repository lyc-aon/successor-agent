"""find / fd — find-files."""

from __future__ import annotations

from ..cards import ToolCard
from ..parser import bash_parser, clip_at_operators


@bash_parser("find")
def parse_find(args: list[str], *, raw_command: str) -> ToolCard:
    args = clip_at_operators(args)
    """Parse `find <path> [-name pattern] [-type x] [...]` into find-files.

    `find` has a baroque expression language; we capture only the
    common cases (path, -name pattern, -type, -maxdepth) and treat
    everything else as opaque. The raw command is always preserved
    so the user can see what's actually running.
    """
    path: str | None = None
    name_pattern: str | None = None
    type_filter: str | None = None
    max_depth: str | None = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-name" and i + 1 < len(args):
            name_pattern = args[i + 1]
            i += 2
            continue
        if arg == "-iname" and i + 1 < len(args):
            name_pattern = args[i + 1]
            i += 2
            continue
        if arg == "-type" and i + 1 < len(args):
            type_filter = args[i + 1]
            i += 2
            continue
        if arg == "-maxdepth" and i + 1 < len(args):
            max_depth = args[i + 1]
            i += 2
            continue
        if not arg.startswith("-") and path is None:
            path = arg
        i += 1

    params: list[tuple[str, str]] = [("path", path or ".")]
    if name_pattern is not None:
        params.append(("name", name_pattern))
    if type_filter is not None:
        params.append(("type", type_filter))
    if max_depth is not None:
        params.append(("max-depth", max_depth))

    return ToolCard(
        verb="find-files",
        params=tuple(params),
        risk="safe",
        raw_command=raw_command,
        confidence=0.9,
        parser_name="find",
    )


@bash_parser("fd", "fdfind")
def parse_fd(args: list[str], *, raw_command: str) -> ToolCard:
    """Parse fd (the Rust find replacement). Simpler grammar than find:
    fd [pattern] [path]."""
    args = clip_at_operators(args)
    pattern: str | None = None
    path: str | None = None
    type_filter: str | None = None
    extension: str | None = None

    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-t", "--type") and i + 1 < len(args):
            type_filter = args[i + 1]
            i += 2
            continue
        if arg in ("-e", "--extension") and i + 1 < len(args):
            extension = args[i + 1]
            i += 2
            continue
        if not arg.startswith("-"):
            if pattern is None:
                pattern = arg
            elif path is None:
                path = arg
        i += 1

    params: list[tuple[str, str]] = []
    if pattern is not None:
        params.append(("pattern", pattern))
    params.append(("path", path or "."))
    if type_filter is not None:
        params.append(("type", type_filter))
    if extension is not None:
        params.append(("extension", extension))

    return ToolCard(
        verb="find-files",
        params=tuple(params),
        risk="safe",
        raw_command=raw_command,
        confidence=0.92,
        parser_name="fd",
    )
