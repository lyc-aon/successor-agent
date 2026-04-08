"""Optional Playwright-backed browser tool."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
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
class BrowserPythonRuntime:
    python_executable: str
    in_process: bool


@dataclass(frozen=True, slots=True)
class BrowserRuntimeStatus:
    package_available: bool
    python_executable: str
    using_external_runtime: bool
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


def _normalize_python_executable(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    expanded = os.path.expanduser(os.path.expandvars(text))
    if os.path.isabs(expanded):
        return expanded
    return shutil.which(expanded) or expanded


def _python_has_playwright(python_executable: str) -> bool:
    if not python_executable:
        return False
    try:
        proc = subprocess.run(
            [python_executable, "-c", "import playwright.sync_api"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


@lru_cache(maxsize=16)
def _detect_playwright_runtime(explicit_python: str) -> BrowserPythonRuntime | None:
    candidates: list[str] = []
    if explicit_python:
        candidates.append(_normalize_python_executable(explicit_python))
    current_python = _normalize_python_executable(sys.executable)
    candidates.append(current_python)
    candidates.append(_normalize_python_executable("python3"))

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if not _python_has_playwright(candidate):
            continue
        in_process = candidate == current_python and playwright_available()
        return BrowserPythonRuntime(
            python_executable=candidate,
            in_process=in_process,
        )
    return None


def playwright_runtime(config: BrowserConfig) -> BrowserPythonRuntime | None:
    return _detect_playwright_runtime(config.python_executable.strip())


def browser_runtime_status(profile_name: str, config: BrowserConfig) -> BrowserRuntimeStatus:
    runtime = playwright_runtime(config)
    return BrowserRuntimeStatus(
        package_available=runtime is not None,
        python_executable=(
            runtime.python_executable
            if runtime is not None else
            config.resolved_python_executable()
        ),
        using_external_runtime=bool(runtime and not runtime.in_process),
        channel=config.channel,
        executable_path=config.resolved_executable_path(),
        user_data_dir=str(config.resolved_user_data_dir(profile_name)),
    )


def _browser_config_payload(config: BrowserConfig) -> dict[str, Any]:
    return {
        "headless": config.headless,
        "channel": config.channel,
        "python_executable": config.python_executable,
        "executable_path": config.executable_path,
        "user_data_dir": config.user_data_dir,
        "viewport_width": config.viewport_width,
        "viewport_height": config.viewport_height,
        "timeout_s": config.timeout_s,
        "screenshot_on_error": config.screenshot_on_error,
    }


class _PlaywrightBridgeProcess:
    """Persistent external Python helper that owns the Playwright session."""

    def __init__(
        self,
        *,
        profile_name: str,
        config: BrowserConfig,
        runtime: BrowserPythonRuntime,
    ) -> None:
        self._profile_name = profile_name
        self._config = config
        self._runtime = runtime
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()

    def submit(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        with self._lock:
            try:
                self._ensure_proc()
            except Exception as exc:  # noqa: BLE001
                self.close()
                return ToolExecutionResult(
                    stderr=f"browser helper failed to start: {type(exc).__name__}: {exc}",
                    exit_code=1,
                )
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdout is None:
                return ToolExecutionResult(
                    stderr="browser helper is unavailable.",
                    exit_code=1,
                )
            try:
                proc.stdin.write(json.dumps({
                    "op": "action",
                    "arguments": dict(arguments),
                }) + "\n")
                proc.stdin.flush()
                line = proc.stdout.readline()
            except Exception as exc:  # noqa: BLE001
                self.close()
                return ToolExecutionResult(
                    stderr=f"browser helper communication failed: {type(exc).__name__}: {exc}",
                    exit_code=1,
                )
            if not line:
                self.close()
                return ToolExecutionResult(
                    stderr="browser helper exited before returning a result.",
                    exit_code=1,
                )
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                self.close()
                return ToolExecutionResult(
                    stderr=f"browser helper returned invalid JSON: {exc}",
                    exit_code=1,
                )
            return ToolExecutionResult(
                output=str(payload.get("output", "") or ""),
                stderr=str(payload.get("stderr", "") or ""),
                exit_code=int(payload.get("exit_code", 0) or 0),
                truncated=bool(payload.get("truncated", False)),
            )

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            if proc is None:
                return
            try:
                if proc.stdin is not None:
                    proc.stdin.write(json.dumps({"op": "close"}) + "\n")
                    proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _ensure_proc(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        env = os.environ.copy()
        repo_src = str(Path(__file__).resolve().parents[2])
        cur_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            repo_src if not cur_pythonpath else f"{repo_src}{os.pathsep}{cur_pythonpath}"
        )
        self._proc = subprocess.Popen(
            [
                self._runtime.python_executable,
                "-m",
                "successor.web.browser_helper",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        self._proc.stdin.write(json.dumps({
            "op": "start",
            "profile_name": self._profile_name,
            "config": _browser_config_payload(self._config),
        }) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            stderr = ""
            if self._proc.stderr is not None:
                try:
                    stderr = self._proc.stderr.read().strip()
                except Exception:
                    stderr = ""
            raise RuntimeError(stderr or "no startup acknowledgement from browser helper")
        payload = json.loads(line)
        if not payload.get("ok", False):
            raise RuntimeError(str(payload.get("error", "browser helper refused startup")))


class PlaywrightBrowserManager:
    """Single persistent Playwright session owned by one worker thread."""

    def __init__(self, *, profile_name: str, config: BrowserConfig) -> None:
        self._profile_name = profile_name
        self._config = config
        self._runtime = playwright_runtime(config)
        self._queue: queue.Queue[BrowserRequest | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._bridge: _PlaywrightBridgeProcess | None = None

    def submit(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        if self._runtime is None:
            return ToolExecutionResult(
                stderr=(
                    "Playwright Python package is not available in the configured "
                    "runtime. Install with `pip install 'successor[browser]'`, or "
                    "set browser.python_executable to a Python interpreter that "
                    "already has Playwright installed."
                ),
                exit_code=1,
            )
        if not self._runtime.in_process:
            if self._bridge is None:
                self._bridge = _PlaywrightBridgeProcess(
                    profile_name=self._profile_name,
                    config=self._config,
                    runtime=self._runtime,
                )
            return self._bridge.submit(arguments)
        self._ensure_thread()
        req = BrowserRequest(arguments=dict(arguments), done=threading.Event(), result=[])
        self._queue.put(req)
        req.done.wait()
        return req.result[0]

    def close(self) -> None:
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None
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
                context, page, user_data_dir = _launch_browser_context(
                    playwright=playwright,
                    config=self._config,
                    profile_name=self._profile_name,
                    console_errors=console_errors,
                )
                while True:
                    req = self._queue.get()
                    if req is None:
                        return
                    req.result.append(
                        _execute_browser_action(
                            page=page,
                            arguments=req.arguments,
                            console_errors=console_errors,
                            user_data_dir=user_data_dir,
                            config=self._config,
                        )
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


def _launch_browser_context(
    *,
    playwright: Any,
    config: BrowserConfig,
    profile_name: str,
    console_errors: list[str],
) -> tuple[Any, Any, Path]:
    launch_kwargs: dict[str, Any] = {
        "headless": config.headless,
        "viewport": {
            "width": config.viewport_width,
            "height": config.viewport_height,
        },
    }
    if config.channel.strip():
        launch_kwargs["channel"] = config.channel.strip()
    executable_path = config.resolved_executable_path()
    if executable_path:
        launch_kwargs["executable_path"] = executable_path
    user_data_dir = config.resolved_user_data_dir(profile_name)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        **launch_kwargs,
    )
    page = context.pages[0] if context.pages else context.new_page()
    page.set_default_timeout(config.timeout_s * 1000.0)
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
    return context, page, user_data_dir


def _execute_browser_action(
    *,
    page: Any,
    arguments: dict[str, Any],
    console_errors: list[str],
    user_data_dir: Path,
    config: BrowserConfig,
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
            locator = _resolve_type_locator(page, arguments)
            text = str(arguments.get("text", "") or "")
            locator = _fill_locator_with_fallback(
                page,
                arguments,
                locator=locator,
                text=text,
            )
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
        if config.screenshot_on_error:
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


def _resolve_type_locator(page: Any, arguments: dict[str, Any]):
    """Resolve a type target, allowing explicit human-target fallbacks.

    Models sometimes over-specify `target_kind="placeholder"` or
    `target_kind="label"` when the other human-readable route is the
    real match. For typing, prefer a quick fallback to the standard
    input-candidate cascade over burning the full Playwright timeout.
    """
    kind = str(arguments.get("target_kind", "auto") or "auto").strip().lower()
    locator = _resolve_locator(page, arguments, prefer_inputs=True)
    if kind not in {"label", "placeholder", "text"}:
        return locator
    if _locator_has_matches(locator):
        return locator
    retry_args = dict(arguments)
    retry_args["target_kind"] = "auto"
    return _resolve_locator(page, retry_args, prefer_inputs=True)


def _fill_locator_with_fallback(
    page: Any,
    arguments: dict[str, Any],
    *,
    locator: Any,
    text: str,
):
    """Fill a locator, retrying once through auto input resolution.

    This keeps the browser tool tolerant of slightly wrong human-target
    hints while still respecting explicit CSS selectors.
    """
    kind = str(arguments.get("target_kind", "auto") or "auto").strip().lower()
    try:
        locator.fill(text)
        return locator
    except Exception:
        if kind not in {"label", "placeholder", "text"}:
            raise
        retry_args = dict(arguments)
        retry_args["target_kind"] = "auto"
        retry_locator = _resolve_locator(page, retry_args, prefer_inputs=True)
        if retry_locator is locator:
            raise
        retry_locator.fill(text)
        return retry_locator


def _locator_has_matches(locator: Any) -> bool:
    try:
        return bool(locator.count() > 0)
    except Exception:
        return False


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
