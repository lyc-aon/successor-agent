"""Built-in tool modules.

Each `*.py` file in this directory becomes one or more registered
tools when the ToolRegistry loads it. The agent loop (when it lands
in a future phase) consumes the registry and dispatches tool calls.

Phase 6 ships one example plugin tool — `demo_read_text` — to
demonstrate the @tool decorator and prove the loader runs end-to-end.
This registry is separate from the native chat tool surface, which is
already wired into the chat. `successor tools` inventories these
Python-import tools.
"""
