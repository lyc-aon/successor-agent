"""Optional Playwright-backed browser tool."""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..bash.cards import ToolCard
from ..tool_runner import ToolExecutionResult, ToolProgress
from .config import BrowserConfig


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


@dataclass(frozen=True, slots=True)
class BrowserRuntimeStatus:
    package_available: bool
    channel: str
    executable_path: str
    user_data_dir: str


@dataclass(frozen=True, slots=True)
class BrowserRequest:
    arguments: dict[str, Any]
    done: threading.Event
    result: list[ToolExecutionResult]


def browser_preview_card(arguments: dict[str, Any], *, tool_call_id: str) -> ToolCard:
    action = str(arguments.get("action", "") or "").strip().lower() or "browser"
    target = str(arguments.get("target", "") or arguments.get("url", "") or "").strip()
    text = str(arguments.get("text", "") or "").strip()
    params: list[tuple[str, str]] = [("action", action)]
    if target:
        params.append(("target", target))
    if text:
        params.append(("text", text[:48] + ("…" if len(text) > 48 else "")))
    raw = " ".join(bit for bit in (action, target or text) if bit)
    return ToolCard(
        verb=_verb_for_action(action),
        params=tuple(params),
        risk="safe",
        raw_command=raw,
        confidence=1.0,
        parser_name="native-browser",
        tool_name="browser",
        tool_arguments={
            key: value
            for key, value in arguments.items()
            if value not in (None, "", False)
        },
        raw_label_prefix="◉",
        tool_call_id=tool_call_id,
    )


def browser_runtime_status(profile_name: str, config: BrowserConfig) -> BrowserRuntimeStatus:
    return BrowserRuntimeStatus(
        package_available=playwright_available(),
        channel=config.channel,
        executable_path=config.resolved_executable_path(),
        user_data_dir=str(config.resolved_user_data_dir(profile_name)),
    )


