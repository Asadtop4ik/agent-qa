"""Bounded, thread-safe token-bucket rate limiting."""

from __future__ import annotations

import math
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from agent_qa.errors import ApiError

DEFAULT_BURST = 120
DEFAULT_REFILL_PER_SECOND = 60.0
MAX_BURST = 100_000
MAX_REFILL_PER_SECOND = 10_000.0
MAX_BUCKETS = 1000
MAX_OVERRIDES = 100
_IDENTITY_PATTERN = re.compile(r"^(key|client|ip):[A-Za-z0-9._:-]{1,64}$")


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ[name])
    except (KeyError, ValueError, OverflowError):
        return default
    return value if minimum <= value <= maximum else default


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ[name])
    except (KeyError, ValueError, OverflowError):
        return default
    return value if math.isfinite(value) and minimum <= value <= maximum else default


def validate_identity(identity: object) -> bool:
    """Return whether an override identity has the supported bounded form."""
    return isinstance(identity, str) and bool(_IDENTITY_PATTERN.fullmatch(identity))


def validate_policy(burst: object, refill_per_second: object) -> tuple[int, float]:
    """Validate and normalize a burst/refill pair or raise a public 400 error."""
    valid_burst = (
        isinstance(burst, int)
        and not isinstance(burst, bool)
        and 1 <= burst <= MAX_BURST
    )
    valid_refill = (
        isinstance(refill_per_second, (int, float))
        and not isinstance(refill_per_second, bool)
        and 0.001 <= refill_per_second <= MAX_REFILL_PER_SECOND
        and math.isfinite(refill_per_second)
    )
    if not valid_burst or not valid_refill:
        raise ApiError(
            400,
            "validation_error",
            "Invalid rate limit policy",
            [{"field": "policy", "message": "Burst or refill is out of range"}],
        )
    return burst, float(refill_per_second)


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass(frozen=True)
class ConsumeResult:
    allowed: bool
    headers: dict[str, str]


