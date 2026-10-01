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
        if route.get("conditional_headers"):
            conditional_errors = {
                "400": (
                    "Malformed conditional header (invalid_precondition) or request."
                ),
                "412": (
                    "The supplied If-Match value does not match "
                    "(precondition_failed)."
                ),
                "428": (
                    "If-Match is required when AGENT_QA_REQUIRE_IF_MATCH=true "
                    "(precondition_required)."
                ),
            }
            for status, description in conditional_errors.items():
                if status in operation["responses"]:
                    operation["responses"][status].update(
                        {
                            "description": description,
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Error"}
                                }
                            },
                        }
                    )
            etag_header = {
                "description": (
                    "The current entity tag for the returned representation."
                ),
                "schema": {"type": "string"},
            }
            for status in ("200", "201", "304", "412"):
                if status in operation["responses"]:
                    operation["responses"][status].setdefault("headers", {})["ETag"] = (
                        deepcopy(etag_header)
                    )
            if "304" in operation["responses"]:
                operation["responses"]["304"].setdefault("headers", {})[
                    "X-Request-Id"
                ] = {
                    "description": "Request ID for this response.",
                    "schema": {"type": "string"},
                }
        elif route.get("etag_response"):
            for status in ("200", "201"):
                if status in operation["responses"]:
                    operation["responses"][status].setdefault("headers", {})["ETag"] = {
                        "description": (
                            "The current entity tag for the returned representation."
                        ),
                        "schema": {"type": "string"},
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
        for status, description in route.get("error_responses", {}).items():
            if status in operation["responses"]:
                operation["responses"][status].update(
                    {
                        "description": description,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Error"}
                            }
                        },
                    }
                )
        for status, headers in route.get("response_headers", {}).items():
            if status in operation["responses"]:
                operation["responses"][status].setdefault("headers", {}).update(
                    deepcopy(headers)
                )
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
