"""read_file — read a file from the local filesystem.

The canonical "first tool" — every agent harness has one. This is the
example built-in that proves the @tool decorator and ToolRegistry
loader work end-to-end. It's NOT yet wired into the chat (the agent
loop hasn't been built), but `successor tools list` shows it.
"""

from __future__ import annotations

from pathlib import Path

from successor.tools import tool


@tool(
    name="read_file",
    description=(
        "Read a file from the local filesystem and return its contents "
        "as text. Used by the agent to inspect source files, configs, "
        "logs, or any other text-shaped data."
    ),
    schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute or relative path to the file",
            },
            "encoding": {
                "type": "string",
                "description": "Text encoding (default: utf-8)",
                "default": "utf-8",
            },
        },
        "required": ["path"],
    },
)
def read_file(path: str, encoding: str = "utf-8") -> str:
    """Read the entire file at `path` and return it as a string."""
    return Path(path).read_text(encoding=encoding)
