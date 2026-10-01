"""Declarative environment settings and validation helpers.

Importing this module has no configuration side effects. Call :func:`load` to
validate an environment mapping, or :func:`current` to read the process
environment when a value is needed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import os
import re


@dataclass(frozen=True)
class Setting:
    name: str
    type: str
    default: object
    description: str
    secret: bool = False
    min: int | float | None = None
    max: int | float | None = None
    pattern: str | None = None


@dataclass(frozen=True)
class SettingError:
    field: str
    message: str


@dataclass(frozen=True)
class Loaded:
    values: dict[str, object]
    sources: dict[str, str]
    errors: tuple[SettingError, ...]
    unknown_env: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors


SETTINGS: dict[str, Setting] = {
    "APP_PORT": Setting("APP_PORT", "int", 8080, "HTTP server port.", min=1, max=65535),
    "AGENT_QA_GIT_SHA": Setting(
        "AGENT_QA_GIT_SHA",
        "str",
        "unknown",
        "Build revision reported by the service.",
        min=1,
        max=128,
    ),
    "AGENT_QA_API_KEY": Setting(
        "AGENT_QA_API_KEY",
        "str",
        "qa-synthetic-key",
        "API key required for authenticated requests.",
        secret=True,
        min=1,
        max=4096,
    ),
    "AGENT_QA_IDEMPOTENCY_TTL_SECONDS": Setting(
        "AGENT_QA_IDEMPOTENCY_TTL_SECONDS",
        "int",
        600,
        "Lifetime of stored idempotent responses in seconds.",
        min=1,
        max=86400,
    ),
    "AGENT_QA_REQUIRE_IF_MATCH": Setting(
        "AGENT_QA_REQUIRE_IF_MATCH",
        "bool",
        False,
        "Require If-Match for conditional updates.",
    ),
    "AGENT_QA_AUDIT_CAPACITY": Setting(
        "AGENT_QA_AUDIT_CAPACITY",
        "int",
        500,
        "Maximum number of audit entries retained in memory.",
        min=10,
        max=5000,
    ),
    "AGENT_QA_RATE_BURST": Setting(
        "AGENT_QA_RATE_BURST",
        "int",
        120,
        "Token bucket request burst size.",
        min=1,
        max=100000,
    ),
    "AGENT_QA_RATE_REFILL_PER_SECOND": Setting(
        "AGENT_QA_RATE_REFILL_PER_SECOND",
        "float",
        60.0,
        "Token bucket refill rate per second.",
        min=0.001,
        max=10000.0,
    ),
    "AGENT_QA_JOB_WORKERS": Setting(
        "AGENT_QA_JOB_WORKERS",
        "int",
        2,
        "Number of lazily started background job workers.",
        min=1,
        max=3,
    ),
    "AGENT_QA_JOB_RETENTION": Setting(
        "AGENT_QA_JOB_RETENTION",
        "int",
        100,
        "Maximum number of terminal jobs retained in memory.",
        min=10,
        max=1000,
    ),
}

_BOOLS = {
    "true": True,
    "1": True,
    "yes": True,
    "false": False,
    "0": False,
    "no": False,
}


def _error(setting: Setting) -> str:
    if setting.type == "int":
        return f"Must be an integer from {setting.min} to {setting.max}"
    if setting.type == "float":
        return f"Must be a number from {setting.min} to {setting.max}"
    if setting.type == "bool":
        return "Must be a boolean (true/false)"
    return f"Must contain {setting.min} to {setting.max} characters"


def _parse(setting: Setting, raw: str) -> object:
    if setting.type == "int":
        # This setting registry uses at most six digit maxima. Reject enormous
        # strings before int() so hostile values cannot consume excess resources.
        if len(raw) > 32:
            raise ValueError
        value = int(raw)
        if setting.min is not None and value < setting.min:
            raise ValueError
        if setting.max is not None and value > setting.max:
            raise ValueError
        return value
    if setting.type == "float":
        if len(raw) > 128:
            raise ValueError
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError
        if setting.min is not None and value < setting.min:
            raise ValueError
        if setting.max is not None and value > setting.max:
            raise ValueError
        return value
    if setting.type == "bool":
        if len(raw) > 5:
            raise ValueError
        try:
            return _BOOLS[raw.lower()]
        except (AttributeError, KeyError) as exc:
            raise ValueError from exc
    if len(raw) < (setting.min or 0) or len(raw) > (setting.max or 2**31):
        raise ValueError
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError from exc
    if setting.pattern is not None and re.fullmatch(setting.pattern, raw) is None:
        raise ValueError
    return raw


def load(env: Mapping[str, str]) -> Loaded:
    """Load settings without mutating the environment or exposing bad values."""
    values: dict[str, object] = {}
    sources: dict[str, str] = {}
    errors: list[SettingError] = []
    for name, setting in SETTINGS.items():
        raw = env.get(name)
        if raw is None or raw == "":
            values[name] = setting.default
            sources[name] = "default"
            continue
        try:
            value = _parse(setting, raw)
        except (TypeError, ValueError, OverflowError):
            values[name] = setting.default
            sources[name] = "default"
            errors.append(SettingError(name, _error(setting)))
        else:
            values[name] = value
            sources[name] = "env"
    errors.sort(key=lambda error: error.field)
    unknown_env = tuple(
        sorted(
            name
            for name in env
            if isinstance(name, str)
            and name.startswith("AGENT_QA_")
            and name not in SETTINGS
        )
    )
    return Loaded(values, sources, tuple(errors), unknown_env)


def current() -> Loaded:
    """Read and validate the current process environment on demand."""
    return load(os.environ)


def describe(loaded: Loaded, name: str) -> dict[str, object]:
    """Return redacted, JSON-friendly metadata for one registered setting."""
    setting = SETTINGS[name]
    value = loaded.values[name]
    default = setting.default
    return {
        "value": "***" if setting.secret else value,
        "default": "***" if setting.secret else default,
        "source": loaded.sources[name],
        "type": setting.type,
        "secret": setting.secret,
        "description": setting.description,
        "is_default": value == default,
    }
