"""Shared per-item handling for bounded bulk creation requests."""

from __future__ import annotations

import logging
from typing import Any, Callable

from agent_qa.errors import ApiError

logger = logging.getLogger(__name__)

_MAX_ITEMS = 50


def validate_bulk_input(items: Any, atomic: Any) -> None:
    """Validate common bulk request fields before any storage is locked."""
    details: list[dict[str, str]] = []
    if not isinstance(items, list) or not 1 <= len(items) <= _MAX_ITEMS:
        details.append({"field": "items", "message": "Must contain 1 to 50 items"})
    if not isinstance(atomic, bool):
        details.append({"field": "atomic", "message": "Must be a boolean"})
    if details:
        details.sort(key=lambda item: item["field"])
        raise ApiError(400, "validation_error", "Request validation failed", details)


def _item_error(error: ApiError) -> dict[str, Any]:
    return {
        "code": error.code,
        "message": error.message,
        "details": error.details,
    }


def run_bulk(
    items: list[Any],
    apply_one: Callable[[Any], dict[str, Any]],
    rollback: Callable[[], None],
    atomic: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Apply items in order and optionally undo all successes after a failure."""
    validate_bulk_input(items, atomic)
    results: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0

    for index, item in enumerate(items):
        try:
            data = apply_one(item)
        except ApiError as error:
            failed += 1
            results.append(
                {"index": index, "status": error.status, "error": _item_error(error)}
            )
        except Exception:
            logger.exception("Unexpected error applying bulk item at index %d", index)
            if atomic:
                try:
                    rollback()
                except Exception:
                    logger.exception("Unexpected error rolling back bulk request")
                    raise
            raise
        else:
            succeeded += 1
            results.append({"index": index, "status": 201, "data": data})

    if atomic and failed:
        try:
            rollback()
        except Exception:
            logger.exception("Unexpected error rolling back failed bulk request")
            raise
        results = [
            (
                result
                if "error" in result
                else {
                    "index": result["index"],
                    "status": 424,
                    "error": {
                        "code": "rolled_back",
                        "message": "Rolled back because another item failed",
                    },
                }
            )
            for result in results
        ]
        succeeded = 0
        failed = len(items)
        status = 422
    elif failed == 0:
        status = 201
    elif succeeded:
        status = 207
    else:
        status = 422

    return status, {
        "results": results,
        "summary": {"total": len(items), "succeeded": succeeded, "failed": failed},
    }
