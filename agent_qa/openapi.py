"""OpenAPI document construction without HTTP or process dependencies."""

from copy import deepcopy
from typing import Any, Iterable

from agent_qa.schemas import SCHEMAS
from agent_qa.versioning import DEPRECATED_EPOCH, SUNSET_HTTP_DATE


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
            "x-rate-limited": route.get("rate_limited", True),
            "x-required-role": route.get(
                "role", "write" if route["auth_required"] else None
            ),
            "responses": {
                status: {
                    "description": f"HTTP {status} response",
                    **(
                        {
                            "content": {
                                produces[0]: {
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
        api_version = route.get("api_version")
        if api_version is not None:
            operation["x-api-version"] = str(api_version)
        if route.get("deprecated"):
            operation["deprecated"] = True
            operation["responses"].setdefault(
                "410",
                {"description": ("Deprecated API version has passed its sunset date.")},
            )
        if route.get("tenant_scoped"):
            tenant_errors = {"400": "invalid_tenant"}
            if route.get("method") in {"POST", "PUT", "PATCH", "DELETE"}:
                tenant_errors["409"] = "tenant_limit"
            for status, code in tenant_errors.items():
                operation["responses"].setdefault(
                    status,
                    {
                        "description": f"Request failed ({code}).",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Error"}
                            }
                        },
                    },
                )
        for status, response in operation["responses"].items():
            if (
                status.startswith("2")
                and status not in response_schemas
                and produces != ["application/json"]
            ):
                response["content"] = {
                    media_type: {"schema": {"type": "string"}}
                    for media_type in produces
                }
        operation["responses"]["403"] = {
            "description": "The API key role is insufficient (forbidden).",
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
            },
        }
        if operation["x-rate-limited"]:
            error_response = {
                "description": "Request rate limit exceeded (rate_limited).",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/Error"}
                    }
                },
                "headers": {
                    "Retry-After": {
                        "description": "Seconds until one token is available.",
                        "schema": {"type": "string"},
                    }
                },
            }
            operation["responses"]["429"] = error_response
        operation["responses"]["406"] = {
            "description": (
                "No route response representation is acceptable according to "
                "Accept (not_acceptable)."
            ),
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
        consumes = route.get("consumes", ["application/json"])
        if route.get("request_schema") is not None:
            operation["requestBody"] = {
                "required": True,
                "content": {
                    media_type: {"schema": deepcopy(route["request_schema"])}
                    for media_type in consumes
                },
            }
        elif route.get("body") and route.get("consumes"):
            operation["requestBody"] = {
                "required": True,
                "content": {
                    media_type: {
                        "schema": {
                            "type": "string",
                            "description": "UTF-8 CSV text.",
                        }
                    }
                    for media_type in consumes
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
        if route.get("method") in {"POST", "PUT", "PATCH", "DELETE"} and not str(
            route.get("path", "")
        ).startswith("/admin/"):
            maintenance = {
                "description": "Configured maintenance retry delay in seconds.",
                "schema": {"type": "string"},
            }
            response = operation["responses"].get("503")
            if response is None:
                response = {
                    "description": (
                        "Writes are temporarily rejected during maintenance."
                    ),
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"}
                        }
                    },
                    "headers": {},
                }
                operation["responses"]["503"] = response
            else:
                response["description"] += (
                    " Writes are also rejected during maintenance."
                )
            response.setdefault("headers", {})["Retry-After"] = maintenance
        if operation["x-rate-limited"]:
            rate_headers = {
                "RateLimit-Limit": {
                    "description": "The configured token bucket capacity.",
                    "schema": {"type": "string"},
                },
                "RateLimit-Remaining": {
                    "description": "The remaining whole request tokens.",
                    "schema": {"type": "string"},
                },
                "RateLimit-Reset": {
                    "description": "Seconds until the bucket is full.",
                    "schema": {"type": "string"},
                },
            }
            for response in operation["responses"].values():
                response.setdefault("headers", {}).update(deepcopy(rate_headers))
        if route.get("role", route["auth_required"]):
            operation["security"] = [{"ApiKeyAuth": []}]
        if api_version is not None:
            version_header = {
                "description": "API version used for this response.",
                "schema": {"type": "string", "enum": [str(api_version)]},
            }
            for response in operation["responses"].values():
                headers = response.setdefault("headers", {})
                headers["X-API-Version"] = deepcopy(version_header)
                if route.get("deprecated"):
                    headers["Deprecation"] = {
                        "description": "RFC 9745 deprecation date.",
                        "schema": {"type": "string", "example": f"@{DEPRECATED_EPOCH}"},
                    }
                    headers["Sunset"] = {
                        "description": "RFC 8594 sunset date.",
                        "schema": {"type": "string", "example": SUNSET_HTTP_DATE},
                    }
                    successor = route.get("successor")
                    if isinstance(successor, str):
                        existing_link = headers.get("Link", {})
                        description = existing_link.get("description", "")
                        if description:
                            description += " A successor-version link is also present."
                        else:
                            description = "Successor API version."
                        headers["Link"] = {
                            "description": description,
                            "schema": {"type": "string"},
                        }
            if str(api_version) == "2":
                for status, response in operation["responses"].items():
                    if status.startswith("4") or status.startswith("5"):
                        response["content"] = {
                            "application/problem+json": {"schema": {"type": "object"}}
                        }
        if route.get("tenant_scoped"):
            parameters = operation.setdefault("parameters", [])
            if not any(
                parameter.get("name") == "X-Tenant" and parameter.get("in") == "header"
                for parameter in parameters
            ):
                parameters.append(
                    {
                        "name": "X-Tenant",
                        "in": "header",
                        "required": False,
                        "description": "Tenant name; defaults to 'default'.",
                        "schema": {
                            "type": "string",
                            "pattern": "^[a-z0-9][a-z0-9-]{0,23}$",
                            "default": "default",
                        },
                    }
                )
            tenant_header = {
                "description": "Tenant selected for this response.",
                "schema": {"type": "string"},
            }
            for response in operation["responses"].values():
                response.setdefault("headers", {})["X-Tenant"] = deepcopy(tenant_header)
            tenant_errors = {"400": "invalid_tenant"}
            if route.get("method") in {"POST", "PUT", "PATCH", "DELETE"}:
                tenant_errors["409"] = "tenant_limit"
            for status, code in tenant_errors.items():
                response = operation["responses"][status]
                description = response.get("description", f"HTTP {status} response")
                tenant_description = (
                    "The X-Tenant value is invalid (invalid_tenant)."
                    if code == "invalid_tenant"
                    else "The tenant limit is reached (tenant_limit)."
                )
                if code not in description:
                    response["description"] = f"{description} {tenant_description}"
                response.setdefault(
                    "content",
                    {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"}
                        }
                    },
                )
        paths.setdefault(path, {})[method] = operation

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "agent-qa",
            "version": "1.0.0",
            "x-git-sha": git_sha,
            "description": (
                "Requests may negotiate successful response types with Accept. "
                "Errors use the standard error envelope unless "
                "application/problem+json is accepted at least as strongly as "
                "application/json; then errors use RFC 9457 problem details. "
                "Responses with bodies may be compressed with gzip when accepted "
                "by Accept-Encoding and the body is at least 256 bytes. "
                "Request Content-Encoding values other than identity are rejected "
                "with 415 unsupported_content_encoding."
            ),
        },
        "paths": paths,
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
            },
            "schemas": deepcopy(SCHEMAS),
        },
    }
