"""Tests for the generic Registry pattern in loader.py.

Uses real temp dirs and real files via the temp_config_dir fixture.
Each test exercises one specific behavior of the registry: built-in
loading, user dir loading, name collision precedence, broken file
skipping, idempotent load, and the various read APIs.

The Registry doesn't know what a theme/profile/skill IS — it takes a
parser callable. Tests use a tiny dummy dataclass so they only exercise
the registry behavior, not theme parsing (that's tested in
test_theme_loader.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from successor.loader import Registry, builtin_root, config_dir


# ─── Tiny test fixture type ───


@dataclass(frozen=True)
class _DummyItem:
    """Stand-in for a real registry item — just a name and a payload."""
    name: str
    payload: str


def _parse_dummy(path: Path) -> _DummyItem | None:
    """Parser that reads {"name": ..., "payload": ...} JSON files.

    Returns None for files that don't have a 'name' field — that's the
    "silently skip" path that's used for README.md and similar dropped
    in alongside real config files.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("not a dict")
    if "name" not in data:
        return None
    return _DummyItem(name=data["name"], payload=data.get("payload", ""))


def _make_registry() -> Registry[_DummyItem]:
    return Registry[_DummyItem](
        kind="dummies",
        file_glob="*.json",
        parser=_parse_dummy,
        description="dummy",
    )


# ─── config_dir / builtin_root resolution ───


def test_config_dir_honors_env_var(temp_config_dir: Path) -> None:
    """config_dir() must return the path set by SUCCESSOR_CONFIG_DIR."""
    assert config_dir() == temp_config_dir


def test_builtin_root_points_at_package() -> None:
    """builtin_root() lives inside the installed successor package."""
    root = builtin_root()
    assert root.name == "builtin"
    assert root.parent.name == "successor"


# ─── load() — basic walking ───


def test_load_empty_user_dir(temp_config_dir: Path) -> None:
    """A registry over a kind with no user dir loads silently."""
    reg = _make_registry()
    reg.load()
    # No dummies/ subdirectory exists in builtin/ or the temp user dir,
    # so the registry is empty.
    assert len(reg) == 0
    assert reg.all() == []


def test_load_user_files(temp_config_dir: Path) -> None:
    """User files in the matching kind dir are picked up."""
    user_dummies = temp_config_dir / "dummies"
    user_dummies.mkdir()
    (user_dummies / "alpha.json").write_text(
        json.dumps({"name": "alpha", "payload": "first"})
    )
    (user_dummies / "beta.json").write_text(
        json.dumps({"name": "beta", "payload": "second"})
    )

    reg = _make_registry()
    reg.load()

    assert len(reg) == 2
    assert "alpha" in reg
    assert "beta" in reg
    alpha = reg.get("alpha")
    assert alpha is not None
    assert alpha.payload == "first"


def test_load_skips_non_matching_glob(temp_config_dir: Path) -> None:
    """Files that don't match file_glob are not parsed."""
    user_dummies = temp_config_dir / "dummies"
    user_dummies.mkdir()
    (user_dummies / "alpha.json").write_text(json.dumps({"name": "alpha"}))
    (user_dummies / "README.md").write_text("# notes")
    (user_dummies / "ignored.txt").write_text("not json")

    reg = _make_registry()
    reg.load()

    assert len(reg) == 1
    assert reg.has("alpha")


def test_load_skips_subdirectories(temp_config_dir: Path) -> None:
    """The loader is shallow — it doesn't recurse into subdirs."""
    user_dummies = temp_config_dir / "dummies"
    nested = user_dummies / "nested"
    nested.mkdir(parents=True)
    (nested / "ignored.json").write_text(json.dumps({"name": "ignored"}))
    (user_dummies / "top.json").write_text(json.dumps({"name": "top"}))

    reg = _make_registry()
    reg.load()

    assert reg.names() == ["top"]


# ─── Parser failures ───


