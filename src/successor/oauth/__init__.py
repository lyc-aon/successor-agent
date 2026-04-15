"""Stdlib-only OAuth 2.0 device authorization grant for Kimi Code.

All HTTP uses urllib.request. No asyncio, no requests.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

# Kimi Code OAuth constants
KIMI_CODE_CLIENT_ID: str = "17e5f671-d194-4dfb-9706-5516cb48c098"
DEFAULT_OAUTH_HOST: str = "https://auth.kimi.com"


# ─── Data models ───


@dataclass(frozen=True, slots=True)
class OAuthToken:
    access_token: str
    refresh_token: str
    expires_at: float
    scope: str
    token_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> OAuthToken:
        return cls(
            access_token=str(payload.get("access_token", "")),
            refresh_token=str(payload.get("refresh_token", "")),
            expires_at=float(payload.get("expires_at", 0)),
            scope=str(payload.get("scope", "")),
            token_type=str(payload.get("token_type", "Bearer")),
        )

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> OAuthToken:
        """Build from an OAuth token endpoint response.

        Converts ``expires_in`` (seconds from now) to ``expires_at``
        (absolute Unix timestamp).
        """
        expires_in = payload.get("expires_in")
        expires_at = time.time() + float(expires_in) if expires_in else 0.0
        return cls(
            access_token=str(payload.get("access_token", "")),
            refresh_token=str(payload.get("refresh_token", "")),
            expires_at=expires_at,
            scope=str(payload.get("scope", "")),
            token_type=str(payload.get("token_type", "Bearer")),
        )


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    user_code: str
    device_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int | None
    interval: int


# ─── HTTP helper ───


def _http_post_form(
    url: str,
    data: dict[str, str],
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """POST form-encoded data and return (status_code, json_body)."""
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "successor/1.0",
            **(headers or {}),
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30.0) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


# ─── Device flow ───


def request_device_authorization(
    client_id: str = KIMI_CODE_CLIENT_ID,
    oauth_host: str = DEFAULT_OAUTH_HOST,
) -> DeviceAuthorization:
    """Start a device authorization flow.

    Returns a ``DeviceAuthorization`` with the user code and
    verification URL the user must visit.
    """
    url = f"{oauth_host}/api/oauth/device_authorization"
    status, data = _http_post_form(url, {"client_id": client_id})
    if status != 200:
        raise RuntimeError(
            f"device authorization failed (HTTP {status}): {data}"
        )
    return DeviceAuthorization(
        user_code=str(data.get("user_code", "")),
        device_code=str(data.get("device_code", "")),
        verification_uri=str(data.get("verification_uri", "")),
        verification_uri_complete=str(data.get("verification_uri_complete", "")),
        expires_in=int(data["expires_in"]) if "expires_in" in data else None,
        interval=int(data.get("interval", 5)),
    )


def request_device_token(
    device_code: str,
    client_id: str = KIMI_CODE_CLIENT_ID,
    oauth_host: str = DEFAULT_OAUTH_HOST,
) -> tuple[int, dict[str, Any]]:
    """Poll the token endpoint during device flow.

    Returns (status, payload). Caller checks for 200 + access_token
    or inspects payload["error"] for pending / slow_down / expired.
    """
    url = f"{oauth_host}/api/oauth/token"
    return _http_post_form(url, {
        "client_id": client_id,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })


def refresh_access_token(
    refresh_token: str,
    client_id: str = KIMI_CODE_CLIENT_ID,
    oauth_host: str = DEFAULT_OAUTH_HOST,
) -> OAuthToken:
    """Refresh an access token using a refresh token.

    Returns a new ``OAuthToken`` on success.
    Raises ``RuntimeError`` on auth failure.
    """
    url = f"{oauth_host}/api/oauth/token"
    status, data = _http_post_form(url, {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    if status != 200 or "access_token" not in data:
        error = data.get("error", "unknown")
        desc = data.get("error_description", "")
        raise RuntimeError(
            f"token refresh failed: {error}"
            + (f" — {desc}" if desc else "")
        )
    return OAuthToken.from_response(data)


__all__ = [
    "OAuthToken",
    "DeviceAuthorization",
    "KIMI_CODE_CLIENT_ID",
    "DEFAULT_OAUTH_HOST",
    "request_device_authorization",
    "request_device_token",
    "refresh_access_token",
]
