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
@tool decorator scan) because the built-in tool surface is still small.
When we add more tools, we add entries here. When we eventually want
user-installed tools (the @tool decorator path), we merge them into a
copy of the dict at chat startup.

A "tool" in this context is BIGGER than a single bash command parser.
It's an entire capability the model can invoke — today `bash`,
`subagent`, `holonet`, `browser`, and `vision`. Later we might add
"git_diff", "db_query", or user-installed tools as separate entries.

Per-tool configuration (timeout, max output, allow_dangerous, etc.)
lives in `Profile.tool_config` keyed by tool name. The registry just
declares that the tool EXISTS and gives it a description. The active
configuration is the merger of registry defaults + profile overrides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


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
      schema            OpenAI-style native tool schema, or None when
                        the tool is not exposed through `tools=`
      system_prompt_doc longer markdown that explains how the model
                        should USE this tool. Gets injected into the
                        system prompt's "## Available Tools" section.
                        Should include a one-line description, an
                        example invocation, and any safety notes.
      model_guidance    extra behavioral guidance appended to the
                        active system prompt when the tool is enabled.
                        Use this for semantic rules that the generic
                        schema description cannot teach by itself.
      user_visible      whether setup/config should show this tool as
                        a user-toggleable capability. Internal helper
                        tools like `skill` stay hidden and are enabled
                        dynamically by runtime conditions instead.
    """

    name: str
    label: str
    description: str
    default_enabled: bool
    system_prompt_doc: str
    schema: dict[str, Any] | None = None
    model_guidance: str = ""
    user_visible: bool = True


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
block may contain a single command or a multi-command script — the
whole block is handed to `bash` as one script.

Example — read a file:

    ```bash
    git status
    ```

Example — run tests:

    ```bash
    pytest tests/test_chat_bash.py
    ```

Example — a multi-step script:

    ```bash
    npm install
    npm run build
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

Use bash for shell and system work: builds, tests, package managers,
git, process inspection, servers, and one-off system commands.
Prefer the native `read_file`, `write_file`, and `edit_file` tools
for normal file IO.

When you need a local dev server, prefer a free high port. Do not
assume `8080`, and do not kill an unknown process just because it is
holding the port you wanted. If a preferred port is busy, pick another
free port first. Never reclaim the active provider endpoint unless the
user explicitly told you to replace that service.
"""

BASH_MODEL_GUIDANCE = """\
## Using bash

Use bash for shell and system work, not as the default path for file IO.

- Prefer `read_file` over `cat`/`sed`/`head` for source inspection when file tools are enabled.
- Prefer `edit_file` and `write_file` over heredocs, `echo > file`, or ad hoc shell rewriting for normal source changes.
- Before starting a local server, choose a free port instead of assuming `8080`.
- If the port you wanted is occupied, pick another free port instead of killing the current owner.
- Only stop an existing process when you have positively identified it as your own stale child, or the user explicitly asked you to replace that service.
"""

_BASH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Execute a shell command or multi-line script "
            "in the user's working directory and return its stdout, "
            "stderr, and exit code. Prefer read_file, write_file, and "
            "edit_file for normal file IO."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "The bash command to execute. Pipes, redirects, "
                        "substitutions, and multi-step scripts "
                        "all work because the harness runs the string with "
                        "shell=True."
                    ),
                },
            },
            "required": ["command"],
        },
    },
}

READ_FILE_DOC = """\
### read_file — read a text file from disk

Use `read_file` to inspect local text files directly instead of
shelling out to `cat`, `head`, `tail`, or `sed`.

Call it with:

- `file_path` — absolute path preferred
- `offset` — optional starting line number (1-based)
- `limit` — optional number of lines to return

Results come back with deterministic line numbers so you can quote
or patch exact regions later.
"""

WRITE_FILE_DOC = """\
### write_file — create or fully replace a text file

Use `write_file` to create new files or replace an existing file's
entire contents in one call.

Call it with:

- `file_path` — absolute path preferred
- `content` — full file contents to write

Existing files must be fully read first. If the file changed since
you read it, the tool refuses the write and tells you to read again.
"""

EDIT_FILE_DOC = """\
### edit_file — make an exact text replacement in a file

Use `edit_file` for targeted changes to an existing file instead of
shell text surgery with `sed`, `awk`, or inline Python.

Call it with:

- `file_path`
- `old_string`
- `new_string`
- `replace_all` — optional; false by default

The tool requires a prior full read, refuses stale files, and fails
when `old_string` matches more than one location unless
`replace_all=true`.
"""

_READ_FILE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a local UTF-8 text file with deterministic line numbers. "
            "Prefer this over cat, head, tail, or sed for file inspection."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to read.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional starting line number (1-based).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional maximum number of lines to return.",
                },
            },
            "required": ["file_path"],
        },
    },
}

