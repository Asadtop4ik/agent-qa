"""Bounded CSV import and export helpers for products and orders."""

from __future__ import annotations

import csv
from io import StringIO
from typing import Any, Iterable, Mapping

from agent_qa.errors import ApiError
from agent_qa.schemas import MAX_PRICE_CENTS, MAX_STOCK, MAX_TOTAL_CENTS

PRODUCT_COLUMNS = (
    "id",
    "sku",
    "name",
    "category",
    "price_cents",
    "stock",
    "tags",
    "active",
    "created_at",
    "updated_at",
)
ORDER_COLUMNS = (
    "id",
    "customer_id",
    "total_cents",
    "status",
    "items_count",
    "created_at",
)

_IMPORT_COLUMNS = {
    "products": (
        {"sku", "name", "category", "price_cents"},
        {
            "sku",
            "name",
            "category",
            "price_cents",
            "stock",
            "tags",
            "active",
            "id",
            "created_at",
            "updated_at",
        },
    ),
    "orders": (
        {"customer_id", "total_cents"},
        {
            "customer_id",
            "total_cents",
            "id",
            "status",
            "items_count",
            "created_at",
        },
    ),
}
_MAX_RECORDS = 200
_MAX_CELL_LENGTH = 65536
_MAX_BODY_BYTES = 65536
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _invalid_csv(field: str, message: str) -> ApiError:
    return ApiError(
        400,
        "invalid_csv",
        "Invalid CSV document",
        [{"field": field, "message": message}],
    )


def _unescape_text(value: str) -> str:
    index = 0
    while index < len(value) and value[index] == "'":
        index += 1
    if index and index < len(value) and value[index] in _FORMULA_PREFIXES:
        return value[1:]
    return value


def sanitize_text(value: str) -> str:
    """Prefix spreadsheet formula markers while keeping the escape reversible."""
    index = 0
    while index < len(value) and value[index] == "'":
        index += 1
    if index < len(value) and value[index] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def render_csv(
    columns: Iterable[str],
    rows: Iterable[Mapping[str, Any]],
    text_columns: Iterable[str] = (),
) -> str:
    """Render rows with RFC 4180 CRLF endings and formula-safe text fields."""
    headers = tuple(columns)
    textual = frozenset(text_columns)
    output = StringIO(newline="")
    writer = csv.writer(output, dialect="excel", lineterminator="\r\n")
    writer.writerow(headers)
    for row in rows:
        cells = []
        for column in headers:
            value = row.get(column, "")
            if column in textual and isinstance(value, str):
                value = sanitize_text(value)
            elif column == "tags" and isinstance(value, (list, tuple)):
                value = "|".join(str(tag) for tag in value)
            elif column == "active" and isinstance(value, bool):
                value = "true" if value else "false"
            cells.append(value)
        writer.writerow(cells)
    return output.getvalue()


def parse_csv(text: str, kind: str) -> list[tuple[int, dict[str, str]]]:
    """Parse a structurally valid, bounded product or order CSV document."""
    if kind not in _IMPORT_COLUMNS:
        raise ValueError("kind must be products or orders")
    if not isinstance(text, str):
        raise _invalid_csv("body", "CSV body must be UTF-8 text")
    text = text.removeprefix("\ufeff")
    if len(text) > _MAX_BODY_BYTES:
        raise _invalid_csv("body", "CSV body exceeds 65536 bytes")
    try:
        encoded_length = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise _invalid_csv("body", "CSV body is not valid UTF-8 text") from None
    if encoded_length > _MAX_BODY_BYTES:
        raise _invalid_csv("body", "CSV body exceeds 65536 bytes")
    if not text:
        raise _invalid_csv("body", "CSV body must contain a header and data rows")
    reader = csv.reader(StringIO(text, newline=""), dialect="excel", strict=True)
    try:
        header = next(reader)
    except StopIteration:
        raise _invalid_csv(
            "body", "CSV body must contain a header and data rows"
        ) from None
    except csv.Error:
        raise _invalid_csv("header", "Malformed CSV header") from None
    required, allowed = _IMPORT_COLUMNS[kind]
    if not header or any(not item for item in header):
        raise _invalid_csv("header", "Header names must not be empty")
    if len(header) != len(set(header)):
        raise _invalid_csv("header", "Duplicate header names are not allowed")
    if any(item not in allowed for item in header):
        raise _invalid_csv("header", "Unknown header name")
    if not required.issubset(header):
        raise _invalid_csv("header", "Required header is missing")
    parsed: list[tuple[int, dict[str, str]]] = []
    try:
        for record_number, cells in enumerate(reader, start=2):
            if not cells:
                raise _invalid_csv("body", "Blank records are not allowed")
            if len(cells) != len(header):
                raise _invalid_csv("body", "Record has the wrong number of columns")
            if any(len(cell) > _MAX_CELL_LENGTH for cell in cells):
                raise _invalid_csv("body", "CSV field is too long")
            if len(parsed) >= _MAX_RECORDS:
                raise _invalid_csv("body", "CSV may contain at most 200 data rows")
            parsed.append((record_number, dict(zip(header, cells))))
    except csv.Error:
        raise _invalid_csv("body", "Malformed CSV record") from None
    if not parsed:
        raise _invalid_csv("body", "CSV must contain at least one data row")
    return parsed


def _parse_integer(value: str, maximum: int) -> int | None:
    # Bound conversion while preserving the normal schema validator's error model.
    stripped = value.strip()
    negative = stripped.startswith("-")
    digits = stripped[1:] if stripped[:1] in {"+", "-"} else stripped
    if len(stripped) > 32 and digits.isdigit():
        significant = digits.lstrip("0") or "0"
        if len(significant) > 10:
            return -1 if negative else maximum + 1
        stripped = ("-" if negative else "") + significant
    try:
        return int(stripped)
    except ValueError:
        return None


def coerce_row(kind: str, values: Mapping[str, str]) -> dict[str, Any]:
    """Convert writable CSV columns to the ordinary JSON creation shape."""
    if kind not in _IMPORT_COLUMNS:
        raise ValueError("kind must be products or orders")
    text_fields = (
        {"sku", "name", "category", "tags", "created_at", "updated_at"}
        if kind == "products"
        else {"customer_id", "status", "created_at"}
    )
    row = {
        key: _unescape_text(value) if key in text_fields else value
        for key, value in values.items()
    }
    if kind == "products":
        result: dict[str, Any] = {
            key: row[key] for key in ("sku", "name", "category", "price_cents")
        }
        result["price_cents"] = _parse_integer(row["price_cents"], MAX_PRICE_CENTS)
        if "stock" in row:
            result["stock"] = _parse_integer(row["stock"], MAX_STOCK)
        if "tags" in row:
            result["tags"] = row["tags"].split("|") if row["tags"] else []
        if "active" in row:
            value = row["active"].lower()
            result["active"] = (
                value in {"true", "1"} if value in {"true", "false", "1", "0"} else None
            )
        return result
    return {
        "customer_id": row["customer_id"],
        "total_cents": _parse_integer(row["total_cents"], MAX_TOTAL_CENTS),
    }
