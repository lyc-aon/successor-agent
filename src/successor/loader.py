"""The Registry pattern — built-in dir + user dir, user wins on collision.

Six places in Successor need the same loading shape:

  themes      JSON files in builtin/themes/ and ~/.config/successor/themes/
  profiles    JSON files in builtin/profiles/ and ~/.config/successor/profiles/
  skills      Markdown files in builtin/skills/ and ~/.config/successor/skills/
  tools       Python files in builtin/tools/ and ~/.config/successor/tools/
  intros      Frame directories in builtin/intros/ and ~/.config/successor/intros/
  (anything else with a "name" field that ships built-in and accepts user override)

This module provides ONE generic Registry[T] that all of them reuse, with
exactly one precedence rule: built-ins load first, then user files, and a
user file with the same `name` as a built-in wins. Broken files are skipped
with a stderr warning so a single corrupt file never prevents the harness
from starting.

Two design decisions worth knowing about:

1. Loader is hermetic-testable via the same `SUCCESSOR_CONFIG_DIR` env var that
   `config.py` already uses. Tests point it at a temp dir, drop fixture
   files into the temp dir, and the loader picks them up — no mocking, no
   filesystem stubs, no special test paths.

2. Parsers are passed in by the caller. This module knows nothing about
   JSON, markdown frontmatter, Python imports, or any specific schema —
   the caller supplies a `Path -> T | None` callable that does the parsing
   and validation. A return of None means "skip this file silently"
   (e.g. a README.md in a themes/ directory). A raised exception means
   "skip this file noisily" (a malformed JSON theme).

The Registry instance is the public surface: callers hold a Registry,
call `load()` once at startup, then use `get(name)` or iterate. There's
no global state inside this module.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Generic, Iterator, Protocol, TypeVar


# ─── Config dir resolution ───
#
# Mirrors `config.py:_config_dir` but lives here too so the loader can be
# imported without pulling in the chat config code path. The two functions
# MUST stay in sync — both honor SUCCESSOR_CONFIG_DIR for hermetic tests.

CONFIG_DIR_ENV = "SUCCESSOR_CONFIG_DIR"
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "successor"


def config_dir() -> Path:
    """Resolve the user config directory, honoring $SUCCESSOR_CONFIG_DIR."""
    env = os.environ.get(CONFIG_DIR_ENV)
    if env:
        return Path(env)
    return DEFAULT_CONFIG_DIR


def builtin_root() -> Path:
    """Return the path to the package's `builtin/` directory.

    Walks up from this file (`src/successor/loader.py`) to find `src/successor/`,
    then descends into `builtin/`. This works for both `pip install -e .`
    development installs and regular installs because the package layout
    is the same in both.
    """
    return Path(__file__).resolve().parent / "builtin"


# ─── Named protocol ───
#
# Anything we put in a Registry must have a `name` attribute so we can
# look it up and apply the user-wins-on-collision rule.

class Named(Protocol):
    name: str


T = TypeVar("T", bound=Named)


# ─── Registry ───


class Registry(Generic[T]):
    """A generic loader for one kind of thing (themes, profiles, skills, etc).

    Builds a `dict[str, T]` from two source directories — a built-in dir
    inside the package and a user dir under `~/.config/successor/`. The two
    are loaded in order: built-in first, user second. Items with the same
    name overwrite (so user files win).

    Use:
        themes = Registry[Theme](
            kind="themes",
            file_glob="*.json",
            parser=parse_theme_file,
            description="theme",
        )
        themes.load()
        steel = themes.get("steel")
    """

    __slots__ = (
        "kind",
        "file_glob",
        "parser",
        "description",
        "_items",
        "_sources",
        "_loaded",
    )

    def __init__(
        self,
        *,
        kind: str,
        file_glob: str,
        parser: Callable[[Path], T | None],
        description: str | None = None,
    ) -> None:
        """
        kind:         subdirectory name under both builtin/ and the user
                      config dir (e.g. "themes", "profiles", "skills")
        file_glob:    pattern that matches the files we should try to parse
                      (e.g. "*.json", "*.md", "*.py")
        parser:       Path -> T | None. Returns None to silently skip a
                      file (it isn't an error — it's a non-match). Raises
                      to noisily skip with a stderr warning.
        description:  human-readable singular noun for stderr messages,
                      defaults to kind without the trailing 's' (themes ->
                      "theme"). Used in "skipping {description} {path}".
        """
        self.kind = kind
        self.file_glob = file_glob
        self.parser = parser
        self.description = description or kind.rstrip("s")
        self._items: dict[str, T] = {}
        # Tracks where each loaded item came from ("builtin" or "user").
        # Useful for `successor themes list` etc to show source labels.
        self._sources: dict[str, str] = {}
        self._loaded = False

    # ─── Loading ───

    def load(self) -> None:
        """Walk built-in dir then user dir, parsing every match.

        Idempotent: calling load() twice doesn't reload — use reload() to
        force a fresh scan. This matters for setup wizards that create a
        new theme/profile mid-session and want to see it immediately.
        """
        if self._loaded:
            return
        self._do_load()
        self._loaded = True

    def reload(self) -> None:
        """Force a fresh scan, dropping any items currently in the registry."""
        self._items.clear()
        self._sources.clear()
        self._loaded = False
        self.load()

    def _do_load(self) -> None:
        builtin_dir = builtin_root() / self.kind
        user_dir = config_dir() / self.kind

        # Built-ins first so user files override on collision.
        self._scan_dir(builtin_dir, source="builtin")
        self._scan_dir(user_dir, source="user")

    def _scan_dir(self, directory: Path, *, source: str) -> None:
        if not directory.exists() or not directory.is_dir():
            return
        # sorted() so the load order is stable for tests and for any
        # error messages we emit. Glob is shallow — we don't recurse
        # into subdirs because Successor's registries are flat by design.
        for path in sorted(directory.glob(self.file_glob)):
            if not path.is_file():
                continue
            self._add(path, source=source)

    def _add(self, path: Path, *, source: str) -> None:
        try:
            item = self.parser(path)
        except Exception as exc:
            self._warn(f"skipping {source} {self.description} {path.name}: {exc}")
            return
        if item is None:
            return
        name = getattr(item, "name", None)
        if not isinstance(name, str) or not name:
            self._warn(
                f"skipping {source} {self.description} {path.name}: "
                f"missing or invalid 'name' field"
            )
            return
        # User files always win on name collision because we load
        # built-ins first. The collision is intentional — users override
        # built-ins by giving their file the same name.
        self._items[name] = item
        self._sources[name] = source

    def _warn(self, message: str) -> None:
        """Emit a stderr warning. Routed through a method so tests can
        capture it via the standard `capsys` fixture without poking
        directly at sys.stderr."""
        print(f"successor: {message}", file=sys.stderr)

    # ─── Read API ───

    def get(self, name: str) -> T | None:
        """Look up an item by name. Returns None if missing.

        Auto-loads on first access if load() hasn't been called yet —
        a small convenience that lets simple callers skip the explicit
        load() step. Tests should still call load() explicitly so the
        load order is deterministic relative to fixture creation.
        """
        if not self._loaded:
            self.load()
        return self._items.get(name)

    def get_or_raise(self, name: str) -> T:
        """Look up an item by name. Raises KeyError with the available
        names listed if missing. Use this when a caller knows the name
        must be valid (e.g. resolved from a profile).
        """
        item = self.get(name)
        if item is None:
            available = ", ".join(sorted(self._items.keys())) or "(none)"
            raise KeyError(
                f"no {self.description} named '{name}'. "
                f"available: {available}"
            )
        return item

    def has(self, name: str) -> bool:
        if not self._loaded:
            self.load()
        return name in self._items

    def all(self) -> list[T]:
        """All items in load order (built-ins first, then user, sorted by
        filename within each source)."""
        if not self._loaded:
            self.load()
        return list(self._items.values())

    def names(self) -> list[str]:
        """All registered names. Useful for completers and listings."""
        if not self._loaded:
            self.load()
        return list(self._items.keys())

    def source_of(self, name: str) -> str | None:
        """Return 'builtin' or 'user' for a registered name, or None
        if the name is not in the registry."""
        if not self._loaded:
            self.load()
        return self._sources.get(name)

    def __iter__(self) -> Iterator[T]:
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
