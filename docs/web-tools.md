# Web Tools

Successor ships two optional web-facing tool families:

- `holonet`: deterministic API-backed web and research retrieval
- `browser`: live Playwright browser control

Neither is enabled for the model by default on the bundled `default`
profile. Turn them on in the setup wizard's `tools` step or later in
`/config`.

Successor also ships built-in helper skills for these tools:

- `holonet-research`
- `biomedical-research`
- `browser-operator`

Profiles created through `successor setup` automatically seed the
matching skills when you enable `holonet` or `browser`. Existing
profiles can edit the skill list later in `/config` under `extensions`.

## Holonet

`holonet` is the first choice when the task needs search, article
retrieval, or biomedical lookup but does not require a real browser
session. It keeps the runtime fast and deterministic because it goes
straight to APIs instead of clicking through pages.

Supported routes:

- `brave_search`: general web search
- `brave_news`: current-news search
- `firecrawl_search`: article discovery with summaries/excerpts
- `firecrawl_scrape`: scrape one concrete page URL
- `europe_pmc`: life-sciences paper search
- `clinicaltrials`: ClinicalTrials.gov study lookup
- `biomedical_research`: Europe PMC + ClinicalTrials.gov together

Credential model:

- needs key: `brave_search`, `brave_news`, `firecrawl_search`, `firecrawl_scrape`
- keyless: `europe_pmc`, `clinicaltrials`
- composite: `biomedical_research`

Configuration lives under `tool_config.holonet` on the active profile.
The config menu exposes:

- `default_provider`
- per-provider enabled toggles
- inline API key fields
- API key file paths

Key resolution order:

1. inline profile value
2. configured key file
3. environment variable

Environment variables:

- `SUCCESSOR_BRAVE_API_KEY`
- `SUCCESSOR_FIRECRAWL_API_KEY`

## Browser

`browser` is the live page path. Use it when the task needs actual
navigation, clicks, typing, JS execution, local-app verification,
screenshots, or console errors.

The browser tool is intentionally optional. Base Successor stays
stdlib-only. To add the Python package:

```bash
pip install -e ".[browser]"
```

That installs the Playwright Python library only. You then have two
supported ways to provide the actual browser runtime:

1. Use Playwright-managed browser binaries:

```bash
python -m playwright install chromium
```

2. Point Successor at an existing browser install through
   `browser.channel` or `browser.executable_path`

3. Point Successor at a different Python that already has Playwright
   installed through `browser.python_executable`

That third path matters when your main Successor environment stays lean
but your system Python or another venv already has a working Playwright
install. Successor can run the browser helper under that interpreter
while the main chat keeps running in the original environment.

The current browser actions are:

- `open`
- `click`
- `type`
- `wait_for`
- `extract_text`
- `screenshot`
- `console_errors`

Configuration lives under `tool_config.browser`. The config menu
exposes:

- `headless`
- `channel`
- `python_executable`
- `executable_path`
- `user_data_dir`
- `viewport_width`
- `viewport_height`
- `timeout_s`
- `screenshot_on_error`

Successor keeps one persistent browser session per profile. That means
login state, cookies, and page context survive across multiple tool
calls in the same chat.

## Wizard And Config

The setup wizard only decides whether the tool is enabled. Detailed
provider/browser settings live in `/config`, because those fields are
too granular for the 10-step first-run flow.

Once `holonet` or `browser` is enabled on a profile, the config menu
reveals the corresponding section automatically.

## Doctor Output

`successor doctor` reports the active profile's web/browser readiness:

- `holonet` default route and which providers are actually usable
- whether the Playwright Python package is available
- which Python interpreter Successor will use for Playwright
- browser channel / executable path
- persistent browser user-data directory

Run `successor doctor` first if a profile is not surfacing `holonet` or
`browser` the way you expect.

## Guidance

- Prefer `holonet` for search, news, article retrieval, papers, and
  clinical-study lookup.
- Use `browser` only when a real page session or JS execution matters.
- Let the built-in skills handle the routing details when they are
  available. They keep the base prompt smaller and teach the model when
  to prefer `holonet` over `browser`.
- Keep `browser` disabled on profiles that do not need it. It is
  heavier than API-backed retrieval and should be treated as a focused
  capability, not the default path for all web tasks.
