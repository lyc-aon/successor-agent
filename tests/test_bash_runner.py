"""Tests for bash/runner.py — async subprocess execution.

The runner spawns a worker thread that runs Popen + reads stdout/stderr
line-by-line into a thread-safe queue. The chat polls drain() each tick.
These tests cover the worker lifecycle, line capture, exit code,
cancellation, timeout, and the byte-cap truncation path.

All tests use real subprocess calls against shell builtins (echo, sleep,
true, false) so no mocking. Hermetic by virtue of running short-lived
commands in the test process's /tmp.
"""

from __future__ import annotations

import time

import pytest

from successor.bash.runner import (
    BashRunner,
    OutputLine,
    RunnerCompleted,
    RunnerErrored,
    RunnerStarted,
)


def _drain_until_done(runner: BashRunner, timeout_s: float = 5.0) -> list:
    """Pump drain() until is_done() flips, return all events seen."""
    start = time.monotonic()
    events = []
    while not runner.is_done():
        events.extend(runner.drain())
        if time.monotonic() - start > timeout_s:
            raise AssertionError(
                f"runner did not finish within {timeout_s}s"
            )
        time.sleep(0.01)
    events.extend(runner.drain())
    return events


def test_simple_echo_emits_started_output_completed() -> None:
    r = BashRunner("echo hello")
    r.start()
    events = _drain_until_done(r)

    types = [type(e).__name__ for e in events]
    assert "RunnerStarted" in types
    assert "OutputLine" in types
    assert "RunnerCompleted" in types
    assert r.exit_code == 0
    assert r.stdout == "hello\n"
    assert r.stderr == ""
    assert r.error == ""


def test_two_lines_capture_in_order() -> None:
    r = BashRunner("echo first; echo second")
    r.start()
    events = _drain_until_done(r)

    output_lines = [e for e in events if isinstance(e, OutputLine)]
    assert len(output_lines) == 2
    assert output_lines[0].text == "first\n"
    assert output_lines[1].text == "second\n"
    assert r.stdout == "first\nsecond\n"


def test_stderr_routed_to_stderr_buffer() -> None:
    r = BashRunner("echo only-out; echo only-err 1>&2")
    r.start()
    _drain_until_done(r)

    assert r.stdout == "only-out\n"
    assert r.stderr == "only-err\n"
    assert r.exit_code == 0


def test_nonzero_exit_propagated() -> None:
    r = BashRunner("false")
    r.start()
    _drain_until_done(r)
    assert r.exit_code == 1


def test_exit_code_127_for_unknown_command() -> None:
    r = BashRunner("definitely_not_a_real_command_12345")
    r.start()
    _drain_until_done(r)
    assert r.exit_code == 127
    # bash writes the "command not found" message to stderr
    assert "not found" in r.stderr.lower() or "command" in r.stderr.lower()


def test_async_does_not_block_caller() -> None:
    """The whole point: while the subprocess sleeps, the caller can
    do other work. We measure how many drain() calls happen during a
    100ms sleep — should be many, not 1."""
    r = BashRunner("sleep 0.1 && echo done")
    r.start()
    drain_count = 0
    while not r.is_done():
        r.drain()
        drain_count += 1
        time.sleep(0.005)
    # 100ms wall / 5ms per tick = ~20 ticks expected
    assert drain_count >= 5, (
        f"expected the caller to drain many times during the sleep, "
        f"got only {drain_count} (suggests blocking)"
    )
    assert r.exit_code == 0
    assert r.stdout == "done\n"


def test_streaming_output_lines_arrive_incrementally() -> None:
    """Critical: lines must arrive over time, not all at once at the
    end. Without this, the running tool card UX wouldn't update."""
    r = BashRunner(
        "for i in 1 2 3; do echo line$i; sleep 0.05; done"
    )
    r.start()
    line_arrival_times: list[float] = []
    while not r.is_done():
        events = r.drain()
        for ev in events:
            if isinstance(ev, OutputLine):
                line_arrival_times.append(time.monotonic() - r.started_at)
        time.sleep(0.01)
    # Drain any remaining
    for ev in r.drain():
        if isinstance(ev, OutputLine):
            line_arrival_times.append(time.monotonic() - r.started_at)

    assert len(line_arrival_times) == 3
    # Lines must be spread out over time — at least 30ms between first
    # and last (sleep 0.05 between three lines = ~100ms total)
    spread = line_arrival_times[-1] - line_arrival_times[0]
    assert spread >= 0.03, (
        f"lines arrived too close together ({spread:.3f}s) — "
        f"the runner may be buffering instead of streaming"
    )


