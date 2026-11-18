"""Discord Rich Presence for VorteX AI.

The presence worker is intentionally best-effort: Discord may be closed or the
RPC socket may disappear at any time, so every failure is contained and retried
without blocking a request.
"""

from __future__ import annotations

import threading
import time

APPLICATION_ID = "1572007916725084190"
DETAILS = "Working at Agents"
TEST_STATE = "VorteX AI · Presence test"


class DiscordPresence:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = False
        self._state = "VorteX AI · Chat"
        self._started = int(time.time())
        self._thread: threading.Thread | None = None
        self._client = None
        self._wake = threading.Event()
        self._connected = False
        self._last_error = ""
        self._last_update = 0.0
        self._last_state = ""

    def configure(self, enabled: bool, state: str | None = None) -> dict:
        state = (state or "VorteX AI · Chat").strip()[:128] or "VorteX AI · Chat"
        with self._lock:
            was_enabled = self._enabled
            self._enabled = bool(enabled)
            self._state = state
            if self._enabled and not was_enabled:
                self._started = int(time.time())
            if self._enabled and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(
                    target=self._run, name="vortex-discord-rpc", daemon=True
                )
                self._thread.start()
        self._wake.set()
        if not enabled:
            self._close_client()
        return {"enabled": self._enabled, "state": self._state, **self.status()}

    def status(self) -> dict:
        with self._lock:
            return {
                "connected": self._connected,
                "last_error": self._last_error,
                "last_update": self._last_update,
                "last_state": self._last_state,
            }

    def test(self, timeout: float = 8.0) -> dict:
        """Force a fresh activity update and wait for Discord to acknowledge it."""
        started = time.time()
        with self._lock:
            self._last_update = 0.0
            self._last_state = ""
            self._last_error = ""
        self.configure(True, state=TEST_STATE)
        deadline = started + timeout
        connected = False
        last_update = 0.0
        while time.time() < deadline:
            with self._lock:
                connected = self._connected
                last_update = self._last_update
                last_state = self._last_state
                error = self._last_error
            if connected and last_update >= started and last_state == TEST_STATE:
                return {
                    "accepted": True,
                    "connected": True,
                    "state": TEST_STATE,
                    "error": "",
                    "last_update": last_update,
                }
            if error:
                return {
                    "accepted": False,
                    "connected": connected,
                    "state": TEST_STATE,
                    "error": error,
                    "last_update": last_update,
                }
            time.sleep(0.1)
        return {
            "accepted": False,
            "connected": connected,
            "state": TEST_STATE,
            "error": "Discord did not acknowledge the activity in time. Make sure Discord is running and Activity Privacy is enabled.",
            "last_update": last_update,
        }

    def _read(self) -> tuple[bool, str, int]:
        with self._lock:
            return self._enabled, self._state, self._started

    def _close_client(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
            self._connected = False
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - Discord may already be gone
                pass

    def _run(self) -> None:
        try:
            from pypresence import Presence
        except ImportError as exc:
            with self._lock:
                self._last_error = f"pypresence is not installed: {exc}"
            return
        while True:
            enabled, state, started = self._read()
            if not enabled:
                self._close_client()
                # Stay alive so switching the setting back on reconnects
                # immediately instead of racing a worker that is exiting.
                self._wake.wait(1)
                self._wake.clear()
                continue
            try:
                with self._lock:
                    client = self._client
                if client is None:
                    client = Presence(APPLICATION_ID)
                    client.connect()
                    with self._lock:
                        self._client = client
                        self._connected = True
                        self._last_error = ""
                client.update(
                    details=DETAILS,
                    state=state,
                    start=started,
                    large_text="VorteX AI",
                )
                with self._lock:
                    self._connected = True
                    self._last_error = ""
                    self._last_update = time.time()
                    self._last_state = state
            except Exception as exc:  # noqa: BLE001 - retry when Discord is available
                with self._lock:
                    self._connected = False
                    self._last_error = str(exc)[:300]
                self._close_client()
            # Wake up when the setting or active model changes, and retry
            # periodically while Discord is closed.
            for _ in range(15):
                if self._wake.wait(1):
                    self._wake.clear()
                    break
                enabled, _, _ = self._read()
                if not enabled:
                    break


presence = DiscordPresence()
