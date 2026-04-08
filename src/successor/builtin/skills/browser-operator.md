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

## Working style

1. Use `open` to establish the page context.
2. Use `extract_text` to read what changed after interactive steps.
3. Use `click` and `type` with human-visible targets when possible.
4. Use `wait_for` when the page needs time to settle.
5. Use `console_errors` after flows that might have JS/runtime issues.
6. Use `screenshot` only when the user asked for a visual artifact or the
   page state needs visual confirmation.

## Avoid

- Do not use the browser for routine web search or article retrieval when
  `holonet` can answer the question faster.
- Do not keep reopening the same page after every interaction.
- Do not guess what happened after a click or form entry; read the
  returned page snapshot or extract the needed text.
