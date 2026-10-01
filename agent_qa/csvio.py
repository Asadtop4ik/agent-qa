"""CSV serialization and bounded import helpers for products and orders."""

from __future__ import annotations

import csv
from io import StringIO
from typing import Any, Iterable, Mapping

from agent_qa.errors import ApiError
from agent_qa.orders import OrderError, validate_create as validate_order_create
from agent_qa.products import validate_create as validate_product_create

MAX_IMPORT_ROWS = 200
MAX_EXPORT_ROWS = 1000
PRODUCT_IMPORT_HEADERS = (
    "sku",
    "name",
    "category",
    "price_cents",
    "stock",
    "tags",
    "active",
)
PRODUCT_REQUIRED_HEADERS = ("sku", "name", "category", "price_cents")
ORDER_IMPORT_HEADERS = ("customer_id", "total_cents")


class CsvStructureError(ValueError):
    """Malformed CSV syntax or an invalid import header/row shape."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def sanitize_cell(value: Any, *, text: bool = False) -> Any:
    """Prefix dangerous spreadsheet formulas in textual cells only."""
    if (
        text
        and isinstance(value, str)
        and value.startswith(("=", "+", "-", "@", "\t", "\r"))
    ):
        return "'" + value
    return value


def render_csv(
    headers: Iterable[str],
    rows: Iterable[Mapping[str, Any] | Iterable[Any]],
    *,
    text_columns: set[str] | frozenset[str] = frozenset(),
) -> str:
    """Render rows as RFC 4180 CSV using CRLF and minimal quoting."""
    columns = tuple(headers)
    output = StringIO(newline="")
    writer = csv.writer(output, dialect="excel", lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        if isinstance(row, Mapping):
            values = [
                sanitize_cell(row.get(name, ""), text=name in text_columns)
                for name in columns
            ]
        else:
            values = list(row)
            if len(values) != len(columns):
                raise ValueError("CSV row width does not match header")
            values = [
                sanitize_cell(value, text=columns[index] in text_columns)
                for index, value in enumerate(values)
            ]
        writer.writerow(values)
    return output.getvalue()


def parse_csv(
    text: str,
    expected_headers: Iterable[str],
    required_headers: Iterable[str],
) -> list[dict[str, str]]:
    """Parse a bounded CSV document and validate header and record widths."""
    if not isinstance(text, str):
        raise CsvStructureError("body", "CSV body must be UTF-8 text")
    text = text.removeprefix("\ufeff")
    _validate_csv_quoting(text)
    reader = csv.reader(StringIO(text, newline=""), strict=True)
    try:
        headers = next(reader)
    except StopIteration as error:
        raise CsvStructureError("header", "CSV must include a header row") from error
    except csv.Error as error:
        raise CsvStructureError("header", "Malformed CSV header") from error

    allowed = tuple(expected_headers)
    required = set(required_headers)
    if not headers or any(header == "" for header in headers):
        raise CsvStructureError("header", "Header names must not be empty")
    if len(headers) != len(set(headers)):
        raise CsvStructureError("header", "Duplicate header name")
    if any(header not in allowed for header in headers):
        raise CsvStructureError("header", "Unknown header name")
    if not required.issubset(headers):
        raise CsvStructureError("header", "Missing required header")

    records: list[dict[str, str]] = []
    try:
        for row in reader:
            if len(records) >= MAX_IMPORT_ROWS:
                raise CsvStructureError("body", "CSV must contain at most 200 rows")
            if len(row) != len(headers):
                raise CsvStructureError(
                    "body", "CSV row has the wrong number of fields"
                )
            records.append(dict(zip(headers, row)))
    except csv.Error as error:
        raise CsvStructureError("body", "Malformed CSV body") from error
    if not records:
        raise CsvStructureError("header", "CSV must contain at least one data row")
    return records


def _validate_csv_quoting(text: str) -> None:
    """Reject quote placement that csv.reader otherwise accepts permissively."""
    field_start, unquoted, quoted, after_quote = range(4)
    state = field_start
    index = 0
    first_record = True
    while index < len(text):
        char = text[index]
        if state == quoted:
            if char == '"':
                if index + 1 < len(text) and text[index + 1] == '"':
                    index += 2
                    continue
                state = after_quote
        elif state == after_quote:
            if char == ",":
                state = field_start
            elif char in "\r\n":
                state = field_start
                first_record = False
            else:
                raise CsvStructureError(
                    "header" if first_record else "body", "Malformed CSV quoting"
                )
        elif char == '"':
            if state != field_start:
                raise CsvStructureError(
                    "header" if first_record else "body", "Malformed CSV quoting"
                )
            state = quoted
        elif char == "," or char in "\r\n":
            state = field_start
            if char in "\r\n":
                first_record = False
        else:
            state = unquoted
        index += 1
    if state == quoted:
        raise CsvStructureError(
            "header" if first_record else "body", "Malformed CSV quoting"
        )


def coerce_int(value: str) -> int | str:
    """Convert bounded integer text while avoiding Python's huge-int parser cost."""
    stripped = value.strip()
    if not stripped or len(stripped.lstrip("+-")) > 100:
        return value
    try:
        return int(stripped)
    except ValueError:
        return value


