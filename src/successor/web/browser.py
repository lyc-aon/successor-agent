"""Optional Playwright-backed browser tool."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
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


@dataclass(slots=True)
class _BrowserProgressTracker:
    """Track repeated failures and no-op browser actions within one session."""

    last_action: str = ""
    last_target: str = ""
    last_state_hash: str = ""
    stagnant_repeats: int = 0
    last_failure_signature: tuple[str, str] | None = None
    repeat_failures: int = 0
    last_controls_summary: str = ""

    def annotate(
        self,
        arguments: dict[str, Any],
        result: ToolExecutionResult,
    ) -> ToolExecutionResult:
        action = str(arguments.get("action", "") or "").strip().lower()
        target = str(arguments.get("target", "") or arguments.get("url", "") or "").strip()
        metadata = result.metadata or {}
        state_hash = str(metadata.get("state_hash", "") or "")
        controls_summary = str(metadata.get("controls_summary", "") or "").strip()
        if controls_summary:
            self.last_controls_summary = controls_summary

        if result.exit_code != 0:
            signature = (action, target)
            if signature == self.last_failure_signature:
                self.repeat_failures += 1
            else:
                self.repeat_failures = 1
                self.last_failure_signature = signature
            if self.repeat_failures < 2:
                return result
            note = (
                "\nProgress note: this browser action has failed repeatedly. "
                "Stop retrying the same step. Call `inspect` to list the "
                "actual visible controls and selector hints, or switch strategy."
            )
            if self.last_controls_summary:
                note = f"{note}\n\n{self.last_controls_summary}"
            return replace(result, stderr=(result.stderr or "").rstrip() + note)

        self.repeat_failures = 0
        self.last_failure_signature = None

        repeated_open = (
            action == "open"
            and state_hash
            and self.last_action == "open"
            and target == self.last_target
            and state_hash == self.last_state_hash
        )
        self.last_action = action
        self.last_target = target

        if repeated_open:
            note = (
                "\nProgress note: you reopened the same page and got the same "
                "state back. Reuse the current browser session unless a code "
                "edit, storage reset, or explicit reload is actually required."
            )
            if controls_summary:
                note = f"{note}\n\n{controls_summary}"
            return replace(result, output=(result.output or "").rstrip() + note)

        if action not in {"click", "type", "press", "select", "wait_for"} or not state_hash:
            if state_hash:
                self.last_state_hash = state_hash
            self.stagnant_repeats = 0
            return result

        if state_hash == self.last_state_hash:
            self.stagnant_repeats += 1
        else:
            self.stagnant_repeats = 0
        self.last_state_hash = state_hash

        if self.stagnant_repeats < 2:
            return result

        note = (
            "\nProgress note: page state has not meaningfully changed across "
            "the last 3 browser actions. Stop exploratory clicking. Use "
            "`inspect` to see visible controls and stable selectors before "
            "trying again."
        )
        if controls_summary:
            note = f"{note}\n\n{controls_summary}"
        return replace(result, output=(result.output or "").rstrip() + note)


def browser_preview_card(arguments: dict[str, Any], *, tool_call_id: str) -> ToolCard:
    action = str(arguments.get("action", "") or "").strip().lower() or "browser"
    target = str(arguments.get("target", "") or arguments.get("url", "") or "").strip()
    text = str(arguments.get("text", "") or "").strip()
    option = str(arguments.get("option", "") or "").strip()
    key = str(arguments.get("key", "") or "").strip()
    scope = str(arguments.get("scope", "") or "").strip()
    params: list[tuple[str, str]] = [("action", action)]
    if target:
        params.append(("target", target))
    if text:
        params.append(("text", text[:48] + ("…" if len(text) > 48 else "")))
    if option:
        params.append(("option", option[:48] + ("…" if len(option) > 48 else "")))
    if key:
        params.append(("key", key))
    if scope:
        params.append(("scope", scope))
    raw = " ".join(bit for bit in (action, target or text, option, key, scope) if bit)
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
                metadata=(
                    dict(payload.get("metadata") or {})
                    if isinstance(payload.get("metadata"), dict)
                    else None
                ),
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
        self._submit_lock = threading.RLock()
        self._progress = _BrowserProgressTracker()

    def submit(self, arguments: dict[str, Any]) -> ToolExecutionResult:
        with self._submit_lock:
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
                result = self._bridge.submit(arguments)
                return self._progress.annotate(arguments, result)
            self._ensure_thread()
            req = BrowserRequest(arguments=dict(arguments), done=threading.Event(), result=[])
            self._queue.put(req)
            req.done.wait()
            return self._progress.annotate(arguments, req.result[0])

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
            return _page_result("Opened page.", page, include_controls=True)
        if action == "inspect":
            return _page_result("Inspected page.", page, include_controls=True)
        if action == "click":
            locator = _resolve_locator(page, arguments)
            locator.click()
            page.wait_for_timeout(250)
            return _page_result("Clicked target.", page)
        if action == "type":
            text = str(arguments.get("text", "") or "")
            if _has_target(arguments):
                locator = _resolve_type_locator(page, arguments)
                locator = _fill_locator_with_fallback(
                    page,
                    arguments,
                    locator=locator,
                    text=text,
                )
                if bool(arguments.get("press_enter", False)):
                    locator.press("Enter")
                prefix = "Typed into target."
            else:
                _type_into_focused_target(
                    page,
                    text,
                    press_enter=bool(arguments.get("press_enter", False)),
                )
                prefix = "Typed into the focused target."
            page.wait_for_timeout(150)
            return _page_result(prefix, page)
        if action == "press":
            key = str(arguments.get("key", "") or "").strip()
            if not key:
                raise RuntimeError("press requires a key")
            if _has_target(arguments):
                locator = _resolve_locator(page, arguments, prefer_inputs=True)
                locator.press(key)
                prefix = f"Pressed {key} on target."
            else:
                page.keyboard.press(key)
                prefix = f"Pressed {key}."
            page.wait_for_timeout(150)
            return _page_result(prefix, page)
        if action == "select":
            locator = _resolve_locator(page, arguments, prefer_inputs=True)
            option = str(arguments.get("option", "") or arguments.get("text", "") or "").strip()
            if not option:
                raise RuntimeError("select requires an option")
            _select_locator_option(locator, option)
            page.wait_for_timeout(150)
            return _page_result("Selected option.", page)
        if action == "storage_state":
            return ToolExecutionResult(
                output=_storage_state_summary(page),
                metadata=_page_state_metadata(page),
            )
        if action == "clear_storage":
            scope = str(arguments.get("scope", "") or "both").strip().lower()
            if scope not in {"local", "session", "both"}:
                raise RuntimeError("clear_storage scope must be local, session, or both")
            _clear_page_storage(page, scope)
            page.wait_for_timeout(100)
            return _page_result(f"Cleared {scope} storage.", page)
        if action == "wait_for":
            locator = _resolve_locator(page, arguments)
            locator.wait_for(state="visible")
            return _page_result("Target became visible.", page)
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
                ),
                metadata=_page_state_metadata(page),
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
                ),
                metadata=_page_state_metadata(page),
            )
        if action == "console_errors":
            errors = [entry for entry in console_errors if entry]
            if not errors:
                return ToolExecutionResult(
                    output="No console errors recorded for the current page.",
                    metadata=_page_state_metadata(page),
                )
            return ToolExecutionResult(
                output="\n".join(
                    ["Console errors:"] + [f"  {idx + 1}. {entry}" for idx, entry in enumerate(errors[-10:])]
                ),
                metadata=_page_state_metadata(page),
            )
        raise RuntimeError(
            "unsupported browser action. Use open, inspect, click, type, "
            "press, select, storage_state, clear_storage, wait_for, extract_text, "
            "screenshot, or console_errors."
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


def _page_result(prefix: str, page: Any, *, include_controls: bool = False) -> ToolExecutionResult:
    metadata = _page_state_metadata(page)
    output = _page_snapshot(prefix, page)
    controls_summary = str(metadata.get("controls_summary", "") or "").strip()
    if include_controls and controls_summary:
        output = f"{output}\n\n{controls_summary}"
    return ToolExecutionResult(output=output, metadata=metadata)


def _page_snapshot(prefix: str, page: Any) -> str:
    title = page.title().strip()
    text = _best_page_text(page)[:1200].strip()
    lines = [prefix, f"URL: {page.url}"]
    if title:
        lines.append(f"Title: {title}")
    if text:
        lines.extend(["", text])
    return "\n".join(lines)


def _page_state_metadata(page: Any) -> dict[str, Any]:
    title = page.title().strip()
    text = _best_page_text(page)[:1200].strip()
    controls_summary = _visible_controls_summary(page)
    digest = hashlib.sha1(
        "\n".join((page.url, title, text, controls_summary)).encode("utf-8")
    ).hexdigest()[:12]
    return {
        "state_hash": digest,
        "controls_summary": controls_summary,
    }


def _visible_controls_summary(page: Any, *, limit: int = 8) -> str:
    controls = _visible_controls(page, limit=limit)
    if not controls:
        return ""
    lines = ["Visible controls:"]
    for item in controls:
        role = str(item.get("role") or item.get("tag") or "element")
        label = str(item.get("label") or "").strip()
        selector = str(item.get("selector") or "").strip()
        extras: list[str] = []
        if label:
            extras.append(f'"{label}"')
        if selector:
            extras.append(f"selector={selector}")
        value = str(item.get("value") or "").strip()
        if value:
            extras.append(f'value="{value[:60]}"')
        options = item.get("options")
        if isinstance(options, list) and options:
            extras.append("options=" + " | ".join(str(opt) for opt in options[:4]))
        lines.append(f"- {role}: {'; '.join(extras) if extras else '(no label)'}")
    return "\n".join(lines)


def _visible_controls(page: Any, *, limit: int = 8) -> list[dict[str, Any]]:
    try:
        result = page.evaluate(
            """(limit) => { /* successor-visible-controls */
                const isVisible = (el) => {
                    if (!(el instanceof Element)) return false;
                    const style = window.getComputedStyle(el);
                    if (!style || style.display === "none" || style.visibility === "hidden") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                };
                const clean = (value) => String(value || "").replace(/\\s+/g, " ").trim();
                const selectorFor = (el) => {
                    if (el.id) return `#${el.id}`;
                    const testId = el.getAttribute("data-testid");
                    if (testId) return `[data-testid="${testId}"]`;
                    const name = el.getAttribute("name");
                    if (name) return `${el.tagName.toLowerCase()}[name="${name}"]`;
                    const cls = Array.from(el.classList || []).filter(Boolean).slice(0, 2);
                    if (cls.length) return `${el.tagName.toLowerCase()}.${cls.join(".")}`;
                    const text = clean(el.innerText || el.textContent || "");
                    if (text) return `${el.tagName.toLowerCase()}:has-text("${text.slice(0, 40)}")`;
                    return el.tagName.toLowerCase();
                };
                const labelFor = (el) => {
                    return clean(
                        el.getAttribute("aria-label")
                        || el.getAttribute("placeholder")
                        || el.innerText
                        || el.textContent
                        || el.value
                    );
                };
                const optionsFor = (el) => {
                    if (el.tagName.toLowerCase() !== "select") return [];
                    return Array.from(el.options || []).map((opt) => clean(opt.textContent)).filter(Boolean).slice(0, 6);
                };
                const valueFor = (el) => {
                    const tag = el.tagName.toLowerCase();
                    if (tag === "select") {
                        const selected = el.selectedOptions && el.selectedOptions[0];
                        return clean(selected ? selected.textContent : el.value);
                    }
                    if (tag === "input" || tag === "textarea") {
                        return clean(el.value);
                    }
                    if (el.isContentEditable) {
                        return clean(el.innerText || el.textContent || "");
                    }
                    return "";
                };
                const nodes = Array.from(document.querySelectorAll(
                    'input, textarea, select, button, a, [role="button"], [contenteditable="true"], summary'
                ));
                const seen = new Set();
                const out = [];
                for (const el of nodes) {
                    if (!isVisible(el)) continue;
                    const selector = selectorFor(el);
                    const dedupe = `${el.tagName}|${selector}|${labelFor(el)}`;
                    if (seen.has(dedupe)) continue;
                    seen.add(dedupe);
                    const tag = el.tagName.toLowerCase();
                    const explicitRole = clean(el.getAttribute("role"));
                    let role = explicitRole || tag;
                    if (!explicitRole && tag === "input") {
                        role = clean(el.getAttribute("type")) || "textbox";
                    }
                    out.push({
                        tag,
                        role,
                        label: labelFor(el),
                        selector,
                        value: valueFor(el),
                        options: optionsFor(el),
                    });
                    if (out.length >= limit) break;
                }
                return out;
            }""",
            limit,
        )
    except Exception:
        return []
    if not isinstance(result, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in result[:limit]:
        if not isinstance(item, dict):
            continue
        normalized.append({
            "tag": str(item.get("tag") or "").strip(),
            "role": str(item.get("role") or "").strip(),
            "label": str(item.get("label") or "").strip(),
            "selector": str(item.get("selector") or "").strip(),
            "value": str(item.get("value") or "").strip(),
            "options": list(item.get("options") or [])[:6],
        })
    return normalized


def _storage_state_summary(page: Any, *, value_limit: int = 120) -> str:
    state = _storage_state(page, value_limit=value_limit)
    lines = ["Browser storage state.", f"URL: {page.url}", ""]
    for title, items in (
        ("Local storage", state.get("local", [])),
        ("Session storage", state.get("session", [])),
    ):
        lines.append(f"{title}:")
        if not items:
            lines.append("- (empty)")
        else:
            for item in items:
                key = str(item.get("key") or "").strip()
                value = str(item.get("value") or "").strip()
                lines.append(f'- {key}="{value}"')
        lines.append("")
    return "\n".join(lines).rstrip()


def _storage_state(page: Any, *, value_limit: int = 120) -> dict[str, list[dict[str, str]]]:
    try:
        payload = page.evaluate(
            """(valueLimit) => { /* successor-storage-state */
                const readStorage = (storage) => {
                    const out = [];
                    for (let i = 0; i < storage.length; i += 1) {
                        const key = storage.key(i);
                        if (!key) continue;
                        const raw = storage.getItem(key) ?? "";
                        out.push({
                            key,
                            value: String(raw).slice(0, valueLimit),
                        });
                    }
                    return out;
                };
                return {
                    local: readStorage(window.localStorage),
                    session: readStorage(window.sessionStorage),
                };
            }""",
            value_limit,
        )
    except Exception:
        return {"local": [], "session": []}
    if not isinstance(payload, dict):
        return {"local": [], "session": []}
    out: dict[str, list[dict[str, str]]] = {"local": [], "session": []}
    for scope in ("local", "session"):
        raw_items = payload.get(scope)
        if not isinstance(raw_items, list):
            continue
        items: list[dict[str, str]] = []
        for item in raw_items[:20]:
            if not isinstance(item, dict):
                continue
            items.append({
                "key": str(item.get("key") or "").strip(),
                "value": str(item.get("value") or "").strip(),
            })
        out[scope] = items
    return out


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


def _has_target(arguments: dict[str, Any]) -> bool:
    return bool(str(arguments.get("target", "") or "").strip())


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


def _active_element_is_editable(page: Any) -> bool:
    try:
        return bool(page.evaluate(
            """() => {
                const el = document.activeElement;
                if (!el) return false;
                if (el.isContentEditable) return true;
                const tag = (el.tagName || "").toLowerCase();
                if (tag === "textarea") return true;
                if (tag !== "input") return false;
                const type = (el.getAttribute("type") || "text").toLowerCase();
                return ![
                    "button",
                    "checkbox",
                    "color",
                    "file",
                    "hidden",
                    "image",
                    "radio",
                    "range",
                    "reset",
                    "submit",
                ].includes(type);
            }"""
        ))
    except Exception:
        return False


def _type_into_focused_target(page: Any, text: str, *, press_enter: bool) -> None:
    if not _active_element_is_editable(page):
        raise RuntimeError("type requires a target or a focused editable element")
    page.keyboard.type(text)
    if press_enter:
        page.keyboard.press("Enter")


def _clear_page_storage(page: Any, scope: str) -> None:
    page.evaluate(
        """(scope) => { /* successor-clear-storage */
            if (scope === "local" || scope === "both") {
                window.localStorage.clear();
            }
            if (scope === "session" || scope === "both") {
                window.sessionStorage.clear();
            }
        }""",
        scope,
    )


def _select_locator_option(locator: Any, option: str) -> None:
    """Select an option by visible label first, then by raw value."""
    try:
        locator.select_option(label=option)
        return
    except Exception:
        pass
    locator.select_option(value=option)


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
        "inspect": "browser-inspect",
        "click": "browser-click",
        "type": "browser-type",
        "press": "browser-press",
        "select": "browser-select",
        "storage_state": "browser-storage-read",
        "clear_storage": "browser-storage-clear",
        "wait_for": "browser-wait",
        "extract_text": "browser-read",
        "screenshot": "browser-screenshot",
        "console_errors": "browser-console",
    }.get(action, "browser-open")
