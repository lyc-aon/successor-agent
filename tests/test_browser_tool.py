"""Browser tool unit coverage without requiring a live Playwright run."""

from __future__ import annotations

from successor.tool_runner import ToolExecutionResult
from successor.web.browser import (
    BrowserRuntimeStatus,
    PlaywrightBrowserManager,
    _BrowserProgressTracker,
    _execute_browser_action,
    _fill_locator_with_fallback,
    _resolve_type_locator,
    _select_locator_option,
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
        self.pressed: list[str] = []
        self._inner_text = "Issue Desk"
        self.first = self

    def count(self) -> int:
        return self._count_value

    def fill(self, text: str) -> None:
        if self._fill_error is not None:
            raise self._fill_error
        self.filled.append(text)

    def press(self, key: str) -> None:
        self.pressed.append(key)

    def inner_text(self) -> str:
        return self._inner_text


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


class _FakeKeyboard:
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.pressed: list[str] = []

    def type(self, text: str) -> None:
        self.typed.append(text)

    def press(self, key: str) -> None:
        self.pressed.append(key)


class _FakeActionPage(_FakePage):
    def __init__(
        self,
        mapping: dict[str, _FakeLocator] | None = None,
        *,
        active_editable: bool = True,
        visible_controls: list[dict[str, object]] | None = None,
        local_storage: dict[str, str] | None = None,
        session_storage: dict[str, str] | None = None,
    ) -> None:
        super().__init__(mapping or {})
        self.keyboard = _FakeKeyboard()
        self.url = "file:///tmp/issue-desk/index.html"
        self._title = "Issue Desk"
        self._active_editable = active_editable
        self._visible_controls = visible_controls or [
            {"tag": "input", "role": "textbox", "label": "Search issues...", "selector": "#search", "value": "", "options": []},
            {"tag": "select", "role": "select", "label": "Status filter", "selector": "#status-filter", "value": "All", "options": ["All", "Open", "Closed"]},
            {"tag": "button", "role": "button", "label": "Add Issue", "selector": "#add-issue", "value": "", "options": []},
        ]
        self.local_storage = dict(local_storage or {})
        self.session_storage = dict(session_storage or {})
        self.waits: list[int] = []
        self._mapping.setdefault("selector:main", _FakeLocator())
        self._mapping.setdefault("selector:body", _FakeLocator())
        self._mapping["selector:main"]._inner_text = "Issue Desk main content"
        self._mapping["selector:body"]._inner_text = "Issue Desk body content"

    def title(self) -> str:
        return self._title

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)

    def evaluate(self, script: str, *args):
        if "document.activeElement" in script:
            return self._active_editable
        if "successor-clear-storage" in script:
            scope = args[0] if args else "both"
            if scope in ("local", "both"):
                self.local_storage.clear()
            if scope in ("session", "both"):
                self.session_storage.clear()
            return None
        if "successor-storage-state" in script:
            return {
                "local": [
                    {"key": key, "value": value}
                    for key, value in self.local_storage.items()
                ],
                "session": [
                    {"key": key, "value": value}
                    for key, value in self.session_storage.items()
                ],
            }
        limit = int(args[0]) if args else len(self._visible_controls)
        return self._visible_controls[:limit]


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


class _FakeSelectLocator(_FakeLocator):
    def __init__(self) -> None:
        super().__init__(count_value=1)
        self.selected: list[tuple[str, str]] = []
        self.fail_label = False

    def select_option(self, *, label: str | None = None, value: str | None = None):
        if label is not None:
            if self.fail_label:
                raise RuntimeError("label not found")
            self.selected.append(("label", label))
            return
        if value is not None:
            self.selected.append(("value", value))
            return
        raise RuntimeError("missing selection")


def test_select_locator_option_prefers_label_then_value() -> None:
    locator = _FakeSelectLocator()
    _select_locator_option(locator, "Closed")
    assert locator.selected == [("label", "Closed")]


def test_select_locator_option_falls_back_to_value() -> None:
    locator = _FakeSelectLocator()
    locator.fail_label = True
    _select_locator_option(locator, "closed")
    assert locator.selected == [("value", "closed")]


def test_type_action_can_use_focused_editable_without_target(tmp_path) -> None:
    page = _FakeActionPage(active_editable=True)

    result = _execute_browser_action(
        page=page,
        arguments={"action": "type", "text": "Keyboard navigation bug", "press_enter": True},
        console_errors=[],
        user_data_dir=tmp_path,
        config=BrowserConfig(),
    )

    assert result.exit_code == 0
    assert page.keyboard.typed == ["Keyboard navigation bug"]
    assert page.keyboard.pressed == ["Enter"]


def test_press_action_without_target_uses_keyboard(tmp_path) -> None:
    page = _FakeActionPage(active_editable=True)

    result = _execute_browser_action(
        page=page,
        arguments={"action": "press", "key": "Escape"},
        console_errors=[],
        user_data_dir=tmp_path,
        config=BrowserConfig(),
    )

    assert result.exit_code == 0
    assert page.keyboard.pressed == ["Escape"]


