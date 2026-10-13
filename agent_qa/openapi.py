"""OpenAPI document construction without HTTP or process dependencies."""

from copy import deepcopy
from typing import Any, Iterable

from agent_qa.schemas import SCHEMAS


def build_openapi(routes: Iterable[dict[str, Any]], git_sha: str) -> dict[str, Any]:
    """Build a deterministic OpenAPI 3.0.3 document from route metadata."""
    paths: dict[str, dict[str, Any]] = {}
    for route in routes:
        method = route["method"].lower()
        path = route["path"]
        response_schemas = route.get("response_schemas", {})
        responses = list(route["responses"])
        parameters = deepcopy(route.get("parameters", []))
        if route.get("idempotent"):
            for status in ("422", "409"):
                if status not in responses:
                    responses.append(status)
            parameters.append(
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": False,
                    "description": (
                        "Optional key used to replay a successful request safely."
                    ),
                    "schema": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                        "pattern": "^[A-Za-z0-9._:-]+$",
                    },
                }
            )
        operation: dict[str, Any] = {
            "operationId": route["operation_id"],
            "summary": route["summary"],
            "responses": {
                status: {
                    "description": f"HTTP {status} response",
                    **(
                        {
                            "content": {
                                "application/json": {
                                    "schema": deepcopy(response_schemas[status])
                                }
                            }
                        }
                        if status in response_schemas
                        else {}
                    ),
                }
                for status in responses
            },
        }
        if route.get("idempotent"):
            success_headers = {
                "Idempotent-Replay": {
                    "description": (
                        "Present as true when the response replays a stored result."
                    ),
                    "schema": {"type": "string", "enum": ["true"]},
                },
                "Idempotency-Key": {
                    "description": "Echo of the supplied idempotency key.",
                    "schema": {"type": "string"},
                },
                "X-Request-Id": {
                    "description": "Request ID for this request, including replays.",
                    "schema": {"type": "string"},
                },
            }
            for status in route["responses"]:
                if status.startswith("2"):
                    response = operation["responses"][status]
                    response["headers"] = deepcopy(success_headers)
            for status, description in (
                (
                    "422",
                    "Request error with code idempotency_key_reused when the key "
                    "was used with a different request body.",
                ),
                (
                    "409",
                    "Request error with code idempotency_in_progress when a request "
                    "using this key is still in progress; other route conflicts may "
                    "also return 409.",
                ),
            ):
                operation["responses"][status] = {
                    "description": description,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"}
                        }
                    },
                }
        if parameters:
            operation["parameters"] = parameters
        if route.get("request_schema") is not None:
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {"schema": deepcopy(route["request_schema"])}
                },
            }
        if route["auth_required"]:
            operation["security"] = [{"ApiKeyAuth": []}]
        paths.setdefault(path, {})[method] = operation

    return {
        "openapi": "3.0.3",
        "info": {"title": "agent-qa", "version": "1.0.0", "x-git-sha": git_sha},
        "paths": paths,
        "components": {
            "schemas": deepcopy(SCHEMAS),
            "securitySchemes": {
                "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
            },
        },
    }
