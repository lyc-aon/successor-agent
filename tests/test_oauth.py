"""Tests for the OAuth client, storage, and refresh worker.

All tests are hermetic — the credentials dir is monkeypatched to a
temp directory so nothing touches the real filesystem.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from successor.oauth import (
    DeviceAuthorization,
    OAuthToken,
)


# ─── OAuthToken round-trip ───


def test_oauth_token_to_dict_round_trip() -> None:
    token = OAuthToken(
        access_token="at_123",
        refresh_token="rt_456",
        expires_at=1700000000.0,
        scope="read write",
        token_type="Bearer",
    )
    d = token.to_dict()
    assert d["access_token"] == "at_123"
    assert d["refresh_token"] == "rt_456"
    assert d["expires_at"] == 1700000000.0

    restored = OAuthToken.from_dict(d)
    assert restored == token


def test_oauth_token_from_response() -> None:
    now = time.time()
    payload = {
        "access_token": "new_at",
        "refresh_token": "new_rt",
        "expires_in": 3600,
        "scope": "coding",
        "token_type": "Bearer",
    }
    token = OAuthToken.from_response(payload)
    assert token.access_token == "new_at"
    assert token.refresh_token == "new_rt"
    # expires_at should be approximately now + 3600
    assert abs(token.expires_at - (now + 3600)) < 5


def test_oauth_token_from_dict_missing_fields() -> None:
    token = OAuthToken.from_dict({})
    assert token.access_token == ""
    assert token.refresh_token == ""
    assert token.expires_at == 0.0


def test_device_authorization_fields() -> None:
    auth = DeviceAuthorization(
        user_code="ABCD-1234",
        device_code="dc_xyz",
        verification_uri="https://auth.kimi.com/device",
        verification_uri_complete="https://auth.kimi.com/device?code=ABCD-1234",
        expires_in=600,
        interval=5,
    )
    assert auth.user_code == "ABCD-1234"
    assert auth.interval == 5


# ─── Storage ───


@pytest.fixture
def cred_dir(temp_config_dir: Path) -> Path:
    """Provide a credentials dir inside the hermetic config dir."""
    d = temp_config_dir / "credentials"
    d.mkdir(exist_ok=True)
    return d


def test_save_and_load_token(cred_dir: Path) -> None:
    from successor.oauth.storage import save_token, load_token

    token = OAuthToken(
        access_token="at_save",
        refresh_token="rt_save",
        expires_at=1700000000.0,
        scope="read",
        token_type="Bearer",
    )
    save_token("oauth/kimi-code", token)
    loaded = load_token("oauth/kimi-code")
    assert loaded is not None
    assert loaded.access_token == "at_save"


def test_load_token_missing(cred_dir: Path) -> None:
    from successor.oauth.storage import load_token

    assert load_token("oauth/nonexistent") is None


def test_load_token_corrupt(cred_dir: Path) -> None:
    from successor.oauth.storage import load_token

    bad_file = cred_dir / "kimi-code.json"
    bad_file.write_text("not json{{", encoding="utf-8")
    assert load_token("oauth/kimi-code") is None


def test_delete_token(cred_dir: Path) -> None:
    from successor.oauth.storage import save_token, load_token, delete_token

    token = OAuthToken("at", "rt", 0.0, "", "Bearer")
    save_token("oauth/kimi-code", token)
    assert load_token("oauth/kimi-code") is not None
    delete_token("oauth/kimi-code")
    assert load_token("oauth/kimi-code") is None


def test_delete_token_idempotent(cred_dir: Path) -> None:
    from successor.oauth.storage import delete_token

    delete_token("oauth/never-existed")  # should not raise


# ─── Refresh worker ───


class _FakeClient:
    def __init__(self):
        self.api_key = "initial"


def test_worker_refreshes_when_within_threshold(temp_config_dir: Path, monkeypatch) -> None:
    from successor.oauth.worker import OAuthRefreshWorker
    from successor.oauth.storage import save_token
    import successor.oauth

    # Create credentials dir in the hermetic config dir
    (temp_config_dir / "credentials").mkdir(exist_ok=True)

    # Save a token that expires in 60 seconds (within 300s threshold)
    token = OAuthToken("old_at", "rt_refresh", time.time() + 60, "read", "Bearer")
    save_token("oauth/test", token)

    # Mock refresh by patching the module attribute directly
    new_token = OAuthToken("new_at", "new_rt", time.time() + 3600, "read", "Bearer")
    monkeypatch.setattr(successor.oauth, "refresh_access_token",
        lambda *a, **kw: new_token,
    )

    from successor.profiles.profile import OAuthRef
    client = _FakeClient()
    ref = OAuthRef(storage="file", key="oauth/test")
    worker = OAuthRefreshWorker(ref, client, interval_s=0.1, threshold_s=300.0)
    worker.start()

    # Wait for at least one check cycle
    time.sleep(0.5)
    worker.stop()

    assert client.api_key == "new_at"
    assert worker.last_error is None


def test_worker_does_nothing_when_token_healthy(temp_config_dir: Path, monkeypatch) -> None:
    from successor.oauth.worker import OAuthRefreshWorker
    from successor.oauth.storage import save_token
    import successor.oauth

    (temp_config_dir / "credentials").mkdir(exist_ok=True)

    # Save a token that expires in 1 hour (way past 300s threshold)
    token = OAuthToken("healthy_at", "rt", time.time() + 3600, "read", "Bearer")
    save_token("oauth/test", token)

    refresh_called = []
    def mock_refresh(*a, **kw):
        refresh_called.append(True)
        return OAuthToken("should_not", "happen", 0, "", "Bearer")

    monkeypatch.setattr(successor.oauth, "refresh_access_token", mock_refresh)

    from successor.profiles.profile import OAuthRef
    client = _FakeClient()
    ref = OAuthRef(storage="file", key="oauth/test")
    worker = OAuthRefreshWorker(ref, client, interval_s=0.1, threshold_s=300.0)
    worker.start()
    time.sleep(0.5)
    worker.stop()

    assert not refresh_called
    # Worker only mutates api_key on refresh — healthy tokens leave it alone
    assert client.api_key == "initial"


def test_worker_captures_error(temp_config_dir: Path, monkeypatch) -> None:
    from successor.oauth.worker import OAuthRefreshWorker
    from successor.oauth.storage import save_token
    import successor.oauth

    (temp_config_dir / "credentials").mkdir(exist_ok=True)

    # Token about to expire
    token = OAuthToken("old_at", "rt", time.time() + 60, "read", "Bearer")
    save_token("oauth/test", token)

    def mock_refresh(*a, **kw):
        raise RuntimeError("network failure")

    monkeypatch.setattr(successor.oauth, "refresh_access_token", mock_refresh)

    from successor.profiles.profile import OAuthRef
    client = _FakeClient()
    ref = OAuthRef(storage="file", key="oauth/test")
    worker = OAuthRefreshWorker(ref, client, interval_s=0.1, threshold_s=300.0)
    worker.start()
    time.sleep(0.5)
    worker.stop()

    assert worker.last_error is not None
    assert "network failure" in worker.last_error
