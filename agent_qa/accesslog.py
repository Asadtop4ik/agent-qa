"""JSON access logging for the HTTP server."""

from datetime import datetime, timezone
import json
import threading


_WRITE_LOCK = threading.Lock()


def write_access_log(
    method: str,
    path: str,
    route: str,
    status: int,
    duration_seconds: float,
    request_id: str,
    trace_id: str = "",
) -> None:
    """Write one request summary without headers, query values, or body data."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "method": method,
        "path": path,
        "route": route,
        "status": status,
        "duration_ms": round(duration_seconds * 1000, 3),
        "request_id": request_id,
        "trace_id": trace_id,
    }
    with _WRITE_LOCK:
        print(json.dumps(entry, separators=(",", ":")), flush=True)
