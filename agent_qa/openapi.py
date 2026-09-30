"""OpenAPI document construction without HTTP or process dependencies."""

from copy import deepcopy
from typing import Any, Iterable


def build_openapi(routes: Iterable[dict[str, Any]], git_sha: str) -> dict[str, Any]:
    """Build a deterministic OpenAPI 3.0.3 document from route metadata."""
    paths: dict[str, dict[str, Any]] = {}
    for route in routes:
        method = route["method"].lower()
        path = route["path"]
        response_schemas = route.get("response_schemas", {})
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
                for status in route["responses"]
            },
        }
        if route.get("parameters"):
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
            }
        },
    }
