---
name: browser-verifier
description: Use this skill when verifying a local app or recent browser-facing change so you stay focused, selector-driven, and resistant to rabbit holes.
when_to_use: Invoke when the task is browser verification, QA, visual/runtime checking, or reproducing/fixing a local UI issue after code changes. Use this before browser work that is meant to confirm behavior, not just interact casually.
allowed-tools: browser
---

# Browser Verifier

This skill is for disciplined verification, not freeform browsing.

## Verification rhythm

1. `open` the target page once.
2. `inspect` immediately if the next control is not already obvious.
3. Use the smallest possible action to advance the check.
4. Read the returned snapshot after every interaction.
5. Stop as soon as the requested behavior is verified or falsified.

## Selector policy

- For local apps or fixtures you just built, prefer stable selectors you
  already know from the source: `#search`, `#new-title`,
  `#status-filter`, `#theme-toggle`.
- Use visible text only when it is distinctive and truly identifies the
  intended control.
- If labels repeat, scope the selector to the relevant row or region.

## Keyboard policy

- If a field is already focused and the goal is "type, then Enter", use
  one `type` call with `press_enter=true`.
- Use `press` for `Escape` and other genuine keyboard-only steps.
- Do not claim an edit is verified while the input is merely focused.

## Anti-rabbit-hole rules

- If 2 browser actions fail, stop and `inspect`.
- If 3 browser actions leave the page state unchanged, stop and `inspect`.
- Do not keep retrying ambiguous clicks like `Open`, `Closed`, or
  placeholder text when the page exposes stable selectors.
- Do not reopen the same page repeatedly unless a code edit or reload is
  actually required.
- If persisted browser state is poisoning the check after a reload, use
  `storage_state` to inspect it or `clear_storage` to reset it. Do not
  patch the app just to wipe test data.
- If the question is visual rather than structural, capture a screenshot
  and use `vision` before concluding.

## Verification mindset

- Your job is to confirm the real behavior, not the intended behavior.
- Prefer one decisive check over many exploratory clicks.
- If the page state after a change does not prove success, perform one
  targeted follow-up action. If that still does not prove it, `inspect`
  and replan instead of thrashing.