class TokenBucketLimiter:
    """A bounded token-bucket table with per-identity policy overrides."""

    def __init__(
        self,
        burst: int = DEFAULT_BURST,
        refill_per_second: float = DEFAULT_REFILL_PER_SECOND,
        clock: Callable[[], float] = time.monotonic,
        max_buckets: int = MAX_BUCKETS,
        max_overrides: int = MAX_OVERRIDES,
    ) -> None:
        self.burst, self.refill_per_second = validate_policy(burst, refill_per_second)
        if (
            isinstance(max_buckets, bool)
            or not isinstance(max_buckets, int)
            or max_buckets < 1
        ):
            raise ValueError("max_buckets must be a positive integer")
        if (
            isinstance(max_overrides, bool)
            or not isinstance(max_overrides, int)
            or max_overrides < 0
        ):
            raise ValueError("max_overrides must be a non-negative integer")
        self.max_buckets = min(max_buckets, MAX_BUCKETS)
        self.max_overrides = min(max_overrides, MAX_OVERRIDES)
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()
        self._overrides: dict[str, tuple[int, float]] = {}

    def _policy(self, identity: str) -> tuple[int, float]:
        return self._overrides.get(identity, (self.burst, self.refill_per_second))

    def _refill(self, bucket: _Bucket, now: float, rate: float, capacity: int) -> None:
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(float(capacity), bucket.tokens + elapsed * rate)
        bucket.updated_at = max(bucket.updated_at, now)

    def _least_recent_full_bucket(self, now: float) -> str | None:
        for key, bucket in self._buckets.items():
            capacity, refill = self._policy(key)
            self._refill(bucket, now, refill, capacity)
            if bucket.tokens >= capacity:
                return key
        return None

    def _seconds_until_bucket_full(self, now: float) -> int:
        waits = []
        for key, bucket in self._buckets.items():
            capacity, refill = self._policy(key)
            self._refill(bucket, now, refill, capacity)
            waits.append(math.ceil(max(0.0, capacity - bucket.tokens) / refill))
        return max(1, min(waits, default=1))

    def consume(self, identity: str) -> ConsumeResult:
        """Consume one token and return standard rate-limit headers."""
        if not isinstance(identity, str) or not identity or len(identity) > 80:
            raise ValueError("identity must be a non-empty bounded string")
        now = self._clock()
        with self._lock:
            burst, refill = self._policy(identity)
            bucket = self._buckets.get(identity)
            if bucket is None:
                if len(self._buckets) >= self.max_buckets:
                    evictable = self._least_recent_full_bucket(now)
                    if evictable is None:
                        retry_after = self._seconds_until_bucket_full(now)
                        return ConsumeResult(
                            False,
                            {
                                "RateLimit-Limit": str(burst),
                                "RateLimit-Remaining": "0",
                                "RateLimit-Reset": str(retry_after),
                                "Retry-After": str(retry_after),
                            },
                        )
                    del self._buckets[evictable]
                bucket = _Bucket(float(burst), now)
                self._buckets[identity] = bucket
            else:
                self._buckets.move_to_end(identity)
            self._refill(bucket, now, refill, burst)
            allowed = bucket.tokens >= 1.0
            if allowed:
                bucket.tokens -= 1.0
            remaining = math.floor(bucket.tokens)
            reset = math.ceil(max(0.0, burst - bucket.tokens) / refill)
            headers = {
                "RateLimit-Limit": str(burst),
                "RateLimit-Remaining": str(remaining),
                "RateLimit-Reset": str(reset),
            }
            if not allowed:
                headers["Retry-After"] = str(
                    max(1, math.ceil((1.0 - bucket.tokens) / refill))
                )
                headers["RateLimit-Remaining"] = "0"
            return ConsumeResult(allowed, headers)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "default": {
                    "burst": self.burst,
                    "refill_per_second": self.refill_per_second,
                },
                "overrides": {
                    identity: {
                        "burst": policy[0],
                        "refill_per_second": policy[1],
                    }
                    for identity, policy in sorted(self._overrides.items())
                },
                "buckets": len(self._buckets),
            }

    def set_override(
        self, identity: str, burst: object, refill_per_second: object
    ) -> dict[str, object]:
        if not validate_identity(identity):
            raise ApiError(
                400,
                "validation_error",
                "Invalid identity",
                [{"field": "identity", "message": "Invalid identity"}],
            )
        normalized_burst, normalized_refill = validate_policy(burst, refill_per_second)
        with self._lock:
            if (
                identity not in self._overrides
                and len(self._overrides) >= self.max_overrides
            ):
                raise ApiError(
                    409, "override_limit", "Rate limit override limit reached"
                )
            self._overrides[identity] = (normalized_burst, normalized_refill)
            bucket = self._buckets.get(identity)
            now = self._clock()
            if bucket is None:
                if len(self._buckets) >= self.max_buckets:
                    evictable = self._least_recent_full_bucket(now)
                    if evictable is not None:
                        del self._buckets[evictable]
                    else:
                        # An explicit admin reset may replace the LRU partial
                        # bucket when ordinary full-only eviction cannot make room.
                        self._buckets.popitem(last=False)
                self._buckets[identity] = _Bucket(float(normalized_burst), now)
            else:
                bucket.tokens = float(normalized_burst)
                bucket.updated_at = now
                self._buckets.move_to_end(identity)
        return {
            "identity": identity,
            "burst": normalized_burst,
            "refill_per_second": normalized_refill,
        }

    def delete_override(self, identity: str) -> None:
        if not validate_identity(identity):
            raise ApiError(
                400,
                "validation_error",
                "Invalid identity",
                [{"field": "identity", "message": "Invalid identity"}],
            )
        with self._lock:
            if identity not in self._overrides:
                raise ApiError(
                    404, "override_not_found", "Rate limit override not found"
                )
            del self._overrides[identity]
            self._buckets.pop(identity, None)


LIMITER = TokenBucketLimiter(
    _env_int("AGENT_QA_RATE_BURST", DEFAULT_BURST, 1, MAX_BURST),
    _env_float(
        "AGENT_QA_RATE_REFILL_PER_SECOND",
        DEFAULT_REFILL_PER_SECOND,
        0.001,
        MAX_REFILL_PER_SECOND,
    ),
)