class PlaywrightBrowserManager:
    """Single persistent Playwright session owned by one worker thread."""

    def __init__(self, *, profile_name: str, config: BrowserConfig) -> None:
        self._profile_name = profile_name
        self._config = config
        self._queue: queue.Queue[BrowserRequest | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()

    def submit(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        if not playwright_available():
            return ToolExecutionResult(
                stderr=(
                    "Playwright Python package is not installed. Install with "
                    "`pip install 'successor[browser]'` or point this profile at "
                    "an environment where Playwright is already available."
                ),
                exit_code=1,
            )
        self._ensure_thread()
        req = BrowserRequest(arguments=dict(arguments), done=threading.Event(), result=[])
        self._queue.put(req)
        req.done.wait()
        return req.result[0]

    def close(self) -> None:
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join(timeout=2.0)
        self._thread = None

    def _ensure_thread(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"browser-manager-{self._profile_name}",
        )
        self._thread.start()
        self._started.wait(timeout=5.0)

    def _run(self) -> None:
        from playwright.sync_api import sync_playwright

        context = None
        page = None
        console_errors: list[str] = []
        self._started.set()
        try:
            with sync_playwright() as playwright:
                launch_kwargs: dict[str, Any] = {
                    "headless": self._config.headless,
                    "viewport": {
                        "width": self._config.viewport_width,
                        "height": self._config.viewport_height,
                    },
                }
                if self._config.channel.strip():
                    launch_kwargs["channel"] = self._config.channel.strip()
                executable_path = self._config.resolved_executable_path()
                if executable_path:
                    launch_kwargs["executable_path"] = executable_path
                user_data_dir = self._config.resolved_user_data_dir(self._profile_name)
                user_data_dir.mkdir(parents=True, exist_ok=True)
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    **launch_kwargs,
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(self._config.timeout_s * 1000.0)
                page.on(
                    "console",
                    lambda msg: console_errors.append(
                        f"{msg.type}: {msg.text}" if msg.type == "error" else ""
                    ),
                )
                page.on(
                    "pageerror",
                    lambda exc: console_errors.append(f"pageerror: {exc}"),
                )

                while True:
                    req = self._queue.get()
                    if req is None:
                        return
                    req.result.append(
                        self._handle_request(page, req.arguments, console_errors, user_data_dir)
                    )
                    req.done.set()
        except Exception as exc:  # noqa: BLE001
            while True:
                try:
                    req = self._queue.get_nowait()
                except queue.Empty:
                    break
                if req is None:
                    continue
                req.result.append(
                    ToolExecutionResult(
                        stderr=f"browser manager failed: {type(exc).__name__}: {exc}",
                        exit_code=1,
                    )
                )
                req.done.set()
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass

    def _handle_request(
        self,
        page: Any,
        arguments: dict[str, Any],
        console_errors: list[str],
        user_data_dir: Path,
    ) -> ToolExecutionResult:
        action = str(arguments.get("action", "") or "").strip().lower()
        try:
            if action == "open":
                url = str(arguments.get("url", "") or "").strip()
                if not url:
                    raise RuntimeError("open requires a url")
                page.goto(url, wait_until="domcontentloaded")
                return ToolExecutionResult(output=_page_snapshot("Opened page.", page))
            if action == "click":
                locator = _resolve_locator(page, arguments)
                locator.click()
                page.wait_for_timeout(250)
                return ToolExecutionResult(output=_page_snapshot("Clicked target.", page))
            if action == "type":
                locator = _resolve_locator(page, arguments, prefer_inputs=True)
                text = str(arguments.get("text", "") or "")
                locator.fill(text)
                if bool(arguments.get("press_enter", False)):
                    locator.press("Enter")
                page.wait_for_timeout(150)
                return ToolExecutionResult(output=_page_snapshot("Typed into target.", page))
            if action == "wait_for":
                locator = _resolve_locator(page, arguments)
                locator.wait_for(state="visible")
                return ToolExecutionResult(output=_page_snapshot("Target became visible.", page))
            if action == "extract_text":
                target = str(arguments.get("target", "") or "").strip()
                if target:
                    locator = _resolve_locator(page, arguments)
                    text = locator.inner_text()
                else:
                    text = _best_page_text(page)
                return ToolExecutionResult(
                    output="\n".join(
                        [
                            "Extracted text from the current page.",
                            f"URL: {page.url}",
                            "",
                            text.strip()[:4000],
                        ]
                    )
                )
            if action == "screenshot":
                path = str(arguments.get("path", "") or "").strip()
                if path:
                    screenshot_path = Path(os.path.expanduser(os.path.expandvars(path)))
                else:
                    shots_dir = user_data_dir / "artifacts"
                    shots_dir.mkdir(parents=True, exist_ok=True)
                    screenshot_path = shots_dir / f"screenshot-{int(time.time() * 1000)}.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                return ToolExecutionResult(
                    output="\n".join(
                        [
                            "Captured browser screenshot.",
                            f"URL: {page.url}",
                            f"Path: {screenshot_path}",
                        ]
                    )
                )
            if action == "console_errors":
                errors = [entry for entry in console_errors if entry]
                if not errors:
                    return ToolExecutionResult(output="No console errors recorded for the current page.")
                return ToolExecutionResult(
                    output="\n".join(
                        ["Console errors:"] + [f"  {idx + 1}. {entry}" for idx, entry in enumerate(errors[-10:])]
                    )
                )
            raise RuntimeError(
                "unsupported browser action. Use open, click, type, wait_for, "
                "extract_text, screenshot, or console_errors."
            )
        except Exception as exc:  # noqa: BLE001
            screenshot_note = ""
            if self._config.screenshot_on_error:
                try:
                    shots_dir = user_data_dir / "artifacts"
                    shots_dir.mkdir(parents=True, exist_ok=True)
                    failure_path = shots_dir / f"failure-{int(time.time() * 1000)}.png"
                    page.screenshot(path=str(failure_path), full_page=True)
                    screenshot_note = f"\nScreenshot: {failure_path}"
                except Exception:
                    screenshot_note = ""
            return ToolExecutionResult(
                stderr=(
                    f"browser action failed: {type(exc).__name__}: {exc}{screenshot_note}"
                ),
                exit_code=1,
            )


def run_browser_action(
    arguments: dict[str, Any],
    *,
    manager: PlaywrightBrowserManager,
    progress: ToolProgress | None = None,
) -> ToolExecutionResult:
    if progress is not None:
        action = str(arguments.get("action", "") or "").strip().lower() or "browser"
        progress.stdout(f"browser: {action}")
    return manager.submit(arguments)


def _page_snapshot(prefix: str, page: Any) -> str:
    title = page.title().strip()
    text = _best_page_text(page)[:1200].strip()
    lines = [prefix, f"URL: {page.url}"]
    if title:
        lines.append(f"Title: {title}")
    if text:
        lines.extend(["", text])
    return "\n".join(lines)


def _best_page_text(page: Any) -> str:
    try:
        main = page.locator("main")
        if main.count() > 0:
            text = main.first.inner_text()
            if text.strip():
                return text
    except Exception:
        pass
    try:
        return page.locator("body").inner_text()
    except Exception:
        return ""


def _resolve_locator(page: Any, arguments: dict[str, Any], *, prefer_inputs: bool = False):
    target = str(arguments.get("target", "") or "").strip()
    if not target:
        raise RuntimeError("browser action requires a target")
    kind = str(arguments.get("target_kind", "auto") or "auto").strip().lower()
    if kind == "selector":
        return page.locator(target).first
    if kind == "label":
        return page.get_by_label(target, exact=False).first
    if kind == "placeholder":
        return page.get_by_placeholder(target, exact=False).first
    if kind == "text":
        return page.get_by_text(target, exact=False).first

    candidates = []
    if prefer_inputs:
        candidates.extend(
            [
                page.get_by_label(target, exact=False),
                page.get_by_placeholder(target, exact=False),
                page.get_by_role("textbox", name=target, exact=False),
            ]
        )
    candidates.extend(
        [
            page.get_by_role("button", name=target, exact=False),
            page.get_by_role("link", name=target, exact=False),
            page.get_by_text(target, exact=False),
        ]
    )
    if _looks_like_selector(target):
        candidates.append(page.locator(target))
    for locator in candidates:
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    raise RuntimeError(f"could not find target {target!r}")


def _looks_like_selector(text: str) -> bool:
    return (
        text.startswith(("#", ".", "//", "["))
        or ">" in text
        or ":" in text
        or text.startswith("text=")
    )


def _verb_for_action(action: str) -> str:
    return {
        "open": "browser-open",
        "click": "browser-click",
        "type": "browser-type",
        "wait_for": "browser-wait",
        "extract_text": "browser-read",
        "screenshot": "browser-screenshot",
        "console_errors": "browser-console",
    }.get(action, "browser-open")
