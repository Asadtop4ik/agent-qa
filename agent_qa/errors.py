"""API error types and response body construction."""

from http import HTTPStatus
from urllib.parse import urlsplit


class ApiError(Exception):
    """An expected API error with an HTTP status and public details."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def envelope(
    code: str,
    message: str,
    details: list[dict[str, str]] | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    """Build the service's standard error response body."""
    error: dict[str, object] = {"code": code, "message": message}
    if request_id is not None:
        error["request_id"] = request_id
    if details is not None:
        error["details"] = details
    return {"error": error}


def problem(
    status: int,
    code: str,
    message: str,
    details: list[dict[str, str]] | None = None,
    request_id: str | None = None,
    instance: str = "",
) -> dict[str, object]:
    """Build an RFC 9457 problem details response with service extensions."""
    try:
        title = HTTPStatus(status).phrase
    except ValueError:
        title = "Unknown Error"
    try:
        path = urlsplit(instance).path
    except ValueError:
        path = ""
    result: dict[str, object] = {
        "type": f"https://agent-qa.invalid/problems/{code}",
        "title": title,
        "status": status,
        "detail": message,
        "instance": path,
        "code": code,
    }
    if request_id is not None:
        result["request_id"] = request_id
    if details is not None:
        result["errors"] = details
    return result
