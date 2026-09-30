"""Shared JSON-schema subset definitions and order limits."""

STATUSES = ("new", "paid", "shipped", "cancelled")
ORDER_STATUSES = frozenset(STATUSES)
MIN_LIMIT = 1
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
MIN_OFFSET = 0
DEFAULT_OFFSET = 0
MIN_TOTAL_CENTS = 0
MAX_TOTAL_CENTS = 100_000_000
MIN_CUSTOMER_ID_LENGTH = 1
MAX_CUSTOMER_ID_LENGTH = 64
MAX_ORDERS = 1000

_CUSTOMER_ID = {
    "type": "string",
    "minLength": MIN_CUSTOMER_ID_LENGTH,
    "maxLength": MAX_CUSTOMER_ID_LENGTH,
    "x-nonBlank": True,
}
_TOTAL_CENTS = {
    "type": "integer",
    "minimum": MIN_TOTAL_CENTS,
    "maximum": MAX_TOTAL_CENTS,
}
_STATUS = {"type": "string", "enum": list(STATUSES)}

SCHEMAS = {
    "CreateOrder": {
        "type": "object",
        "required": ["customer_id", "total_cents"],
        "properties": {"customer_id": _CUSTOMER_ID, "total_cents": _TOTAL_CENTS},
        "additionalProperties": False,
    },
    "UpdateOrder": {
        "type": "object",
        "properties": {"status": _STATUS, "total_cents": _TOTAL_CENTS},
        "minProperties": 1,
        "additionalProperties": False,
    },
    "Order": {
        "type": "object",
        "required": ["id", "customer_id", "total_cents", "status", "created_at"],
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "customer_id": _CUSTOMER_ID,
            "total_cents": _TOTAL_CENTS,
            "status": _STATUS,
            "created_at": {"type": "string", "format": "date-time"},
        },
        "additionalProperties": False,
    },
    "OrderList": {
        "type": "object",
        "required": ["items", "total", "limit", "offset"],
        "properties": {
            "items": {"type": "array", "items": {"$ref": "#/components/schemas/Order"}},
            "total": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": MIN_LIMIT, "maximum": MAX_LIMIT},
            "offset": {"type": "integer", "minimum": MIN_OFFSET},
        },
        "additionalProperties": False,
    },
    "Error": {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message", "request_id"],
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "request_id": {"type": "string"},
                    "details": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["field", "message"],
                            "properties": {
                                "field": {"type": "string"},
                                "message": {"type": "string"},
                            },
                            "additionalProperties": False,
                        },
                    },
                },
                "additionalProperties": False,
            }
        },
        "additionalProperties": False,
    },
}

# Keep this nested schema shared with the named Order component while leaving
# it directly traversable by the validator's supported subset.
SCHEMAS["OrderList"]["properties"]["items"]["items"] = SCHEMAS["Order"]
