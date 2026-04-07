"""which / type / command -v — locate-binary."""

from __future__ import annotations

from ..cards import ToolCard
from ..parser import bash_parser, clip_at_operators


@bash_parser("which")
def parse_which(args: list[str], *, raw_command: str) -> ToolCard:
    args = clip_at_operators(args)
    bins = [a for a in args if not a.startswith("-")]
    return ToolCard(
        verb="locate-binary",
        params=(
            ("binary", bins[0] if len(bins) == 1 else ", ".join(bins) if bins else "(missing)"),
        ),
        risk="safe",
        raw_command=raw_command,
        confidence=0.95,
        parser_name="which",
    )


@bash_parser("type")
def parse_type(args: list[str], *, raw_command: str) -> ToolCard:
    args = clip_at_operators(args)
    bins = [a for a in args if not a.startswith("-")]
    return ToolCard(
        verb="describe-command",
        params=(
            ("name", bins[0] if len(bins) == 1 else ", ".join(bins) if bins else "(missing)"),
        ),
        risk="safe",
        raw_command=raw_command,
        confidence=0.9,
        parser_name="type",
    )
