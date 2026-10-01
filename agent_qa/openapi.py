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
        produces = route.get("produces", ["application/json"])
        operation: dict[str, Any] = {
            "operationId": route["operation_id"],
            "summary": route["summary"],
            "description": (
                "Accept negotiates the successful response media type. "
                "application/problem+json errors are available when its quality "
                "is at least the JSON alternatives; an unacceptable response "
                "type returns 406. Accept-Encoding may request gzip for response "
                "bodies of at least 256 bytes."
            ),
            "x-required-role": route.get(
                "role", "write" if route["auth_required"] else None
            ),
            "responses": {
                status: {
                    "description": f"HTTP {status} response",
                    **(
                        {
                            "content": {
                                media_type: {
                                    "schema": deepcopy(response_schemas[status])
                                }
                                for media_type in produces
                            }
                        }
                        if status in response_schemas
                        else {}
                    ),
                }
                for status in route["responses"]
            },
        }
        operation["responses"]["403"] = {
            "description": "The API key role is insufficient (forbidden).",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
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
            if "422" in route.get("response_schemas", {}) and 422 in route.get(
                "idempotency_replay_statuses", ()
            ):
                operation["responses"]["422"]["description"] = (
                    "The bulk result contains no successful items, or the "
                    "Idempotency-Key was reused with a different request body."
                )
                operation["responses"]["422"]["content"]["application/json"][
                    "schema"
                ] = {
                    "oneOf": [
                        deepcopy(route["response_schemas"]["422"]),
                        {"$ref": "#/components/schemas/Error"},
                    ]
                }
            else:
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
                if 200 <= int(status) < 300 or int(status) in route.get(
                    "idempotency_replay_statuses", ()
                ):
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
        if route.get("body"):
            operation["x-max-body-bytes"] = route.get("max_body_bytes", 4096)
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
        operation["responses"].setdefault(
            "406",
            {
                "description": "No acceptable response representation is available.",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/Error"}
                    },
                    "application/problem+json": {
                        "schema": {"$ref": "#/components/schemas/Problem"}
                    },
                },
            },
        )
        for status, response in operation["responses"].items():
            if 200 <= int(status) < 300 and int(status) not in (204, 205):
                response.setdefault(
                    "content", {media_type: {} for media_type in produces}
                )
            if 400 <= int(status) < 600:
                response.setdefault("content", {}).setdefault(
                    "application/problem+json",
                    {"schema": {"$ref": "#/components/schemas/Problem"}},
                )
        if route.get("role", route["auth_required"]):
            operation["security"] = [{"ApiKeyAuth": []}]
        paths.setdefault(path, {})[method] = operation

    schemas = deepcopy(SCHEMAS)
    schemas["Problem"] = {
        "type": "object",
        "required": [
            "type",
            "title",
            "status",
            "detail",
            "instance",
            "code",
            "request_id",
        ],
        "properties": {
            "type": {"type": "string", "format": "uri"},
            "title": {"type": "string"},
            "status": {"type": "integer", "minimum": 400, "maximum": 599},
            "detail": {"type": "string"},
            "instance": {"type": "string"},
            "code": {"type": "string"},
            "request_id": {"type": "string"},
            "errors": {"type": "array", "items": {"type": "object"}},
        },
        "additionalProperties": False,
    }
    return {
        "openapi": "3.0.3",
        "info": {"title": "agent-qa", "version": "1.0.0", "x-git-sha": git_sha},
        "paths": paths,
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
            },
            "schemas": schemas,
        },
    }