_WRITE_FILE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Create a new UTF-8 text file or fully replace an existing one. "
            "Prefer this over heredocs, echo redirection, or shell file writes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to write.",
                },
                "content": {
                    "type": "string",
                    "description": "Full file contents to write.",
                },
            },
            "required": ["file_path", "content"],
        },
    },
}

_EDIT_FILE_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "Edit an existing UTF-8 text file by replacing one exact string "
            "with another. Prefer this over sed, awk, perl, or inline Python "
            "for targeted file edits."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to the file to edit.",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to replace.",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "Replace every exact match instead of requiring a unique match."
                    ),
                },
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
}

FILE_TOOLS_MODEL_GUIDANCE = """\
## Working with local files

Use native file tools for file work and reserve `bash` for shell work.

- Use `read_file` instead of `cat`, `head`, `tail`, or `sed` when you need file contents.
- Use `edit_file` instead of `sed`, `awk`, `perl`, or inline Python when you need to change an existing file.
- Use `write_file` instead of heredocs, `echo >`, or shell redirection when you need to create a file or replace one completely.
- Existing files must be read before you edit or overwrite them.
- If you already read a file and nothing external changed, do not re-read the same full file again unless you genuinely need fresh context.
- Prefer absolute paths in file-tool calls.
- Use `offset` and `limit` on `read_file` when you only need a specific region of a long file.
"""

SUBAGENT_DOC = """\
### subagent — fork a background worker

Launch a background worker that inherits the current conversation
context, uses the same profile and provider, and runs autonomously
until it produces a final report. The parent chat keeps running while
the worker works; completion arrives later as a background-task
notification.

### How to invoke

Call the `subagent` tool with a short `prompt` directive and, when
helpful, a short `name` so the task badge and `/tasks` list are easy
to scan. You may also pass `role="verification"` to launch a stricter
read-only verifier instead of a normal worker.

Example — fork a repo audit:

    {"prompt": "Audit the repo for version mismatches between pyproject.toml and src/successor/__init__.py. Report what is actually true.", "name": "version-audit"}

Example — fork a verification pass:

    {"prompt": "Verify the new playback zoom controls. Run the relevant tests, use the browser if needed, and report PASS/FAIL with evidence.", "name": "playback-qa", "role": "verification"}

### Critical rules

- The worker inherits the current context, so the prompt should be a
  directive, not a full re-explanation of the task.
- Use subagents when intermediate tool noise would clutter the main
  context or when background work can proceed independently.
- Use `role="verification"` when you want an independent read-only
  checker that runs tests, browser checks, or edge cases without
  editing project files.
- Verification-role workers inherit a stricter runtime: native file
  mutation tools are removed and bash mutating/dangerous actions are
  disabled, which makes them safer for final-mile QA.
- Do not assume results before the completion notification arrives.
- Do not read the worker transcript while it is still running unless
  the user explicitly asks for a progress check.
"""

SUBAGENT_MODEL_GUIDANCE = """\
## Using background subagents

The `subagent` tool forks a background worker that inherits your current
conversation context. Use it when the intermediate tool output is not
worth keeping in your own context or when background work can proceed
independently.

### When to fork

- Research or repo-audit tasks where you want the answer, not the raw grep,
  cat, or git output.
- Multi-step implementation or verification work that can proceed in the
  background while you keep the foreground turn clean.
- Narrow, self-contained tasks where a concise final report is more useful
  than watching every intermediate tool call.
- Use `role="verification"` when you want a fresh read-only checker to
  run tests, lint, browser checks, or adversarial probes before you
  declare the work done.
- Prefer a verification-role subagent for complex browser-heavy or
  multi-file work where confirmation bias is likely.

### Writing the prompt

Because the fork inherits the current context, write a directive, not a full
briefing. Be specific about scope, what to check, and what a good final
report should contain. Do not restate background the worker already has.

### Critical rules

- Do not re-run the same research in the foreground after you fork it.
- Do not predict or fabricate what the worker will find.
- The worker result arrives later as a background-task notification injected
  into the conversation as a user-role event. That notification is not
  something you write yourself.
- Do not inspect a running worker's transcript unless the user explicitly
  asks for progress. Wait for the completion notification.
"""

