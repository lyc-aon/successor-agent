"""cat — read-file (and concatenate-files for multi-arg)."""

from __future__ import annotations

from ..cards import ToolCard
from ..parser import bash_parser, clip_at_operators


@bash_parser("cat")
def parse_cat(args: list[str], *, raw_command: str) -> ToolCard:
    """Parse `cat` commands into a read-file / concatenate-files /
    write-file card depending on whether there's a redirect.

    Multi-line heredoc writes (common model pattern for file creation)
    look like `cat > target <<'EOF' ... EOF` — detect the redirect
    target BEFORE clipping at operators and surface it as a proper
    write-file action with the target path as the primary param.
    """
    # Detect a redirect write: `cat > path` or `cat >> path`. We need
    # to inspect the raw args BEFORE clipping at operators because
    # clip_at_operators drops everything after `>`. shlex has already
    # tokenized the command so `>` is its own token OR the start of
    # a glued token like `>path`.
    write_target = _find_redirect_target(args)

    if write_target is not None:
        return ToolCard(
            verb="write-file",
            params=(("path", write_target),),
            risk="mutating",  # redirects mutate disk
            raw_command=raw_command,
            confidence=0.9,
            parser_name="cat",
        )

    # No redirect — normal cat read behavior
    args = clip_at_operators(args)
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


def _find_redirect_target(args: list[str]) -> str | None:
    """Scan args for `>` or `>>` followed by a target path.

    Handles both separated (`cat > file.html`) and glued
    (`cat >file.html`) forms. Returns the target path or None.
    """
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in (">", ">>") and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(">>") and len(arg) > 2:
            return arg[2:]
        if arg.startswith(">") and len(arg) > 1 and not arg.startswith(">>"):
            return arg[1:]
        i += 1
    return None
