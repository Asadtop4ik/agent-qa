"""Compatibility accessors for validated application settings."""

from pathlib import Path

from agent_qa import settings


APP_DIR = Path(__file__).resolve().parent.parent
FIXTURE_PATH = APP_DIR / "data" / "synthetic-customer.json"

# Preserve the established import-time constants while making malformed values
# harmless during import. ``server.main`` performs strict validation before bind.
_IMPORT_SETTINGS = settings.current()
GIT_SHA = _IMPORT_SETTINGS.values["AGENT_QA_GIT_SHA"]
API_KEY = _IMPORT_SETTINGS.values["AGENT_QA_API_KEY"]


def idempotency_ttl_seconds() -> int:
    """Return the configured lifetime, using its default if invalid."""
    return settings.current().values["AGENT_QA_IDEMPOTENCY_TTL_SECONDS"]


def job_workers() -> int:
    """Return the configured number of lazily started job workers."""
    return settings.current().values["AGENT_QA_JOB_WORKERS"]


def job_retention() -> int:
    """Return the configured number of terminal jobs kept in memory."""
    return settings.current().values["AGENT_QA_JOB_RETENTION"]


def port() -> int:
    """Return the configured HTTP port, falling back on malformed input."""
    return settings.current().values["APP_PORT"]
