"""Per-request context shared by handlers and storage layers."""

from __future__ import annotations

from dataclasses import dataclass
import threading


@dataclass
class RequestContext:
    """Bounded metadata for the request currently handled by this thread."""

    request_id: str
    actor: str = "anonymous"
    role: str | None = None
    resource: str | None = None
    resource_id: int | str | None = None
    changes: dict[str, object] | None = None


_LOCAL = threading.local()


def set_context(context: RequestContext) -> None:
    """Set the current thread's request context."""
    if not isinstance(context, RequestContext):
        raise TypeError("context must be a RequestContext")
    _LOCAL.context = context


def get_context() -> RequestContext | None:
    """Return this thread's current request context, if one is set."""
    return getattr(_LOCAL, "context", None)


def clear_context() -> None:
    """Remove this thread's request context."""
    if hasattr(_LOCAL, "context"):
        del _LOCAL.context
