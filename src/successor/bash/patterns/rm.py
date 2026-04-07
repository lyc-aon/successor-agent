"""rm — delete-file (mutating, always; dangerous if recursive+force)."""

from __future__ import annotations

from ..cards import ToolCard
from ..parser import bash_parser, clip_at_operators


@bash_parser("rm")
def parse_rm(args: list[str], *, raw_command: str) -> ToolCard:
    args = clip_at_operators(args)
    """Parse rm. Risk classification:
      - rm <file>            mutating (single file delete)
      - rm -r <dir>          mutating (recursive but not forced)
      - rm -rf <dir>         dangerous (recursive force — model can't recover)
      - rm -rf / variants    dangerous and special-cased by risk.py
    """
    recursive = False
    force = False
    paths: list[str] = []
    for arg in args:
        if arg.startswith("--"):
            if arg == "--recursive":
                recursive = True
            elif arg == "--force":
                force = True
        elif arg.startswith("-"):
            for ch in arg[1:]:
                if ch == "r" or ch == "R":
                    recursive = True
                elif ch == "f":
                    force = True
        else:
            paths.append(arg)

    if not paths:
        return ToolCard(
            verb="delete-file",
            params=(("path", "(missing)"),),
            risk="dangerous" if recursive and force else "mutating",
            raw_command=raw_command,
            confidence=0.4,
            parser_name="rm",
        )

    params: list[tuple[str, str]] = [
        ("path", paths[0] if len(paths) == 1 else ", ".join(paths))
    ]
    if recursive:
        params.append(("recursive", "yes"))
    if force:
        params.append(("force", "yes"))

    risk = "dangerous" if (recursive and force) else "mutating"
    verb = "delete-tree" if recursive else "delete-file"

    return ToolCard(
        verb=verb,
        params=tuple(params),
        risk=risk,
        raw_command=raw_command,
        confidence=0.95,
        parser_name="rm",
    )
