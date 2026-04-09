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

_BASH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Execute a shell command (or multi-line script / heredoc) "
            "in the user's working directory and return its stdout, "
            "stderr, and exit code. Successful commands may produce "
            "no stdout — that is normal for writes, redirects, mkdir, "
            "touch, chmod, and most mutating commands."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "The bash command to execute. Pipes, redirects, "
                        "substitutions, heredocs, and multi-step scripts "
                        "all work because the harness runs the string with "
                        "shell=True."
                    ),
                },
            },
            "required": ["command"],
        },
    },
}

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
to scan.

Example — fork a repo audit:

    {"prompt": "Audit the repo for version mismatches between pyproject.toml and src/successor/__init__.py. Report what is actually true.", "name": "version-audit"}

### Critical rules

- The worker inherits the current context, so the prompt should be a
  directive, not a full re-explanation of the task.
- Use subagents when intermediate tool noise would clutter the main
  context or when background work can proceed independently.
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
            },
            "required": ["prompt"],
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
- `type` — fill an input or textarea, or type into the focused field
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
Enter". When a button label is repeated, prefer a scoped selector such as
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
    "bash": ToolDescriptor(
        name="bash",
        label="bash",
        description="Run shell commands. Dangerous commands refused automatically.",
        default_enabled=True,
        schema=_BASH_TOOL_SCHEMA,
        system_prompt_doc=BASH_DOC,
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
