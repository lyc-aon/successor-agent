"""Verb classification — group parsed verbs into visual classes.

The bash parser produces ~15 distinct verbs (`read-file`, `search-content`,
`list-directory`, `create-file`, `git-status`, etc.). The renderer wants
a coarser taxonomy so tool cards of the same *kind* share an icon,
border shape, and output treatment — the user can recognize card
classes by peripheral-vision shape instead of reading every verb.

This module is a pure lookup: `verb_class(verb) -> VerbClass`. No I/O,
no state, no imports beyond the standard library. The class enum is
small on purpose — more granularity invites bikeshedding.

Classes:

    READ       file/stream reads       ◲  (read-file, cat, head, tail)
    SEARCH     content & path queries  ⌕  (grep, find, locate, which)
    LIST       directory listings      ☰  (ls)
    INSPECT    introspection/status    ⊙  (pwd, git-status/log/diff)
    MUTATE     filesystem writes       ✎  (mkdir, touch, rm, cp, mv,
                                           git-add/commit/push, sed -i)
    EXEC       program invocations     ▶  (python, eval at safe levels)
    DANGER     anything risk=dangerous ⚠  (classifier escalation wins)
    UNKNOWN    no parser matched       ?  (generic fallback card)

Risk escalation: if a card's `risk == "dangerous"`, its class is
ALWAYS `DANGER` regardless of the parser verb. This way a git-push
--force gets the `⚠` treatment without changing the parser. The
call site passes both verb and risk to `verb_class_for(verb, risk)`
so the classifier's verdict can override the verb's native class.

The class table is exhaustive over the current verb inventory and
defensive: any verb not in the table falls through to UNKNOWN rather
than raising. Adding a new parser that returns a new verb requires
one entry here.
"""

from __future__ import annotations

from enum import Enum

from .cards import Risk


class VerbClass(Enum):
    """Visual classification of a bash tool card.

    Drives the card's icon glyph, border hint, and (in a later phase)
    its output pipeline. Orthogonal to risk — a READ card can be safe
    OR dangerous (e.g., `cat /etc/shadow` is a read but risky).
    """

    READ = "read"
    SEARCH = "search"
    LIST = "list"
    INSPECT = "inspect"
    MUTATE = "mutate"
    EXEC = "exec"
    DANGER = "danger"
    UNKNOWN = "unknown"


# ─── Static verb → class mapping ───
#
# Ordered deliberately: read-* prefixes first, then search/find,
# then mutating families, then exec. Comments group by parser of
# origin to make maintenance trivial.

