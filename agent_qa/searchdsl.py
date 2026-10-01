"""Small, deterministic search query language for orders and products."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import difflib
import json
import re
from typing import Any, Callable

from agent_qa.errors import ApiError
from agent_qa.schemas import STATUSES

MAX_QUERY_LENGTH = 500
MAX_NESTING = 8
MAX_TERMS = 20
_BARE_VALUE = re.compile(r"^[A-Za-z0-9_.@-]+$")
_BARE_START = re.compile(r"[A-Za-z0-9_.@-]")

FIELD_METADATA: dict[str, dict[str, dict[str, Any]]] = {
    "orders": {
        "id": {"type": "int"},
        "customer_id": {"type": "str"},
        "total_cents": {"type": "int"},
        "status": {"type": "enum", "enum": list(STATUSES)},
        "created_at": {"type": "date"},
        "items_count": {"type": "int"},
        "product_id": {"type": "int", "multi": True},
        "sku": {"type": "str", "multi": True},
    },
    "products": {
        "id": {"type": "int"},
        "sku": {"type": "str"},
        "name": {"type": "str"},
        "category": {"type": "str"},
        "price_cents": {"type": "int"},
        "stock": {"type": "int"},
        "tags": {"type": "str", "multi": True},
        "active": {"type": "bool"},
        "created_at": {"type": "date"},
    },
}


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    position: int


def _error(message: str, position: int = 0) -> ApiError:
    detail = {"field": "q", "message": message, "position": str(position)}
    return ApiError(400, "invalid_search_query", message, [detail])


def tokenize(query: str) -> list[Token]:
    """Split a bounded query into words, quoted values, and operators."""
    if not isinstance(query, str):
        raise _error("Query must not be empty")
    if len(query) > MAX_QUERY_LENGTH:
        raise _error("Query is too long (max 500)", MAX_QUERY_LENGTH)
    tokens: list[Token] = []
    index = 0
    while index < len(query):
        char = query[index]
        if char.isspace():
            index += 1
            continue
        start = index
        if char == '"':
            index += 1
            value: list[str] = []
            while index < len(query) and query[index] != '"':
                if ord(query[index]) < 32 or ord(query[index]) == 127:
                    char = query[index]
                    raise _error(
                        f"Unexpected token {char!r} at position {index}", index
                    )
                if query[index] == "\\":
                    index += 1
                    if index >= len(query) or query[index] not in {'"', "\\"}:
                        raise _error(
                            f"Unexpected token '\\' at position {index - 1}",
                            index - 1,
                        )
                value.append(query[index])
                index += 1
            if index >= len(query):
                raise _error(f"Expected value at position {start}", start)
            index += 1
            tokens.append(Token("value", "".join(value), start))
            continue
        if char in "(),":
            tokens.append(Token(char, char, start))
            index += 1
            continue
        if char == "!" and (index + 1 >= len(query) or query[index + 1] != "="):
            tokens.append(Token("invalid", char, start))
            index += 1
            continue
        if char in "=:!<>~":
            index += 1
            if index < len(query) and query[index] == "=" and char in "!<>":
                index += 1
            if char == ":":
                tokens.append(Token("operator", "=", start))
            else:
                tokens.append(Token("operator", query[start:index], start))
            continue
        if _BARE_START.fullmatch(char):
            index += 1
            while index < len(query) and _BARE_START.fullmatch(query[index]):
                index += 1
            tokens.append(Token("word", query[start:index], start))
            continue
        tokens.append(Token("invalid", char, start))
        index += 1
    if not tokens:
        raise _error("Query must not be empty")
    return tokens


class _Parser:
    def __init__(self, query: str, resource: str, tokens: list[Token]) -> None:
        if resource not in FIELD_METADATA:
            raise ValueError("resource must be orders or products")
        self.query = query
        self.resource = resource
        self.fields = FIELD_METADATA[resource]
        self.tokens = tokens
        self.index = 0
        self.terms = 0

    def peek(self) -> Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def take(self) -> Token:
        token = self.peek()
        if token is None:
            raise _error(
                f"Expected value at position {len(self.query)}", len(self.query)
            )
        self.index += 1
        return token

    def unexpected(self, token: Token) -> ApiError:
        return _error(
            f"Unexpected token '{token.value}' at position {token.position}",
            token.position,
        )

    def parse(self) -> dict[str, Any]:
        result = self.parse_or(0)
        if self.peek() is not None:
            raise self.unexpected(self.take())
        return result

    def parse_or(self, depth: int) -> dict[str, Any]:
        children = [self.parse_and(depth)]
        while (
            (token := self.peek()) is not None
            and token.kind == "word"
            and token.value.upper() == "OR"
        ):
            self.take()
            children.append(self.parse_and(depth))
        return _combine("or", children)

    def parse_and(self, depth: int) -> dict[str, Any]:
        children = [self.parse_not(depth)]
        while (token := self.peek()) is not None:
            if token.kind == "word" and token.value.upper() == "OR":
                break
            if token.kind == "word" and token.value.upper() == "AND":
                self.take()
                children.append(self.parse_not(depth))
                continue
            if self._starts_primary(token):
                children.append(self.parse_not(depth))
                continue
            break
        return _combine("and", children)

    @staticmethod
    def _starts_primary(token: Token) -> bool:
        return token.kind == "(" or (
            token.kind == "word" and token.value.upper() not in {"AND", "OR", "IN"}
        )

    def parse_not(self, depth: int) -> dict[str, Any]:
        token = self.peek()
        if token is not None and token.kind == "word" and token.value.upper() == "NOT":
            if depth + 1 > MAX_NESTING:
                raise _error("Query is nested too deeply (max 8)", token.position)
            self.take()
            return {"op": "not", "arg": self.parse_not(depth + 1)}
        return self.parse_primary(depth)

    def parse_primary(self, depth: int) -> dict[str, Any]:
        token = self.peek()
        if token is not None and token.kind == "(":
            if depth + 1 > MAX_NESTING:
                raise _error("Query is nested too deeply (max 8)", token.position)
            self.take()
            result = self.parse_or(depth + 1)
            closing = self.peek()
            if closing is None or closing.kind != ")":
                position = len(self.query) if closing is None else closing.position
                raise _error(f"Expected ')' at position {position}", position)
            self.take()
            return result
        return self.parse_term()

    def parse_term(self) -> dict[str, Any]:
        field_token = self.take()
        if field_token.kind != "word":
            raise self.unexpected(field_token)
        field = field_token.value
        if field.upper() in {"AND", "OR", "NOT", "IN"}:
            raise self.unexpected(field_token)
        if field not in self.fields:
            match = difflib.get_close_matches(field, self.fields, n=1)
            suffix = f" Did you mean '{match[0]}'?" if match else ""
            raise _error(f"Unknown field '{field}'.{suffix}", field_token.position)
        operator = self.take()
        metadata = self.fields[field]
        if operator.kind == "word" and operator.value.upper() == "IN":
            op = "in"
        elif operator.kind == "operator":
            op = operator.value
        else:
            raise self.unexpected(operator)
        if op not in supported_operators(metadata):
            raise _error(
                f"Operator {op} is not supported for field {field} "
                f"at position {operator.position}",
                operator.position,
            )
        values = (
            self.parse_in_values(field) if op == "in" else [self.parse_value(field)]
        )
        self.terms += 1
        if self.terms > MAX_TERMS:
            raise _error("Too many terms (max 20)", field_token.position)
        value: Any = values if op == "in" else values[0]
        return {"field": field, "op": op, "value": value}

    def parse_in_values(self, field: str) -> list[Any]:
        opening = self.take()
        if opening.kind != "(":
            raise self.unexpected(opening)
        values = [self.parse_value(field)]
        while self.peek() is not None and self.peek().kind == ",":
            self.take()
            values.append(self.parse_value(field))
        closing = self.peek()
        if closing is None or closing.kind != ")":
            position = len(self.query) if closing is None else closing.position
            raise _error(f"Expected ')' at position {position}", position)
        self.take()
        return values

    def parse_value(self, field: str) -> Any:
        token = self.peek()
        if token is None:
            raise _error(
                f"Expected value at position {len(self.query)}", len(self.query)
            )
        if token.kind not in {"word", "value"}:
            raise _error(f"Expected value at position {token.position}", token.position)
        self.take()
        metadata = self.fields[field]
        field_type = metadata["type"]
        try:
            if field_type == "int":
                if not token.value.isascii() or not re.fullmatch(r"-?\d+", token.value):
                    raise ValueError
                return int(token.value)
            if field_type == "bool":
                if token.value.lower() not in {"true", "false"}:
                    raise ValueError
                return token.value.lower() == "true"
            if field_type == "date":
                _parse_date(token.value)
                return token.value
        except (ValueError, OverflowError):
            expected = {"int": "integer", "bool": "boolean", "date": "date"}[field_type]
            raise _error(
                f"Expected {expected} for field {field} at position {token.position}",
                token.position,
            ) from None
        if field_type == "enum" and token.value not in metadata["enum"]:
            raise _error(
                f"Unknown value '{token.value}' for field {field} "
                f"at position {token.position}",
                token.position,
            )
        return token.value


def _parse_date(value: str) -> datetime:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.combine(date.fromisoformat(value), time.min, timezone.utc)
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}" r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def supported_operators(metadata: dict[str, Any]) -> tuple[str, ...]:
    """Return the DSL operators allowed by a field's shared metadata."""
    operators = ["=", "!=", "in"]
    if metadata["type"] in {"int", "date"}:
        operators.extend((">", ">=", "<", "<="))
    if metadata["type"] == "str":
        operators.append("~")
    return tuple(operators)


