"""Tools registry — what tools the harness offers and how to describe them.

This is the SOURCE OF TRUTH for "what tools exist". Three consumers:

  1. **Setup wizard** — iterates AVAILABLE_TOOLS to show enable/disable
     toggles when creating a profile. Default selection becomes the
     new profile's `tools` field.

  2. **Config menu** — iterates AVAILABLE_TOOLS to show toggles for
     editing an existing profile's tool list.

  3. **Chat** — uses the registry to:
       - Decide whether to instantiate a `BashStreamDetector` for the
         current profile (only if "bash" is in `profile.tools`)
       - Build the "## Available Tools" section of the system prompt
         that gets sent to the model so it learns what tools it can
         call

The registry is intentionally a small constant `dict` (not a dynamic
@tool decorator scan) because v0 ships exactly one tool. When we add
more tools, we add entries here. When we eventually want user-installed
tools (the @tool decorator path), we merge them into a copy of the dict
at chat startup.

A "tool" in this context is BIGGER than a single bash command parser.
It's an entire capability the model can invoke — currently just
"bash" which encompasses every command pattern in `bash/patterns/`.
Later we might add "web_search", "read_file", "git_diff", etc as
separate tools.

Per-tool configuration (timeout, max output, allow_dangerous, etc.)
lives in `Profile.tool_config` keyed by tool name. The registry just
declares that the tool EXISTS and gives it a description. The active
configuration is the merger of registry defaults + profile overrides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """One entry in AVAILABLE_TOOLS — describes a tool the harness offers.

    Fields:
      name              short identifier, used as the key in
                        Profile.tools and Profile.tool_config
      label             human-readable label for setup wizard / config menu
      description       one-line description of what the tool does
      default_enabled   whether new profiles default to having this
                        tool enabled (the setup wizard pre-checks the
                        toggle if True)
      system_prompt_doc longer markdown that explains how the model
                        should USE this tool. Gets injected into the
                        system prompt's "## Available Tools" section.
                        Should include a one-line description, an
                        example invocation, and any safety notes.
    """

    name: str
    label: str
    description: str
    default_enabled: bool
    system_prompt_doc: str


# ─── The registry ───
#
# Add new tools here. Each entry is independent — adding `read_file`
# tomorrow doesn't touch `bash` at all. The setup wizard, config menu,
# and chat all iterate this dict, so a new entry is auto-discovered
# everywhere.

BASH_DOC = """\
### bash — execute shell commands

You run shell commands by emitting a fenced code block tagged
`bash`. The harness parses each block, executes it, and the
command's output comes back to you as a tool response on the next
turn. A successful command may produce no output at all — that is
normal and means it worked.

### How to invoke

Put every command you want to run in a fenced bash block. The
block may contain a single command, a multi-command script, a
heredoc file write, or any valid bash — the whole block is handed
to `bash` as one script.

Example — read a file:

    ```bash
    cat README.md
    ```

Example — write a file with a heredoc:

    ```bash
    cat > index.html <<'EOF'
    <!DOCTYPE html>
    <html>...</html>
    EOF
    ```

Example — a multi-step script:

    ```bash
    mkdir -p src/lib
    touch src/lib/__init__.py
    echo "done"
    ```

### Reading tool responses

When a command finishes, the next turn begins with a tool response
containing whatever the command printed. Treat it as ground truth.
Empty content is the success signal for writes, redirects, mkdir,
touch, chmod, and most other mutating commands — they finished
without printing anything because there was nothing to print. A
non-empty response shows you the actual stdout / stderr.

After you see a tool response, your next move is one of:

  1. Issue the *next* command needed to advance the user's task.
  2. If the task is complete, reply with plain text — no more
     bash blocks. Plain text ends your turn and gives control
     back to the user.

Do not re-issue a command you just ran. Acting on the tool
response, not repeating the call, is what moves things forward.

### Critical rules

- **ALWAYS use fenced bash blocks.** Plain-text commands like
  `$ cat README.md` will NOT run — only fenced blocks dispatch.
- **One block per action is fine, multiple blocks per turn is
  also fine.** Each block runs independently in order.
- **End with plain text when done.** A turn that contains no
  fenced bash block terminates the loop.

### Safety

By default, dangerous commands (`rm -rf /`, `sudo`, `curl | sh`,
`eval`, etc.) are REFUSED before running. The user's profile
controls this via bash.allow_dangerous; if it's on, dangerous
commands WILL run. Mutating commands (`mkdir`, `touch`, `rm`,
`git add`, etc.) run by default unless the profile is in
read-only mode.

Use bash freely for read-only operations. For mutating operations
think briefly about whether the user actually wants the change
before invoking.
"""


AVAILABLE_TOOLS: Mapping[str, ToolDescriptor] = {
    "bash": ToolDescriptor(
        name="bash",
        label="bash",
        description="Run shell commands. Dangerous commands refused automatically.",
        default_enabled=True,
        system_prompt_doc=BASH_DOC,
    ),
}


# ─── Helpers ───


def is_known_tool(name: str) -> bool:
    """True if `name` is a tool the harness knows about."""
    return name in AVAILABLE_TOOLS


def filter_known(names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Filter a list of tool names down to the ones we recognize.

    Used when loading a profile that may reference tools we don't
    have (e.g., a profile saved by a future version, or a typo in
    a hand-edited JSON file). Unknown tools are silently dropped.
    """
    return tuple(n for n in names if n in AVAILABLE_TOOLS)


def default_enabled_tools() -> tuple[str, ...]:
    """The list of tool names that should be enabled by default in
    a new profile. Used by the setup wizard to pre-check toggles."""
    return tuple(
        d.name for d in AVAILABLE_TOOLS.values() if d.default_enabled
    )


def build_system_prompt_tools_section(
    enabled_tools: list[str] | tuple[str, ...],
) -> str:
    """Build the "## Available Tools" section of the system prompt.

    Returns a markdown string ready to be appended to the chat's
    base system prompt. If `enabled_tools` is empty (chat-only mode),
    returns an empty string — no tools listing, the model behaves
    as a pure chat assistant.

    Output structure:

        ## Available Tools

        You have access to the following tools. Use them when they
        would help answer the user's question.

        ### bash — execute shell commands
        ... (the per-tool doc) ...

        ### read_file — read a file from disk
        ... (the per-tool doc) ...
    """
    if not enabled_tools:
        return ""

    sections: list[str] = []
    sections.append("## Available Tools")
    sections.append("")
    sections.append(
        "You have access to the following tools. Use them when they "
        "would help answer the user's question. The harness parses "
        "your fenced code blocks, executes them, and feeds the "
        "results back to you in subsequent turns."
    )
    sections.append("")

    for name in enabled_tools:
        descriptor = AVAILABLE_TOOLS.get(name)
        if descriptor is None:
            continue  # silently skip unknown tools
        sections.append(descriptor.system_prompt_doc)
        sections.append("")  # blank line between tools

    return "\n".join(sections)
