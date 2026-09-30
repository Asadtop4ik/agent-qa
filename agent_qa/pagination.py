"""Bounded, process-local keyset pagination helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from typing import Any

from agent_qa.errors import ApiError
from agent_qa.schemas import MAX_PRICE_CENTS

MAX_CURSOR_LENGTH = 4096
_MAX_CURSOR_PAYLOAD_BYTES = MAX_CURSOR_LENGTH * 3 // 4
_CURSOR_KEY = secrets.token_bytes(32)
_TOKEN_RE = re.compile(rf"[A-Za-z0-9_.-]{{1,{MAX_CURSOR_LENGTH}}}\Z")
_SORTS = {
    "id",
    "-id",
    "price_cents",
    "-price_cents",
    "name",
    "-name",
    "created_at",
}
_FILTER_OMIT = {"pagination", "cursor", "limit", "offset", "sort"}


def filter_fingerprint(filters: dict[str, Any]) -> str:
    """Return a stable digest for the filters that constrain a listing."""
    selected = {key: value for key, value in filters.items() if key not in _FILTER_OMIT}
    encoded = json.dumps(
        selected, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def encode_cursor(
    sort_spec: str, fingerprint: str, key: tuple[Any, int] | list[Any]
) -> str:
    """Encode a signed keyset boundary for this process."""
    if (
        sort_spec not in _SORTS
        or not isinstance(fingerprint, str)
        or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
        or not isinstance(key, (tuple, list))
        or len(key) != 2
    ):
        raise ValueError("invalid cursor fields")
    value, row_id = key
    field = sort_spec.lstrip("-")
    if (
        isinstance(row_id, bool)
        or not isinstance(row_id, int)
        or not 1 <= row_id <= 9_223_372_036_854_775_807
    ):
        raise ValueError("invalid cursor key")
    if field == "id":
        valid_value = (
            isinstance(value, int) and not isinstance(value, bool) and value == row_id
        )
    elif field == "price_cents":
        valid_value = (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= MAX_PRICE_CENTS
        )
    else:
        valid_value = isinstance(value, str) and len(value) <= 256
    if not valid_value:
        raise ValueError("invalid cursor key")
    payload = json.dumps(
        {"v": 1, "s": sort_spec, "f": fingerprint, "k": list(key)},
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = hmac.digest(_CURSOR_KEY, encoded.encode("ascii"), "sha256").hex()[:16]
    return f"{encoded}.{signature}"


def _invalid_cursor() -> ApiError:
    return ApiError(400, "invalid_cursor", "Invalid cursor")


def decode_cursor(token: str, sort_spec: str, fingerprint: str) -> tuple[Any, int]:
    """Validate and decode a signed cursor; distinguish stale query state."""
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise _invalid_cursor()
    encoded, separator, signature = token.partition(".")
    if not separator or not encoded or not re.fullmatch(r"[0-9a-f]{16}", signature):
        raise _invalid_cursor()
    expected = hmac.digest(_CURSOR_KEY, encoded.encode("ascii"), "sha256").hex()[:16]
    if not hmac.compare_digest(signature, expected):
        raise _invalid_cursor()
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if len(raw) > _MAX_CURSOR_PAYLOAD_BYTES:
            raise ValueError
        payload = json.loads(raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise _invalid_cursor() from None
    if not isinstance(payload, dict) or set(payload) != {"v", "s", "f", "k"}:
        raise _invalid_cursor()
    key = payload["k"]
    if (
        type(payload["v"]) is not int
        or payload["v"] != 1
        or not isinstance(payload["s"], str)
        or payload["s"] not in _SORTS
        or not isinstance(payload["f"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["f"])
        or not isinstance(key, list)
        or len(key) != 2
    ):
        raise _invalid_cursor()
    value, row_id = key
    field = payload["s"].lstrip("-")
    if (
        isinstance(row_id, bool)
        or not isinstance(row_id, int)
        or not 1 <= row_id <= 9_223_372_036_854_775_807
    ):
        raise _invalid_cursor()
    if field == "id":
        if isinstance(value, bool) or not isinstance(value, int) or value != row_id:
            raise _invalid_cursor()
    elif field == "price_cents":
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= MAX_PRICE_CENTS
        ):
            raise _invalid_cursor()
    elif field in {"name", "created_at"}:
        if not isinstance(value, str) or len(value) > 256:
            raise _invalid_cursor()
    else:
        raise _invalid_cursor()
    if payload["s"] != sort_spec or payload["f"] != fingerprint:
        raise ApiError(400, "cursor_mismatch", "Cursor does not match query")
    return value, row_id


def _sort_value(row: dict[str, Any], sort_spec: str) -> Any:
    field = sort_spec.lstrip("-")
    # Product offset ordering is case-sensitive; cursor ordering matches it.
    return row[field]


def _after_cursor(
    row: dict[str, Any], sort_spec: str, cursor_key: tuple[Any, int]
) -> bool:
    value = _sort_value(row, sort_spec)
    cursor_value, cursor_id = cursor_key
    descending = sort_spec.startswith("-")
    if value == cursor_value:
        return row["id"] > cursor_id
    return value < cursor_value if descending else value > cursor_value


def keyset_page(
    rows: list[dict[str, Any]],
    sort_spec: str,
    cursor_key: tuple[Any, int] | None,
    limit: int,
) -> tuple[list[dict[str, Any]], tuple[Any, int] | None]:
    """Sort rows deterministically and select a page plus its next boundary."""
    if (
        sort_spec not in _SORTS
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
    ):
        raise ValueError("invalid keyset parameters")
    field = sort_spec.lstrip("-")
    if field == "id":
        ordered = sorted(rows, key=lambda row: row["id"], reverse=sort_spec == "-id")
    else:
        ordered = sorted(rows, key=lambda row: row["id"])
        ordered.sort(
            key=lambda row: _sort_value(row, sort_spec),
            reverse=sort_spec.startswith("-"),
        )
    if cursor_key is not None:
        ordered = [row for row in ordered if _after_cursor(row, sort_spec, cursor_key)]
    candidates = ordered[: limit + 1]
    page = candidates[:limit]
    next_key = None
    if len(candidates) > limit and page:
        last = page[-1]
        next_key = (_sort_value(last, sort_spec), last["id"])
    return page, next_key
