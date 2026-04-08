"""Browser tool unit coverage without requiring a live Playwright run."""

from __future__ import annotations

from successor.web.browser import (
    PlaywrightBrowserManager,
    browser_preview_card,
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
        "successor.web.browser.playwright_available",
        lambda: False,
    )
    manager = PlaywrightBrowserManager(
        profile_name="browser-test",
        config=BrowserConfig(),
    )
    result = manager.submit({"action": "open", "url": "https://example.com"})

    assert result.exit_code == 1
    assert "Playwright" in result.stderr
