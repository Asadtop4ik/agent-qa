"""Small, bounded JSON Schema subset used by the HTTP API."""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from agent_qa.errors import ApiError

MAX_VALIDATION_DEPTH = 64
MAX_VALIDATION_NODES = 10_000
MAX_QUERY_VALUE_LENGTH = 1024
MAX_QUERY_PARAMETERS = 128
MAX_QUERY_NAME_LENGTH = 128
LOGGER = logging.getLogger(__name__)


def _matches_type(value: Any, expected: str) -> bool:
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
            and not (isinstance(value, float) and not math.isfinite(value))
        )
    if expected == "boolean":
        return isinstance(value, bool)
    return True


def _type_message(expected: str, field: str) -> str:
    messages = {
        "object": "Must be a JSON object",
        "array": "Must be an array",
        "string": "Must be a string",
        "integer": "Must be an integer",
        "number": "Must be a number",
        "boolean": "Must be a boolean",
    }
    return messages.get(expected, "Invalid value")


def _enum_message(values: list[Any]) -> str:
    display = sorted((str(value) for value in values))
    return "Must be one of: " + ", ".join(display)


def _json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values, keeping booleans distinct from numbers."""
    pending = [(left, right)]
    while pending:
        left_value, right_value = pending.pop()
        if (
            isinstance(left_value, (int, float))
            and not isinstance(left_value, bool)
            and isinstance(right_value, (int, float))
            and not isinstance(right_value, bool)
        ):
            if left_value != right_value:
                return False
        elif isinstance(left_value, dict) and isinstance(right_value, dict):
            if left_value.keys() != right_value.keys():
                return False
            pending.extend((left_value[key], right_value[key]) for key in left_value)
        elif isinstance(left_value, list) and isinstance(right_value, list):
            if len(left_value) != len(right_value):
                return False
            pending.extend(zip(left_value, right_value))
        elif type(left_value) is not type(right_value) or left_value != right_value:
            return False
    return True


def _json_equal_bounded(left: Any, right: Any, budget: list[int]) -> bool | None:
    """Compare JSON values while charging work against a shared node budget."""
    pending = [(left, right)]
    while pending:
        budget[0] -= 1
        if budget[0] < 0:
            return None
        left_value, right_value = pending.pop()
        if (
            isinstance(left_value, (int, float))
            and not isinstance(left_value, bool)
            and isinstance(right_value, (int, float))
            and not isinstance(right_value, bool)
        ):
            if left_value != right_value:
                return False
        elif isinstance(left_value, dict) and isinstance(right_value, dict):
            if left_value.keys() != right_value.keys():
                return False
            pending.extend((left_value[key], right_value[key]) for key in left_value)
        elif isinstance(left_value, list) and isinstance(right_value, list):
            if len(left_value) != len(right_value):
                return False
            pending.extend(zip(left_value, right_value))
        elif type(left_value) is not type(right_value) or left_value != right_value:
            return False
    return True


def _enum_contains(values: list[Any], instance: Any) -> bool:
    return any(_json_equal(value, instance) for value in values)


def validate(schema: dict[str, Any], instance: Any) -> list[dict[str, str]]:
    """Return sorted validation errors for the supported schema vocabulary."""
    errors: list[dict[str, str]] = []
    visited = 0
    stopped = False

    def add(field: str, message: str) -> None:
        nonlocal stopped
        if stopped:
            return
        if len(errors) >= MAX_VALIDATION_NODES:
            errors.append(
                {
                    "field": field or "body",
                    "message": "Maximum validation complexity exceeded",
                }
            )
            stopped = True
            return
        errors.append({"field": field or "body", "message": message})

    def walk(current_schema: Any, value: Any, field: str, depth: int) -> None:
        nonlocal visited, stopped
        if stopped:
            return
        visited += 1
        if depth > MAX_VALIDATION_DEPTH or visited > MAX_VALIDATION_NODES:
            add(field, "Maximum validation complexity exceeded")
            stopped = True
            return
        if not isinstance(current_schema, dict):
            return

        if isinstance(value, (dict, list)) and len(value) > MAX_VALIDATION_NODES:
            add(field, "Maximum validation complexity exceeded")
            stopped = True
            return

        expected = current_schema.get("type")
        negated_schema = current_schema.get("not")
        if isinstance(negated_schema, dict) and not validate(negated_schema, value):
            add(field, "Must not match the prohibited schema")
        if isinstance(expected, str) and not _matches_type(value, expected):
            enum = current_schema.get("enum")
            if (
                expected == "string"
                and isinstance(enum, list)
                and all(isinstance(item, str) for item in enum)
            ):
                add(field, _enum_message(enum))
                return
            add(field, _type_message(expected, field))
            return

        if "enum" in current_schema and not _enum_contains(
            current_schema["enum"], value
        ):
            add(field, _enum_message(current_schema["enum"]))

        if isinstance(value, str):
            minimum = current_schema.get("minLength")
            maximum = current_schema.get("maxLength")
            too_short = isinstance(minimum, int) and len(value) < minimum
            too_long = isinstance(maximum, int) and len(value) > maximum
            if too_short or too_long:
                if isinstance(minimum, int) and isinstance(maximum, int):
                    add(field, f"Must contain {minimum} to {maximum} characters")
                elif too_short:
                    add(field, f"Must contain at least {minimum} characters")
                else:
                    add(field, f"Must contain at most {maximum} characters")
            if (
                current_schema.get("x-nonBlank") is True
                and (not isinstance(minimum, int) or len(value) >= minimum)
                and (not isinstance(maximum, int) or len(value) <= maximum)
                and not value.strip()
            ):
                add(field, "Must not be blank")
            pattern = current_schema.get("pattern")
            if isinstance(pattern, str):
                try:
                    matcher = (
                        re.fullmatch if current_schema.get("x-fullMatch") else re.search
                    )
                    matched = matcher(pattern, value) is not None
                except re.error:
                    LOGGER.exception(
                        "Invalid pattern in validation schema for field %s", field
                    )
                    add(field, "Invalid validation schema")
                    return
                if not matched:
                    add(field, "Must match the required pattern")

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            minimum = current_schema.get("minimum")
            maximum = current_schema.get("maximum")
            below = minimum is not None and value < minimum
            above = maximum is not None and value > maximum
            if below or above:
                if minimum is not None and maximum is not None:
                    add(field, f"Must be between {minimum} and {maximum}")
                elif below:
                    add(field, f"Must be at least {minimum}")
                else:
                    add(field, f"Must be at most {maximum}")

        if isinstance(value, dict):
            exactly_one = current_schema.get("x-exactlyOne")
            if isinstance(exactly_one, dict):
                fields = exactly_one.get("fields")
                error_field = exactly_one.get("errorField")
                if (
                    isinstance(fields, list)
                    and 1 < len(fields) <= MAX_VALIDATION_NODES
                    and all(isinstance(name, str) for name in fields)
                    and isinstance(error_field, str)
                ):
                    present_count = sum(name in value for name in fields)
                    if present_count != 1:
                        message_key = (
                            "bothMessage" if present_count > 1 else "neitherMessage"
                        )
                        message = exactly_one.get(message_key)
                        if isinstance(message, str):
                            add(error_field, message)
            minimum = current_schema.get("minProperties")
            maximum = current_schema.get("maxProperties")
            if isinstance(minimum, int) and len(value) < minimum:
                if minimum == 1 and not field:
                    add(field, "At least one field is required")
                else:
                    add(field, f"Must have at least {minimum} properties")
            if isinstance(maximum, int) and len(value) > maximum:
                add(field, f"Must have at most {maximum} properties")

            properties = current_schema.get("properties", {})
            if not isinstance(properties, dict):
                properties = {}
            required = current_schema.get("required", [])
            if isinstance(required, list):
                for name in required:
                    if stopped:
                        break
                    if isinstance(name, str) and name not in value:
                        add(_child_field(field, name), "Required")
            for name in sorted(value, key=str):
                if stopped:
                    break
                child = _child_field(field, name)
                if name in properties:
                    walk(properties[name], value[name], child, depth + 1)
                elif current_schema.get("additionalProperties") is False:
                    message = (
                        "Cannot be changed"
                        if name in current_schema.get("x-immutableFields", [])
                        else "Unknown field"
                    )
                    add(child, message)
                elif isinstance(current_schema.get("additionalProperties"), dict):
                    walk(
                        current_schema["additionalProperties"],
                        value[name],
                        child,
                        depth + 1,
                    )

        if isinstance(value, list):
            minimum = current_schema.get("minItems")
            maximum = current_schema.get("maxItems")
            if isinstance(minimum, int) and len(value) < minimum:
                add(field, f"Must contain at least {minimum} items")
            if isinstance(maximum, int) and len(value) > maximum:
                add(field, f"Must contain at most {maximum} items")
            if current_schema.get("uniqueItems") is True:
                comparison_budget = [MAX_VALIDATION_NODES]
                for index, item in enumerate(value):
                    if stopped:
                        break
                    for previous_index in range(index):
                        equal = _json_equal_bounded(
                            item, value[previous_index], comparison_budget
                        )
                        if equal is None:
                            add(field, "Maximum validation complexity exceeded")
                            stopped = True
                            break
                        if equal:
                            add(field, "Must contain unique items")
                            break
                    if errors and errors[-1]["field"] == (field or "body"):
                        if errors[-1]["message"] in {
                            "Must contain unique items",
                            "Maximum validation complexity exceeded",
                        }:
                            break
            item_schema = current_schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    if stopped:
                        break
                    walk(item_schema, item, f"{field or 'body'}[{index}]", depth + 1)

    walk(schema, instance, "", 0)
    errors.sort(key=lambda item: item["field"])
    return errors


def _child_field(parent: str, name: Any) -> str:
    child = str(name)
    if not parent:
        return child
    return f"{parent}.{child}" if parent else child


def validate_query_params(
    parameters: list[dict[str, Any]], query: list[tuple[str, str]]
) -> dict[str, Any]:
    """Parse query pairs using parameter schemas and raise ApiError on errors."""
    if len(query) > MAX_QUERY_PARAMETERS:
        raise ApiError(400, "invalid_query", "Too many query parameters")
    definitions = {
        parameter["name"]: parameter
        for parameter in parameters
        if parameter.get("in") == "query" and isinstance(parameter.get("name"), str)
    }
    raw: dict[str, str] = {}
    errors: list[dict[str, str]] = []
    for name, value in query:
        if len(name) > MAX_QUERY_NAME_LENGTH:
            raise ApiError(400, "invalid_query", "Parameter name is too long")
        if name not in definitions:
            errors.append({"field": name, "message": "Unsupported query parameter"})
        elif name in raw:
            errors.append({"field": name, "message": "Parameter may appear once"})
        elif len(value) > MAX_QUERY_VALUE_LENGTH:
            errors.append({"field": name, "message": "Parameter value is too long"})
        else:
            raw[name] = value

    result: dict[str, Any] = {}
    for name, parameter in definitions.items():
        schema = parameter.get("schema", {})
        if name not in raw:
            if "default" in schema:
                result[name] = schema["default"]
            elif parameter.get("required"):
                errors.append({"field": name, "message": "Required"})
            continue
        value = raw[name]
        expected = schema.get("type", "string")
        parsed: Any = value
        if expected == "integer":
            try:
                if not re.fullmatch(r"[+-]?[0-9]{1,128}", value):
                    raise ValueError
                parsed = int(value)
            except ValueError:
                parsed = None
            minimum = schema.get("minimum")
            maximum = schema.get("maximum")
            if (
                parsed is None
                or (minimum is not None and parsed < minimum)
                or (maximum is not None and parsed > maximum)
            ):
                if minimum is not None and maximum is not None:
                    message = f"Must be an integer from {minimum} to {maximum}"
                elif minimum is not None:
                    message = f"Must be an integer of at least {minimum}"
                elif maximum is not None:
                    message = f"Must be an integer of at most {maximum}"
                else:
                    message = "Must be an integer"
                errors.append({"field": name, "message": message})
                continue
        elif expected == "boolean":
            if value not in {"true", "false"}:
                errors.append({"field": name, "message": "Must be a boolean"})
                continue
            parsed = value == "true"
        elif expected == "string":
            minimum = schema.get("minLength")
            maximum = schema.get("maxLength")
            if minimum is not None and len(value) < minimum:
                errors.append(
                    {
                        "field": name,
                        "message": f"Must contain at least {minimum} characters",
                    }
                )
                continue
            if maximum is not None and len(value) > maximum:
                errors.append(
                    {
                        "field": name,
                        "message": f"Must contain at most {maximum} characters",
                    }
                )
                continue
        enum = schema.get("enum")
        if enum is not None and parsed not in enum:
            errors.append({"field": name, "message": _enum_message(enum)})
            continue
        result[name] = parsed

    if errors:
        errors.sort(key=lambda item: (item["field"], item["message"]))
        first_message = errors[0]["message"]
        raise ApiError(400, "invalid_query", first_message, errors)
    return result
