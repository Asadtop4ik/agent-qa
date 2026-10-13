"""Thread-safe in-memory storage for idempotent request results."""

from __future__ import annotations

import copy
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from threading import Condition, Event
from time import monotonic


@dataclass(frozen=True)
class Response:
    status: int
    body: object
    headers: dict[str, str]


class BeginKind(Enum):
    NEW = auto()
    REPLAY = auto()
    MISMATCH = auto()
    IN_PROGRESS = auto()


@dataclass(frozen=True)
class BeginResult:
    kind: BeginKind
    response: Response | None = None


NEW = BeginResult(BeginKind.NEW)
MISMATCH = BeginResult(BeginKind.MISMATCH)
IN_PROGRESS = BeginResult(BeginKind.IN_PROGRESS)


@dataclass
class _Entry:
    fingerprint: str
    completed_at: float | None = None
    response: Response | None = None
    event: Event | None = None


class IdempotencyStore:
    """Keep recent successful replies and mark requests currently being handled."""

    def __init__(
        self,
        ttl_seconds: int = 600,
        capacity: int = 500,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not 1 <= ttl_seconds <= 86400:
            raise ValueError("ttl_seconds must be between 1 and 86400")
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.ttl_seconds = ttl_seconds
        self.capacity = capacity
        self.clock = clock
        self._entries: OrderedDict[object, _Entry] = OrderedDict()
        self._condition = Condition()

    def _expire(self, now: float) -> None:
        expired = [
            scope
            for scope, entry in self._entries.items()
            if entry.completed_at is not None
            and now - entry.completed_at >= self.ttl_seconds
        ]
        for scope in expired:
            del self._entries[scope]

    def begin(self, scope: object, fingerprint: str) -> BeginResult:
        """Claim a scope, replay its response, or report a conflicting request."""
        with self._condition:
            while True:
                now = self.clock()
                self._expire(now)
                entry = self._entries.get(scope)
                if entry is not None:
                    self._entries.move_to_end(scope)
                    if entry.event is not None:
                        return IN_PROGRESS
                    if entry.fingerprint != fingerprint:
                        return MISMATCH
                    assert entry.response is not None
                    return BeginResult(
                        BeginKind.REPLAY,
                        Response(
                            entry.response.status,
                            copy.deepcopy(entry.response.body),
                            copy.deepcopy(entry.response.headers),
                        ),
                    )
                if len(self._entries) < self.capacity:
                    self._entries[scope] = _Entry(fingerprint, event=Event())
                    return NEW
                # Evict the least recently used completed entry. Pending work is
                # never discarded.
                completed_scope = next(
                    (
                        key
                        for key, candidate in self._entries.items()
                        if candidate.event is None
                    ),
                    None,
                )
                if completed_scope is not None:
                    del self._entries[completed_scope]
                    continue
                # The fixed-size store is also an admission bound for concurrent
                # requests; reject new work when every slot holds active work.
                return IN_PROGRESS

    def complete(self, scope: object, fingerprint: str, response: Response) -> None:
        """Save a successful response and signal that its active claim ended."""
        with self._condition:
            entry = self._entries.get(scope)
            if entry is None or entry.fingerprint != fingerprint:
                return
            if not 200 <= response.status < 300:
                self._remove_pending(scope, entry)
                return
            entry.response = Response(
                response.status,
                copy.deepcopy(response.body),
                copy.deepcopy(response.headers),
            )
            entry.completed_at = self.clock()
            assert entry.event is not None
            entry.event.set()
            entry.event = None
            self._entries.move_to_end(scope)
            self._condition.notify_all()

    def abort(self, scope: object, fingerprint: str) -> None:
        """Release a failed request so a corrected retry can claim its key."""
        with self._condition:
            entry = self._entries.get(scope)
            if entry is not None and entry.fingerprint == fingerprint:
                self._remove_pending(scope, entry)

    def _remove_pending(self, scope: object, entry: _Entry) -> None:
        if entry.event is not None:
            entry.event.set()
            del self._entries[scope]
            self._condition.notify_all()