def test_press_action_with_target_uses_locator_press(tmp_path) -> None:
    locator = _FakeLocator(count_value=1)
    page = _FakeActionPage({"text:Save": locator}, active_editable=False)

    result = _execute_browser_action(
        page=page,
        arguments={"action": "press", "key": "Enter", "target": "Save", "target_kind": "text"},
        console_errors=[],
        user_data_dir=tmp_path,
        config=BrowserConfig(),
    )

    assert result.exit_code == 0
    assert locator.pressed == ["Enter"]


def test_inspect_action_returns_visible_controls_summary(tmp_path) -> None:
    page = _FakeActionPage(
        active_editable=False,
        visible_controls=[
            {"tag": "input", "role": "textbox", "label": "Issue title...", "selector": "#new-title", "value": "Keyboard nav bug", "options": []},
            {"tag": "select", "role": "select", "label": "Priority", "selector": "#new-priority", "value": "High", "options": ["Low", "Medium", "High"]},
        ],
    )

    result = _execute_browser_action(
        page=page,
        arguments={"action": "inspect"},
        console_errors=[],
        user_data_dir=tmp_path,
        config=BrowserConfig(),
    )

    assert result.exit_code == 0
    assert "Visible controls:" in result.output
    assert "#new-title" in result.output
    assert 'value="Keyboard nav bug"' in result.output
    assert 'value="High"' in result.output
    assert result.metadata is not None
    assert result.metadata["controls_summary"].startswith("Visible controls:")


def test_storage_state_action_reports_local_and_session_storage(tmp_path) -> None:
    page = _FakeActionPage(
        active_editable=False,
        local_storage={"issueDeskIssues": "[1,2,3]"},
        session_storage={"draftIssue": "Keyboard nav bug"},
    )

    result = _execute_browser_action(
        page=page,
        arguments={"action": "storage_state"},
        console_errors=[],
        user_data_dir=tmp_path,
        config=BrowserConfig(),
    )

    assert result.exit_code == 0
    assert "Browser storage state." in result.output
    assert 'issueDeskIssues="[1,2,3]"' in result.output
    assert 'draftIssue="Keyboard nav bug"' in result.output


def test_clear_storage_action_clears_requested_scope(tmp_path) -> None:
    page = _FakeActionPage(
        active_editable=False,
        local_storage={"issueDeskIssues": "[1,2,3]"},
        session_storage={"draftIssue": "Keyboard nav bug"},
    )

    local_only = _execute_browser_action(
        page=page,
        arguments={"action": "clear_storage", "scope": "local"},
        console_errors=[],
        user_data_dir=tmp_path,
        config=BrowserConfig(),
    )

    assert local_only.exit_code == 0
    assert page.local_storage == {}
    assert page.session_storage == {"draftIssue": "Keyboard nav bug"}

    both = _execute_browser_action(
        page=page,
        arguments={"action": "clear_storage", "scope": "both"},
        console_errors=[],
        user_data_dir=tmp_path,
        config=BrowserConfig(),
    )

    assert both.exit_code == 0
    assert page.local_storage == {}
    assert page.session_storage == {}


def test_progress_tracker_warns_after_repeated_unchanged_states() -> None:
    tracker = _BrowserProgressTracker()
    result = ToolExecutionResult(
        output="Clicked target.",
        exit_code=0,
        metadata={
            "state_hash": "steady",
            "controls_summary": "Visible controls:\n- textbox: \"Search issues...\"; selector=#search",
        },
    )

    tracker.annotate({"action": "click", "target": "#search"}, result)
    tracker.annotate({"action": "click", "target": "#search"}, result)
    warned = tracker.annotate({"action": "click", "target": "#search"}, result)

    assert "Progress note: page state has not meaningfully changed" in warned.output
    assert "Visible controls:" in warned.output


def test_progress_tracker_warns_after_reopening_same_page_state() -> None:
    tracker = _BrowserProgressTracker()
    result = ToolExecutionResult(
        output="Opened page.",
        exit_code=0,
        metadata={
            "state_hash": "same-page",
            "controls_summary": "Visible controls:\n- button: \"Toggle theme\"; selector=#theme-toggle",
        },
    )

    tracker.annotate({"action": "open", "url": "file:///tmp/app.html"}, result)
    warned = tracker.annotate({"action": "open", "url": "file:///tmp/app.html"}, result)

    assert "Progress note: you reopened the same page" in warned.output
    assert "Visible controls:" in warned.output


def test_progress_tracker_warns_after_repeated_failures() -> None:
    tracker = _BrowserProgressTracker()
    success = ToolExecutionResult(
        output="Opened page.",
        exit_code=0,
        metadata={"state_hash": "page", "controls_summary": "Visible controls:\n- button: \"Add Issue\"; selector=#add-issue"},
    )
    tracker.annotate({"action": "open", "url": "file:///tmp/app.html"}, success)

    failing = ToolExecutionResult(stderr="browser action failed: could not find target", exit_code=1)
    tracker.annotate({"action": "click", "target": "Open"}, failing)
    warned = tracker.annotate({"action": "click", "target": "Open"}, failing)

    assert "Progress note: this browser action has failed repeatedly" in warned.stderr
    assert "Visible controls:" in warned.stderr
