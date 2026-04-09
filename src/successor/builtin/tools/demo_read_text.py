"""demo_read_text — simple plugin-registry example tool.

This file exists only to prove the Python-import ToolRegistry loader
works end-to-end. It is intentionally separate from the native chat
file tools (`read_file`, `write_file`, `edit_file`) so the project has
one clear owner for real file IO semantics.
"""

from __future__ import annotations

from pathlib import Path

from successor.tools import tool


@tool(
    name="demo_read_text",
    description=(
        "Demo plugin tool that reads a text file from disk. This is a "
        "ToolRegistry example, not the native chat file-IO path."
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
def demo_read_text(path: str, encoding: str = "utf-8") -> str:
    """Read the entire file at `path` and return it as a string."""
    return Path(path).read_text(encoding=encoding)
