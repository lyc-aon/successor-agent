"""head/tail — read-file-head and read-file-tail (one parser, two names)."""

from __future__ import annotations

from ..cards import ToolCard
from ..parser import bash_parser, clip_at_operators


def _parse_head_or_tail(
    args: list[str],
    *,
    raw_command: str,
    verb_name: str,
    parser_name: str,
) -> ToolCard:
    args = clip_at_operators(args)
    paths: list[str] = []
    n_value: str | None = None
    follow = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-n":
            if i + 1 < len(args):
                n_value = args[i + 1]
                i += 2
                continue
        elif arg.startswith("-n"):
            n_value = arg[2:]
        elif arg.startswith("--lines="):
            n_value = arg.split("=", 1)[1]
        elif arg in ("-f", "--follow"):
            follow = True
        elif (
            len(arg) > 1
            and arg.startswith("-")
            and arg[1:].isdigit()
        ):
            # POSIX short form: `head -5 file` is equivalent to
            # `head -n 5 file`. GNU coreutils, BSD userland, and
            # busybox all honor this. The parser now extracts the
            # count so the card's `lines` param matches what the
            # model actually asked for.
            n_value = arg[1:]
        elif not arg.startswith("-"):
            paths.append(arg)
        i += 1

    path = paths[0] if paths else "(stdin)"
    if len(paths) > 1:
        path = ", ".join(paths)

    params: list[tuple[str, str]] = [("path", path)]
    if n_value is not None:
        params.append(("lines", n_value))
    if follow:
        params.append(("follow", "yes"))

    return ToolCard(
        verb=verb_name,
        params=tuple(params),
        risk="safe",
        raw_command=raw_command,
        confidence=0.95,
        parser_name=parser_name,
    )


@bash_parser("head")
def parse_head(args: list[str], *, raw_command: str) -> ToolCard:
    return _parse_head_or_tail(
        args, raw_command=raw_command,
        verb_name="read-file-head", parser_name="head",
    )


@bash_parser("tail")
def parse_tail(args: list[str], *, raw_command: str) -> ToolCard:
    return _parse_head_or_tail(
        args, raw_command=raw_command,
        verb_name="read-file-tail", parser_name="tail",
    )
