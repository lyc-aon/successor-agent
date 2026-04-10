# Chat Runtime Refactor

Date: 2026-04-10
Status: Implemented and verified

## Purpose

The render extraction in `docs/chat-render-refactor-plan.md` lowered the
UI risk inside `src/successor/chat.py`, but the file still carried the
entire native-tool execution subsystem inline. This pass extracts the
tool/runtime orchestration out of `chat.py` without changing the live
behavior, test-facing method names, or trace/playback semantics.

The goal is not a redesign. The goal is to make the runtime easier to
reason about while preserving the current controller surface.

## Result

`src/successor/chat.py` now delegates tool execution flow into:

- `src/successor/chat_tool_runtime.py`
  - bash refusal/spawn flow
  - native tool dispatch
  - file-tool runner spawn/finalization
  - task / verify / runbook / skill / subagent tool handling
  - browser / holonet / vision runner spawn flow
  - runner completion, cancellation, and progress-summary emission

`SuccessorChat` keeps the same method names that tests and higher-level
controller code already use:

- `_spawn_bash_runner`
- `_dispatch_streamed_bash_blocks`
- `_spawn_*_runner`
- `_dispatch_native_tool_calls`
- `_pump_running_tools`
- `_finalize_runner`
- `_cancel_running_tools`
- `_refusal_hint`

Those methods are now thin compatibility wrappers over
`ChatToolRuntime`, so the call surface stayed stable while the
implementation moved out of `chat.py`.

## Compatibility Notes

Two compatibility shims remain intentionally in `chat.py`:

- module-level web/runtime functions still resolve through `chat.py`
  helper methods so existing monkeypatch-based tests keep working
- `_native_tool_call_failure_message` is still exposed through the chat
  instance for malformed native-tool-call reporting

This is deliberate. The extraction is behavior-preserving first, not a
"purify every import path" exercise.

## Size Change

After this pass:

- `src/successor/chat.py`: `7087` lines
- `src/successor/chat_tool_runtime.py`: `1337` lines

Before the extraction, `src/successor/chat.py` was `8224` lines.

## Verification

Automated verification that passed:

- lint:
  - `PYTHONPATH=src ruff check src/successor/chat.py src/successor/chat_tool_runtime.py`
- bytecode:
  - `PYTHONPATH=src python3 -m py_compile src/successor/chat.py src/successor/chat_tool_runtime.py`
- targeted runtime slice:
  - `PYTHONPATH=src pytest -q tests/test_chat_bash.py tests/test_bash_diff_capture.py tests/test_file_tools.py tests/test_session_trace.py`
  - `66 passed`
  - `PYTHONPATH=src pytest -q tests/test_chat_tasks.py tests/test_chat_runbook.py tests/test_chat_verification.py tests/test_chat_web_tools.py tests/test_chat_subagents.py`
  - `27 passed`
- full suite:
  - `PYTHONPATH=src pytest -q`
  - `1227 passed in 12.50s`

Human-emulated runtime verification that passed:

- drove `?`, `Esc`, `/budget`, and a live local-model build prompt
  through `SuccessorChat.on_key()` + `on_tick()` with `RecordingBundle`
- built a 3-file issue-triage demo in a clean temp workspace
- observed native file writes plus browser verification in the live trace
- generated a playback bundle and screenshots for both:
  - the built app
  - the session playback viewer

Artifacts from this pass:

- bundle: `/tmp/successor-refactor-e2e-bundle`
- app workspace: `/tmp/successor-refactor-e2e-1bpyrt`
- screenshots:
  - `/tmp/successor-refactor-e2e-app.png`
  - `/tmp/successor-refactor-e2e-app-mobile.png`
  - `/tmp/successor-refactor-e2e-playback.png`

Observed tool mix in the live session:

- `write_file`: 3
- `bash`: 3
- `browser`: 33

Observed bash usage was limited to runtime shell work:

- starting local `http.server`
- one ad-hoc curl-based probe

No bash heredoc file writes were observed in the session trace.

## What The E2E Run Told Us

The extraction itself is stable, but the live run also surfaced a real
behavior pattern worth keeping in mind:

- native file authoring stayed on the intended path
- browser-led verification is effective, but can stay active for many
  continuation turns when the model keeps finding another thing to poke

That is not a refactor bug, but it is useful signal for the next loop
hardening pass.

## Next Safe Extraction

The highest-value remaining cluster in `chat.py` is the agent-turn /
stream controller layer:

- `_submit`
- `_begin_agent_turn`
- `_build_api_messages_native`
- `_pump_stream`
- `_format_stream_error`

That should be the next extraction if we continue reducing `chat.py`.
The runtime seam is now clean enough that the next pass can focus on the
agent loop rather than tool execution.
