"""Holonet route and response parsing coverage."""

from __future__ import annotations

import json

from successor.web import HolonetConfig, resolve_route, run_holonet


class _JsonResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_resolve_route_auto_prefers_biomedical_for_research_query() -> None:
    cfg = HolonetConfig(
        brave_enabled=False,
        firecrawl_enabled=False,
        europe_pmc_enabled=True,
        clinicaltrials_enabled=True,
        biomedical_enabled=True,
    )
    route = resolve_route({"query": "semaglutide obesity clinical research"}, cfg)
    assert route.provider == "biomedical_research"


def test_run_holonet_formats_brave_results(monkeypatch) -> None:
    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        assert "api.search.brave.com" in req.full_url
        return _JsonResponse(
            {
                "web": {
                    "results": [
                        {
                            "title": "Llama.cpp slots guide",
                            "url": "https://example.com/slots",
                            "description": "How slots work in llama.cpp.",
                            "meta_url": {"netloc": "example.com"},
                            "page_age": "2d",
                        }
                    ]
                }
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    cfg = HolonetConfig(brave_api_key="brave-key")
    route = resolve_route({"provider": "brave_search", "query": "llama.cpp slots"}, cfg)
    result = run_holonet(route, cfg)

    assert "Brave Search web API" in result.output
    assert "Llama.cpp slots guide" in result.output
    assert "example.com" in result.output


def test_run_holonet_formats_firecrawl_scrape(monkeypatch) -> None:
    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        assert "firecrawl.dev" in req.full_url
        return _JsonResponse(
            {
                "data": {
                    "summary": "A concise article summary.",
                    "markdown": "# Heading\n\nBody text here.",
                    "metadata": {
                        "title": "Successor article",
                        "sourceURL": "https://news.example.com/story",
                    },
                }
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    cfg = HolonetConfig(firecrawl_api_key="firecrawl-key")
    route = resolve_route(
        {"provider": "firecrawl_scrape", "url": "https://news.example.com/story"},
        cfg,
    )
    result = run_holonet(route, cfg)

    assert "Firecrawl scrape" in result.output
    assert "Successor article" in result.output
    assert "A concise article summary." in result.output


def test_run_holonet_formats_europe_pmc(monkeypatch) -> None:
    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        assert "europepmc" in req.full_url
        return _JsonResponse(
            {
                "resultList": {
                    "result": [
                        {
                            "title": "Semaglutide obesity review",
                            "doi": "10.1000/example",
                            "pubYear": 2026,
                            "citedByCount": 12,
                            "journalInfo": {"journal": {"title": "Medical Journal"}},
                            "authorList": {"author": [{"fullName": "Ada Lovelace"}]},
                            "abstractText": "A review of semaglutide evidence.",
                        }
                    ]
                }
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    cfg = HolonetConfig(
        brave_enabled=False,
        firecrawl_enabled=False,
        europe_pmc_enabled=True,
    )
    route = resolve_route({"provider": "europe_pmc", "query": "semaglutide obesity"}, cfg)
    result = run_holonet(route, cfg)

    assert "Europe PMC" in result.output
    assert "Semaglutide obesity review" in result.output
    assert "Medical Journal" in result.output


def test_run_holonet_formats_clinical_trials(monkeypatch) -> None:
    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        assert "clinicaltrials.gov" in req.full_url
        return _JsonResponse(
            {
                "studies": [
                    {
                        "protocolSection": {
                            "identificationModule": {
                                "nctId": "NCT01234567",
                                "briefTitle": "Semaglutide obesity study",
                            },
                            "statusModule": {
                                "overallStatus": "RECRUITING",
                                "lastUpdatePostDateStruct": {"date": "2026-03-01"},
                            },
                            "designModule": {"phases": ["PHASE3"]},
                            "descriptionModule": {"briefSummary": "Tests semaglutide."},
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    cfg = HolonetConfig(
        brave_enabled=False,
        firecrawl_enabled=False,
        clinicaltrials_enabled=True,
    )
    route = resolve_route({"provider": "clinicaltrials", "query": "semaglutide obesity"}, cfg)
    result = run_holonet(route, cfg)

    assert "ClinicalTrials.gov" in result.output
    assert "NCT01234567" in result.output
    assert "RECRUITING" in result.output
