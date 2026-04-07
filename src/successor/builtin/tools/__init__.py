"""Built-in tool modules.

Each `*.py` file in this directory becomes one or more registered
tools when the ToolRegistry loads it. The agent loop (when it lands
in a future phase) consumes the registry and dispatches tool calls.

Phase 6 ships one example tool — `read_file` — to demonstrate the
@tool decorator and prove the loader runs end-to-end. The tool itself
is not yet wired into the chat (no agent loop), but `successor tools list`
shows it.
"""
