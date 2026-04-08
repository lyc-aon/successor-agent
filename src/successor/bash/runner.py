"""BashRunner — async subprocess executor with line-buffered output capture.

Mirrors the `providers.llama.ChatStream` pattern: a worker thread runs
the slow blocking work (subprocess + pipe reads) while the main thread
polls a queue for events each frame. The chat's tick loop never blocks
on subprocess execution, so the rest of the UI keeps animating: the
spinner stays alive, scrolling stays responsive, the running tool card
itself can pulse and stream output line-by-line as it arrives.

Three threads per runner:

  1. main worker — spawns Popen, waits for exit, owns lifecycle
  2. stdout reader — readline() loop pushing OutputLine events
  3. stderr reader — same for stderr

Events emitted to the queue:

  RunnerStarted()              — first thing after Popen succeeds
  OutputLine(channel, text)    — one stdout/stderr line as it arrives
  RunnerCompleted(...)         — exit code + duration + truncation flag
  RunnerErrored(message)       — exception or timeout

The chat consumes these via `drain()` each tick. Cancellation goes
through `cancel()`, which signals the main worker to terminate the
subprocess (SIGTERM, then SIGKILL after 500ms grace).

Output is capped at MAX_OUTPUT_BYTES (8 KiB by default). Once the
cap is hit, the runner keeps draining the pipes (so the subprocess
doesn't block on a full pipe buffer) but stops emitting OutputLine
events and sets the `truncated` flag on the final RunnerCompleted.

Why a separate type from `dispatch_bash`:
  - dispatch_bash is the SYNCHRONOUS entry point used by tests, by
    burn rigs, by anything that just wants "run this and give me a
    card". It still works because subprocess.run blocks the caller.
  - BashRunner is the ASYNCHRONOUS path used by the chat dispatch
    so the tick loop can keep running. The chat creates one runner
    per tool call, polls them via `drain()`, and finalizes a card
    when the runner is done.
"""

from __future__ import annotations

import os
import queue
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Literal


# ─── Constants ───

# Buffer cap before we start dropping output. The subprocess keeps
# running and we keep draining the pipe so it doesn't block, but the
# extra bytes don't make it into the card.
DEFAULT_MAX_OUTPUT_BYTES: int = 8 * 1024

# Default timeout (seconds). Mirrors dispatch_bash's DEFAULT_TIMEOUT_S.
DEFAULT_TIMEOUT_S: float = 30.0

# How long the worker will spin between poll() calls when waiting for
# the subprocess to exit. Small enough that cancellation feels instant.
_POLL_INTERVAL_S: float = 0.02

# Grace period between SIGTERM and SIGKILL on cancel.
_TERM_GRACE_S: float = 0.5


# ─── Event types ───


Channel = Literal["stdout", "stderr"]


@dataclass(slots=True, frozen=True)
class RunnerStarted:
    """Subprocess has started — Popen succeeded."""
    pass


@dataclass(slots=True, frozen=True)
class OutputLine:
    """One line of output from the subprocess. May not include the
    trailing newline if the subprocess didn't emit one before exit
    (Popen's readline returns the partial line on EOF)."""
    channel: Channel
    text: str


@dataclass(slots=True, frozen=True)
class RunnerCompleted:
    """Subprocess exited cleanly. exit_code may be 0 (success), >0
    (failure), or negative (signal). truncated indicates the byte cap
    was hit. error is set when the worker terminated the process due
    to timeout or cancellation."""
    exit_code: int
    duration_ms: float
    truncated: bool
    error: str = ""


@dataclass(slots=True, frozen=True)
class RunnerErrored:
    """The runner failed before or during subprocess startup —
    Popen raised, environment was bad, etc. The chat surfaces this
    as a synthetic error message."""
    message: str


RunnerEvent = RunnerStarted | OutputLine | RunnerCompleted | RunnerErrored


# ─── BashRunner ───


def _new_call_id() -> str:
    """Generate a synthetic tool_call_id when the caller doesn't
    provide one (legacy paths, /bash slash command, tests)."""
    return "call_" + secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:24]


