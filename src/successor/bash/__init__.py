"""Bash parser + risk + executor — the bash-masking subsystem.

The premise: mid-grade local models (Qwen 3.5 27B and friends) are
fluent in bash because they've eaten millions of bash commands during
pretraining. They are halting at structured tool-call schemas because
those are out-of-distribution.

So we don't ask them to learn a tool schema. We let them write bash in
fenced code blocks (their strongest mode), parse the bash client-side,
and render it as a structured tool card with verb + params + risk +
output. The model sees raw bash; the user sees a clean structured
action. The renderer's diff layer makes this possible because it can
rewrite cells after the fact — no scrollback library can do this.

Public surface:
    ToolCard            the structured representation (cards.py)
    bash_parser         the @decorator for registering parsers (parser.py)
    parse_bash          parse a raw command into a ToolCard (parser.py)
    classify_risk       independent risk pass on a raw command (risk.py)
    dispatch_bash       parse + classify + execute, returns enriched card (exec.py)
"""

from __future__ import annotations

from .cards import Risk, ToolCard
from .parser import (
    bash_parser,
    get_parser,
    has_parser,
    parse_bash,
    registered_commands,
)

# Importing the patterns package triggers the @bash_parser decorators
# in each module, populating the _PARSERS registry. This must happen
# AFTER the parser module is imported (above) so the decorator exists.
from . import patterns  # noqa: F401, E402  -- import-for-side-effects

# Risk classifier (after patterns so test imports get a populated registry)
from .risk import classify_risk  # noqa: E402

# Executor (after everything else)
from .exec import (  # noqa: E402
    DEFAULT_TIMEOUT_S,
    MAX_OUTPUT_BYTES,
    BashConfig,
    DangerousCommandRefused,
    MutatingCommandRefused,
    RefusedCommand,
    ReservedPortCommandRefused,
    dispatch_bash,
    preview_bash,
    resolve_bash_config,
)

# Verb classification (after the cards module)
from .verbclass import (  # noqa: E402
    VerbClass,
    glyph_for_class,
    verb_class_for,
)

# Renderer (kept inside the bash package so paint.py stays generic)
from .render import (  # noqa: E402
    DEFAULT_MAX_OUTPUT_LINES,
    measure_tool_card_height,
    paint_tool_card,
)


__all__ = [
    "BashConfig",
    "DEFAULT_MAX_OUTPUT_LINES",
    "DEFAULT_TIMEOUT_S",
    "DangerousCommandRefused",
    "MAX_OUTPUT_BYTES",
    "MutatingCommandRefused",
    "RefusedCommand",
    "ReservedPortCommandRefused",
    "Risk",
    "ToolCard",
    "VerbClass",
    "bash_parser",
    "classify_risk",
    "dispatch_bash",
    "get_parser",
    "glyph_for_class",
    "has_parser",
    "measure_tool_card_height",
    "paint_tool_card",
    "parse_bash",
    "preview_bash",
    "registered_commands",
    "resolve_bash_config",
    "verb_class_for",
]
