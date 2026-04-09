---
name: browser-operator
description: Use this skill when a task needs a real Playwright page session for clicking, typing, screenshots, console errors, or local-app verification.
when_to_use: Use when the user wants live page interaction, JS-rendered content, screenshots, form entry, clicks, or console-error inspection. Invoke this before using the browser tool for real page work.
allowed-tools: browser
---

# Browser Operator

This skill is for real page sessions. Use it only when `holonet` is not
enough.

## First move

Start by opening the page you need to inspect. If the user already gave a
URL or local fixture path, use it directly. If they named a page already
open in the persistent browser session, continue from that session rather
than reopening everything blindly.

If this is a local app or fixture and you are not certain which control
to target next, call `inspect` immediately after `open` so you can work
from real controls and selector hints instead of guessing.

## Working style

1. Use `open` to establish the page context.
2. For open-ended "inspect it like a human" or polish tasks, sample at
   most one or two representative interactions, pick the most important
   issue, fix it, verify it, and stop. Do not tour every control.
3. Use `extract_text` to read what changed after interactive steps.
4. Use `click`, `type`, `press`, and `select` with human-visible targets when possible.
   For local apps or fixtures you just built, prefer stable selectors
   like `#search`, `#new-title`, or `#status-filter` over ambiguous
   visible text.
5. If an input is already focused, `type` may omit `target`. If the next
   step is simply Enter, prefer one `type` call with `press_enter=true`.
   Use `press` mainly for `Escape` or other keyboard-driven flows that
   are not just "type, then Enter".
6. When labels repeat, prefer a scoped selector like
   `li:has-text("Issue title") button.status-btn` over a bare text click.
7. Use `wait_for` when the page needs time to settle.
8. Use `console_errors` after flows that might have JS/runtime issues.
9. Use `screenshot` when the user asked for a visual artifact, when the
   page state needs visual confirmation, or when `vision` is the right
   way to inspect what is on screen.
10. If 2-3 browser actions fail, or if repeated actions leave the page
    unchanged, stop guessing and call `inspect` before trying again.
11. If a local app reload picks up stale browser state that interferes
    with verification, use `storage_state` or `clear_storage` in the
    browser instead of changing the app code just to reset the test.
12. For layout, spacing, clipping, overlap, contrast, or other visibly
    grounded UI questions, capture a screenshot and use `vision`
    instead of guessing from DOM text alone.

## Avoid

- Do not use the browser for routine web search or article retrieval when
  `holonet` can answer the question faster.
- Do not keep reopening the same page after every interaction.
- Do not guess what happened after a click or form entry; read the
  returned page snapshot or extract the needed text.
- Do not click ambiguous words like `Open`, `Closed`, or placeholder
  copy when the page exposes a stable selector for the actual control.
- Do not keep retrying the same selector after repeated failures or an
  unchanged page state. Re-inspect the page first.
- Do not `type` into real dropdowns. Use `select` for `<select>` menus
  and other option-picking controls.
- Do not claim an inline edit is verified while the field is merely
  focused. Save or cancel it with the actual keyboard action first.
