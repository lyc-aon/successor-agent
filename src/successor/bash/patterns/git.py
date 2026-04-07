"""git — many subcommands, mostly safe (status/diff/log) or mutating (add/commit/push)."""

from __future__ import annotations

from ..cards import Risk, ToolCard
from ..parser import bash_parser, clip_at_operators


# Per-subcommand metadata. Risk classification is conservative — git
# push and git rebase get flagged as mutating even though they're
# usually safe in practice, because the user wants to know they're
# happening.
_GIT_SUBCOMMAND_RISK: dict[str, tuple[str, Risk]] = {
    # safe (read-only) subcommands
    "status": ("git-status", "safe"),
    "diff": ("git-diff", "safe"),
    "log": ("git-log", "safe"),
    "show": ("git-show", "safe"),
    "blame": ("git-blame", "safe"),
    "branch": ("git-branch", "safe"),
    "remote": ("git-remote", "safe"),
    "config": ("git-config", "safe"),
    "rev-parse": ("git-rev-parse", "safe"),
    "ls-files": ("git-ls-files", "safe"),
    "describe": ("git-describe", "safe"),
    "tag": ("git-tag", "safe"),
    "stash": ("git-stash", "safe"),
    "ls-remote": ("git-ls-remote", "safe"),
    # mutating subcommands
    "add": ("git-add", "mutating"),
    "commit": ("git-commit", "mutating"),
    "checkout": ("git-checkout", "mutating"),
    "switch": ("git-switch", "mutating"),
    "merge": ("git-merge", "mutating"),
    "pull": ("git-pull", "mutating"),
    "fetch": ("git-fetch", "mutating"),
    "push": ("git-push", "mutating"),
    "rebase": ("git-rebase", "mutating"),
    "reset": ("git-reset", "mutating"),
    "restore": ("git-restore", "mutating"),
    "rm": ("git-rm", "mutating"),
    "mv": ("git-mv", "mutating"),
    "init": ("git-init", "mutating"),
    "clone": ("git-clone", "mutating"),
    "apply": ("git-apply", "mutating"),
    "cherry-pick": ("git-cherry-pick", "mutating"),
    "revert": ("git-revert", "mutating"),
    "clean": ("git-clean", "mutating"),
}


@bash_parser("git")
def parse_git(args: list[str], *, raw_command: str) -> ToolCard:
    """Parse `git <subcommand> [args]`. Subcommand drives risk."""
    args = clip_at_operators(args)
    if not args:
        return ToolCard(
            verb="git",
            params=(("subcommand", "(missing)"),),
            risk="safe",
            raw_command=raw_command,
            confidence=0.4,
            parser_name="git",
        )

    # Skip leading global flags like `git -c foo=bar status`. shlex
    # tokenized them so we just walk past anything starting with `-`
    # at the front to find the subcommand.
    sub_idx = 0
    while sub_idx < len(args) and args[sub_idx].startswith("-"):
        sub_idx += 1
        # global flag may take a value (-c foo=bar) — heuristic skip
        if sub_idx < len(args) and not args[sub_idx].startswith("-"):
            sub_idx += 1
    if sub_idx >= len(args):
        return ToolCard(
            verb="git",
            params=(("subcommand", "(missing)"),),
            risk="safe",
            raw_command=raw_command,
            confidence=0.3,
            parser_name="git",
        )

    sub = args[sub_idx]
    rest = args[sub_idx + 1:]

    verb, risk = _GIT_SUBCOMMAND_RISK.get(sub, (f"git-{sub}", "safe"))
    confidence = 0.95 if sub in _GIT_SUBCOMMAND_RISK else 0.6

    params: list[tuple[str, str]] = [("subcommand", sub)]

    # Capture the most useful per-subcommand details
    if sub == "commit":
        for i, a in enumerate(rest):
            if a in ("-m", "--message") and i + 1 < len(rest):
                msg = rest[i + 1]
                if len(msg) > 50:
                    msg = msg[:47] + "…"
                params.append(("message", msg))
                break
    elif sub == "add":
        added = [a for a in rest if not a.startswith("-")]
        if added:
            params.append(("paths", ", ".join(added) if len(added) > 1 else added[0]))
    elif sub in ("checkout", "switch"):
        target = next((a for a in rest if not a.startswith("-")), None)
        if target:
            params.append(("target", target))
    elif sub == "push":
        positional = [a for a in rest if not a.startswith("-")]
        if positional:
            params.append(("remote", positional[0]))
            if len(positional) > 1:
                params.append(("branch", positional[1]))
        if any(a == "--force" or a == "-f" for a in rest):
            params.append(("force", "yes"))
            risk = "dangerous"  # force push is always dangerous

    return ToolCard(
        verb=verb,
        params=tuple(params),
        risk=risk,
        raw_command=raw_command,
        confidence=confidence,
        parser_name="git",
    )
