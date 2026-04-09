"""Tools — Python functions the agent can invoke.

Phase 6 ships the SCAFFOLD only:
  - Tool dataclass + @tool decorator
  - ToolRegistry that imports Python files and harvests decorated funcs
  - Gated user-tool loading (default off, audit to stderr when on)
  - `successor tools` inventory command
  - One example built-in plugin tool (`demo_read_text`) so the loader
    has something to load and you can see what the API looks like

What phase 6 deliberately does NOT do:
  - Dispatch these Python-import tools from the chat loop yet
  - Implement any dispatch / execution / sandboxing
  - Register the example tool with any model

The native chat tool surface (`read_file`, `write_file`, `edit_file`,
`bash`, etc.) is separate and already wired through `tools_registry.py`
and `chat.py`. This module is only about the Python-import plugin tool
loader. Those tools are visible via `successor tools` but still inert
at runtime unless explicitly integrated.

Public surface:
    Tool             dataclass: name, description, schema, callable, source_path
    tool             decorator that registers a function as a Tool
    ToolRegistry     custom registry that imports Python files
    TOOL_REGISTRY    the singleton instance
    get_tool(name)   convenience lookup
    all_tools()      list of every registered tool
"""

from .tool import (
    TOOL_REGISTRY,
    Tool,
    ToolRegistry,
    all_tools,
    get_tool,
    tool,
)

__all__ = [
    "TOOL_REGISTRY",
    "Tool",
    "ToolRegistry",
    "all_tools",
    "get_tool",
    "tool",
]
