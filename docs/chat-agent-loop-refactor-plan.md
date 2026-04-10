# Chat Agent-Loop Refactor

Date: 2026-04-10
Status: Implemented and verified

## Purpose

After the render and tool-runtime extractions, `src/successor/chat.py`
still owned the biggest remaining high-risk controller cluster:

- `_submit`
- `_begin_agent_turn`
- `_build_api_messages_native`
- `_pump_stream`
- `_format_stream_error`

Those methods are the bridge between user submission, native tool-call
history construction, streaming state, and continuation turns. This
pass extracts that controller logic without changing the public
`SuccessorChat` method surface, test entrypoints, or trace semantics.

## Result

`src/successor/chat.py` now delegates the controller layer into:

- `src/successor/chat_agent_loop.py`
  - user submit command routing
  - per-turn prompt assembly
  - native tool-call history assembly
  - stream draining and continuation logic
  - friendly stream-error rendering
  - pure helper functions used by the API-history builder and stream
    trace summaries

`SuccessorChat` keeps the same method names that tests and higher-level
controller code already use:

- `_submit`
- `_begin_agent_turn`
- `_build_api_messages_native`
- `_pump_stream`
- `_format_stream_error`

Those methods are now thin compatibility wrappers over `ChatAgentLoop`.

## Compatibility Notes

The chat still owns:

- all mutable state and render state
- all trace sinks and event emitters
- all existing helper methods that tests already monkeypatch or call
- the same `_Message`-based history structure used by playback and
  compaction adapters

The extraction intentionally did not redesign the agent loop. The goal
was a behavior-preserving seam with a smaller blast radius inside
`chat.py`.

## Size Change

After this pass:

- `src/successor/chat.py`: `5741` lines
- `src/successor/chat_agent_loop.py`: `1174` lines
- `src/successor/chat_tool_runtime.py`: `1331` lines

Before the extraction, `src/successor/chat.py` was `7087` lines after
the runtime seam pass.

## Verification

Automated verification that passed:

- lint:
  - `PYTHONPATH=src ruff check src/successor/chat.py src/successor/chat_agent_loop.py`
- bytecode:
  - `PYTHONPATH=src python3 -m py_compile src/successor/chat.py src/successor/chat_agent_loop.py`
- targeted controller slice:
  - `PYTHONPATH=src pytest -q tests/test_chat_bash.py tests/test_chat_tasks.py tests/test_chat_web_tools.py tests/test_chat_subagents.py tests/test_chat_stream_error.py`
  - `78 passed`
  - `PYTHONPATH=src pytest -q tests/test_file_tools.py tests/test_input_history.py tests/test_playback.py tests/test_chat_autocompact_gate.py`
  - `59 passed`
- full suite:
  - `PYTHONPATH=src pytest -q`
  - `1227 passed in 12.52s`

Human-emulated runtime verification that passed:

- ran a fresh live local-model session against the real `michaelreal`
  profile with native file tools, bash, browser, and vision enabled
- built a 3-file issue-triage app in:
  - `/tmp/successor-agent-loop-live-o7z3xj1r`
- captured the chat evolution in:
  - `/tmp/successor-agent-loop-live-artifacts`
- observed the completed tool mix:
  - `write_file`: 3
  - `bash`: 5
  - `browser`: 42
- verified the session settled cleanly after `220.512s`
- visually inspected generated screenshots:
  - `/tmp/successor-agent-loop-live-app.png`
  - `/tmp/successor-agent-loop-live-app-mobile.png`
- independently replayed the built app with Playwright and verified:
  - seeded counts render correctly
  - search narrows the list
  - status filters work
  - create issue works
  - close/reopen updates counts
  - created issue persists through reload
  - theme persists through reload

## What The Live Run Told Us

The extraction held up under a real local run:

- native `write_file` stayed the default authoring path
- bash remained shell/runtime support work
- browser verification stayed coherent over a long interaction-heavy
  phase and still exited with a final plain-text summary

One heavier bash-only E2E scenario (`issue_desk_supervised` in the older
driver profile) spent too long in an initial thinking phase before
producing output. That does not look like a regression from this seam;
it looks like a scenario/profile quality issue in that older driver.

## Follow-on

The next high-value display/runtime cluster named here was completed in
the follow-up pass documented at:

- `docs/chat-display-runtime-refactor-plan.md`

That pass moved the row-builder / streaming-preview / footer painter
cluster out of `chat.py` while preserving the same `SuccessorChat`
wrapper surface.
