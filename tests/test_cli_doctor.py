"""CLI coverage for `successor doctor` provider diagnostics."""

from __future__ import annotations

import argparse

from successor.cli import cmd_doctor
from successor.profiles import Profile
from successor.providers.llama import LlamaCppRuntimeCapabilities
from successor.web.browser import BrowserRuntimeStatus


class _FakeClient:
    base_url = "http://localhost:8080"
    model = "local"

    def health(self) -> bool:
        return True

    def detect_context_window(self) -> int:
        return 262144

    def detect_runtime_capabilities(self) -> LlamaCppRuntimeCapabilities:
        return LlamaCppRuntimeCapabilities(
            context_window=262144,
            total_slots=4,
            endpoint_slots=True,
            supports_parallel_tool_calls=True,
        )


def test_doctor_reports_llama_runtime_capabilities(
    monkeypatch,
    capsys,
) -> None:
    profile = Profile(
        name="doctor-test",
        provider={
            "type": "llamacpp",
            "base_url": "http://localhost:8080",
            "model": "local",
        },
    )
    monkeypatch.setattr(
        "successor.profiles.get_active_profile",
        lambda: profile,
    )
    monkeypatch.setattr(
        "successor.providers.make_provider",
        lambda cfg: _FakeClient(),
    )

    assert cmd_doctor(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "slots       4 total (/slots on)" in out
    assert "tool calls  parallel supported" in out


def test_doctor_reports_holonet_and_browser_status(
    monkeypatch,
    capsys,
) -> None:
    profile = Profile(
        name="doctor-web",
        provider={
            "type": "llamacpp",
            "base_url": "http://localhost:8080",
            "model": "local",
        },
        tools=("holonet", "browser"),
        tool_config={
            "holonet": {
                "default_provider": "auto",
                "brave_enabled": False,
                "firecrawl_enabled": False,
                "europe_pmc_enabled": True,
                "clinicaltrials_enabled": True,
                "biomedical_enabled": True,
            },
            "browser": {"channel": "chrome"},
        },
    )
    monkeypatch.setattr(
        "successor.profiles.get_active_profile",
        lambda: profile,
    )
    monkeypatch.setattr(
        "successor.providers.make_provider",
        lambda cfg: _FakeClient(),
    )
    monkeypatch.setattr(
        "successor.web.browser_runtime_status",
        lambda *_args, **_kwargs: BrowserRuntimeStatus(
            package_available=True,
            python_executable="/usr/bin/python3",
            using_external_runtime=True,
            channel="chrome",
            executable_path="",
            user_data_dir="/tmp/browser",
        ),
    )

    assert cmd_doctor(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "holonet     default=auto" in out
    assert "holonet ok  europe_pmc, clinicaltrials, biomedical_research" in out
    assert "browser     playwright ready" in out
    assert "browser py  /usr/bin/python3 (external runtime)" in out