_SUBAGENT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "subagent",
        "description": (
            "Launch a background worker that inherits the current "
            "conversation context, uses the same profile and provider, "
            "and later reports back through a background-task "
            "notification."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "The worker directive. Since the child inherits the "
                        "current conversation context, write this as a scoped "
                        "instruction describing what to do."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Optional short task label, ideally one or two words, "
                        "used in the task badge and completion notice."
                    ),
                },
                "role": {
                    "type": "string",
                    "enum": ["worker", "verification"],
                    "description": (
                        "Optional worker role. Use `worker` for normal background "
                        "execution. Use `verification` for a stricter read-only "
                        "verifier that should prove the work with evidence."
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
}

TASK_DOC = """\
### task — update the session task ledger

Use `task` to keep a compact session-local ledger for multi-step work.
This is the structured place to track what is pending, what is actively
in progress, and what has been completed.

### How to invoke

Call `task` with the full current task list in priority order. The tool
replaces the previous ledger; it is not a patch operation.

Each task item contains:

- `content` — short task text
- `active_form` — present-tense wording for the active task
- `status` — `pending`, `in_progress`, or `completed`

Example — start a multi-step implementation:

    {"items": [
      {"content": "Inspect the current browser loop", "active_form": "inspecting the current browser loop", "status": "completed"},
      {"content": "Implement a task ledger", "active_form": "implementing a task ledger", "status": "in_progress"},
      {"content": "Run recorded E2E verification", "active_form": "running recorded E2E verification", "status": "pending"}
    ]}

### Critical rules

- Use `task` for multi-step work that spans several tool calls,
  edits, or verification passes.
- Keep the list compact and concrete.
- Keep at most one task `in_progress`.
- When you stop because the work is done or blocked on user input,
  clear the active task by updating the ledger first.
"""

TASK_MODEL_GUIDANCE = """\
## Using the task ledger

Use `task` to keep a compact session-local ledger authoritative.

- Prefer coarse tasks that match meaningful phases of work, not
  bookkeeping for every individual file or click.
- Keep the list short and concrete; do not dump a full essay into it.
- Keep exactly one task `in_progress` while you are actively working.
- For long scoped work, create the ledger before the first big write,
  server-management step, browser loop, or other substantive mutation.
- Do not jump straight into a large `write_file` payload, a long bash
  script, or repeated browser actions before the ledger exists.
- Update the ledger when you switch focus, complete work, or hand
  control back.
- Do not leave a task `in_progress` when you are done or waiting on
  the user.
"""

VERIFY_DOC = """\
### verify — update the session verification contract

Use `verify` to keep a compact session-local contract for what must be
proven before the work is really done.

### How to invoke

Call `verify` with the full current list of verification items. The
tool replaces the previous contract; it is not a patch operation.

Each verification item contains:

- `claim` — what should be true
- `evidence` — what concrete runtime evidence will prove it
- `status` — `pending`, `in_progress`, `passed`, or `failed`
- `observed` — optional concise outcome once evidence exists

Example — verifying a local interactive app:

    {"items": [
      {"claim": "Typing a valid command increments score", "evidence": "player script + score HUD", "status": "in_progress"},
      {"claim": "Mistyped commands reduce lives", "evidence": "scripted bad input + runtime log", "status": "pending"}
    ]}

### Critical rules

- Use `verify` for non-trivial runtime, browser, visual, or stateful work.
- Keep the list compact and concrete.
- Keep at most one item `in_progress`.
- Update items as evidence arrives; do not leave stale claims in the contract.
- For non-trivial work, include at least one failure-path or adversarial
  check instead of verifying only the happy path.
"""

VERIFY_MODEL_GUIDANCE = """\
## Using the verification contract

Use `verify` to keep a compact list of claims plus evidence.

- Keep claims concrete and evidence specific.
- Prefer runtime evidence over source inspection.
- Use `observed` for concise outcomes, especially on failures.
- For interactive claims, name the exact state delta that must change
  and prove that specific change.
- For non-trivial work, include at least one failure-path or adversarial
  probe before you declare success.
- Do not leave an item `in_progress` once the evidence is in.
"""

RUNBOOK_DOC = """\
### runbook — update the session experiment runbook

Use `runbook` to keep a compact session-local experiment contract for
long iterative work.

### How to invoke

Call `runbook` with the current run-level objective, success
definition, baseline status, active hypothesis, and evaluator steps.
The tool replaces the previous runbook state. You may also include an
optional `attempt` object to record the result of one bounded
experiment.

Core fields:

- `objective` — what the run is trying to achieve
- `success_definition` — what counts as success
- `scope` — optional in-scope files or surfaces
- `protected_surfaces` — optional out-of-scope or fragile areas
- `baseline_status` — `missing`, `captured`, or `stale`
- `baseline_summary` — optional concise summary of current baseline
- `active_hypothesis` — one concrete idea currently being tested
- `evaluator` — stable list of checks to rerun after attempts
- `status` — `planning`, `running`, `blocked`, or `complete`

Example — long local app iteration:

    {
      "objective": "Ship a stable typing game loop with correct scoring and lives",
      "success_definition": "The game plays end to end, scoring is correct, lives decrement on mistakes, and no console errors occur during the scripted playthrough",
      "scope": ["src/game", "src/ui"],
      "baseline_status": "captured",
      "baseline_summary": "Current build opens and renders, but the scripted player stalls after the first wave",
      "active_hypothesis": "Input focus is being lost after wave transitions",
      "evaluator": [
        {"id": "build", "kind": "command", "spec": "npm run build", "pass_condition": "exit 0"},
        {"id": "player", "kind": "script", "spec": "node scripts/play-game.mjs", "pass_condition": "playthrough reaches game over with expected score log"},
        {"id": "browser", "kind": "browser_flow", "spec": "open app, run player, inspect console", "pass_condition": "no console errors and HUD updates correctly"}
      ],
      "status": "running"
    }

Optional `attempt` object:

- `hypothesis`
- `summary`
- `decision` — `kept`, `discarded`, `inconclusive`, or `failed_env`
- `files_touched`
- `evaluator_summary`
- `verification_summary`
- `artifact_refs`

### Critical rules

- Use `runbook` for long iterative work with multiple edit -> evaluate loops.
- Keep one active hypothesis at a time.
- Capture baseline before major edits when it is missing.
- Keep evaluator steps stable unless the task genuinely changes.
- Record concise attempt results instead of retrying failed ideas blindly.
"""

RUNBOOK_MODEL_GUIDANCE = """\
## Using the experiment runbook

Use `runbook` to make long iterative runs explicit.

- Create it early for work that will involve multiple edit -> run -> verify loops.
- If you already expect repeated experiments, set the runbook up before
  the first major attempt instead of after several loose retries.
- Capture or refresh the baseline before making major comparisons.
- Keep one active hypothesis at a time.
- Reuse the evaluator bundle instead of inventing a new verification plan every turn.
- After a bounded attempt, record a keep/discard decision with a concise attempt summary.
"""

_VERIFY_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "verify",
        "description": (
            "Replace the current session verification contract with a "
            "compact list of claims, evidence plans, and statuses."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": (
                        "Full replacement list for the session verification "
                        "contract."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {
                                "type": "string",
                                "description": "The behavior or outcome to prove.",
                            },
                            "evidence": {
                                "type": "string",
                                "description": (
                                    "The concrete evidence that will prove the claim."
                                ),
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "passed", "failed"],
                                "description": "Current verification status.",
                            },
                            "observed": {
                                "type": "string",
                                "description": (
                                    "Optional concise observed outcome once evidence exists."
                                ),
                            },
                        },
                        "required": ["claim", "evidence", "status"],
                    },
                },
            },
            "required": ["items"],
        },
    },
}

