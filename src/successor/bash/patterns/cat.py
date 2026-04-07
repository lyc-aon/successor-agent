"""cat — read-file (and concatenate-files for multi-arg)."""

from __future__ import annotations

from ..cards import ToolCard
from ..parser import bash_parser, clip_at_operators


@bash_parser("cat")
def parse_cat(args: list[str], *, raw_command: str) -> ToolCard:
    args = clip_at_operators(args)
    """Parse `cat [-n] file1 [file2 ...]` into a read-file card.

    Multiple files become "concatenate-files" with a comma-joined path
    list, since the model usually means "show me these files" when
    concatenating. The -n (line numbers) flag is preserved as a hint.
    """
    paths: list[str] = []
    numbered = False
    for arg in args:
        if arg == "-n" or arg == "--number":
            numbered = True
        elif arg.startswith("-"):
            # Other flags get folded into the params for transparency
            continue
        else:
            paths.append(arg)

    if not paths:
        return ToolCard(
            verb="read-stdin",
            params=(),
            risk="safe",
            raw_command=raw_command,
            confidence=0.6,
            parser_name="cat",
        )

    if len(paths) == 1:
        verb = "read-file"
        params: list[tuple[str, str]] = [("path", paths[0])]
    else:
        verb = "concatenate-files"
        params = [("paths", ", ".join(paths)), ("count", str(len(paths)))]
    if numbered:
        params.append(("line-numbers", "yes"))

    return ToolCard(
        verb=verb,
        params=tuple(params),
        risk="safe",
        raw_command=raw_command,
        confidence=0.95,
        parser_name="cat",
    )
