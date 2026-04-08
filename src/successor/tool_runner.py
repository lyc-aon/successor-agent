"""Generic native-tool runner for non-bash async work.

Holonet and browser actions need the same non-blocking UI contract as
bash: background execution on a worker thread, live status updates, and
a final ToolCard once the work settles. This runner keeps that contract
without pretending every tool is a subprocess.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .bash.cards import ToolCard
from .bash.runner import (
    OutputLine,
    RunnerCompleted,
    RunnerErrored,
    RunnerEvent,
    RunnerStarted,
)


@dataclass(slots=True, frozen=True)
class ToolExecutionResult:
    """Final result payload returned by a native tool worker."""

    output: str = ""
    stderr: str = ""
    exit_code: int = 0
    truncated: bool = False
    final_card: ToolCard | None = None


class ToolProgress:
    """Thread-safe progress sink exposed to worker callables."""

    def __init__(self, runner: "CallableToolRunner") -> None:
        self._runner = runner

    def stdout(self, text: str) -> None:
        self._runner._append_output("stdout", text)

    def stderr(self, text: str) -> None:
        self._runner._append_output("stderr", text)


class CallableToolRunner:
    """Run an arbitrary callable behind the BashRunner-like contract."""

    def __init__(
        self,
        *,
        tool_call_id: str,
        worker: Callable[[ToolProgress], ToolExecutionResult],
    ) -> None:
        self.tool_call_id = tool_call_id
        self._worker_fn = worker

        self._queue: queue.Queue[RunnerEvent] = queue.Queue()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._buf_lock = threading.Lock()
        self._stdout_buf: list[str] = []
        self._stderr_buf: list[str] = []
        self._started_at: float = 0.0
        self._completed_at: float = 0.0
        self._exit_code: int | None = None
        self._error: str = ""
        self._truncated: bool = False
        self._final_card: ToolCard | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = time.monotonic()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"tool-runner-{self.tool_call_id[:12]}",
        )
        self._thread.start()

    def drain(self) -> list[RunnerEvent]:
        out: list[RunnerEvent] = []
        try:
            while True:
                out.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        return out

    def is_done(self) -> bool:
        return self._done.is_set()

    def cancel(self) -> None:
        # Generic workers are cooperative; browser actions currently
        # serialize quickly and holonet requests are bounded by network
        # timeouts, so there is no forcible cancellation path yet.
        return

    @property
    def stdout(self) -> str:
        with self._buf_lock:
            return "".join(self._stdout_buf)

    @property
    def stderr(self) -> str:
        with self._buf_lock:
            return "".join(self._stderr_buf)

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def error(self) -> str:
        return self._error

    @property
    def pid(self) -> int | None:
        return None

    @property
    def started_at(self) -> float:
        return self._started_at

    def elapsed(self, now: float | None = None) -> float:
        if self._started_at == 0.0:
            return 0.0
        if self._completed_at > 0.0:
            return self._completed_at - self._started_at
        return (now if now is not None else time.monotonic()) - self._started_at

    def build_final_card(self, preview: ToolCard) -> ToolCard:
        if self._final_card is not None:
            return self._final_card
        from dataclasses import replace

        return replace(
            preview,
            output=self.stdout,
            stderr=self.stderr,
            exit_code=self._exit_code if self._exit_code is not None else -1,
            duration_ms=self.elapsed() * 1000.0,
            truncated=self._truncated,
        )

    def _append_output(self, channel: str, text: str) -> None:
        if not text:
            return
        with self._buf_lock:
            target = self._stdout_buf if channel == "stdout" else self._stderr_buf
            target.append(text if text.endswith("\n") else text + "\n")
        self._queue.put(OutputLine(channel=channel, text=text))

    def _run(self) -> None:
        self._queue.put(RunnerStarted())
        progress = ToolProgress(self)
        try:
            result = self._worker_fn(progress)
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            self._queue.put(RunnerErrored(self._error))
            self._exit_code = -1
        else:
            self._truncated = bool(result.truncated)
            self._exit_code = int(result.exit_code)
            self._final_card = result.final_card
            if result.output:
                with self._buf_lock:
                    self._stdout_buf = [result.output]
            if result.stderr:
                with self._buf_lock:
                    self._stderr_buf = [result.stderr]
        finally:
            self._completed_at = time.monotonic()
            self._queue.put(
                RunnerCompleted(
                    exit_code=self._exit_code if self._exit_code is not None else -1,
                    duration_ms=self.elapsed() * 1000.0,
                    truncated=self._truncated,
                    error=self._error,
                )
            )
            self._done.set()
