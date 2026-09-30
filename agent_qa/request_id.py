"""Request ID validation and generation."""

import re
from uuid import uuid4

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def request_id(value: str | None) -> str:
    """Return a valid supplied request ID or generate a new one."""
    if value is not None and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return uuid4().hex