_VERB_TO_CLASS: dict[str, VerbClass] = {
    # cat / head / tail
    "read-file": VerbClass.READ,
    "read-stdin": VerbClass.READ,
    "concatenate-files": VerbClass.READ,
    "read-file-head": VerbClass.READ,
    "read-file-tail": VerbClass.READ,
    # grep / rg / ripgrep
    "search-content": VerbClass.SEARCH,
    # find / fd
    "find-files": VerbClass.SEARCH,
    # which / type
    "locate-binary": VerbClass.SEARCH,
    "describe-command": VerbClass.INSPECT,
    # ls
    "list-directory": VerbClass.LIST,
    # pwd / echo
    "working-directory": VerbClass.INSPECT,
    "print-text": VerbClass.EXEC,
    "noop": VerbClass.EXEC,
    # mkdir / touch / rm / cp / mv
    "create-file": VerbClass.MUTATE,
    "create-directory": VerbClass.MUTATE,
    "delete-file": VerbClass.MUTATE,
    "copy-files": VerbClass.MUTATE,
    "move-files": VerbClass.MUTATE,
    # cat with redirect — file writes (heredoc and one-liners alike)
    "write-file": VerbClass.MUTATE,
    # python
    "run-python-inline": VerbClass.EXEC,
    "run-python-module": VerbClass.EXEC,
    "run-python-script": VerbClass.EXEC,
    "python-repl": VerbClass.EXEC,
    # git — safe subcommands are INSPECT, mutating subcommands MUTATE.
    # These are the verbs the git parser produces directly; anything
    # it doesn't recognize gets `git-<sub>` which falls to UNKNOWN
    # below and then the runtime risk decides.
    "git": VerbClass.INSPECT,
    "git-status": VerbClass.INSPECT,
    "git-diff": VerbClass.INSPECT,
    "git-log": VerbClass.INSPECT,
    "git-show": VerbClass.INSPECT,
    "git-blame": VerbClass.INSPECT,
    "git-branch": VerbClass.INSPECT,
    "git-remote": VerbClass.INSPECT,
    "git-config": VerbClass.INSPECT,
    "git-rev-parse": VerbClass.INSPECT,
    "git-ls-files": VerbClass.INSPECT,
    "git-describe": VerbClass.INSPECT,
    "git-tag": VerbClass.INSPECT,
    "git-stash": VerbClass.INSPECT,
    "git-ls-remote": VerbClass.INSPECT,
    "git-add": VerbClass.MUTATE,
    "git-commit": VerbClass.MUTATE,
    "git-checkout": VerbClass.MUTATE,
    "git-switch": VerbClass.MUTATE,
    "git-merge": VerbClass.MUTATE,
    "git-pull": VerbClass.MUTATE,
    "git-fetch": VerbClass.MUTATE,
    "git-push": VerbClass.MUTATE,
    "git-rebase": VerbClass.MUTATE,
    "git-reset": VerbClass.MUTATE,
    "git-restore": VerbClass.MUTATE,
    "git-rm": VerbClass.MUTATE,
    "git-mv": VerbClass.MUTATE,
    "git-init": VerbClass.MUTATE,
    "git-clone": VerbClass.MUTATE,
    "git-apply": VerbClass.MUTATE,
    "git-cherry-pick": VerbClass.MUTATE,
    "git-revert": VerbClass.MUTATE,
    "git-clean": VerbClass.MUTATE,
    # holonet
    "web-search": VerbClass.SEARCH,
    "news-search": VerbClass.SEARCH,
    "page-scrape": VerbClass.READ,
    "paper-search": VerbClass.SEARCH,
    "trial-search": VerbClass.SEARCH,
    "biomedical-search": VerbClass.SEARCH,
    # browser
    "browser-open": VerbClass.INSPECT,
    "browser-click": VerbClass.EXEC,
    "browser-type": VerbClass.MUTATE,
    "browser-wait": VerbClass.INSPECT,
    "browser-read": VerbClass.READ,
    "browser-screenshot": VerbClass.INSPECT,
    "browser-console": VerbClass.INSPECT,
    # generic fallback
    "bash": VerbClass.UNKNOWN,
    "(empty)": VerbClass.UNKNOWN,
}


def verb_class_for(verb: str, risk: Risk) -> VerbClass:
    """Classify a (verb, risk) pair into a visual VerbClass.

    Risk escalation rule: if risk == "dangerous", the class is
    ALWAYS DANGER regardless of the verb. This lets the risk
    classifier override the parser's verb-based class when it
    catches something harmful (git push --force, sudo cat, etc.).

    An unknown verb falls through to UNKNOWN. The renderer still
    has enough data to paint the generic card via the raw_command,
    it just doesn't get a class-specific icon or body layout.
    """
    if risk == "dangerous":
        return VerbClass.DANGER
    return _VERB_TO_CLASS.get(verb, VerbClass.UNKNOWN)


# ─── Icon glyphs ───
#
# One Unicode glyph per class. Chosen for distinctness in peripheral
# vision and for baseline alignment with the box border. No emoji —
# monospace-stable Unicode only.

_CLASS_GLYPH: dict[VerbClass, str] = {
    VerbClass.READ: "◲",
    VerbClass.SEARCH: "⌕",
    VerbClass.LIST: "☰",
    VerbClass.INSPECT: "⊙",
    VerbClass.MUTATE: "✎",
    VerbClass.EXEC: "▶",
    VerbClass.DANGER: "⚠",
    VerbClass.UNKNOWN: "?",
}


def glyph_for_class(cls: VerbClass) -> str:
    """Return the single-character glyph for this class. Always one
    cell wide at render time (verified by the renderer test)."""
    return _CLASS_GLYPH.get(cls, "?")
