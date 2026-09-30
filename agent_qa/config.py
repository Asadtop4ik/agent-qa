"""Configuration shared by the agent QA service.

The API key default is synthetic and intended only for QA environments.
"""

import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent.parent
FIXTURE_PATH = APP_DIR / "data" / "synthetic-customer.json"
GIT_SHA = os.environ.get("AGENT_QA_GIT_SHA", "unknown")
API_KEY = os.environ.get("AGENT_QA_API_KEY") or "qa-synthetic-key"


def idempotency_ttl_seconds() -> int:
    """Return the bounded idempotency response lifetime in seconds."""
    raw_value = os.environ.get("AGENT_QA_IDEMPOTENCY_TTL_SECONDS", "600")
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(
            "AGENT_QA_IDEMPOTENCY_TTL_SECONDS must be between 1 and 86400"
        ) from error
    if not 1 <= value <= 86400:
        raise ValueError("AGENT_QA_IDEMPOTENCY_TTL_SECONDS must be between 1 and 86400")
    return value


def port() -> int:
    """Return the configured HTTP port."""
    return int(os.environ.get("APP_PORT", "8080"))