def _combine(op: str, children: list[dict[str, Any]]) -> dict[str, Any]:
    if len(children) == 1:
        return children[0]
    flattened: list[dict[str, Any]] = []
    for child in children:
        if child.get("op") == op:
            flattened.extend(child["args"])
        else:
            flattened.append(child)
    return {"op": op, "args": flattened}


def parse(query: str, resource: str) -> dict[str, Any]:
    """Parse a query into a typed AST."""
    tokens = tokenize(query)
    return _Parser(query, resource, tokens).parse()


def count_terms(ast: dict[str, Any]) -> int:
    if "field" in ast:
        return 1
    if ast.get("op") == "not":
        return count_terms(ast["arg"])
    return sum(count_terms(child) for child in ast["args"])


def normalize(ast: dict[str, Any]) -> str:
    """Serialize a typed AST into its canonical query form."""
    if "field" in ast:
        field, op, value = ast["field"], ast["op"], ast["value"]
        if op == "in":
            return f"{field} IN ({', '.join(_format_value(item) for item in value)})"
        return f"{field} {op} {_format_value(value)}"
    op = ast["op"]
    if op == "not":
        child = ast["arg"]
        rendered = normalize(child)
        if child.get("op") in {"and", "or"}:
            rendered = f"({rendered})"
        return f"NOT {rendered}"
    joiner = " AND " if op == "and" else " OR "
    rendered_children = []
    for child in ast["args"]:
        rendered = normalize(child)
        if op == "and" and child.get("op") == "or":
            rendered = f"({rendered})"
        rendered_children.append(rendered)
    return joiner.join(rendered_children)


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and _BARE_VALUE.fullmatch(value):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def compile_predicate(
    ast: dict[str, Any], resource: str
) -> Callable[[dict[str, Any]], bool]:
    """Compile an AST into a bounded, storage-independent row predicate."""
    fields = FIELD_METADATA[resource]
    if "field" not in ast:
        if ast["op"] == "not":
            child = compile_predicate(ast["arg"], resource)
            return lambda row: not child(row)
        children = [compile_predicate(item, resource) for item in ast["args"]]
        if ast["op"] == "and":
            return lambda row: all(child(row) for child in children)
        return lambda row: any(child(row) for child in children)

    field, op, expected = ast["field"], ast["op"], ast["value"]
    metadata = fields[field]

    def values(row: dict[str, Any]) -> list[Any]:
        if field == "items_count":
            return [len(row.get("items", []))]
        if field in {"product_id", "sku"} and resource == "orders":
            return [item.get(field) for item in row.get("items", [])]
        raw = row.get(field)
        if metadata.get("multi"):
            return list(raw or [])
        return [raw]

    def matches(actual: Any, wanted: Any, operator: str) -> bool:
        if actual is None:
            return False
        if operator == "in":
            return any(matches(actual, item, "=") for item in wanted)
        if metadata["type"] == "date":
            actual = _parse_date(str(actual))
            wanted = _parse_date(str(wanted))
        if operator == "=":
            return actual == wanted
        if operator == "!=":
            return actual != wanted
        if operator == ">":
            return actual > wanted
        if operator == ">=":
            return actual >= wanted
        if operator == "<":
            return actual < wanted
        if operator == "<=":
            return actual <= wanted
        if operator == "~":
            return isinstance(actual, str) and wanted.casefold() in actual.casefold()
        return False

    def predicate(row: dict[str, Any]) -> bool:
        actual_values = values(row)
        if op == "!=":
            return not any(matches(actual, expected, "=") for actual in actual_values)
        return any(matches(actual, expected, op) for actual in actual_values)

    return predicate
