# Chat Display Runtime Refactor

Date: 2026-04-10
Status: Implemented and verified

## Purpose

After the render-module split, tool-runtime extraction, and agent-loop
extraction, `src/successor/chat.py` still owned the highest-risk
display-sensitive cluster:

- `_paint_chat_area`
- `_is_empty_chat`
- `_has_intro_art`
- `_resolve_intro_art`
- `_paint_empty_state`
- `_build_intro_panel_lines`
- `_paint_chat_row`
- `_build_message_lines`
- `_build_rows_from_messages`
- `_build_streaming_lines`
- `_streaming_tool_call_preview_rows`
- `_paint_static_footer`

Those methods define what the user actually sees while the chat is
idle, while slash-command input is open, and while the model is
thinking or streaming content. The goal of this pass was to move that
render-model assembly out of `chat.py` without changing the live
surface, wrapper method names, or runtime behavior.

## Result

`src/successor/chat.py` now delegates the display/render-model layer
into:

- `src/successor/chat_display_runtime.py`
  - empty-state intro art resolution and panel assembly
  - viewport row assembly for committed messages
  - live streaming reply rows
  - streaming tool-call preview rows
  - static footer paint
  - display-facing compatibility helpers for row rendering and search

`SuccessorChat` keeps the same method names that tests and the rest of
the runtime already use:

- `_paint_chat_area`
- `_is_empty_chat`
- `_has_intro_art`
- `_resolve_intro_art`
- `_paint_empty_state`
- `_build_intro_panel_lines`
- `_paint_chat_row`
- `_build_message_lines`
- `_build_rows_from_messages`
- `_fade_prepainted_rows`
- `_render_*_rows`
- `_build_streaming_lines`
- `_streaming_tool_call_preview_rows`
- `_paint_static_footer`

Those methods are now thin compatibility wrappers over
`ChatDisplayRuntime`.

## Compatibility Notes

This pass intentionally preserved three things:

- `on_tick()` remains the top-level coordinator in `chat.py`
- the existing `_Message` and `RenderedRow` shapes remain unchanged
- test-facing wrapper method names and monkeypatch seams remain intact

This is a seam extraction, not a renderer redesign.

## Size Change

After this pass:

- `src/successor/chat.py`: `4668` lines
- `src/successor/chat_display_runtime.py`: `975` lines
- `src/successor/chat_agent_loop.py`: `1174` lines
- `src/successor/chat_tool_runtime.py`: `1331` lines

Before this extraction, `src/successor/chat.py` was `5741` lines after
the agent-loop seam pass.

## Verification

Automated verification that passed:

- lint:
  - `PYTHONPATH=src ruff check src/successor/chat.py src/successor/chat_display_runtime.py`
- bytecode:
  - `PYTHONPATH=src python3 -m py_compile src/successor/chat.py src/successor/chat_display_runtime.py`
- targeted display slices:
  - `PYTHONPATH=src pytest -q tests/test_intro_art.py tests/test_context_fill_bar.py tests/test_snapshot_themes.py`
  - `57 passed`
  - `PYTHONPATH=src pytest -q tests/test_chat_bash.py tests/test_bash_prepared_output.py`
  - `72 passed`
  - `PYTHONPATH=src pytest -q tests/test_compaction_animation.py tests/test_chat_mouse.py tests/test_chat_paste.py tests/test_chat_perf.py`
  - `55 passed`
- full suite:
  - `PYTHONPATH=src pytest -q`
  - `1227 passed in 12.42s`

Human-emulated visual/runtime verification that passed:

- used a real local `SuccessorChat` against the live llama.cpp profile
  at `http://localhost:8080`
- visually inspected:
  - intro screen
  - slash-command draft state
  - post-command theme application
  - live thinking frame
  - live content-streaming frame
  - settled response frame
- artifacts:
  - browser-rendered verification screenshots:
    - `/tmp/successor-chat-display-browser-verify-20260410/01-intro-default.png`
    - `/tmp/successor-chat-display-browser-verify-20260410/02-slash-draft.png`
    - `/tmp/successor-chat-display-browser-verify-20260410/03-theme-after-submit.png`
    - `/tmp/successor-chat-display-browser-verify-20260410/04-stream-thinking.png`
    - `/tmp/successor-chat-display-browser-verify-20260410/05-stream-content.png`
    - `/tmp/successor-chat-display-browser-verify-20260410/06-stream-settled.png`
  - supporting HTML captures:
    - `/tmp/successor-chat-display-browser-verify-20260410/*.html`

One verification false start was caught and corrected:

- the first ad-hoc PNG verifier used a font path that did not render
  the intro braille art correctly, so the intro looked like tofu boxes
- the final visual pass switched to HTML + headless Chrome screenshots
  so the inspection matched the actual glyph rendering path

## What The Visual Pass Told Us

The extraction held up under real usage:

- intro art and info rail still align correctly
- slash autocomplete still paints at the input edge with the same
  selection styling
- thinking and live content streaming still anchor at the bottom of the
  viewport
- the footer stays stable while the model is streaming

This matters because those are exactly the parts of the chat most
likely to regress when row assembly and footer paint move around.

## Follow-on

The biggest remaining non-trivial clusters in `chat.py` are now mostly:

- `__init__` state assembly
- `_handle_key_event`
- smaller paint/input helpers that still live alongside the controller

Those should be handled as narrower focused extractions rather than
another large move.
