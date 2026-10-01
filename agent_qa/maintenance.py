"""Thread-safe process-local maintenance mode state."""

from __future__ import annotations

from datetime import datetime, timezone
import threading


class MaintenanceState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = False
        self._message: str | None = None
        self._retry_after_seconds = 30
        self._since: str | None = None

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "message": self._message,
                "retry_after_seconds": self._retry_after_seconds,
                "since": self._since,
            }

    def update(
        self,
        enabled: bool,
        message: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> dict[str, object]:
        with self._lock:
            was_enabled = self._enabled
            self._enabled = enabled
            if retry_after_seconds is not None:
                self._retry_after_seconds = retry_after_seconds
            if enabled:
                if message is not None:
                    self._message = message
                elif not was_enabled:
                    self._message = "Service is temporarily unavailable for maintenance"
                if not was_enabled:
                    self._since = (
                        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    )
            else:
                self._message = None
                self._since = None
            return {
                "enabled": self._enabled,
                "message": self._message,
                "retry_after_seconds": self._retry_after_seconds,
                "since": self._since,
            }


MAINTENANCE = MaintenanceState()
