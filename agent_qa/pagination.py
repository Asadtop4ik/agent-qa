"""Signed, process-local cursors and shared keyset pagination helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from typing import Any

from agent_qa.errors import ApiError
from agent_qa.schemas import (
    MAX_LIMIT,
    MAX_PRICE_CENTS,
    MAX_PRODUCT_NAME_LENGTH,
    MIN_LIMIT,
    PRODUCT_SORTS,
)

CURSOR_KEY = secrets.token_bytes(32)
MAX_CURSOR_LENGTH = 4096
_TOKEN_RE = re.compile(rf"^[A-Za-z0-9_.-]{{1,{MAX_CURSOR_LENGTH}}}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_CURSOR_INT = (1 << 63) - 1
_SUPPORTED_SORTS = frozenset(PRODUCT_SORTS) | {"id", "-id"}


def filter_fingerprint(filters: dict[str, Any]) -> str:
    """Return a stable digest for normalized filters, excluding paging/sort fields."""
    excluded = {"limit", "offset", "pagination", "cursor", "sort"}
    normalized = {
        name: value for name, value in filters.items() if name not in excluded
    }
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _cursor_error(code: str, message: str) -> ApiError:
    return ApiError(400, code, message, [{"field": "cursor", "message": message}])


def _valid_key(sort: str, key: Any) -> bool:
    if not isinstance(key, list) or len(key) != 2:
        return False
    value, row_id = key
    if (
        isinstance(row_id, bool)
        or not isinstance(row_id, int)
        or not 1 <= row_id <= _MAX_CURSOR_INT
    ):
        return False
    if sort not in _SUPPORTED_SORTS:
        return False
    field = sort.lstrip("-")
    if field == "id":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 1 <= value <= _MAX_CURSOR_INT
            and value == row_id
        )
    if field == "price_cents":
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= MAX_PRICE_CENTS
        )
    if field == "name":
        return isinstance(value, str) and 1 <= len(value) <= MAX_PRODUCT_NAME_LENGTH
    if field == "created_at":
        return isinstance(value, str) and 1 <= len(value) <= 32
    return False


def encode_cursor(sort: str, fingerprint: str, key: list[Any]) -> str:
    """Encode a sort key and bind it to its filter and sort using process HMAC."""
    if (
        not isinstance(sort, str)
        or not 1 <= len(sort) <= 64
        or not isinstance(fingerprint, str)
        or not _FINGERPRINT_RE.fullmatch(fingerprint)
        or not _valid_key(sort, key)
    ):
        raise ValueError("invalid cursor payload")
    payload = {"v": 1, "s": sort, "f": fingerprint, "k": key}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    signature = hmac.new(CURSOR_KEY, body.encode("ascii"), hashlib.sha256).hexdigest()[
        :16
    ]
    return f"{body}.{signature}"


def decode_cursor(cursor: str, sort: str, fingerprint: str) -> list[Any]:
    """Validate an opaque cursor and return its (sort value, id) key."""
    if not isinstance(cursor, str) or not _TOKEN_RE.fullmatch(cursor):
        raise _cursor_error("invalid_cursor", "Invalid cursor")
    try:
        body, signature = cursor.split(".")
        expected = hmac.new(
            CURSOR_KEY, body.encode("ascii"), hashlib.sha256
        ).hexdigest()[:16]
        if not re.fullmatch(r"[0-9a-f]{16}", signature) or not hmac.compare_digest(
            signature, expected
        ):
            raise ValueError
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw.decode("ascii"))
        if not isinstance(payload, dict) or set(payload) != {"v", "s", "f", "k"}:
            raise ValueError
        if type(payload["v"]) is not int or payload["v"] != 1:
            raise ValueError
        if (
            not isinstance(payload["s"], str)
            or not isinstance(payload["f"], str)
            or not _FINGERPRINT_RE.fullmatch(payload["f"])
            or not _valid_key(payload["s"], payload["k"])
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise _cursor_error("invalid_cursor", "Invalid cursor") from None
    if payload["s"] != sort or payload["f"] != fingerprint:
        raise _cursor_error("cursor_mismatch", "Cursor does not match filters or sort")
    return payload["k"]


def keyset_page(
    rows: list[dict[str, Any]],
    sort_spec: tuple[str, bool],
    cursor_key: list[Any] | None,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Sort rows by primary key and ascending ID ties, then return limit+1 probe."""
    field, descending = sort_spec
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not MIN_LIMIT <= limit <= MAX_LIMIT
    ):
        raise ValueError(f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}")
    if cursor_key is not None and not _valid_key(field, cursor_key):
        raise _cursor_error("invalid_cursor", "Invalid cursor")
    ordered = sorted(rows, key=lambda row: row["id"])
    ordered.sort(key=lambda row: row[field], reverse=descending)
    if cursor_key is not None:
        after_value, after_id = cursor_key
        if not ordered:
            return [], False
        if type(after_value) is not type(ordered[0][field]):
            raise _cursor_error("invalid_cursor", "Invalid cursor")
        minimum = 0 if field == "price_cents" else 1
        maximum = MAX_PRICE_CENTS if field == "price_cents" else _MAX_CURSOR_INT
        if isinstance(after_value, int) and not minimum <= after_value <= maximum:
            raise _cursor_error("invalid_cursor", "Invalid cursor")
        if descending:
            ordered = [
                row
                for row in ordered
                if row[field] < after_value
                or (row[field] == after_value and row["id"] > after_id)
            ]
        else:
            ordered = [
                row
                for row in ordered
                if row[field] > after_value
                or (row[field] == after_value and row["id"] > after_id)
            ]
    probed = ordered[: limit + 1]
    return probed[:limit], len(probed) > limit
