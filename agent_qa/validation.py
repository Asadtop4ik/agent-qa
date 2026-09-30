"""Validation helpers for the service's supported JSON Schema subset."""

from __future__ import annotations

import math
import re
from itertools import islice
from typing import Any

from agent_qa.errors import ApiError

_MAX_DEPTH = 100
_MAX_ERRORS = 1000
_MAX_QUERY_PAIRS = 1000
_MAX_QUERY_TEXT_LENGTH = 4096
_TYPE_MESSAGES = {
    "object": "Must be a JSON object",
    "array": "Must be an array",
    "string": "Must be a string",
    "integer": "Must be an integer",
    "number": "Must be a number",
    "boolean": "Must be a boolean",
}


def _matches_type(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    if expected == "boolean":
        return isinstance(value, bool)
    return True


def _enum_matches(candidate: Any, option: Any) -> bool:
    """Compare JSON values without Python's bool/int equality overlap."""
    if isinstance(candidate, bool) or isinstance(option, bool):
        return type(candidate) is type(option) and candidate == option
    if isinstance(candidate, (int, float)) and isinstance(option, (int, float)):
        return (
            not isinstance(candidate, bool)
            and not isinstance(option, bool)
            and candidate == option
        )
    if type(candidate) is not type(option):
        return False
    return candidate == option


def _enum_message(options: list[Any]) -> str:
    values = sorted(options, key=lambda value: str(value))
    rendered = ", ".join(str(value) for value in values)
    return f"Must be one of: {rendered}"


def validate(schema: dict[str, Any], instance: Any) -> list[dict[str, str]]:
    """Return validation errors for a bounded JSON-Schema subset.

    Supported keywords are type, required, properties, additionalProperties,
    enum, minimum, maximum, minLength, maxLength, pattern, items, minItems,
    maxItems, minProperties, and x-nonBlank.
    """
    errors: list[dict[str, str]] = []

    def add(field: str, message: str) -> None:
        if len(errors) < _MAX_ERRORS:
            errors.append({"field": field, "message": message})

    def visit(current_schema: Any, value: Any, field: str, depth: int) -> None:
        if len(errors) >= _MAX_ERRORS:
            return
        if depth > _MAX_DEPTH:
            add(field, "Maximum nesting depth exceeded")
            return
        if not isinstance(current_schema, dict):
            add(field, "Invalid schema")
            return

        enum = current_schema.get("enum")
        if isinstance(enum, list) and not any(
            _enum_matches(value, option) for option in enum
        ):
            add(field, _enum_message(enum))
            return

        expected = current_schema.get("type")
        if isinstance(expected, str) and expected in _TYPE_MESSAGES:
            if not _matches_type(expected, value):
                add(field, _TYPE_MESSAGES[expected])
                return

        if isinstance(value, str):
            minimum_length = current_schema.get("minLength")
            maximum_length = current_schema.get("maxLength")
            if isinstance(minimum_length, int) and not isinstance(minimum_length, bool):
                if len(value) < minimum_length:
                    if isinstance(maximum_length, int) and minimum_length == 1:
                        add(
                            field,
                            f"Must contain {minimum_length} to "
                            f"{maximum_length} characters",
                        )
                    else:
                        add(field, f"Must contain at least {minimum_length} characters")
                    return
            if isinstance(maximum_length, int) and not isinstance(maximum_length, bool):
                if len(value) > maximum_length:
                    if isinstance(minimum_length, int):
                        add(
                            field,
                            f"Must contain {minimum_length} to "
                            f"{maximum_length} characters",
                        )
                    else:
                        add(field, f"Must contain at most {maximum_length} characters")
                    return
            pattern = current_schema.get("pattern")
            if isinstance(pattern, str):
                try:
                    if re.search(pattern, value) is None:
                        add(field, "Must match the required pattern")
                        return
                except re.error:
                    add(field, "Invalid schema pattern")
                    return
            if current_schema.get("x-nonBlank") is True and not value.strip():
                add(field, "Must not be blank")
                return

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and not math.isfinite(value):
                add(field, "Must be a number")
                return
            minimum = current_schema.get("minimum")
            maximum = current_schema.get("maximum")
            if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
                if value < minimum:
                    if isinstance(maximum, (int, float)) and not isinstance(
                        maximum, bool
                    ):
                        message = f"Must be between {minimum} and {maximum}"
                    else:
                        message = f"Must be at least {minimum}"
                    add(field, message)
                    return
            if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
                if value > maximum:
                    if isinstance(minimum, (int, float)) and not isinstance(
                        minimum, bool
                    ):
                        message = f"Must be between {minimum} and {maximum}"
                    else:
                        message = f"Must be at most {maximum}"
                    add(field, message)
                    return

        if isinstance(value, dict):
            required = current_schema.get("required", [])
            if isinstance(required, list):
                for name in required:
                    if isinstance(name, str) and name not in value:
                        child_field = name if depth == 0 else f"{field}.{name}"
                        add(child_field, "Required")
            minimum_properties = current_schema.get("minProperties")
            if (
                isinstance(minimum_properties, int)
                and not isinstance(minimum_properties, bool)
                and len(value) < minimum_properties
            ):
                add(field, "At least one field is required")
            properties = current_schema.get("properties", {})
            if not isinstance(properties, dict):
                properties = {}
            additional = current_schema.get("additionalProperties", True)
            for name in sorted(value, key=lambda item: str(item)):
                child_field = str(name) if depth == 0 else f"{field}.{name}"
                if name in properties:
                    visit(properties[name], value[name], child_field, depth + 1)
                elif additional is False:
                    add(child_field, "Unknown field")
                elif isinstance(additional, dict):
                    visit(additional, value[name], child_field, depth + 1)

        if isinstance(value, list):
            minimum_items = current_schema.get("minItems")
            maximum_items = current_schema.get("maxItems")
            if isinstance(minimum_items, int) and not isinstance(minimum_items, bool):
                if len(value) < minimum_items:
                    add(field, f"Must contain at least {minimum_items} items")
            if isinstance(maximum_items, int) and not isinstance(maximum_items, bool):
                if len(value) > maximum_items:
                    add(field, f"Must contain at most {maximum_items} items")
            item_schema = current_schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    visit(item_schema, item, f"{field}[{index}]", depth + 1)
                    if len(errors) >= _MAX_ERRORS:
                        break

    visit(schema, instance, "body", 0)
    errors.sort(key=lambda item: item["field"])
    return errors


def validate_query_params(
    parameters: list[dict[str, Any]], query: list[tuple[str, str]]
) -> dict[str, Any]:
    """Validate query pairs using OpenAPI-style query parameter definitions."""
    definitions = {
        parameter["name"]: parameter
        for parameter in parameters
        if isinstance(parameter, dict)
        and parameter.get("in") == "query"
        and isinstance(parameter.get("name"), str)
    }
    values: dict[str, str] = {}
    errors: list[dict[str, str]] = []

    def add_error(field: str, message: str) -> None:
        if len(errors) < _MAX_ERRORS:
            errors.append({"field": field, "message": message})

    try:
        pairs = iter(islice(query, _MAX_QUERY_PAIRS + 1))
    except TypeError:
        pairs = iter(())
        errors.append({"field": "query", "message": "Invalid query parameters"})
    for index, pair in enumerate(pairs):
        if index == _MAX_QUERY_PAIRS:
            add_error("query", "Too many query parameters")
            break
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            add_error("query", "Invalid query parameters")
            continue
        name, value = pair
        if not isinstance(name, str) or not isinstance(value, str):
            add_error(str(name), "Invalid query parameter")
        elif len(name) > _MAX_QUERY_TEXT_LENGTH or len(value) > _MAX_QUERY_TEXT_LENGTH:
            add_error(name[:_MAX_QUERY_TEXT_LENGTH], "Query parameter is too long")
        elif name not in definitions:
            add_error(name, "Unsupported query parameter")
        elif name in values:
            add_error(name, "Parameter may appear once")
        else:
            values[name] = value

    result: dict[str, Any] = {}
    for name, parameter in definitions.items():
        if name not in values:
            schema = parameter.get("schema", {})
            if "default" in schema:
                result[name] = schema["default"]
            elif parameter.get("required"):
                add_error(name, "Required")
            continue
        raw = values[name]
        schema = parameter.get("schema", {})
        kind = schema.get("type") if isinstance(schema, dict) else None
        if kind == "integer":
            minimum = schema.get("minimum")
            maximum = schema.get("maximum")
            minimum_text = minimum if minimum is not None else "-"
            maximum_text = maximum if maximum is not None else "+"
            bounds = f"{minimum_text} to {maximum_text}"
            try:
                if len(raw) > 64 or re.fullmatch(r"[+-]?[0-9]+", raw) is None:
                    raise ValueError
                parsed = int(raw)
                if minimum is not None and parsed < minimum:
                    raise ValueError
                if maximum is not None and parsed > maximum:
                    raise ValueError
                result[name] = parsed
            except (ValueError, OverflowError):
                add_error(name, f"Must be an integer from {bounds}")
        elif kind == "boolean":
            if raw == "true":
                result[name] = True
            elif raw == "false":
                result[name] = False
            else:
                add_error(name, "Must be a boolean")
        elif kind == "string":
            if "minLength" in schema and len(raw) < schema["minLength"]:
                add_error(name, "Must be a string")
            elif "maxLength" in schema and len(raw) > schema["maxLength"]:
                add_error(name, "Must be a string")
            else:
                result[name] = raw
        else:
            result[name] = raw

        enum = schema.get("enum") if isinstance(schema, dict) else None
        if (
            name in result
            and isinstance(enum, list)
            and not any(_enum_matches(result[name], option) for option in enum)
        ):
            add_error(name, _enum_message(enum))
            del result[name]

    if errors:
        errors.sort(key=lambda item: item["field"])
        raise ApiError(400, "invalid_query", "Query validation failed", errors)
    return result
