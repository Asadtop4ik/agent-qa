"""API error types and response body construction."""


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
) -> dict[str, object]:
    """Build the service's standard error response body."""
    error: dict[str, object] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {"error": error}
