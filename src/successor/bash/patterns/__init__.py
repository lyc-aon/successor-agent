"""Bash command pattern parsers.

Importing this package triggers each pattern module's @bash_parser
decorators, populating the parser registry. Add new patterns by
creating a new file here and importing it from this __init__.

Each pattern file:
  - imports `from ..parser import bash_parser`
  - imports `from ..cards import ToolCard`
  - declares one or more `@bash_parser("name")` functions
  - keeps the parser pure: no I/O, no global state, no exceptions

The order of imports here is alphabetical for predictability — the
last-imported parser wins on collision (which only matters during
development; in steady state each command name has exactly one parser).
"""

from __future__ import annotations

from . import (  # noqa: F401  -- import-for-side-effects
    cat,
    cp_mv,
    find,
    git,
    grep,
    head_tail,
    ls,
    mkdir,
    pwd_echo,
    python,
    rm,
    which,
)
