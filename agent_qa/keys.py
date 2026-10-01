"""In-memory API key storage with hash-only secret retention."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from agent_qa.errors import ApiError

_ROLE_RANK = {"read": 1, "write": 2, "admin": 3}
_MAX_ACTIVE_KEYS = 20
_MAX_SECRET_LENGTH = 4096


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()


@dataclass
class _Key:
    key_id: str
    role: str
    label: str
    created_at: datetime
    secret_hash: bytes
    last_used_at: datetime | None = None
    prev_hash: bytes | None = None
    grace_expires_at: datetime | None = None


class KeyStore:
    """Thread-safe, in-memory API key store; only secret hashes are retained."""

    def __init__(self, bootstrap_secret: str, clock: Callable[[], datetime] = _utc_now):
        self._clock = clock
        self._lock = threading.RLock()
        self._keys: dict[str, _Key] = {
            "bootstrap": _Key(
                "bootstrap",
                "admin",
                "bootstrap",
                self._now(),
                _digest(bootstrap_secret),
            )
        }
        self._next_id = 1

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def authenticate(self, secret: str | None) -> dict[str, str] | None:
        """Match every retained digest, without returning early on a match."""
        if not isinstance(secret, str):
            secret = ""
            usable = False
        else:
            usable = 0 < len(secret) <= _MAX_SECRET_LENGTH
        try:
            supplied_hash = _digest(secret if usable else "")
        except UnicodeEncodeError:
            supplied_hash = _digest("")
            usable = False
        with self._lock:
            now = self._now()
            matched: _Key | None = None
            for record in self._keys.values():
                current_match = hmac.compare_digest(supplied_hash, record.secret_hash)
                previous_match = False
                if record.prev_hash is not None:
                    previous_match = hmac.compare_digest(
                        supplied_hash, record.prev_hash
                    )
                previous_valid = (
                    record.grace_expires_at is not None
                    and now < record.grace_expires_at
                )
                if usable and (current_match or (previous_match and previous_valid)):
                    if (
                        matched is None
                        or _ROLE_RANK[record.role] > _ROLE_RANK[matched.role]
                    ):
                        matched = record
            if matched is None:
                return None
            matched.last_used_at = now
            return {
                "key_id": matched.key_id,
                "role": matched.role,
                "label": matched.label,
            }

    def create(self, role: str, label: str) -> dict[str, str]:
        if not isinstance(role, str) or role not in _ROLE_RANK:
            raise ApiError(400, "invalid_role", "Role must be read, write, or admin")
        if not isinstance(label, str) or not 1 <= len(label) <= 40 or not label.strip():
            raise ApiError(400, "invalid_label", "Label must contain 1-40 characters")
        try:
            label.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ApiError(
                400, "invalid_label", "Label must contain valid Unicode"
            ) from error
        with self._lock:
            if len(self._keys) - 1 >= _MAX_ACTIVE_KEYS:
                raise ApiError(409, "key_limit", "Maximum number of API keys reached")
            key_id = f"key_{self._next_id}"
            self._next_id += 1
            secret = f"qa_{role}_{secrets.token_hex(12)}"
            created = self._now()
            self._keys[key_id] = _Key(key_id, role, label, created, _digest(secret))
            return {
                "key_id": key_id,
                "role": role,
                "label": label,
                "key": secret,
                "created_at": _isoformat(created) or "",
            }

    def list_keys(self) -> dict[str, object]:
        with self._lock:
            now = self._now()
            items: list[dict[str, object]] = []
            for record in self._keys.values():
                grace = (
                    record.prev_hash is not None
                    and record.grace_expires_at is not None
                    and now < record.grace_expires_at
                )
                items.append(
                    {
                        "key_id": record.key_id,
                        "role": record.role,
                        "label": record.label,
                        "created_at": _isoformat(record.created_at),
                        "last_used_at": _isoformat(record.last_used_at),
                        "fingerprint": record.secret_hash.hex()[:8],
                        "status": "grace" if grace else "active",
                    }
                )
            return {"items": items, "total": len(items)}

    def rotate(self, key_id: str, grace_seconds: int = 0) -> dict[str, str]:
        if (
            isinstance(grace_seconds, bool)
            or not isinstance(grace_seconds, int)
            or not 0 <= grace_seconds <= 300
        ):
            raise ApiError(400, "invalid_grace_seconds", "grace_seconds must be 0-300")
        with self._lock:
            record = self._get(key_id)
            if key_id == "bootstrap":
                raise ApiError(
                    409, "bootstrap_key_immutable", "Bootstrap key is immutable"
                )
            now = self._now()
            secret = f"qa_{record.role}_{secrets.token_hex(12)}"
            record.prev_hash = record.secret_hash if grace_seconds else None
            record.grace_expires_at = (
                now + timedelta(seconds=grace_seconds) if grace_seconds else None
            )
            record.secret_hash = _digest(secret)
            return {
                "key_id": record.key_id,
                "role": record.role,
                "label": record.label,
                "key": secret,
                "created_at": _isoformat(record.created_at) or "",
            }

    def revoke(self, key_id: str) -> None:
        with self._lock:
            self._get(key_id)
            if key_id == "bootstrap":
                raise ApiError(
                    409, "bootstrap_key_immutable", "Bootstrap key is immutable"
                )
            del self._keys[key_id]

    def _get(self, key_id: str) -> _Key:
        record = self._keys.get(key_id)
        if record is None:
            raise ApiError(404, "key_not_found", "API key not found")
        return record