def test_cancel_terminates_running_subprocess() -> None:
    """A cancel mid-run should kill the subprocess and surface as a
    completed event with error='cancelled' within the grace window."""
    r = BashRunner("sleep 5")
    r.start()
    time.sleep(0.05)  # let it really start
    r.cancel()
    deadline = time.monotonic() + 2.0
    while not r.is_done() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert r.is_done(), "cancel did not terminate the runner in time"
    assert r.elapsed() < 1.5, f"cancel was slow: {r.elapsed():.2f}s"
    assert r.error == "cancelled"
    # exit_code is the negative signal number (-15 = SIGTERM)
    assert r.exit_code is not None
    assert r.exit_code < 0


def test_timeout_terminates_long_running() -> None:
    """A subprocess exceeding timeout should be terminated with a
    timeout error."""
    r = BashRunner("sleep 5", timeout=0.2)
    r.start()
    _drain_until_done(r, timeout_s=3.0)
    assert "timed out" in r.error
    assert r.exit_code is not None
    assert r.exit_code < 0


def test_byte_cap_truncates_runaway_output() -> None:
    """Once the byte cap is hit, subsequent lines are dropped (not
    queued as events) but the subprocess keeps running so it doesn't
    block on a full pipe."""
    # Generate ~2KB of output with a tiny cap
    r = BashRunner(
        "for i in $(seq 1 100); do echo line$i; done",
        max_output_bytes=200,
    )
    r.start()
    _drain_until_done(r)
    assert r.exit_code == 0
    assert r.truncated, "byte cap should have tripped truncated flag"
    # Output buffer is bounded — should not contain all 100 lines
    assert "line100" not in r.stdout
    # Truncation marker is appended
    assert "truncated" in r.stdout.lower()


def test_completed_event_carries_exit_code_and_duration() -> None:
    r = BashRunner("echo a")
    r.start()
    events = _drain_until_done(r)
    completed = [e for e in events if isinstance(e, RunnerCompleted)]
    assert len(completed) == 1
    assert completed[0].exit_code == 0
    assert completed[0].duration_ms >= 0
    assert completed[0].error == ""


def test_elapsed_freezes_after_completion() -> None:
    """elapsed() should report wall-clock-since-start while running,
    then freeze at the wall-clock duration after the runner is done."""
    r = BashRunner("echo x")
    r.start()
    _drain_until_done(r)
    snap1 = r.elapsed()
    time.sleep(0.05)
    snap2 = r.elapsed()
    assert snap1 == snap2, (
        f"elapsed() should be frozen after done, got {snap1} → {snap2}"
    )


def test_multiple_runners_run_concurrently() -> None:
    """Two runners started in parallel should overlap in time. This
    proves the chat can dispatch multiple tool calls in one batch
    and they execute concurrently rather than serially."""
    r1 = BashRunner("sleep 0.1 && echo a")
    r2 = BashRunner("sleep 0.1 && echo b")
    t0 = time.monotonic()
    r1.start()
    r2.start()
    while not (r1.is_done() and r2.is_done()):
        r1.drain()
        r2.drain()
        time.sleep(0.005)
    elapsed = time.monotonic() - t0
    # Two 100ms sleeps run in parallel should take < 200ms wall
    assert elapsed < 0.18, (
        f"two parallel sleeps took {elapsed:.3f}s — running serially?"
    )
    assert r1.exit_code == 0 and r2.exit_code == 0


def test_runner_in_workspace_cwd(tmp_path) -> None:
    """The runner's cwd= argument should pin the subprocess to that
    directory so files land where the user expects."""
    r = BashRunner("pwd && touch sentinel.txt", cwd=str(tmp_path))
    r.start()
    _drain_until_done(r)
    assert r.exit_code == 0
    assert str(tmp_path) in r.stdout
    assert (tmp_path / "sentinel.txt").exists()


def test_tool_call_id_propagated() -> None:
    """The runner stores the tool_call_id given at construction so the
    chat can link the resulting card back to its originating turn."""
    r = BashRunner("echo x", tool_call_id="call_abc_123")
    assert r.tool_call_id == "call_abc_123"
    r.start()
    _drain_until_done(r)
    assert r.tool_call_id == "call_abc_123"


def test_synthesized_tool_call_id_when_omitted() -> None:
    """Without an explicit id, the runner generates one so the field
    is always populated for downstream callers."""
    r = BashRunner("echo x")
    assert r.tool_call_id
    assert r.tool_call_id.startswith("call_")
    r2 = BashRunner("echo x")
    assert r2.tool_call_id != r.tool_call_id
