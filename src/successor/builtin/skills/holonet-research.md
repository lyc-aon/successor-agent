---
name: holonet-research
description: Use this skill for API-backed web research, search, news lookup, and article retrieval without opening a live browser.
when_to_use: Use when the task is web research, article discovery, news lookup, or structured retrieval and a real browser session is not necessary. Prefer this before browser automation whenever APIs are enough.
allowed-tools: holonet
---

# Holonet Research

Use `holonet` as the default research path when the task does not need
live page interaction.

## Tool choice

- Prefer `brave_search` for general web lookup.
- Prefer `brave_news` for current-news queries.
- Prefer `firecrawl_search` when the user wants article discovery plus
  summaries.
- Prefer `firecrawl_scrape` when the user gives a concrete URL and wants
  the content of that page.

## Working style

1. Choose the narrowest provider that matches the request.
2. Ask for 3-5 results unless the user explicitly wants more.
3. Read the returned structured output before deciding whether a second
   call is needed.
4. When the tool output already answers the question, stop and answer in
   plain text instead of opening the browser.

## Avoid

- Do not use the browser for routine research, search, or article
  extraction that `holonet` can do directly.
- Do not bounce between multiple providers without a reason.
- Do not ask `firecrawl_scrape` to fetch a URL the user did not provide
  unless a prior search result clearly justifies it.
