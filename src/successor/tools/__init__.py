"""Tools — Python functions the agent can invoke.

Phase 6 ships the SCAFFOLD only:
  - Tool dataclass + @tool decorator
  - ToolRegistry that imports Python files and harvests decorated funcs
  - Gated user-tool loading (default off, audit to stderr when on)
  - `successor tools list` inventory command
  - One example built-in tool (read_file) so the loader has something
    to load and you can see what the API looks like

What phase 6 deliberately does NOT do:
  - Wire tools into the chat (no agent loop yet)
  - Implement any dispatch / execution / sandboxing
  - Register the example tool with any model

When the agent loop lands, it will consume this registry. Until then,
tools are visible via `successor tools list` but inert.

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
