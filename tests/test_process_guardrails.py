"""Unit coverage for narrow local process/port guardrails."""

from __future__ import annotations

from successor.process_guardrails import (
    detect_reserved_port_reclaim,
    reserved_local_ports_from_urls,
)


def test_reserved_local_ports_only_keeps_loopback_endpoints() -> None:
    ports = reserved_local_ports_from_urls([
        ("provider", "http://127.0.0.1:8080"),
        ("vision", "https://localhost:8443/v1"),
        ("remote", "https://api.openai.com/v1"),
        ("wildcard", "http://0.0.0.0:3000"),
    ])

    assert ports == {
        8080: "provider",
        8443: "vision",
        3000: "wildcard",
    }


def test_detect_reserved_port_reclaim_matches_common_kill_patterns() -> None:
    reserved = {8080: "active provider endpoint"}

    lsof_conflict = detect_reserved_port_reclaim(
        "lsof -ti:8080 | xargs -r kill",
        reserved,
    )
    subshell_conflict = detect_reserved_port_reclaim(
        "kill $(lsof -ti:8080)",
        reserved,
    )
    fuser_conflict = detect_reserved_port_reclaim(
        "fuser -k 8080/tcp",
        reserved,
    )

    assert lsof_conflict is not None
    assert lsof_conflict.port == 8080
    assert subshell_conflict is not None
    assert subshell_conflict.pattern == "kill-subshell-lsof"
    assert fuser_conflict is not None
    assert fuser_conflict.pattern == "fuser-kill"


def test_detect_reserved_port_reclaim_ignores_non_reserved_ports() -> None:
    assert detect_reserved_port_reclaim(
        "lsof -ti:4173 | xargs -r kill",
        {8080: "active provider endpoint"},
    ) is None
