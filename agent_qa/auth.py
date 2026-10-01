"""API-key authentication for the synthetic QA service."""

from collections.abc import Mapping

from agent_qa.config import API_KEY
from agent_qa.keys import KeyStore

KEY_STORE = KeyStore(API_KEY)


def api_key_from_headers(headers: Mapping[str, str]) -> str | None:
    """Return the X-API-Key header value, matching the name case-insensitively."""
    for name, value in headers.items():
        if name.lower() == "x-api-key":
            return value
    return None


def authenticate_api_key(headers: Mapping[str, str]) -> dict[str, str] | None:
    """Return authenticated key identity, updating its last-use timestamp."""
    return KEY_STORE.authenticate(api_key_from_headers(headers))


def is_valid_api_key(headers: Mapping[str, str]) -> bool:
    """Check the supplied key while preserving the original public API."""
    return authenticate_api_key(headers) is not None