_RUNBOOK_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "runbook",
        "description": (
            "Replace the current session experiment runbook for long "
            "iterative work and optionally record the latest bounded "
            "attempt result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "clear": {
                    "type": "boolean",
                    "description": "Clear the current runbook instead of updating it.",
                },
                "objective": {
                    "type": "string",
                    "description": "Run-level objective for the current autonomous effort.",
                },
                "success_definition": {
                    "type": "string",
                    "description": "What counts as success for this run.",
                },
                "scope": {
                    "type": "array",
                    "description": "Optional in-scope files, modules, or product surfaces.",
                    "items": {"type": "string"},
                },
                "protected_surfaces": {
                    "type": "array",
                    "description": "Optional surfaces to avoid or treat carefully.",
                    "items": {"type": "string"},
                },
                "baseline_status": {
                    "type": "string",
                    "enum": ["missing", "captured", "stale"],
                    "description": "Whether a trustworthy baseline has been captured.",
                },
                "baseline_summary": {
                    "type": "string",
                    "description": "Optional concise summary of the current baseline.",
                },
                "active_hypothesis": {
                    "type": "string",
                    "description": "The single concrete idea currently being tested.",
                },
                "evaluator": {
                    "type": "array",
                    "description": "Stable evaluator bundle to rerun after bounded attempts.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "enum": ["command", "browser_flow", "vision_check", "script", "manual_probe"],
                            },
                            "spec": {"type": "string"},
                            "pass_condition": {"type": "string"},
                        },
                        "required": ["id", "kind", "spec", "pass_condition"],
                    },
                },
                "decision_policy": {
                    "type": "string",
                    "description": "Optional concise rule for when an attempt should be kept.",
                },
                "status": {
                    "type": "string",
                    "enum": ["planning", "running", "blocked", "complete"],
                    "description": "Current run-level status.",
                },
                "attempt": {
                    "type": "object",
                    "description": "Optional record of the latest bounded attempt and its outcome.",
                    "properties": {
                        "hypothesis": {"type": "string"},
                        "summary": {"type": "string"},
                        "decision": {
                            "type": "string",
                            "enum": ["kept", "discarded", "inconclusive", "failed_env"],
                        },
                        "files_touched": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "evaluator_summary": {"type": "string"},
                        "verification_summary": {"type": "string"},
                        "artifact_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["hypothesis", "summary", "decision"],
                },
            },
            "required": [],
        },
    },
}

_TASK_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "task",
        "description": (
            "Replace the current session task ledger with a compact list "
            "of pending, in-progress, and completed tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": (
                        "Full replacement list for the session task ledger. "
                        "Use an empty array to clear it."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "Short task text.",
                            },
                            "active_form": {
                                "type": "string",
                                "description": (
                                    "Present-tense wording for the active task."
                                ),
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "Task status.",
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["items"],
        },
    },
}