class BashRunner:
    """One in-flight subprocess execution.

    Lifecycle:
      __init__ stores the configuration but does NOT start anything.
      start()  spawns the worker thread which spawns Popen + readers.
      drain()  pulls all currently-available events (non-blocking).
      is_done() True after the worker thread exits.
      cancel() signals the worker to terminate the subprocess.

    Construction is decoupled from start() so the chat can build the
    runner, attach it to a _Message, then start it after a paint cycle.
    """

    def __init__(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        tool_call_id: str | None = None,
    ) -> None:
        self.command = command
        self._cwd = cwd
        self._env = env
        self._timeout = timeout
        self._max_bytes = max_output_bytes
        self.tool_call_id: str = tool_call_id or _new_call_id()

        self._queue: queue.Queue[RunnerEvent] = queue.Queue()
        self._stop = threading.Event()
        self._done = threading.Event()
        self._worker: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        # Optional before-state capture plan installed by the chat
        # before start(). The worker does not touch it; completion
        # finalization happens in chat.py after the subprocess settles.
        self.change_capture = None

        # Output accumulators visible to the main thread between
        # drain() calls. Both buffers are bounded by _max_bytes total
        # (stdout + stderr combined). The lock guards both lists.
        self._stdout_buf: list[str] = []
        self._stderr_buf: list[str] = []
        self._buf_lock = threading.Lock()
        self._byte_count: int = 0  # combined bytes accepted into buffers
        self._truncated: bool = False

        # Runtime stats published once the worker finishes.
        self._started_at: float = 0.0
        self._completed_at: float = 0.0
        self._exit_code: int | None = None
        self._error: str = ""

    # ─── Public driving API ───

    def start(self) -> None:
        """Spawn the worker thread. Returns immediately."""
        if self._worker is not None:
            return  # already started
        self._started_at = time.monotonic()
        self._worker = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"bash-runner-{self.tool_call_id[:12]}",
        )
        self._worker.start()

    def drain(self) -> list[RunnerEvent]:
        """Pull all currently-available events. Non-blocking."""
        out: list[RunnerEvent] = []
        try:
            while True:
                out.append(self._queue.get_nowait())
        except queue.Empty:
            pass
        return out

    def is_done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the runner finishes or timeout elapses."""
        return self._done.wait(timeout)

    def cancel(self) -> None:
        """Signal the worker to terminate the subprocess. Returns
        immediately; the worker fires the SIGTERM/SIGKILL sequence
        on its next poll. Idempotent."""
        self._stop.set()

    # ─── State accessors (called from main thread between ticks) ───

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
        proc = self._proc
        return None if proc is None else proc.pid

    @property
    def started_at(self) -> float:
        return self._started_at

    def elapsed(self, now: float | None = None) -> float:
        """Wall-clock seconds since start, or until completion if done."""
        if self._started_at == 0.0:
            return 0.0
        if self._completed_at > 0.0:
            return self._completed_at - self._started_at
        return (now if now is not None else time.monotonic()) - self._started_at

    # ─── Worker thread ───

    def _run(self) -> None:
        """Main worker body — spawn Popen, drive readers, wait for exit."""
        try:
            self._proc = subprocess.Popen(
                self.command,
                shell=True,
                executable="/bin/bash",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # line buffered (requires text=True)
                cwd=self._cwd,
                env=self._env,
                # Run the subprocess in its own process group so SIGTERM
                # propagates to children (e.g., a piped chain). Without
                # this, killing the shell can leave grandchildren behind.
                start_new_session=True,
            )
        except FileNotFoundError as e:
            self._error = f"command not found: {e}"
            self._exit_code = 127
            self._completed_at = time.monotonic()
            self._queue.put(RunnerErrored(message=self._error))
            self._done.set()
            return
        except Exception as e:
            self._error = f"{type(e).__name__}: {e}"
            self._exit_code = -1
            self._completed_at = time.monotonic()
            self._queue.put(RunnerErrored(message=self._error))
            self._done.set()
            return

        self._queue.put(RunnerStarted())

        # Spawn the two pipe readers. They run until EOF on their pipe.
        readers = [
            threading.Thread(
                target=self._read_pipe,
                args=("stdout", self._proc.stdout, self._stdout_buf),
                daemon=True,
                name=f"bash-runner-{self.tool_call_id[:12]}-out",
            ),
            threading.Thread(
                target=self._read_pipe,
                args=("stderr", self._proc.stderr, self._stderr_buf),
                daemon=True,
                name=f"bash-runner-{self.tool_call_id[:12]}-err",
            ),
        ]
        for r in readers:
            r.start()

        # Wait loop: poll the subprocess + watch for cancel / timeout.
        deadline = self._started_at + self._timeout
        while True:
            if self._stop.is_set():
                self._terminate_process()
                self._error = "cancelled"
                break
            if time.monotonic() > deadline:
                self._terminate_process()
                self._error = f"timed out after {self._timeout:.1f}s"
                break
            ret = self._proc.poll()
            if ret is not None:
                break
            time.sleep(_POLL_INTERVAL_S)

        # Drain readers — they'll exit on EOF after the process closes
        # its stdout/stderr file descriptors.
        for r in readers:
            r.join(timeout=1.0)

        # Capture final exit code (may be -SIGTERM on cancel/timeout).
        try:
            self._exit_code = self._proc.poll()
            if self._exit_code is None:
                # Process didn't actually exit — should be impossible
                # after _terminate_process, but be defensive.
                self._exit_code = -1
        except Exception:
            self._exit_code = -1

        self._completed_at = time.monotonic()
        duration_ms = (self._completed_at - self._started_at) * 1000.0
        self._queue.put(RunnerCompleted(
            exit_code=self._exit_code,
            duration_ms=duration_ms,
            truncated=self._truncated,
            error=self._error,
        ))
        self._done.set()

    def _terminate_process(self) -> None:
        """Send SIGTERM, wait briefly, then SIGKILL if needed.
        Targets the whole process group because we set start_new_session."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), 15)  # SIGTERM
        except (OSError, ProcessLookupError):
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=_TERM_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), 9)  # SIGKILL
        except (OSError, ProcessLookupError):
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass

    def _read_pipe(
        self,
        channel: Channel,
        pipe,
        buf: list[str],
    ) -> None:
        """Reader thread body — readline loop for one pipe."""
        try:
            for line in iter(pipe.readline, ""):
                if not line:
                    break
                accepted = self._accept_line(channel, line, buf)
                if accepted:
                    self._queue.put(OutputLine(channel=channel, text=line))
                # If not accepted, the byte cap was hit — keep reading
                # to drain the pipe so the subprocess doesn't block,
                # but stop emitting and stop appending to the buffer.
        except Exception:
            # Pipe closed unexpectedly — exit reader silently. The
            # main worker will surface any failure via _error.
            pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def _accept_line(
        self,
        channel: Channel,
        line: str,
        buf: list[str],
    ) -> bool:
        """Append line to buf if there's room. Returns True when accepted,
        False when the byte cap was hit (caller should still drain the
        pipe but not emit events for the dropped data)."""
        with self._buf_lock:
            if self._truncated:
                return False
            line_bytes = len(line.encode("utf-8", errors="replace"))
            if self._byte_count + line_bytes > self._max_bytes:
                # Truncate this line to fit, append, then mark truncated.
                room = max(0, self._max_bytes - self._byte_count)
                if room > 0:
                    # Slice on chars (room is a byte budget but most
                    # output is ASCII; the safety net is the cap, not
                    # exact-byte alignment). Drop trailing whitespace
                    # so the truncation marker reads cleanly.
                    truncated_line = line[:room].rstrip() + "\n"
                    buf.append(truncated_line)
                    self._byte_count += len(truncated_line.encode("utf-8", errors="replace"))
                buf.append("…[output truncated]\n")
                self._truncated = True
                return room > 0
            buf.append(line)
            self._byte_count += line_bytes
            return True
