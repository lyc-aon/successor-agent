"""External Playwright helper process for browser.py.

Runs under a Python interpreter that already has the Playwright package
installed, while the main Successor process may not.
"""

from __future__ import annotations

import json
import sys

from .browser import _execute_browser_action, _launch_browser_context
from .config import BrowserConfig


def main() -> int:
    startup_line = sys.stdin.readline()
    if not startup_line:
        return 1
    try:
        startup = json.loads(startup_line)
    except json.JSONDecodeError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": f"invalid startup json: {exc}"}) + "\n")
        sys.stdout.flush()
        return 1
    if startup.get("op") != "start":
        sys.stdout.write(json.dumps({"ok": False, "error": "expected startup op"}) + "\n")
        sys.stdout.flush()
        return 1

    profile_name = str(startup.get("profile_name") or "default")
    raw_cfg = startup.get("config") or {}
    try:
        config = BrowserConfig(
            headless=bool(raw_cfg.get("headless", True)),
            channel=str(raw_cfg.get("channel", "chrome") or "chrome"),
            python_executable=str(raw_cfg.get("python_executable", "") or ""),
            executable_path=str(raw_cfg.get("executable_path", "") or ""),
            user_data_dir=str(raw_cfg.get("user_data_dir", "") or ""),
            viewport_width=int(raw_cfg.get("viewport_width", 1440)),
            viewport_height=int(raw_cfg.get("viewport_height", 960)),
            timeout_s=float(raw_cfg.get("timeout_s", 20.0)),
            screenshot_on_error=bool(raw_cfg.get("screenshot_on_error", True)),
        )
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "error": f"invalid browser config: {exc}"}) + "\n")
        sys.stdout.flush()
        return 1

    from playwright.sync_api import sync_playwright

    console_errors: list[str] = []
    context = None
    try:
        with sync_playwright() as playwright:
            context, page, user_data_dir = _launch_browser_context(
                playwright=playwright,
                config=config,
                profile_name=profile_name,
                console_errors=console_errors,
            )
            sys.stdout.write(json.dumps({"ok": True}) + "\n")
            sys.stdout.flush()
            while True:
                line = sys.stdin.readline()
                if not line:
                    return 0
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    sys.stdout.write(json.dumps({
                        "output": "",
                        "stderr": f"invalid helper request json: {exc}",
                        "exit_code": 1,
                        "truncated": False,
                    }) + "\n")
                    sys.stdout.flush()
                    continue
                if payload.get("op") == "close":
                    return 0
                if payload.get("op") != "action":
                    sys.stdout.write(json.dumps({
                        "output": "",
                        "stderr": "unsupported helper op",
                        "exit_code": 1,
                        "truncated": False,
                    }) + "\n")
                    sys.stdout.flush()
                    continue
                result = _execute_browser_action(
                    page=page,
                    arguments=dict(payload.get("arguments") or {}),
                    console_errors=console_errors,
                    user_data_dir=user_data_dir,
                    config=config,
                )
                sys.stdout.write(json.dumps({
                    "output": result.output,
                    "stderr": result.stderr,
                    "exit_code": result.exit_code,
                    "truncated": result.truncated,
                    "metadata": result.metadata or {},
                }) + "\n")
                sys.stdout.flush()
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ok": False, "error": f"browser helper failed: {type(exc).__name__}: {exc}"}) + "\n")
        sys.stdout.flush()
        return 1
    finally:
        try:
            if context is not None:
                context.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