SKILL_DOC = """\
### skill — load an enabled skill on demand

Use `skill` to load a profile-enabled skill into the conversation only
when it clearly matches the current task. Skills are specialized
instruction bundles: they tell you when to prefer `holonet` over the
browser, how to operate the browser cleanly, or how to handle a domain
workflow without bloating every turn's base prompt.

### How to invoke

Call `skill` with the exact skill name from the available-skills list.
Pass a short `task` string when a scoped reminder would help the loaded
skill focus on the user's request.

Example — load a browser workflow:

    {"skill": "browser-operator", "task": "Open the local app, click the signup CTA, and report any console errors."}

### Critical rules

- Only invoke skills that appear in the available-skills list for this
  turn.
- If a listed skill clearly matches the user's request, load it BEFORE
  using other tools or answering from memory.
- Do not mention a skill without actually calling `skill`.
- After a skill has been loaded, follow its instructions directly and do
  not reload the same skill unless the task meaningfully changes.
"""

SKILL_MODEL_GUIDANCE = """\
## Using enabled skills

Some profiles expose an internal `skill` tool. Skills are NOT always-on
prompt text; they are loaded on demand when they clearly match the task.

### Skill routing rules

- Check the available-skills list in the system prompt before acting.
- If one skill clearly matches the user's request, invoking `skill` is a
  blocking first step. Load it BEFORE calling other tools or answering.
- Never say "I'll use the X skill" without actually calling `skill`.
- If the conversation already contains that skill's loaded instructions,
  do not reload it unless the task has materially changed.
"""

_SKILL_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "skill",
        "description": (
            "Load one enabled skill into the current conversation so its "
            "specialized instructions become available on demand."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": (
                        "Exact skill name from the available-skills list "
                        "for this turn."
                    ),
                },
                "task": {
                    "type": "string",
                    "description": (
                        "Optional short reminder of the specific task this "
                        "skill should help with right now."
                    ),
                },
            },
            "required": ["skill"],
        },
    },
}

HOLONET_DOC = """\
### holonet — API-backed web research

Use `holonet` for structured web retrieval that does NOT require a
live browser. It covers general search, article extraction, biomedical
papers, and clinical-study lookup while keeping the main chat fast and
deterministic.

### Providers

- `brave_search` — general web search
- `brave_news` — current-news search
- `firecrawl_search` — search plus article summaries / excerpts
- `firecrawl_scrape` — scrape a specific article/page URL
- `europe_pmc` — biomedical papers
- `clinicaltrials` — ClinicalTrials.gov study registry
- `biomedical_research` — Europe PMC + ClinicalTrials.gov together

### How to invoke

Choose a provider explicitly when possible. Pass `query` for search
routes or `url` for `firecrawl_scrape`.

Example — general search:

    {"provider": "brave_search", "query": "llama.cpp slots endpoint", "count": 5}

Example — scrape one article:

    {"provider": "firecrawl_scrape", "url": "https://example.com/article"}

### Critical rules

- Prefer `holonet` over the live browser when API-backed retrieval is
  enough.
- Use `biomedical_research`, `europe_pmc`, or `clinicaltrials` for
  papers and registered studies instead of generic web search.
- When the user gives you a concrete article URL and wants the content,
  use `firecrawl_scrape`.
"""

HOLONET_MODEL_GUIDANCE = """\
## Using holonet

`holonet` is the first choice for web research when you do not need a
live page session. Prefer it over the browser for search, article
extraction, news, biomedical papers, and clinical-study lookup because
it is faster, cleaner, and easier to verify.

Choose a provider explicitly when the task is obvious:

- `brave_search` / `brave_news` for general web and news lookup
- `firecrawl_search` for article discovery with summaries
- `firecrawl_scrape` for a specific page URL
- `europe_pmc` / `clinicaltrials` / `biomedical_research` for medical and research tasks
"""

_HOLONET_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "holonet",
        "description": (
            "Query API-backed web and research providers without opening "
            "a live browser. Supports Brave Search, Firecrawl, Europe PMC, "
            "ClinicalTrials.gov, and a combined biomedical route."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": [
                        "auto",
                        "brave_search",
                        "brave_news",
                        "firecrawl_search",
                        "firecrawl_scrape",
                        "europe_pmc",
                        "clinicaltrials",
                        "biomedical_research",
                    ],
                    "description": "Provider / route to use. Choose explicitly when possible.",
                },
                "query": {
                    "type": "string",
                    "description": "Search query for search-style routes.",
                },
                "url": {
                    "type": "string",
                    "description": "Concrete article/page URL for `firecrawl_scrape`.",
                },
                "count": {
                    "type": "integer",
                    "description": "Requested result count. Typical useful range is 3-8.",
                },
            },
        },
    },
}

