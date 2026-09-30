"""API-key authentication for the synthetic QA service.

The fallback API key is synthetic and intended only for QA environments.
"""

import hmac
from collections.abc import Mapping

from agent_qa.config import API_KEY


def api_key_from_headers(headers: Mapping[str, str]) -> str | None:
    """Return the X-API-Key header value, matching the name case-insensitively."""
    for name, value in headers.items():
        if name.lower() == "x-api-key":
            return value
    return None


def is_valid_api_key(headers: Mapping[str, str]) -> bool:
    """Check a supplied key against the process-configured key."""
    supplied_key = api_key_from_headers(headers)
    if not supplied_key:
        return False
    try:
        supplied_bytes = supplied_key.encode("utf-8")
        expected_bytes = API_KEY.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(supplied_bytes, expected_bytes)