def test_load_skips_broken_file_with_warning(
    temp_config_dir: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A file that raises in the parser is skipped with a stderr warning."""
    user_dummies = temp_config_dir / "dummies"
    user_dummies.mkdir()
    (user_dummies / "good.json").write_text(json.dumps({"name": "good"}))
    (user_dummies / "broken.json").write_text("this is { not valid json")

    reg = _make_registry()
    reg.load()

    # The good file loaded; the broken one was skipped.
    assert len(reg) == 1
    assert reg.has("good")
    assert not reg.has("broken")

    # The warning landed on stderr.
    captured = capsys.readouterr()
    assert "successor:" in captured.err
    assert "broken.json" in captured.err


def test_load_skips_parser_returning_none(temp_config_dir: Path) -> None:
    """parser returning None means 'silently skip' — no warning."""
    user_dummies = temp_config_dir / "dummies"
    user_dummies.mkdir()
    (user_dummies / "real.json").write_text(json.dumps({"name": "real"}))
    # Valid JSON without a 'name' field — parser returns None.
    (user_dummies / "metadata.json").write_text(json.dumps({"foo": "bar"}))

    reg = _make_registry()
    reg.load()

    assert reg.names() == ["real"]


def test_load_skips_item_with_missing_name(
    temp_config_dir: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """An item that comes back from the parser without a name is skipped."""
    # Custom parser that returns a dummy with empty name (a misuse to
    # exercise the registry's defensive check).
    def buggy_parser(path: Path) -> _DummyItem:
        return _DummyItem(name="", payload="oops")

    reg = Registry[_DummyItem](
        kind="dummies",
        file_glob="*.json",
        parser=buggy_parser,
        description="dummy",
    )
    user_dummies = temp_config_dir / "dummies"
    user_dummies.mkdir()
    (user_dummies / "anything.json").write_text("{}")

    reg.load()

    assert len(reg) == 0
    captured = capsys.readouterr()
    assert "missing or invalid 'name'" in captured.err


# ─── User wins on collision ───


def test_user_overrides_builtin_on_name_collision(
    temp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When user file and builtin share a name, user wins.

    Simulates this by creating a temp builtin dir and pointing the
    loader at it via a custom Registry that overrides _do_load.
    """
    # Create a temp builtin dir + user dir, both with a "shared" item.
    builtin_dir = temp_config_dir / "_builtin"
    user_dummies = temp_config_dir / "dummies"
    builtin_dummies = builtin_dir / "dummies"
    builtin_dummies.mkdir(parents=True)
    user_dummies.mkdir()

    (builtin_dummies / "shared.json").write_text(
        json.dumps({"name": "shared", "payload": "from builtin"})
    )
    (user_dummies / "shared.json").write_text(
        json.dumps({"name": "shared", "payload": "from user"})
    )

    # Patch builtin_root for this test only.
    import successor.loader as loader_mod
    monkeypatch.setattr(loader_mod, "builtin_root", lambda: builtin_dir)

    reg = _make_registry()
    reg.load()

    item = reg.get("shared")
    assert item is not None
    assert item.payload == "from user"
    assert reg.source_of("shared") == "user"


def test_builtin_wins_when_no_user_file(
    temp_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A builtin-only item is still loaded when the user dir is empty."""
    builtin_dir = temp_config_dir / "_builtin"
    builtin_dummies = builtin_dir / "dummies"
    builtin_dummies.mkdir(parents=True)
    (builtin_dummies / "core.json").write_text(
        json.dumps({"name": "core", "payload": "from builtin"})
    )

    import successor.loader as loader_mod
    monkeypatch.setattr(loader_mod, "builtin_root", lambda: builtin_dir)

    reg = _make_registry()
    reg.load()

    item = reg.get("core")
    assert item is not None
    assert item.payload == "from builtin"
    assert reg.source_of("core") == "builtin"


# ─── Idempotence + reload ───


def test_load_is_idempotent(temp_config_dir: Path) -> None:
    """Calling load() twice doesn't double-load or change state."""
    user_dummies = temp_config_dir / "dummies"
    user_dummies.mkdir()
    (user_dummies / "x.json").write_text(json.dumps({"name": "x"}))

    reg = _make_registry()
    reg.load()
    first = reg.all()
    reg.load()
    second = reg.all()

    assert first == second
    assert len(reg) == 1


def test_reload_picks_up_new_files(temp_config_dir: Path) -> None:
    """reload() forces a fresh scan, finding files added since last load."""
    user_dummies = temp_config_dir / "dummies"
    user_dummies.mkdir()
    (user_dummies / "first.json").write_text(json.dumps({"name": "first"}))

    reg = _make_registry()
    reg.load()
    assert reg.names() == ["first"]

    # Drop a new file in after the initial load.
    (user_dummies / "second.json").write_text(json.dumps({"name": "second"}))

    reg.reload()
    assert sorted(reg.names()) == ["first", "second"]


def test_reload_drops_removed_files(temp_config_dir: Path) -> None:
    """reload() forgets files that have been deleted since last load."""
    user_dummies = temp_config_dir / "dummies"
    user_dummies.mkdir()
    target = user_dummies / "ephemeral.json"
    target.write_text(json.dumps({"name": "ephemeral"}))

    reg = _make_registry()
    reg.load()
    assert reg.has("ephemeral")

    target.unlink()
    reg.reload()
    assert not reg.has("ephemeral")


# ─── Read API ───


def test_get_or_raise_lists_available_on_miss(temp_config_dir: Path) -> None:
    """get_or_raise's KeyError mentions every available name."""
    user_dummies = temp_config_dir / "dummies"
    user_dummies.mkdir()
    (user_dummies / "a.json").write_text(json.dumps({"name": "a"}))
    (user_dummies / "b.json").write_text(json.dumps({"name": "b"}))

    reg = _make_registry()
    reg.load()

    with pytest.raises(KeyError) as excinfo:
        reg.get_or_raise("missing")
    msg = str(excinfo.value)
    assert "missing" in msg
    assert "a" in msg
    assert "b" in msg


def test_iteration_returns_loaded_items(temp_config_dir: Path) -> None:
    user_dummies = temp_config_dir / "dummies"
    user_dummies.mkdir()
    (user_dummies / "x.json").write_text(json.dumps({"name": "x"}))
    (user_dummies / "y.json").write_text(json.dumps({"name": "y"}))

    reg = _make_registry()
    reg.load()

    seen = sorted(item.name for item in reg)
    assert seen == ["x", "y"]


def test_contains_uses_string_key(temp_config_dir: Path) -> None:
    user_dummies = temp_config_dir / "dummies"
    user_dummies.mkdir()
    (user_dummies / "thing.json").write_text(json.dumps({"name": "thing"}))

    reg = _make_registry()
    reg.load()

    assert "thing" in reg
    assert "missing" not in reg
    # Non-string keys never match (silently False).
    assert 42 not in reg  # type: ignore[operator]


def test_get_auto_loads(temp_config_dir: Path) -> None:
    """get() implicitly triggers load() on first access."""
    user_dummies = temp_config_dir / "dummies"
    user_dummies.mkdir()
    (user_dummies / "auto.json").write_text(json.dumps({"name": "auto"}))

    reg = _make_registry()
    # No explicit reg.load() call — get() should trigger it.
    item = reg.get("auto")
    assert item is not None
    assert item.name == "auto"