BROWSER_DOC = """\
### browser — live Playwright browser control

Use `browser` when the task needs a real interactive page session:
navigation, clicks, form entry, JS-heavy local pages, login state, or
visual/browser-only verification. The browser session is persistent for
the current chat.

### Actions

- `open` — navigate to a URL
- `inspect` — list visible controls and stable selector hints for the current page
- `click` — click a visible target
- `type` — type into an input, textarea, contenteditable field, or the currently focused editable target
- `press` — send a keyboard key like `Enter` or `Escape`
- `select` — choose an option in a `<select>` or similar form control
- `storage_state` — inspect local/session storage for the current page
- `clear_storage` — clear local/session storage for the current page
- `wait_for` — wait until a target is visible
- `extract_text` — read visible text from the page or a target
- `screenshot` — capture a screenshot to disk
- `console_errors` — read recent console/page errors

### How to invoke

Prefer text/label targets when possible; use `target_kind="selector"`
only when the visible text is not distinctive enough.

If you built the local page yourself and know stable selectors such as
`#new-title` or `#status-filter`, prefer those selectors over ambiguous
visible text like `Open`, `Closed`, or placeholder copy.

Example — open a page:

    {"action": "open", "url": "http://127.0.0.1:4173"}

Example — inspect the current page before guessing targets:

    {"action": "inspect"}

Example — click a visible button:

    {"action": "click", "target": "Launch", "target_kind": "text"}

Example — choose from a dropdown:

    {"action": "select", "target": "Status", "target_kind": "label", "option": "Closed"}

Example — inspect persisted app state before retesting:

    {"action": "storage_state"}

Example — reset polluted browser state after a code fix:

    {"action": "clear_storage", "scope": "both"}

Example — commit an inline edit in the focused field:

    {"action": "type", "text": "Keyboard navigation bug", "press_enter": true}

Example — replace an existing field value before typing:

    {"action": "type", "target": "Issue title", "target_kind": "label", "text": "Keyboard navigation bug", "replace_existing": true}

Example — scope a repeated button label to one row:

    {"action": "click", "target": "li:has-text(\"Keyboard navigation bug\") button.status-btn", "target_kind": "selector"}

### Critical rules

- Prefer `holonet` for web research and article retrieval; use the
  live browser only when interactivity or real page execution matters.
- Reuse the existing session. Do not repeatedly open the same page if
  you can continue from the current one.
- After interactive actions, read the returned snapshot before deciding
  what to do next.
- For local verification flows, call `inspect` after `open` or reload if
  you are not sure which control to target next.
- For local apps or fixtures you just built, prefer stable ids/classes
  over ambiguous visible text. Do not click words like `Open` or
  `Closed` unless they are clearly the intended control.
- If local verification is polluted by persisted browser state after a
  reload, use `storage_state` or `clear_storage` in the browser. Do not
  edit the app just to reset test data.
- When multiple controls share the same label, prefer a selector target
  scoped to the relevant row or region instead of a bare text click.
- For inline-edit or keyboard-driven flows, use `press` or
  `press_enter` instead of pretending the change is verified while the
  input is merely focused.
- `type` behaves like real keyboard typing. If the target already has a
  value, your text will append unless the app selects the text for you
  or you pass `replace_existing=true`.
"""

BROWSER_MODEL_GUIDANCE = """\
## Using the browser tool

Use the Playwright browser only for tasks that genuinely need a live
page session: local app verification, clicks, typing, login state,
console errors, screenshots, JS-rendered pages, or real form controls
like dropdowns. For search and content retrieval, prefer `holonet`.

For local verification work, the default rhythm should be: `open`
once, `inspect` to learn the real controls/selectors, perform the
smallest necessary action, then read the returned snapshot before
choosing the next step.

For open-ended local polish or "inspect it like a human" tasks, do not
tour every control. Sample one or two representative interactions, pick
the most important issue, fix it, verify it, and stop.

When an input is already focused, `type` may omit `target`. If the next
step is simply Enter, prefer one `type` call with `press_enter=true`
instead of a separate `press`. Use `press` mainly for keys like
`Escape` or for keyboard-only flows that are not just "type, then
Enter". `type` is human-like: it does not silently clear an existing
value unless the page already selected the text or you explicitly set
`replace_existing=true`. When a button label is repeated, prefer a scoped selector such as
`li:has-text("Issue title") button.status-btn`.

For local fixtures or pages you just built, prefer stable ids/classes
you already know from the source over ambiguous visible text. Use
`#search`, `#new-title`, or `#status-filter` instead of clicking generic
words like `Open`, `Closed`, or placeholder text unless those are the
actual controls you intend.

If a reload or prior verification step leaves stale browser state behind,
use `storage_state` or `clear_storage` instead of changing the app code
just to get a clean test fixture.

When the task is explicitly visual — layout, spacing, clipping,
overflow, hierarchy, contrast, “inspect it like a human”, or design
polish — capture a `screenshot` and use `vision` instead of relying on
DOM text alone.
"""