def coerce_bool(value: str) -> bool | str:
    if value in {"true", "1"}:
        return True
    if value in {"false", "0"}:
        return False
    return value


def _row_errors(details: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {"field": detail["field"], "message": detail["message"]} for detail in details
    ]


def _import_rows(
    text: str,
    headers: tuple[str, ...],
    required: tuple[str, ...],
    converter: Any,
    validator: Any,
    row_error: Any = None,
) -> tuple[list[tuple[int, dict[str, Any]]], list[dict[str, Any]], int]:
    raw_rows = parse_csv(text, headers, required)
    valid: list[tuple[int, dict[str, Any]]] = []
    errors: list[dict[str, Any]] = []
    for line, row in enumerate(raw_rows, start=2):
        if row_error is not None:
            error = row_error(row)
            if error is not None:
                errors.append({"line": line, **error})
                continue
        payload = converter(row)
        try:
            fields = validator(payload)
        except (ApiError, OrderError) as error:
            for detail in _row_errors(error.details or []):
                errors.append({"line": line, **detail})
            continue
        valid.append((line, fields))
    return valid, errors, len(raw_rows)


def _validate_mode(mode: str, on_error: str) -> None:
    if mode not in {"apply", "validate"} or on_error not in {"abort", "skip"}:
        raise ValueError("invalid import mode or error policy")


def _make_report(
    mode: str, rows: int, created: int, errors: list[dict[str, Any]], applied: bool
) -> dict[str, Any]:
    return {
        "mode": mode,
        "rows": rows,
        "created": created,
        "failed": len({error["line"] for error in errors}),
        "applied": applied,
        "errors": errors,
    }


def import_products_csv(
    store: Any, text: str, mode: str = "apply", on_error: str = "abort"
) -> tuple[int, dict[str, Any]]:
    """Validate/import product rows with one lock-protected batch transaction."""
    _validate_mode(mode, on_error)
    seen_skus: set[str] = set()

    def convert(row: dict[str, str]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sku": row["sku"],
            "name": row["name"],
            "category": row["category"],
            "price_cents": coerce_int(row["price_cents"]),
        }
        if "stock" in row:
            payload["stock"] = coerce_int(row["stock"])
        if "tags" in row:
            payload["tags"] = [] if row["tags"] == "" else row["tags"].split("|")
        if "active" in row:
            payload["active"] = coerce_bool(row["active"])
        return payload

    def duplicate(row: dict[str, str]) -> dict[str, str] | None:
        sku = row["sku"]
        if sku in seen_skus:
            return {"field": "sku", "message": "Duplicate sku in file"}
        seen_skus.add(sku)
        return None

    valid, errors, row_count = _import_rows(
        text,
        PRODUCT_IMPORT_HEADERS,
        PRODUCT_REQUIRED_HEADERS,
        convert,
        validate_product_create,
        duplicate,
    )
    status, created, errors = store.apply_csv_import(valid, errors, mode, on_error)
    return status, _make_report(mode, row_count, created, errors, created > 0)


def import_orders_csv(
    store: Any, text: str, mode: str = "apply", on_error: str = "abort"
) -> tuple[int, dict[str, Any]]:
    """Validate/import legacy order rows with one lock-protected transaction."""
    _validate_mode(mode, on_error)

    def convert(row: dict[str, str]) -> dict[str, Any]:
        return {
            "customer_id": row["customer_id"],
            "total_cents": coerce_int(row["total_cents"]),
        }

    valid, errors, row_count = _import_rows(
        text, ORDER_IMPORT_HEADERS, ORDER_IMPORT_HEADERS, convert, validate_order_create
    )
    status, created, errors = store.apply_csv_import(valid, errors, mode, on_error)
    return status, _make_report(mode, row_count, created, errors, created > 0)
