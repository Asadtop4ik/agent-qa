"""API key authentication for the synthetic QA service.

The ``qa-synthetic-key`` default is only for the synthetic QA environment.
"""

import hmac

from agent_qa.config import API_KEY


def header_api_key(value: str | None) -> str | None:
    """Return a non-empty API key header value, if supplied."""
    if value is None or not value:
        return None
    return value


def is_valid_api_key(value: str | None) -> bool:
    """Compare a supplied key with the process-configured key."""
    candidate = header_api_key(value)
    if candidate is None:
        return False
    return hmac.compare_digest(API_KEY.encode("utf-8"), candidate.encode("utf-8"))