_BROWSER_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "browser",
        "description": (
            "Control a persistent Playwright browser session for live "
            "navigation, clicking, typing, extraction, screenshots, and "
            "console-error checks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "open",
                        "inspect",
                        "click",
                        "type",
                        "press",
                        "select",
                        "storage_state",
                        "clear_storage",
                        "wait_for",
                        "extract_text",
                        "screenshot",
                        "console_errors",
                    ],
                    "description": "Browser action to perform.",
                },
                "url": {
                    "type": "string",
                    "description": "URL for `open`.",
                },
                "target": {
                    "type": "string",
                    "description": "Visible text, label, placeholder, or selector target. Optional for focused `type`/`press` actions.",
                },
                "target_kind": {
                    "type": "string",
                    "enum": ["auto", "text", "label", "placeholder", "selector"],
                    "description": "How to interpret `target`. Omit for auto-detection.",
                },
                "text": {
                    "type": "string",
                    "description": "Text payload for `type`.",
                },
                "option": {
                    "type": "string",
                    "description": "Option label or value for `select`.",
                },
                "key": {
                    "type": "string",
                    "description": "Keyboard key for `press`, for example `Enter` or `Escape`.",
                },
                "press_enter": {
                    "type": "boolean",
                    "description": "Press Enter after typing. Useful for inline-edit save and form submit flows.",
                },
                "replace_existing": {
                    "type": "boolean",
                    "description": "Select the focused editable target's existing contents before typing so the new text replaces the current value instead of appending.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional output path for `screenshot`.",
                },
                "scope": {
                    "type": "string",
                    "enum": ["local", "session", "both"],
                    "description": "Storage scope for `clear_storage`. Defaults to `both`.",
                },
            },
            "required": ["action"],
        },
    },
}

VISION_DOC = """\
### vision — inspect screenshots and images with a multimodal model

Use `vision` when the task depends on what is visibly on screen rather
than what the DOM or source code claims should be there: layout,
spacing, clipping, overlap, hierarchy, contrast, empty states, or
other design/runtime details.

`vision` analyzes a local image file. It works well with browser
screenshots, but it can also inspect any other local PNG, JPEG, WEBP,
or GIF that the harness can read.

### How to invoke

Pass a local `path` plus a short `prompt` explaining what you need to
verify.

Example — inspect a browser screenshot:

    {"path": "/tmp/successor-shot.png", "prompt": "Describe the most obvious visual issue in this UI."}

Example — verify a specific layout question:

    {"path": "/tmp/page.png", "prompt": "Check whether the CTA is clipped or overlaps nearby content."}

### Critical rules

- Use `vision` when the task is visually grounded. Do not guess visual
  details from HTML/CSS/DOM text alone.
- For local UI verification, the normal sequence is: `browser open`,
  `browser screenshot`, then `vision`.
- Keep the prompt concrete. Ask for one or two specific visual checks
  instead of an open-ended essay.
"""

VISION_MODEL_GUIDANCE = """\
## Using the vision tool

Use `vision` for screenshot-based inspection: layout, spacing, clipping,
overlap, contrast, hierarchy, and other visible UI/runtime details.

If a browser or local-app task says “inspect it like a human”, “check
the design”, “look for visual weirdness”, or “verify the layout”, take a
browser screenshot and call `vision` instead of inferring everything
from page text or selectors.

For visual verification, keep the loop tight: open once, capture a
screenshot, ask one concrete visual question, then act on the answer.
"""

_VISION_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "vision",
        "description": (
            "Inspect a local image or browser screenshot with a multimodal "
            "model. Useful for layout, visual QA, and screenshot-based "
            "verification."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Local image path to analyze.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Short instruction describing what to inspect in the image.",
                },
                "detail": {
                    "type": "string",
                    "enum": ["auto", "low", "high", "original"],
                    "description": "Optional image-detail level. Omit to use the configured default.",
                },
            },
            "required": ["path"],
        },
    },
}


