"""Tool dataclass, @tool decorator, and the import-based ToolRegistry.

Tools are the only Successor extension type that turns data into code. The
discipline:

  - Built-in tools live in `src/successor/builtin/tools/*.py`. They are
    reviewed at commit time and load by default.

  - User tools live in `~/.config/successor/tools/*.py`. They are GATED
    by an opt-in setting (`allow_user_tools` in chat.json) because
    importing arbitrary Python from a user directory runs whatever
    that file does at import time. When the gate is enabled, every
    user tool file is announced to stderr as it loads — providing an
    audit trail.

  - The @tool decorator captures the function plus its metadata
    (name, description, JSON schema for arguments). It returns a
    Tool dataclass and ALSO appends the Tool to a module-level
    `_PENDING` list that the registry drains after each import.

The registry is a different shape from `loader.Registry` because tools
need a Python import step, not a file-content parser. Multiple tools
per file are supported — the @tool decorator can be applied multiple
times in one .py file and all of them are harvested together.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import load_chat_config
from ..loader import builtin_root, config_dir


# ─── Tool dataclass ───


@dataclass(frozen=True, slots=True)
class Tool:
    """One registered tool — name + description + schema + callable.

    Fields:
      name          unique identifier (matches the @tool name= argument)
      description   one-line "what this does" hint surfaced to the model
      schema        JSON schema dict for the tool's arguments (OpenAI shape)
      func          the actual callable. Signature must match `schema`.
      source_path   absolute path to the .py file the tool was loaded from
    """

    name: str
    description: str
    schema: dict[str, Any]
    func: Callable[..., Any]
    source_path: str = ""


# ─── @tool decorator ───
#
# Decorator-side state: a list that the importer drains after each
# user/builtin module import. Putting it at module level (not on the
# Registry) lets the decorator be imported standalone — user tool files
# do `from successor.tools import tool` and then `@tool(...)` without
# touching any registry instance.

_PENDING: list[Tool] = []


def tool(
    *,
    name: str,
    description: str = "",
    schema: dict[str, Any] | None = None,
) -> Callable[[Callable[..., Any]], Tool]:
    """Register a function as a tool.

    Use:
        @tool(
            name="demo_read_text",
            description="Read a file from disk and return its contents.",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path"},
                },
                "required": ["path"],
            },
        )
        def demo_read_text(path: str) -> str:
            return Path(path).read_text(encoding="utf-8")

    Returns the Tool object (not the original function). Calling the
    returned object IS the same as calling the original function — Tool
    is callable via __call__-on-func passthrough below.
    """

    def decorator(func: Callable[..., Any]) -> Tool:
        instance = Tool(
            name=name,
            description=description,
            schema=schema or {},
            func=func,
        )
        _PENDING.append(instance)
        return instance

    return decorator


# Make Tool instances directly callable so user code that does
#   result = demo_read_text(path="/etc/hostname")
# works after the @tool wrapping. The .func attribute is also exposed
# for callers that want to introspect the underlying function.
Tool.__call__ = lambda self, *args, **kwargs: self.func(*args, **kwargs)  # type: ignore[attr-defined]


# ─── ToolRegistry ───


class ToolRegistry:
    """Import-based registry for Python tool modules.

    Walks two directories — `src/successor/builtin/tools/` and (when
    enabled) `~/.config/successor/tools/` — importing each `*.py` file in
    order. Each import triggers any `@tool` decorators inside, which
    append to the module-level `_PENDING` list. After each import,
    we drain `_PENDING` and assign the file's path as `source_path`
    on each new Tool.

    User-dir loading is gated by the `allow_user_tools` config key
    (default False). When enabled, every user tool file emits an
    audit line to stderr before being imported.
    """

    def __init__(self) -> None:
        self._items: dict[str, Tool] = {}
        self._sources: dict[str, str] = {}
        self._loaded = False

    def load(self) -> None:
        """Walk built-in dir then (if gated on) user dir, importing each module."""
        if self._loaded:
            return
        self._do_load()
        self._loaded = True

    def reload(self) -> None:
        """Force a fresh scan, dropping all registered tools."""
        self._items.clear()
        self._sources.clear()
        self._loaded = False
        # Also clear any leftover decorator state from a previous load,
        # which could happen in tests that import tool modules directly.
        _PENDING.clear()
        self.load()

    def _do_load(self) -> None:
        builtin_dir = builtin_root() / "tools"
        self._scan_dir(builtin_dir, source="builtin")

        # User tool loading is opt-in. Default is OFF.
        cfg = load_chat_config()
        if bool(cfg.get("allow_user_tools", False)):
            user_dir = config_dir() / "tools"
            self._scan_dir(user_dir, source="user")

    def _scan_dir(self, directory: Path, *, source: str) -> None:
        if not directory.exists() or not directory.is_dir():
            return
        for path in sorted(directory.glob("*.py")):
            if not path.is_file():
                continue
            if path.name == "__init__.py":
                continue
            self._import_one(path, source=source)

    def _import_one(self, path: Path, *, source: str) -> None:
        if source == "user":
            print(
                f"successor: loading user tool {path}",
                file=sys.stderr,
            )

        # Use a unique module name so multiple imports of the same path
        # don't collide in sys.modules. Tools loaded by successor live in
        # the synthetic `_successor_tools` namespace.
        mod_name = f"_successor_tools.{source}.{path.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        if spec is None or spec.loader is None:
            self._warn(f"failed to create import spec for {path}")
            return

        # Drain any leftover _PENDING entries from prior imports so we
        # only collect tools registered by THIS file's import.
        before = len(_PENDING)
        try:
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            self._warn(f"skipping {source} tool {path.name}: {exc}")
            # Roll back any partial registrations from a half-imported file
            del _PENDING[before:]
            return

        new_tools = _PENDING[before:]
        del _PENDING[before:]

        for t in new_tools:
            stamped = Tool(
                name=t.name,
                description=t.description,
                schema=t.schema,
                func=t.func,
                source_path=str(path.resolve()),
            )
            # User tools win on collision because we load them after
            # built-ins (same precedence as themes/profiles/skills).
            self._items[stamped.name] = stamped
            self._sources[stamped.name] = source

    def _warn(self, message: str) -> None:
        print(f"successor: {message}", file=sys.stderr)

    # ─── Read API ───

    @property
    def kind(self) -> str:
        return "tools"

    def get(self, name: str) -> Tool | None:
        if not self._loaded:
            self.load()
        return self._items.get(name)

    def has(self, name: str) -> bool:
        if not self._loaded:
            self.load()
        return name in self._items

    def all(self) -> list[Tool]:
        if not self._loaded:
            self.load()
        return list(self._items.values())

    def names(self) -> list[str]:
        if not self._loaded:
            self.load()
        return list(self._items.keys())

    def source_of(self, name: str) -> str | None:
        if not self._loaded:
            self.load()
        return self._sources.get(name)

    def __iter__(self):
        if not self._loaded:
            self.load()
        return iter(self._items.values())

    def __len__(self) -> int:
        if not self._loaded:
            self.load()
        return len(self._items)

    def __contains__(self, name: object) -> bool:
        if not isinstance(name, str):
            return False
        if not self._loaded:
            self.load()
        return name in self._items


# ─── Singleton ───


TOOL_REGISTRY = ToolRegistry()


def get_tool(name: str) -> Tool | None:
    return TOOL_REGISTRY.get(name)


def all_tools() -> list[Tool]:
    return TOOL_REGISTRY.all()
