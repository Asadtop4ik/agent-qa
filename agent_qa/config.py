"""Configuration shared by the agent QA service.

The API key default is synthetic and intended only for QA environments.
"""

import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent.parent
FIXTURE_PATH = APP_DIR / "data" / "synthetic-customer.json"
GIT_SHA = os.environ.get("AGENT_QA_GIT_SHA", "unknown")
API_KEY = os.environ.get("AGENT_QA_API_KEY") or "qa-synthetic-key"


def port() -> int:
    """Return the configured HTTP port."""
    return int(os.environ.get("APP_PORT", "8080"))
