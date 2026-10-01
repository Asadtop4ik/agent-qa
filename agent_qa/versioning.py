"""Shared API version metadata, response headers, and sunset policy."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from typing import Any, Mapping

from agent_qa import settings

DEPRECATED_AT = datetime(2026, 10, 1, tzinfo=timezone.utc)
SUNSET_AT = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
DEPRECATED_EPOCH = 1790812800
SUNSET_HTTP_DATE = format_datetime(SUNSET_AT, usegmt=True)


def versions_document() -> dict[str, object]:
    """Return the stable public API version document."""
    return {
        "versions": [
            {
                "version": "1",
                "status": "deprecated",
                "deprecated_at": "2026-10-01T00:00:00Z",
                "sunset": "2026-12-31T23:59:59Z",
                "base_path": "/orders",
            },
            {"version": "2", "status": "current", "base_path": "/v2/orders"},
        ]
    }


def utcnow() -> datetime:
    """Return the current UTC time; kept separate for deterministic tests."""
    return datetime.now(timezone.utc)


def response_headers(
    route: Mapping[str, Any], path_params: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build version and deprecation headers from route table metadata."""
    version = route.get("api_version")
    if version is None:
        return {}
    headers = {"X-API-Version": str(version)}
    if not route.get("deprecated"):
        return headers
    successor = route.get("successor")
    if isinstance(successor, str):
        for name, value in (path_params or {}).items():
            successor = successor.replace("{" + name + "}", value)
    headers.update(
        {
            "Deprecation": f"@{DEPRECATED_EPOCH}",
            "Sunset": SUNSET_HTTP_DATE,
        }
    )
    if isinstance(successor, str):
        headers["Link"] = f'<{successor}>; rel="successor-version"'
    return headers


def sunset_reached(
    route: Mapping[str, Any],
    *,
    now: datetime | None = None,
    enforce: bool | None = None,
) -> bool:
    """Whether an opted-in deprecated route has passed its sunset instant."""
    if not route.get("deprecated"):
        return False
    if enforce is None:
        loaded = settings.current()
        enforce = bool(loaded.values["AGENT_QA_ENFORCE_SUNSET"])
    if not enforce:
        return False
    current = now if now is not None else utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) >= SUNSET_AT


def map_v2_field_path(field: str) -> str:
    """Translate shared-store error fields into the v2 request vocabulary."""
    if field.startswith("body."):
        field = field[5:]
    aliases = {
        "customer_id": "customer.id",
        "total_cents": "amount.total_cents",
    }
    return aliases.get(field, field)


def map_v2_error_body(body: object) -> object:
    """Copy an error envelope with fields translated for the v2 surface."""
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return body
    result = dict(body)
    error = dict(body["error"])
    details = error.get("details")
    if isinstance(details, list):
        error["details"] = [
            {**detail, "field": map_v2_field_path(detail["field"])}
            if isinstance(detail, dict) and isinstance(detail.get("field"), str)
            else detail
            for detail in details
        ]
    result["error"] = error
    return result
