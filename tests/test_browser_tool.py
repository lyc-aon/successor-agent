"""Browser tool unit coverage without requiring a live Playwright run."""

from __future__ import annotations

from successor.web.browser import (
    BrowserRuntimeStatus,
    PlaywrightBrowserManager,
    _fill_locator_with_fallback,
    _resolve_type_locator,
    browser_preview_card,
    browser_runtime_status,
)
from successor.web.config import BrowserConfig


def test_browser_preview_card_uses_browser_metadata() -> None:
    card = browser_preview_card(
        {"action": "open", "url": "http://127.0.0.1:4173"},
        tool_call_id="call_browser_1",
    )

    assert card.tool_name == "browser"
    assert card.tool_arguments["action"] == "open"
    assert card.raw_label_prefix == "◉"
    assert card.tool_call_id == "call_browser_1"


def test_browser_manager_surfaces_missing_playwright(monkeypatch) -> None:
    monkeypatch.setattr(
        "successor.web.browser.playwright_runtime",
        lambda config: None,  # noqa: ARG005
    )
    manager = PlaywrightBrowserManager(
        profile_name="browser-test",
        config=BrowserConfig(),
    )
    result = manager.submit({"action": "open", "url": "https://example.com"})

    assert result.exit_code == 1
    assert "Playwright" in result.stderr


def test_browser_runtime_status_surfaces_external_python(monkeypatch) -> None:
    monkeypatch.setattr(
        "successor.web.browser.playwright_runtime",
        lambda config: type(
            "_Runtime",
            (),
            {"python_executable": "/usr/bin/python3", "in_process": False},
        )(),
    )
    status = browser_runtime_status("browser-test", BrowserConfig())

    assert status.package_available is True
    assert status.python_executable == "/usr/bin/python3"
    assert status.using_external_runtime is True


class _FakeLocator:
    def __init__(self, *, count_value: int = 1, fill_error: Exception | None = None) -> None:
        self._count_value = count_value
        self._fill_error = fill_error
        self.filled: list[str] = []
        self.first = self

    def count(self) -> int:
        return self._count_value

    def fill(self, text: str) -> None:
        if self._fill_error is not None:
            raise self._fill_error
        self.filled.append(text)


class _FakePage:
    def __init__(self, mapping: dict[str, _FakeLocator]) -> None:
        self._mapping = mapping

    def get_by_label(self, target: str, *, exact: bool = False):  # noqa: ARG002
        return self._mapping.get(f"label:{target}", _FakeLocator(count_value=0))

    def get_by_placeholder(self, target: str, *, exact: bool = False):  # noqa: ARG002
        return self._mapping.get(f"placeholder:{target}", _FakeLocator(count_value=0))

    def get_by_role(self, role: str, *, name: str, exact: bool = False):  # noqa: ARG002
        return self._mapping.get(f"role:{role}:{name}", _FakeLocator(count_value=0))

    def get_by_text(self, target: str, *, exact: bool = False):  # noqa: ARG002
        return self._mapping.get(f"text:{target}", _FakeLocator(count_value=0))

    def locator(self, target: str):
        return self._mapping.get(f"selector:{target}", _FakeLocator(count_value=0))


def test_resolve_type_locator_falls_back_from_missing_placeholder() -> None:
    label = _FakeLocator(count_value=1)
    placeholder = _FakeLocator(count_value=0)
    page = _FakePage({
        "label:Message": label,
        "placeholder:Message": placeholder,
        "role:textbox:Message": _FakeLocator(count_value=0),
        "text:Message": _FakeLocator(count_value=0),
    })

    locator = _resolve_type_locator(
        page,
        {"target": "Message", "target_kind": "placeholder"},
    )

    assert locator is label


def test_fill_locator_with_fallback_retries_auto_resolution() -> None:
    label = _FakeLocator(count_value=1)
    placeholder = _FakeLocator(
        count_value=1,
        fill_error=RuntimeError("placeholder lookup failed"),
    )
    page = _FakePage({
        "label:Message": label,
        "placeholder:Message": placeholder,
        "role:textbox:Message": _FakeLocator(count_value=0),
        "text:Message": _FakeLocator(count_value=0),
    })

    locator = _fill_locator_with_fallback(
        page,
        {"target": "Message", "target_kind": "placeholder"},
        locator=placeholder,
        text="successor",
    )

    assert locator is label
    assert label.filled == ["successor"]
