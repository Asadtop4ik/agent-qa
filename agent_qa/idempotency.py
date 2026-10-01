"""Thread-safe, bounded storage for successful idempotent HTTP responses."""

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from threading import Event, RLock
from time import monotonic
from typing import Callable


@dataclass(frozen=True)
class StoredResponse:
    """A response snapshot safe to replay after a successful operation."""

    status: int
    body: object
    headers: dict[str, str]


@dataclass(frozen=True)
class BeginDecision:
    """The outcome of reserving or looking up a request scope."""

    kind: str
    response: StoredResponse | None = None


@dataclass
class _Entry:
    fingerprint: str
    event: Event
    expires_at: float | None = None
    response: StoredResponse | None = None


class IdempotencyStore:
    """Keep completed response snapshots and in-flight request reservations."""

    def __init__(
        self,
        ttl_seconds: int = 600,
        capacity: int = 500,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or not 1 <= ttl_seconds <= 86400
        ):
            raise ValueError("ttl_seconds must be between 1 and 86400")
        if (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or not 1 <= capacity <= 500
        ):
            raise ValueError("capacity must be between 1 and 500")
        self.ttl_seconds = ttl_seconds
        self.capacity = capacity
        self._clock = clock
        self._entries: OrderedDict[tuple[str, ...], _Entry] = OrderedDict()
        self._lock = RLock()

    def _expire(self, now: float) -> None:
        expired = [
            scope
            for scope, entry in self._entries.items()
            if entry.expires_at is not None and entry.expires_at <= now
        ]
        for scope in expired:
            self._entries.pop(scope, None)

    def begin(self, scope: tuple[str, ...], fingerprint: str) -> BeginDecision:
        """Return new, replay, mismatch, or in_progress for a request scope."""
        now = self._clock()
        with self._lock:
            self._expire(now)
            entry = self._entries.get(scope)
            if entry is not None:
                self._entries.move_to_end(scope)
                if entry.fingerprint != fingerprint:
                    return BeginDecision("mismatch")
                if entry.response is not None:
                    return BeginDecision("replay", deepcopy(entry.response))
                return BeginDecision("in_progress")

            while len(self._entries) >= self.capacity:
                oldest_completed = next(
                    (
                        candidate
                        for candidate, value in self._entries.items()
                        if value.response is not None
                    ),
                    None,
                )
                if oldest_completed is None:
                    # Keep active reservations so a full cache cannot permit a
                    # duplicate side effect. The caller reports this as busy.
                    return BeginDecision("in_progress")
                self._entries.pop(oldest_completed).event.set()

            self._entries[scope] = _Entry(fingerprint, Event())
            return BeginDecision("new")

    def complete(
        self,
        scope: tuple[str, ...],
        response: StoredResponse,
        *,
        allowed_statuses: tuple[int, ...] = (),
    ) -> None:
        """Save a successful or explicitly allowed response and release reservation."""
        if any(
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 400 <= status <= 599
            for status in allowed_statuses
        ):
            raise ValueError("allowed_statuses must contain HTTP error statuses")
        if (
            isinstance(response.status, bool)
            or not isinstance(response.status, int)
            or not 200 <= response.status < 300
            and response.status not in allowed_statuses
        ):
            raise ValueError("response status is not allowed to be stored")
        with self._lock:
            entry = self._entries.get(scope)
            if entry is None or entry.response is not None:
                return
            entry.response = deepcopy(response)
            entry.expires_at = self._clock() + self.ttl_seconds
            entry.event.set()
            self._entries.move_to_end(scope)

    def abort(self, scope: tuple[str, ...]) -> None:
        """Release a failed operation so the key can be retried."""
        with self._lock:
            entry = self._entries.pop(scope, None)
            if entry is not None:
                entry.event.set()

    def clear(self) -> None:
        """Release all reservations and cached replies during tenant purge."""
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            entry.event.set()
