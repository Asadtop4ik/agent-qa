"""Thread-safe, bounded token-bucket request limiter."""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from agent_qa import settings
from agent_qa.errors import ApiError

_IDENTITY = re.compile(r"^(key|client|ip):[A-Za-z0-9._:-]{1,64}$")
_KINDS = frozenset({"key", "client", "ip"})


_IMPORT_SETTINGS = settings.current()
DEFAULT_BURST = _IMPORT_SETTINGS.values["AGENT_QA_RATE_BURST"]
DEFAULT_REFILL_PER_SECOND = _IMPORT_SETTINGS.values["AGENT_QA_RATE_REFILL_PER_SECOND"]


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_after: int
    retry_after: int


@dataclass
class _Bucket:
    tokens: float
    updated_at: float
    touched_at: float


class TokenBucketLimiter:
    """Apply bounded per-identity token buckets with temporary overrides."""

    def __init__(
        self,
        burst: int = DEFAULT_BURST,
        refill_per_second: float = DEFAULT_REFILL_PER_SECOND,
        *,
        clock: Callable[[], float] = time.monotonic,
        capacity: int = 1000,
        max_overrides: int = 100,
    ) -> None:
        self._validate_policy(burst, refill_per_second)
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if (
            isinstance(max_overrides, bool)
            or not isinstance(max_overrides, int)
            or max_overrides < 1
        ):
            raise ValueError("max_overrides must be a positive integer")
        self._burst = burst
        self._refill = float(refill_per_second)
        self._clock = clock
        self._capacity = capacity
        self._max_overrides = max_overrides
        self._lock = threading.RLock()
        self._buckets: dict[str, _Bucket] = {}
        self._overrides: dict[str, tuple[int, float]] = {}

    @staticmethod
    def _validate_policy(burst: int, refill_per_second: float) -> None:
        if (
            isinstance(burst, bool)
            or not isinstance(burst, int)
            or not 1 <= burst <= 100000
        ):
            raise ValueError("burst must be an integer from 1 to 100000")
        if isinstance(refill_per_second, bool) or not isinstance(
            refill_per_second, (int, float)
        ):
            raise ValueError("refill_per_second must be from 0.001 to 10000")
        if isinstance(refill_per_second, float) and not math.isfinite(
            refill_per_second
        ):
            raise ValueError("refill_per_second must be from 0.001 to 10000")
        if not 0.001 <= refill_per_second <= 10000:
            raise ValueError("refill_per_second must be from 0.001 to 10000")

    def _now(self) -> float:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("clock must return a finite number")
        value = float(value)
        if not math.isfinite(value):
            raise TypeError("clock must return a finite number")
        return value

    def _policy(self, identity: str) -> tuple[int, float]:
        return self._overrides.get(identity, (self._burst, self._refill))

    def _new_bucket(self, identity: str, burst: int, now: float) -> _Bucket | None:
        if len(self._buckets) >= self._capacity:
            full = []
            for key, bucket in self._buckets.items():
                existing_burst, existing_refill = self._policy(key)
                projected = min(
                    float(existing_burst),
                    bucket.tokens + max(0.0, now - bucket.updated_at) * existing_refill,
                )
                if projected >= existing_burst:
                    bucket.tokens = projected
                    bucket.updated_at = now
                    full.append((key, bucket))
            if not full:
                return None
            victim = min(full, key=lambda pair: pair[1].touched_at)[0]
            del self._buckets[victim]
        bucket = _Bucket(float(burst), now, now)
        self._buckets[identity] = bucket
        return bucket

    def _refresh(self, bucket: _Bucket, burst: int, refill: float, now: float) -> None:
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(float(burst), bucket.tokens + elapsed * refill)
        bucket.updated_at = now
        bucket.touched_at = now

    def consume(self, identity: str, kind: str) -> RateLimitDecision:
        """Consume one token and return headers-ready timing values."""
        if (
            not isinstance(identity, str)
            or len(identity) > 71
            or not _IDENTITY.fullmatch(identity)
        ):
            raise ValueError("invalid rate-limit identity")
        if (
            not isinstance(kind, str)
            or kind not in _KINDS
            or identity.split(":", 1)[0] != kind
        ):
            raise ValueError("invalid rate-limit identity kind")
        with self._lock:
            now = self._now()
            burst, refill = self._policy(identity)
            bucket = self._buckets.get(identity)
            if bucket is None:
                bucket = self._new_bucket(identity, burst, now)
                if bucket is None:
                    return RateLimitDecision(
                        False,
                        burst,
                        0,
                        math.ceil(burst / refill),
                        math.ceil(1 / refill),
                    )
            self._refresh(bucket, burst, refill, now)
            if bucket.tokens >= 1:
                bucket.tokens -= 1
                allowed = True
            else:
                allowed = False
            remaining = math.floor(bucket.tokens) if allowed else 0
            reset_after = math.ceil(max(0.0, burst - bucket.tokens) / refill)
            retry_after = max(1, math.ceil(max(0.0, 1.0 - bucket.tokens) / refill))
            return RateLimitDecision(
                allowed, burst, remaining, reset_after, retry_after
            )

    def snapshot(self) -> dict[str, object]:
        """Return the public defaults, overrides, and active bucket count."""
        with self._lock:
            return {
                "default": {
                    "burst": self._burst,
                    "refill_per_second": self._refill,
                },
                "overrides": {
                    identity: {
                        "burst": burst,
                        "refill_per_second": refill,
                    }
                    for identity, (burst, refill) in sorted(self._overrides.items())
                },
                "buckets": len(self._buckets),
            }

    def set_override(self, identity: str, burst: int, refill_per_second: float) -> None:
        """Set a policy override and reset its bucket to full."""
        self._validate_identity(identity)
        self._validate_policy(burst, refill_per_second)
        with self._lock:
            if (
                identity not in self._overrides
                and len(self._overrides) >= self._max_overrides
            ):
                raise ApiError(
                    409, "override_limit", "Maximum rate-limit overrides reached"
                )
            self._overrides[identity] = (burst, float(refill_per_second))
            self._buckets.pop(identity, None)

    def delete_override(self, identity: str) -> bool:
        """Delete an override and reset its bucket, returning whether it existed."""
        self._validate_identity(identity)
        with self._lock:
            if identity not in self._overrides:
                return False
            del self._overrides[identity]
            self._buckets.pop(identity, None)
            return True

    @staticmethod
    def _validate_identity(identity: str) -> None:
        if (
            not isinstance(identity, str)
            or len(identity) > 71
            or not _IDENTITY.fullmatch(identity)
        ):
            raise ValueError("invalid rate-limit identity")


RATE_LIMITER = TokenBucketLimiter()
