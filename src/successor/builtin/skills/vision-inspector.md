---
name: vision-inspector
description: Use this skill when a task depends on what is visibly on screen, such as layout, clipping, spacing, contrast, or screenshot-based QA.
when_to_use: Invoke when the user asks for visual verification, design review, “inspect it like a human”, screenshot analysis, or layout debugging. Use this before relying on a screenshot for browser or UI work.
allowed-tools: vision
---

# Vision Inspector

Use this skill when visual evidence matters more than source text.

## Core loop

1. Work from an actual image file, not a guess.
2. Ask one concrete visual question at a time.
3. Prefer high-signal findings:
   - clipping
   - overlap
   - broken hierarchy
   - poor spacing
   - contrast/readability problems
   - empty-state or state-mismatch problems
4. Report only what is visible. If the screenshot is insufficient, say so.

## For browser/local-app work

If the task is visual browser verification:

1. Use `browser` to open the page.
2. Capture a screenshot.
3. Use `vision` on that screenshot.
4. Fix one important issue.
5. Re-screenshot and verify visually again.

## Anti-slop rules

- Do not infer layout quality from HTML/CSS alone when a screenshot is available.
- Do not produce a generic design critique if the task is to verify a specific bug.
- Do not claim the page looks correct unless the screenshot actually supports that conclusion.
