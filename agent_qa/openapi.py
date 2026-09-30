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
        response_headers = route.get("response_headers", {})
        operation: dict[str, Any] = {
            "operationId": route["operation_id"],
            "summary": route["summary"],
            "responses": {
                status: {
                    "description": f"HTTP {status} response",
                    **(
                        {"headers": deepcopy(response_headers[status])}
                        if status in response_headers
                        else {}
                    ),
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
                for status in route["responses"]
            },
        }
        if route.get("idempotent") and method == "post":
            parameter = {
                "name": "Idempotency-Key",
                "in": "header",
                "required": False,
                "description": "Optional key used to safely replay this request.",
                "schema": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "pattern": "^[A-Za-z0-9._:-]+$",
                },
            }
            operation["parameters"] = deepcopy(route.get("parameters", []))
            if not any(
                item.get("name") == "Idempotency-Key" and item.get("in") == "header"
                for item in operation["parameters"]
            ):
                operation["parameters"].append(parameter)
            error_content = {
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/Error"}
                    }
                }
            }
            operation["responses"]["400"] = {
                "description": (
                    "Malformed request, including an invalid Idempotency-Key "
                    "(invalid_idempotency_key)."
                ),
                **deepcopy(error_content),
            }
            operation["responses"]["409"] = {
                "description": (
                    "Request conflicts with current state or another request using "
                    "this Idempotency-Key is in progress (idempotency_in_progress)."
                ),
                **deepcopy(error_content),
            }
            operation["responses"]["422"] = {
                "description": (
                    "This Idempotency-Key was already used with a different "
                    "request body (idempotency_key_reused)."
                ),
                **deepcopy(error_content),
            }
            replay_headers = {
                "Idempotency-Key": {
                    "description": "The Idempotency-Key echoed from the request.",
                    "schema": {"type": "string"},
                },
                "Idempotent-Replay": {
                    "description": (
                        "Present with value true when a saved response is replayed."
                    ),
                    "schema": {"type": "string", "enum": ["true"]},
                },
                "X-Request-Id": {
                    "description": "Request ID for the current HTTP request.",
                    "schema": {"type": "string"},
                },
            }
            for status, response in operation["responses"].items():
                if 200 <= int(status) < 300:
                    response.setdefault("headers", {}).update(deepcopy(replay_headers))
        elif route.get("parameters"):
            operation["parameters"] = deepcopy(route["parameters"])
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
            "securitySchemes": {
                "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
            },
            "schemas": deepcopy(SCHEMAS),
        },
    }
