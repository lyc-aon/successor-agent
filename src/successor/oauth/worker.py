"""Background OAuth token refresh worker.

Follows the same threading pattern as ``_CacheWarmer`` and
``_CompactionWorker`` in ``chat.py``. Runs in a daemon thread,
sleeps between checks, and mutates ``client.api_key`` directly
when a refresh succeeds.
"""

from __future__ import annotations

import threading
import time


class OAuthRefreshWorker:
    """Threaded worker that periodically refreshes an OAuth token.

    The caller is responsible for loading the initial token and
    setting ``client.api_key`` before starting the worker.
    """

    __slots__ = (
        "_oauth_ref", "_client", "_interval_s", "_threshold_s",
        "_thread", "_stop", "last_error",
    )

    def __init__(
        self,
        oauth_ref,
        client,
        *,
        interval_s: float = 60.0,
        threshold_s: float = 300.0,
    ) -> None:
        self._oauth_ref = oauth_ref
        self._client = client
        self._interval_s = interval_s
        self._threshold_s = threshold_s
        self._thread: threading.Thread | None = None
        self._stop: threading.Event | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self.last_error = None
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="successor-oauth-refresh",
        )
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():  # type: ignore[union-attr]
            try:
                self._check_and_refresh()
            except Exception as exc:
                self.last_error = str(exc)
            self._stop.wait(timeout=self._interval_s)  # type: ignore[union-attr]

    def _check_and_refresh(self) -> None:
        # Import the module (not individual functions) so that
        # monkeypatched attributes are visible at call time in tests.
        import successor.oauth as _oauth_mod
        from .storage import load_token, save_token

        token = load_token(self._oauth_ref.key)
        if token is None or not token.refresh_token:
            return
        now = time.time()
        if not token.expires_at or (token.expires_at - now) >= self._threshold_s:
            return
        new_token = _oauth_mod.refresh_access_token(
            token.refresh_token,
            client_id=_oauth_mod.KIMI_CODE_CLIENT_ID,
            oauth_host=_oauth_mod.DEFAULT_OAUTH_HOST,
        )
        save_token(self._oauth_ref.key, new_token)
        self._client.api_key = new_token.access_token
        self.last_error = None
