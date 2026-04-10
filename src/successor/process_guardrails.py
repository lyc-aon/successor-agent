"""Small process/port guardrails for local-agent shell behavior.

These helpers are intentionally narrow. The goal is not to prevent the
model from managing processes at all; it is to stop one specific,
high-impact failure mode:

- "I want port X, so I will kill whatever owns port X"

That behavior is acceptable for a server the agent started itself, but
it is catastrophic when X is the active provider endpoint for Successor.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping
from urllib.parse import urlparse


_LOCAL_HOSTS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
}

_RECLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "lsof-xargs-kill",
        re.compile(
            r"\blsof\s+-ti:?(?P<port>\d{2,5})\b[^\n]*?\|\s*xargs\b[^\n]*?\bkill\b",
            re.IGNORECASE,
        ),
    ),
    (
        "kill-subshell-lsof",
        re.compile(
            r"\bkill(?:all)?\b[^\n]*?\$\(\s*lsof\s+-ti:?(?P<port>\d{2,5})\b[^)]*\)",
            re.IGNORECASE,
        ),
    ),
    (
        "fuser-kill",
        re.compile(
            r"\bfuser\b[^\n]*?\s-k\b[^\n]*?\b(?P<port>\d{2,5})/(?:tcp|udp)\b",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ReservedPortConflict:
    port: int
    label: str
    pattern: str


def reserved_local_ports_from_urls(
    items: Iterable[tuple[str, str | None]],
) -> dict[int, str]:
    """Return local loopback ports keyed to a human-readable label."""
    ports: dict[int, str] = {}
    for label, raw_url in items:
        url = str(raw_url or "").strip()
        if not url:
            continue
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in _LOCAL_HOSTS:
            continue
        port = parsed.port
        if port is None:
            if parsed.scheme == "http":
                port = 80
            elif parsed.scheme == "https":
                port = 443
        if port is None:
            continue
        ports.setdefault(int(port), label.strip() or f"local endpoint {port}")
    return ports


def detect_reserved_port_reclaim(
    command: str,
    reserved_ports: Mapping[int, str],
) -> ReservedPortConflict | None:
    """Return a conflict when the command reclaims a reserved port by force."""
    if not reserved_ports:
        return None
    normalized = " ".join(str(command or "").split())
    if not normalized:
        return None
    for pattern_name, pattern in _RECLAIM_PATTERNS:
        match = pattern.search(normalized)
        if match is None:
            continue
        port = int(match.group("port"))
        label = reserved_ports.get(port)
        if label is None:
            continue
        return ReservedPortConflict(
            port=port,
            label=label,
            pattern=pattern_name,
        )
    return None
