"""Helpers for parsing and comparing HTTP entity tags."""

from __future__ import annotations

import os
from uuid import uuid4

from agent_qa.errors import ApiError

_MAX_HEADER_LENGTH = 8192
_MAX_TAGS = 100
_PROCESS_NONCE = uuid4().hex


class PreconditionFailed(ApiError):
    """A mutation used an entity tag that no longer identifies the resource."""

    def __init__(self, current_etag: str) -> None:
        super().__init__(412, "precondition_failed", "Resource has changed")
        self.current_etag = current_etag


def _invalid_precondition() -> ApiError:
    return ApiError(400, "invalid_precondition", "Invalid entity tag condition")


def parse_etag_list(raw: str) -> tuple[str, ...] | str:
    """Parse an If-Match or If-None-Match value with bounded input size."""
    if not isinstance(raw, str) or not raw or len(raw) > _MAX_HEADER_LENGTH:
        raise _invalid_precondition()

    # Commas inside an opaque quoted tag are data, so split only outside quotes.
    parts: list[str] = []
    start = 0
    quoted = False
    for index, char in enumerate(raw):
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            parts.append(raw[start:index])
            start = index + 1
    if quoted:
        raise _invalid_precondition()
    parts.append(raw[start:])

    tags: list[str] = []
    for part in parts:
        token = part.strip(" \t")
        if not token:
            raise _invalid_precondition()
        if token == "*":
            if len(parts) != 1:
                raise _invalid_precondition()
            return "*"
        if token.startswith("W/"):
            opaque = token[2:]
        else:
            opaque = token
        if len(opaque) < 2 or opaque[0] != '"' or opaque[-1] != '"':
            raise _invalid_precondition()
        if any(
            char == '"' or ord(char) < 0x21 or ord(char) == 0x7F or ord(char) > 0xFF
            for char in opaque[1:-1]
        ):
            raise _invalid_precondition()
        tags.append(token)
        if len(tags) > _MAX_TAGS:
            raise _invalid_precondition()
    return tuple(tags)


def strong_match(parsed: tuple[str, ...] | str, current_etag: str) -> bool:
    """Return whether a parsed condition strongly matches the current tag."""
    if parsed == "*":
        return True
    return any(not tag.startswith("W/") and tag == current_etag for tag in parsed)


def weak_match(parsed: tuple[str, ...] | str, current_etag: str) -> bool:
    """Return whether a parsed condition weakly matches the current tag."""
    if parsed == "*":
        return True
    normalized_current = (
        current_etag[2:] if current_etag.startswith("W/") else current_etag
    )
    return any(
        (tag[2:] if tag.startswith("W/") else tag) == normalized_current
        for tag in parsed
    )


def etag_for(kind: str, resource_id: int, version: int) -> str:
    """Build an order or product entity tag unique to this process lifetime."""
    prefixes = {"order": "o", "product": "p", "o": "o", "p": "p"}
    prefix = prefixes.get(kind)
    if (
        prefix is None
        or isinstance(resource_id, bool)
        or not isinstance(resource_id, int)
        or resource_id < 1
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
    ):
        raise ValueError("kind, resource_id and version must identify a resource")
    return f'"{prefix}{os.getpid():x}.{_PROCESS_NONCE}.{resource_id}.{version}"'


def check_expected_version(
    expected_version: int | tuple[str, ...] | str | None,
    kind: str,
    resource_id: int,
    current_version: int,
) -> None:
    """Raise atomically from a store when an expected entity version is stale."""
    if expected_version is None or expected_version == "*":
        return
    current_etag = etag_for(kind, resource_id, current_version)
    if isinstance(expected_version, bool):
        raise _invalid_precondition()
    if isinstance(expected_version, int):
        matches = expected_version == current_version
    elif isinstance(expected_version, tuple):
        matches = strong_match(expected_version, current_etag)
    else:
        raise _invalid_precondition()
    if not matches:
        raise PreconditionFailed(current_etag)