AVAILABLE_TOOLS: Mapping[str, ToolDescriptor] = {
    "read_file": ToolDescriptor(
        name="read_file",
        label="read",
        description="Read local UTF-8 text files with deterministic line numbers.",
        default_enabled=True,
        schema=_READ_FILE_TOOL_SCHEMA,
        system_prompt_doc=READ_FILE_DOC,
    ),
    "write_file": ToolDescriptor(
        name="write_file",
        label="write",
        description="Create new files or fully replace existing text files.",
        default_enabled=True,
        schema=_WRITE_FILE_TOOL_SCHEMA,
        system_prompt_doc=WRITE_FILE_DOC,
    ),
    "edit_file": ToolDescriptor(
        name="edit_file",
        label="edit",
        description="Make exact string replacements in existing text files.",
        default_enabled=True,
        schema=_EDIT_FILE_TOOL_SCHEMA,
        system_prompt_doc=EDIT_FILE_DOC,
    ),
    "bash": ToolDescriptor(
        name="bash",
        label="bash",
        description="Run shell and system commands. Prefer native file tools for file IO.",
        default_enabled=True,
        schema=_BASH_TOOL_SCHEMA,
        system_prompt_doc=BASH_DOC,
        model_guidance=BASH_MODEL_GUIDANCE,
    ),
    "task": ToolDescriptor(
        name="task",
        label="task",
        description="Internal session task ledger for multi-step work.",
        default_enabled=False,
        schema=_TASK_TOOL_SCHEMA,
        system_prompt_doc=TASK_DOC,
        model_guidance=TASK_MODEL_GUIDANCE,
        user_visible=False,
    ),
    "verify": ToolDescriptor(
        name="verify",
        label="verify",
        description="Internal session verification contract for evidence-bearing completion.",
        default_enabled=False,
        schema=_VERIFY_TOOL_SCHEMA,
        system_prompt_doc=VERIFY_DOC,
        model_guidance=VERIFY_MODEL_GUIDANCE,
        user_visible=False,
    ),
    "runbook": ToolDescriptor(
        name="runbook",
        label="runbook",
        description="Internal experiment runbook for long iterative work.",
        default_enabled=False,
        schema=_RUNBOOK_TOOL_SCHEMA,
        system_prompt_doc=RUNBOOK_DOC,
        model_guidance=RUNBOOK_MODEL_GUIDANCE,
        user_visible=False,
    ),
    "skill": ToolDescriptor(
        name="skill",
        label="skill",
        description="Internal on-demand skill loader for profile-enabled workflows.",
        default_enabled=False,
        schema=_SKILL_TOOL_SCHEMA,
        system_prompt_doc=SKILL_DOC,
        model_guidance=SKILL_MODEL_GUIDANCE,
        user_visible=False,
    ),
    "subagent": ToolDescriptor(
        name="subagent",
        label="subagent",
        description="Launch a background worker that inherits the current context.",
        default_enabled=False,
        schema=_SUBAGENT_TOOL_SCHEMA,
        system_prompt_doc=SUBAGENT_DOC,
        model_guidance=SUBAGENT_MODEL_GUIDANCE,
    ),
    "holonet": ToolDescriptor(
        name="holonet",
        label="holonet",
        description="API-backed web research via Brave, Firecrawl, Europe PMC, and ClinicalTrials.",
        default_enabled=False,
        schema=_HOLONET_TOOL_SCHEMA,
        system_prompt_doc=HOLONET_DOC,
        model_guidance=HOLONET_MODEL_GUIDANCE,
    ),
    "browser": ToolDescriptor(
        name="browser",
        label="browser",
        description="Optional Playwright browser session for live navigation and page interaction.",
        default_enabled=False,
        schema=_BROWSER_TOOL_SCHEMA,
        system_prompt_doc=BROWSER_DOC,
        model_guidance=BROWSER_MODEL_GUIDANCE,
    ),
    "vision": ToolDescriptor(
        name="vision",
        label="vision",
        description="Optional multimodal image inspection for screenshots and visual QA.",
        default_enabled=False,
        schema=_VISION_TOOL_SCHEMA,
        system_prompt_doc=VISION_DOC,
        model_guidance=VISION_MODEL_GUIDANCE,
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
        d.name
        for d in AVAILABLE_TOOLS.values()
        if d.user_visible and d.default_enabled
    )


def selectable_tool_names() -> tuple[str, ...]:
    """Tools that should appear in setup/config multi-select UIs."""
    return tuple(
        name
        for name, descriptor in AVAILABLE_TOOLS.items()
        if descriptor.user_visible
    )


def tool_label(name: str) -> str:
    """Human-facing short label for a tool name."""
    descriptor = AVAILABLE_TOOLS.get(name)
    if descriptor is None:
        return name
    return descriptor.label


def build_native_tool_schemas(
    enabled_tools: list[str] | tuple[str, ...],
) -> list[dict[str, Any]]:
    """Return the native tool schemas for enabled tools.

    Tools without a native schema are skipped. The order matches the
    enabled-tool list so provider requests stay stable.
    """
    schemas: list[dict[str, Any]] = []
    for name in enabled_tools:
        descriptor = AVAILABLE_TOOLS.get(name)
        if descriptor is None or descriptor.schema is None:
            continue
        schemas.append(descriptor.schema)
    return schemas


def build_model_tool_guidance(
    enabled_tools: list[str] | tuple[str, ...],
) -> str:
    """Concatenate extra system-prompt guidance for enabled tools."""
    sections: list[str] = []
    if any(name in {"read_file", "write_file", "edit_file"} for name in enabled_tools):
        sections.append(FILE_TOOLS_MODEL_GUIDANCE.strip())
    for name in enabled_tools:
        descriptor = AVAILABLE_TOOLS.get(name)
        if descriptor is None or not descriptor.model_guidance:
            continue
        sections.append(descriptor.model_guidance.strip())
    return "\n\n".join(section for section in sections if section)


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
        "would help answer the user's question. Native tool calls "
        "execute directly and their results come back to you in "
        "subsequent turns."
    )
    sections.append("")

    for name in enabled_tools:
        descriptor = AVAILABLE_TOOLS.get(name)
        if descriptor is None:
            continue  # silently skip unknown tools
        sections.append(descriptor.system_prompt_doc)
        sections.append("")  # blank line between tools

    return "\n".join(sections)
