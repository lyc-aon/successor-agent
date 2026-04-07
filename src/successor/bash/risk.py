"""Independent risk classifier — runs on the raw command BEFORE the
parser, so even commands without a registered parser get a risk level.

The classifier is a series of regex/substring checks against the
raw command string. It's deliberately conservative: it errs on the
side of "dangerous" when in doubt, because the cost of a false
positive (asking the user to confirm) is tiny, and the cost of a
false negative (auto-running rm -rf /) is unrecoverable.

The classifier output is the AUTHORITATIVE risk for the executor's
gating decision. Pattern parsers also declare their own risk for
the rendered card; the dispatch pipeline takes the MAX of the two
(safe < mutating < dangerous), so if either layer flags something
the more cautious answer wins.
"""

from __future__ import annotations

import re

from .cards import Risk


# ─── Dangerous patterns ───
#
# Each pattern is a (regex, reason) pair. The reason is included on
# the dispatch refusal so the user understands why something was
# blocked. Patterns are case-sensitive deliberately — `RM -RF /`
# would be valid but unusual; we'd rather catch the common case.

_DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # rm -rf / and friends
    (re.compile(r"\brm\s+(?:-[a-zA-Z]*[rRf][a-zA-Z]*\s+)+/(?!\w)"),
     "rm -rf at filesystem root"),
    (re.compile(r"\brm\s+(?:-[a-zA-Z]*[rRf][a-zA-Z]*\s+)+/\*"),
     "rm -rf /*"),
    (re.compile(r"\brm\s+(?:-[a-zA-Z]*[rRf][a-zA-Z]*\s+)+~(?:/|$|\s)"),
     "rm -rf at user home root"),
    (re.compile(r"\brm\s+(?:-[a-zA-Z]*[rRf][a-zA-Z]*\s+)+/(?:etc|var|usr|bin|sbin|boot|sys|proc|dev|lib|lib64|root|home)(?:/|\s|$)"),
     "rm -rf at a system path"),

    # sudo / su — privilege escalation
    (re.compile(r"\bsudo\b"), "sudo (privilege escalation)"),
    (re.compile(r"\bsu\s"), "su (privilege escalation)"),
    (re.compile(r"^su$"), "su (privilege escalation)"),

    # curl|sh and wget|sh — pipe-to-shell
    (re.compile(r"\bcurl\s[^|]*\|\s*(?:sh|bash|zsh|fish|sudo)"),
     "pipe network download to shell"),
    (re.compile(r"\bwget\s[^|]*\|\s*(?:sh|bash|zsh|fish|sudo)"),
     "pipe network download to shell"),

    # eval and exec on user input
    (re.compile(r"\beval\s"), "eval"),
    (re.compile(r"^eval$"), "eval"),

    # chmod 777 / chmod -R 777 / chmod +s
    (re.compile(r"\bchmod\s+(?:-R\s+)?(?:0?777|\+s\b)"),
     "chmod 777 or setuid"),

    # Redirect into system paths
    (re.compile(r">\s*(?:/etc/|/usr/|/var/|/boot/|/sys/|/proc/|/dev/(?!null|tty|stdout|stderr|stdin))"),
     "redirect into a system path"),

    # dd to a block device
    (re.compile(r"\bdd\s+.*\bof=/dev/(?!null|zero)"),
     "dd to a block device"),

    # Forkbomb
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
     "fork bomb"),

    # mkfs / fdisk / parted / wipefs
    (re.compile(r"\b(?:mkfs|fdisk|parted|wipefs|sgdisk|cfdisk)\b"),
     "filesystem/partition tool"),

    # shutdown / reboot / halt / poweroff
    (re.compile(r"\b(?:shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b"),
     "shutdown / reboot"),

    # kill -9 1 / killall init
    (re.compile(r"\bkill\s+-9?\s+1\b"), "kill init (PID 1)"),
    (re.compile(r"\bkillall\s+(?:-9\s+)?init\b"), "killall init"),

    # iptables / nft / firewalld flush
    (re.compile(r"\biptables\s+(?:-F|--flush)"), "flush firewall rules"),
)


# ─── Mutating patterns ───
#
# These are commands that change disk state but aren't outright
# dangerous. We classify them as "mutating" so the user knows
# something is being written, but they don't get refused.

_MUTATING_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # File-writing redirections (but NOT into system paths — those
    # are caught by the dangerous block above — and NOT into /dev/null
    # and friends which are virtual sinks).
    (re.compile(r">\s*(?!/dev/(?:null|tty|stdout|stderr|stdin)\b)[^\s&|]"),
     "redirect output to file"),
    (re.compile(r">>\s*(?!/dev/(?:null|tty|stdout|stderr|stdin)\b)[^\s&|]"),
     "append to file"),

    # Common mutators
    (re.compile(r"\b(?:mkdir|touch|cp|mv|rm|chmod|chown|ln)\b"), "filesystem mutation"),
    (re.compile(r"\bsed\s+-i\b"), "sed in-place edit"),

    # Package managers
    (re.compile(r"\b(?:apt|apt-get|yum|dnf|pacman|brew|pip|npm|cargo|gem|go)\s+(?:install|remove|uninstall|update|upgrade)\b"),
     "package manager mutation"),

    # Git mutators
    (re.compile(r"\bgit\s+(?:add|commit|push|pull|fetch|merge|rebase|reset|checkout|switch|restore|rm|mv|init|clone|apply|cherry-pick|revert|clean|tag\s+-)\b"),
     "git mutating subcommand"),
)


def classify_risk(command: str) -> tuple[Risk, str]:
    """Classify a raw bash command's risk level.

    Returns a (risk, reason) pair. `reason` is empty for "safe" and
    populated for "mutating"/"dangerous" so the dispatch layer can
    show the user *why* something was flagged.

    The check order is dangerous → mutating → safe (default). The
    first matching dangerous pattern wins; we don't aggregate.
    """
    cmd = command.strip()
    if not cmd:
        return ("safe", "")

    for pat, reason in _DANGEROUS_PATTERNS:
        if pat.search(cmd):
            return ("dangerous", reason)

    for pat, reason in _MUTATING_PATTERNS:
        if pat.search(cmd):
            return ("mutating", reason)

    return ("safe", "")


# ─── Risk ordering helper ───


_RISK_ORDER: dict[Risk, int] = {"safe": 0, "mutating": 1, "dangerous": 2}


def max_risk(a: Risk, b: Risk) -> Risk:
    """Return the more cautious of two risk levels."""
    return a if _RISK_ORDER[a] >= _RISK_ORDER[b] else b
