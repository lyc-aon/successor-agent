"""Deterministic API-backed web research routes for Successor."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from ..bash.cards import ToolCard
from ..tool_runner import ToolExecutionResult, ToolProgress
from .config import HOLO_DEFAULT_PROVIDER_OPTIONS, HolonetConfig


_BRAVE_BASE = "https://api.search.brave.com/res/v1"
_FIRECRAWL_BASE = "https://api.firecrawl.dev/v2"
_EUROPEPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
_CLINICALTRIALS_BASE = "https://clinicaltrials.gov/api/v2"

_NEWS_HINT_RE = re.compile(
    r"\b(news|headline|headlines|breaking|story|stories|article|articles|today|latest|recent)\b",
    re.IGNORECASE,
)
_READ_HINT_RE = re.compile(
    r"\b(read|scrape|summari[sz]e|full text|full article|context|deeper|deep dive|details)\b",
    re.IGNORECASE,
)
_TRIAL_HINT_RE = re.compile(
    r"\b(clinical trial|clinical trials|phase i|phase ii|phase iii|phase iv|recruiting|enrolling|nct)\b",
    re.IGNORECASE,
)
_PAPER_HINT_RE = re.compile(
    r"\b(paper|papers|study|studies|journal|abstract|doi|literature|research)\b",
    re.IGNORECASE,
)
_BIOMED_HINT_RE = re.compile(
    r"\b(drug|therapy|disease|obesity|glioblastoma|cancer|trial|clinical|biomedical|medicine|medical)\b",
    re.IGNORECASE,
)

_PROVIDER_ALIASES = {
    "auto": "auto",
    "brave": "brave_search",
    "brave_search": "brave_search",
    "brave_news": "brave_news",
    "news": "brave_news",
    "firecrawl": "firecrawl_search",
    "firecrawl_search": "firecrawl_search",
    "firecrawl_scrape": "firecrawl_scrape",
    "scrape": "firecrawl_scrape",
    "europepmc": "europe_pmc",
    "europe_pmc": "europe_pmc",
    "clinicaltrials": "clinicaltrials",
    "clinical_trials": "clinicaltrials",
    "biomedical": "biomedical_research",
    "biomedical_research": "biomedical_research",
    "biomed": "biomedical_research",
}


class HolonetError(RuntimeError):
    """Raised when a holonet request cannot be completed."""


@dataclass(frozen=True, slots=True)
class HolonetRoute:
    provider: str
    query: str = ""
    url: str = ""
    count: int = 5


def holonet_preview_card(
    route: HolonetRoute,
    *,
    tool_call_id: str,
) -> ToolCard:
    label_parts = [route.provider]
    if route.query:
        label_parts.append(route.query)
    elif route.url:
        label_parts.append(route.url)
    return ToolCard(
        verb=_verb_for_provider(route.provider),
        params=tuple(
            (key, value)
            for key, value in (
                ("provider", route.provider),
                ("query", route.query or route.url),
                ("count", str(route.count)),
            )
            if value
        ),
        risk="safe",
        raw_command=" ".join(label_parts),
        confidence=1.0,
        parser_name="native-holonet",
        tool_name="holonet",
        tool_arguments={
            "provider": route.provider,
            **({"query": route.query} if route.query else {}),
            **({"url": route.url} if route.url else {}),
            **({"count": route.count} if route.count else {}),
        },
        raw_label_prefix="≈",
        tool_call_id=tool_call_id,
    )


def resolve_route(arguments: dict[str, Any], config: HolonetConfig) -> HolonetRoute:
    provider = normalize_provider(str(arguments.get("provider", "") or config.default_provider or "auto"))
    query = " ".join(str(arguments.get("query", "") or "").split()).strip()
    url = str(arguments.get("url", "") or "").strip()
    try:
        count = int(arguments.get("count", 5) or 5)
    except (TypeError, ValueError):
        count = 5
    count = max(1, min(8, count))
    if provider == "auto":
        provider = auto_provider(query=query, url=url, config=config)
    if not provider_enabled(provider, config):
        raise HolonetError(f"{provider} is disabled or missing required credentials")
    if provider == "firecrawl_scrape" and not url:
        raise HolonetError("firecrawl_scrape requires a url")
    if provider != "firecrawl_scrape" and not query:
        raise HolonetError(f"{provider} requires a query")
    return HolonetRoute(provider=provider, query=query, url=url, count=count)


def normalize_provider(name: str) -> str:
    normalized = name.strip().lower()
    return _PROVIDER_ALIASES.get(normalized, "auto")


def provider_enabled(provider: str, config: HolonetConfig) -> bool:
    if provider in {"brave_search", "brave_news"}:
        return config.brave_enabled and bool(config.effective_brave_key())
    if provider in {"firecrawl_search", "firecrawl_scrape"}:
        return config.firecrawl_enabled and bool(config.effective_firecrawl_key())
    if provider == "europe_pmc":
        return config.europe_pmc_enabled
    if provider == "clinicaltrials":
        return config.clinicaltrials_enabled
    if provider == "biomedical_research":
        return config.biomedical_enabled and (
            config.europe_pmc_enabled or config.clinicaltrials_enabled
        )
    return False


def available_provider_status(config: HolonetConfig) -> dict[str, bool]:
    return {
        name: provider_enabled(name, config)
        for name in HOLO_DEFAULT_PROVIDER_OPTIONS
        if name != "auto"
    }


def auto_provider(*, query: str, url: str, config: HolonetConfig) -> str:
    text = (query or "").strip()
    if url and provider_enabled("firecrawl_scrape", config):
        return "firecrawl_scrape"
    if _TRIAL_HINT_RE.search(text):
        if provider_enabled("clinicaltrials", config):
            return "clinicaltrials"
        if provider_enabled("biomedical_research", config):
            return "biomedical_research"
    if _BIOMED_HINT_RE.search(text) and provider_enabled("biomedical_research", config):
        return "biomedical_research"
    if _PAPER_HINT_RE.search(text) and provider_enabled("europe_pmc", config):
        return "europe_pmc"
    if _READ_HINT_RE.search(text):
        if provider_enabled("firecrawl_search", config):
            return "firecrawl_search"
    if _NEWS_HINT_RE.search(text):
        if provider_enabled("brave_news", config):
            return "brave_news"
        if provider_enabled("firecrawl_search", config):
            return "firecrawl_search"
    if provider_enabled("brave_search", config):
        return "brave_search"
    if provider_enabled("firecrawl_search", config):
        return "firecrawl_search"
    if provider_enabled("biomedical_research", config):
        return "biomedical_research"
    if provider_enabled("europe_pmc", config):
        return "europe_pmc"
    if provider_enabled("clinicaltrials", config):
        return "clinicaltrials"
    raise HolonetError("no holonet providers are available")


def run_holonet(
    route: HolonetRoute,
    config: HolonetConfig,
    progress: ToolProgress | None = None,
) -> ToolExecutionResult:
    if progress is not None:
        progress.stdout(f"{route.provider}: dispatching")
    if route.provider in {"brave_search", "brave_news"}:
        text = _run_brave(route, config)
    elif route.provider == "firecrawl_search":
        text = _run_firecrawl_search(route, config)
    elif route.provider == "firecrawl_scrape":
        text = _run_firecrawl_scrape(route, config)
    elif route.provider == "europe_pmc":
        text = _run_europe_pmc(route)
    elif route.provider == "clinicaltrials":
        text = _run_clinicaltrials(route)
    elif route.provider == "biomedical_research":
        text = _run_biomedical(route, config)
    else:
        raise HolonetError(f"unsupported provider {route.provider!r}")
    return ToolExecutionResult(output=text, exit_code=0)


def _json_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> Any:
    if params:
        query = urllib.parse.urlencode(
            {
                key: value
                for key, value in params.items()
                if value is not None and value != ""
            },
            doseq=True,
        )
        url = f"{url}?{query}"
    data = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = str(exc)
        raise HolonetError(f"{method.upper()} {url} failed: {exc.code} {body[:240]}") from exc
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise HolonetError(f"{method.upper()} {url} failed: {exc}") from exc


def _clean(value: str) -> str:
    return " ".join((value or "").split()).strip()


def _run_brave(route: HolonetRoute, config: HolonetConfig) -> str:
    mode = "news" if route.provider == "brave_news" else "web"
    endpoint = "news/search" if mode == "news" else "web/search"
    data = _json_request(
        "GET",
        f"{_BRAVE_BASE}/{endpoint}",
        headers={"X-Subscription-Token": config.effective_brave_key()},
        params={
            "q": route.query,
            "count": route.count,
            "country": "US",
            "search_lang": "en",
            "ui_lang": "en-US",
            "extra_snippets": "true" if mode == "news" else None,
        },
        timeout=15.0,
    )
    items = data.get("results") if mode == "news" else ((data.get("web") or {}).get("results") or [])
    results: list[str] = [
        f"Goal completed via Brave Search {mode} API without opening a browser window.",
        f"Query: {route.query}",
        "",
        "Results:",
    ]
    seen = 0
    for item in items:
        title = _clean(str(item.get("title", "") or ""))
        url = str(item.get("url", "") or "").strip()
        if not title or not url:
            continue
        meta_url = item.get("meta_url") or {}
        source = str(meta_url.get("netloc") or meta_url.get("hostname") or "").strip()
        age = _clean(str(item.get("age") or item.get("page_age") or ""))
        meta = " • ".join(bit for bit in (source, age) if bit)
        results.append(f"  {seen + 1}. {title}" + (f" [{meta}]" if meta else ""))
        results.append(f"     {url}")
        description = _clean(str(item.get("description", "") or ""))
        if description:
            results.append(f"     {description}")
        extra = item.get("extra_snippets") or []
        if extra:
            snippet = _clean(str(extra[0]))
            if snippet and snippet != description:
                results.append(f"     {snippet}")
        seen += 1
        if seen >= route.count:
            break
    if seen == 0:
        raise HolonetError("Brave Search returned no results")
    return "\n".join(results)


def _run_firecrawl_search(route: HolonetRoute, config: HolonetConfig) -> str:
    data = _json_request(
        "POST",
        f"{_FIRECRAWL_BASE}/search",
        headers={"Authorization": f"Bearer {config.effective_firecrawl_key()}"},
        json_body={
            "query": route.query,
            "limit": route.count,
            "sources": ["news", "web"],
            "ignoreInvalidURLs": True,
            "timeout": 60000,
            "scrapeOptions": {
                "formats": [{"type": "summary"}, {"type": "markdown"}],
                "onlyMainContent": True,
            },
        },
        timeout=45.0,
    )
    payload = data.get("data") or {}
    items = (payload.get("news") or []) + (payload.get("web") or [])
    lines = [
        "Goal completed via Firecrawl search without opening a browser window.",
        f"Query: {route.query}",
        "",
        "Results:",
    ]
    seen: set[str] = set()
    count = 0
    for item in items:
        title = _clean(str(item.get("title", "") or ""))
        url = str(item.get("url", "") or "").strip()
        if not title or not url or url in seen:
            continue
        seen.add(url)
        meta = item.get("metadata") or {}
        source = _clean(str(meta.get("ogSiteName") or meta.get("name") or "")) or urllib.parse.urlparse(url).netloc
        age = _clean(str(item.get("date", "") or ""))
        bits = " • ".join(bit for bit in (source, age) if bit)
        lines.append(f"  {count + 1}. {title}" + (f" [{bits}]" if bits else ""))
        lines.append(f"     {url}")
        detail = _clean(
            str(
                item.get("summary")
                or item.get("description")
                or item.get("snippet")
                or ""
            )
        )
        if detail:
            lines.append(f"     {detail}")
        markdown = _clean(str(item.get("markdown", "") or ""))[:320]
        if markdown and markdown != detail:
            lines.append(f"     Context: {markdown}")
        count += 1
        if count >= route.count:
            break
    if count == 0:
        raise HolonetError("Firecrawl search returned no results")
    return "\n".join(lines)


def _run_firecrawl_scrape(route: HolonetRoute, config: HolonetConfig) -> str:
    data = _json_request(
        "POST",
        f"{_FIRECRAWL_BASE}/scrape",
        headers={"Authorization": f"Bearer {config.effective_firecrawl_key()}"},
        json_body={
            "url": route.url,
            "formats": [{"type": "summary"}, {"type": "markdown"}],
            "onlyMainContent": True,
            "maxAge": 900000,
            "timeout": 60000,
        },
        timeout=45.0,
    )
    payload = data.get("data") or {}
    metadata = payload.get("metadata") or {}
    title = _clean(str(metadata.get("title", "") or "")) or route.url
    summary = _clean(str(payload.get("summary", "") or ""))
    markdown = _clean(str(payload.get("markdown", "") or ""))[:1200]
    source = _clean(
        str(metadata.get("sourceURL", "") or metadata.get("url", "") or "")
    ) or route.url
    lines = [
        "Goal completed via Firecrawl scrape without opening a browser window.",
        f"Article: {title}",
        f"URL: {route.url}",
        f"Source: {urllib.parse.urlparse(source).netloc or source}",
    ]
    if summary:
        lines.extend(["", "Summary:", f"  {summary}"])
    if markdown:
        lines.extend(["", "Excerpt:", f"  {markdown}"])
    return "\n".join(lines)


def _run_europe_pmc(route: HolonetRoute) -> str:
    data = _json_request(
        "GET",
        f"{_EUROPEPMC_BASE}/search",
        params={
            "query": route.query,
            "format": "json",
            "pageSize": route.count,
            "resultType": "core",
        },
        timeout=20.0,
    )
    items = ((data.get("resultList") or {}).get("result")) or []
    lines = [
        "Goal completed via Europe PMC without opening a browser window.",
        f"Query: {route.query}",
        "",
        "Papers:",
    ]
    count = 0
    for item in items:
        title = _clean(str(item.get("title", "") or ""))
        if not title:
            continue
        doi = str(item.get("doi", "") or "").strip()
        pmid = str(item.get("pmid", "") or "").strip()
        url = f"https://doi.org/{doi}" if doi else (
            f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""
        )
        source = _clean(
            str((((item.get("journalInfo") or {}).get("journal") or {}).get("title")) or item.get("source") or "")
        )
        year = str(item.get("pubYear", "") or "").strip()
        cited = str(item.get("citedByCount", "") or "").strip()
        meta = " • ".join(bit for bit in (year, source, f"cited by {cited}" if cited else "") if bit)
        lines.append(f"  {count + 1}. {title}" + (f" [{meta}]" if meta else ""))
        if url:
            lines.append(f"     {url}")
        author_items = ((item.get("authorList") or {}).get("author")) or []
        authors = [
            _clean(str(author.get("fullName", "") or ""))
            for author in author_items[:3]
            if _clean(str(author.get("fullName", "") or ""))
        ]
        if authors:
            lines.append(f"     Authors: {', '.join(authors)}")
        abstract = _clean(str(item.get("abstractText", "") or ""))
        if abstract:
            lines.append(f"     Abstract: {abstract}")
        count += 1
        if count >= route.count:
            break
    if count == 0:
        raise HolonetError("Europe PMC returned no results")
    return "\n".join(lines)


def _run_clinicaltrials(route: HolonetRoute) -> str:
    data = _json_request(
        "GET",
        f"{_CLINICALTRIALS_BASE}/studies",
        params={"query.term": route.query, "pageSize": route.count},
        timeout=20.0,
    )
    items = data.get("studies") or []
    lines = [
        "Goal completed via ClinicalTrials.gov without opening a browser window.",
        f"Query: {route.query}",
        "",
        "Trials:",
    ]
    count = 0
    for item in items[: route.count]:
        protocol = item.get("protocolSection") or {}
        ident = protocol.get("identificationModule") or {}
        status = protocol.get("statusModule") or {}
        design = protocol.get("designModule") or {}
        desc = protocol.get("descriptionModule") or {}
        nct_id = str(ident.get("nctId", "") or "").strip()
        title = _clean(str(ident.get("briefTitle") or ident.get("officialTitle") or ""))
        if not nct_id or not title:
            continue
        phases = [
            _clean(str(phase or ""))
            for phase in (design.get("phases") or [])[:2]
            if _clean(str(phase or ""))
        ]
        meta = " • ".join(
            bit
            for bit in (
                _clean(str(status.get("overallStatus", "") or "")),
                "/".join(phases) if phases else "",
                _clean(str(((status.get("lastUpdatePostDateStruct") or {}).get("date")) or "")),
            )
            if bit
        )
        lines.append(f"  {count + 1}. {title} ({nct_id})" + (f" [{meta}]" if meta else ""))
        lines.append(f"     https://clinicaltrials.gov/study/{nct_id}")
        summary = _clean(str(desc.get("briefSummary", "") or ""))
        if summary:
            lines.append(f"     Summary: {summary}")
        count += 1
    if count == 0:
        lines.append("  No matching registered studies found.")
    return "\n".join(lines)


def _run_biomedical(route: HolonetRoute, config: HolonetConfig) -> str:
    with ThreadPoolExecutor(max_workers=2) as pool:
        papers_future = None
        trials_future = None
        if config.europe_pmc_enabled:
            papers_future = pool.submit(_run_europe_pmc, route)
        if config.clinicaltrials_enabled:
            trials_future = pool.submit(_run_clinicaltrials, route)
        papers_text = papers_future.result() if papers_future is not None else ""
        trials_text = trials_future.result() if trials_future is not None else ""
    sections = [
        "Goal completed via Europe PMC + ClinicalTrials.gov without opening a browser window.",
        f"Query: {route.query}",
        "",
    ]
    if papers_text:
        sections.append(papers_text.split("\n", 3)[3] if "\n" in papers_text else papers_text)
    if trials_text:
        if papers_text:
            sections.append("")
        sections.append(trials_text.split("\n", 3)[3] if "\n" in trials_text else trials_text)
    return "\n".join(section for section in sections if section is not None)


def _verb_for_provider(provider: str) -> str:
    return {
        "brave_search": "web-search",
        "brave_news": "news-search",
        "firecrawl_search": "web-search",
        "firecrawl_scrape": "page-scrape",
        "europe_pmc": "paper-search",
        "clinicaltrials": "trial-search",
        "biomedical_research": "biomedical-search",
    }.get(provider, "web-search")
