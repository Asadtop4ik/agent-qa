"""Shared JSON-schema subset definitions and collection limits."""

STATUSES = ("new", "paid", "shipped", "cancelled")
ORDER_STATUSES = frozenset(STATUSES)
MIN_LIMIT = 1
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
MIN_OFFSET = 0
DEFAULT_OFFSET = 0
MIN_TOTAL_CENTS = 0
MAX_TOTAL_CENTS = 100_000_000
MAX_ORDER_ITEMS = 20
MAX_ORDER_QUANTITY = 1000
MIN_CUSTOMER_ID_LENGTH = 1
MAX_CUSTOMER_ID_LENGTH = 64
MAX_ORDERS = 1000
MAX_PRODUCTS = 500
MIN_PRODUCT_NAME_LENGTH = 1
MAX_PRODUCT_NAME_LENGTH = 120
MIN_PRICE_CENTS = 0
MAX_PRICE_CENTS = 100_000_000
MIN_STOCK = 0
MAX_STOCK = 1_000_000
MAX_PRODUCT_TAGS = 10
MAX_PRODUCT_TAG_LENGTH = 24
MAX_SKU_LENGTH = 32
MAX_CATEGORY_LENGTH = 32
MAX_PRODUCT_QUERY_LENGTH = 64
PRODUCT_SORTS = (
    "id",
    "-id",
    "price_cents",
    "-price_cents",
    "name",
    "-name",
    "created_at",
)

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
_ORDER_ITEM_INPUT = {
    "type": "object",
    "required": ["product_id", "quantity"],
    "properties": {
        "product_id": {"type": "integer", "minimum": 1},
        "quantity": {"type": "integer", "minimum": 1, "maximum": MAX_ORDER_QUANTITY},
    },
    "additionalProperties": False,
}
_STATUS = {"type": "string", "enum": list(STATUSES)}
_SKU = {
    "type": "string",
    "minLength": 2,
    "maxLength": MAX_SKU_LENGTH,
    "pattern": "^[A-Z0-9][A-Z0-9-]{1,31}$",
    "x-fullMatch": True,
}
_PRODUCT_NAME = {
    "type": "string",
    "minLength": MIN_PRODUCT_NAME_LENGTH,
    "maxLength": MAX_PRODUCT_NAME_LENGTH,
    "x-nonBlank": True,
}
_CATEGORY = {
    "type": "string",
    "minLength": 1,
    "maxLength": MAX_CATEGORY_LENGTH,
    "pattern": "^[a-z0-9][a-z0-9-]{0,31}$",
    "x-fullMatch": True,
}
_PRICE_CENTS = {
    "type": "integer",
    "minimum": MIN_PRICE_CENTS,
    "maximum": MAX_PRICE_CENTS,
}
_STOCK = {"type": "integer", "minimum": MIN_STOCK, "maximum": MAX_STOCK}
_TAG = {
    "type": "string",
    "minLength": 1,
    "maxLength": MAX_PRODUCT_TAG_LENGTH,
    "pattern": "^[a-z0-9-]{1,24}$",
    "x-fullMatch": True,
}
_TAGS = {
    "type": "array",
    "maxItems": MAX_PRODUCT_TAGS,
    "uniqueItems": True,
    "items": _TAG,
}
_ACTIVE = {"type": "boolean"}
_ORDER_ITEM = {
    "type": "object",
    "required": [
        "product_id",
        "sku",
        "name",
        "quantity",
        "unit_price_cents",
        "line_total_cents",
    ],
    "properties": {
        "product_id": {"type": "integer", "minimum": 1},
        "sku": _SKU,
        "name": _PRODUCT_NAME,
        "quantity": {"type": "integer", "minimum": 1, "maximum": MAX_ORDER_QUANTITY},
        "unit_price_cents": _PRICE_CENTS,
        "line_total_cents": _TOTAL_CENTS,
    },
    "additionalProperties": False,
}
_PRODUCT_FIELDS = {
    "name": _PRODUCT_NAME,
    "category": _CATEGORY,
    "price_cents": _PRICE_CENTS,
    "stock": _STOCK,
    "tags": _TAGS,
    "active": _ACTIVE,
}
_PRODUCT = {
    "type": "object",
    "required": [
        "id",
        "sku",
        "name",
        "category",
        "price_cents",
        "stock",
        "tags",
        "active",
        "created_at",
        "updated_at",
    ],
    "properties": {
        "id": {"type": "integer", "minimum": 1},
        "sku": _SKU,
        **_PRODUCT_FIELDS,
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
    },
    "additionalProperties": False,
}

SCHEMAS = {
    "CreateJob": {
        "type": "object",
        "required": ["type"],
        "properties": {
            "type": {
                "type": "string",
                "enum": ["sleep", "orders_summary", "stock_report", "fail"],
            },
            "params": {"type": "object"},
        },
        "additionalProperties": False,
        "oneOf": [
            {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": ["sleep"]},
                    "params": {"$ref": "#/components/schemas/SleepJobParams"},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": ["orders_summary"]},
                    "params": {"$ref": "#/components/schemas/OrdersSummaryJobParams"},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": ["stock_report"]},
                    "params": {"$ref": "#/components/schemas/StockReportJobParams"},
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": ["fail"]},
                    "params": {"$ref": "#/components/schemas/FailJobParams"},
                },
                "additionalProperties": False,
            },
        ],
    },
    "Job": {
        "type": "object",
        "required": [
            "id",
            "type",
            "params",
            "status",
            "progress",
            "result",
            "error",
            "cancel_requested",
            "created_at",
            "started_at",
            "finished_at",
        ],
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "type": {
                "type": "string",
                "enum": ["sleep", "orders_summary", "stock_report", "fail"],
            },
            "params": {"type": "object"},
            "status": {
                "type": "string",
                "enum": [
                    "queued",
                    "running",
                    "cancelling",
                    "succeeded",
                    "failed",
                    "cancelled",
                ],
            },
            "progress": {"type": "integer", "minimum": 0, "maximum": 100},
            "result": {
                "oneOf": [
                    {"type": "object", "nullable": True, "enum": [None]},
                    {"$ref": "#/components/schemas/SleepJobResult"},
                    {"$ref": "#/components/schemas/OrdersSummaryJobResult"},
                    {"$ref": "#/components/schemas/StockReportJobResult"},
                ],
            },
            "error": {"$ref": "#/components/schemas/JobError"},
            "cancel_requested": {"type": "boolean"},
            "created_at": {"type": "string", "format": "date-time"},
            "started_at": {"type": "string", "format": "date-time", "nullable": True},
            "finished_at": {"type": "string", "format": "date-time", "nullable": True},
        },
        "additionalProperties": False,
    },
    "SleepJobParams": {
        "type": "object",
        "properties": {
            "duration_ms": {
                "type": "integer",
                "minimum": 0,
                "maximum": 5000,
                "default": 100,
            }
        },
        "additionalProperties": False,
    },
    "OrdersSummaryJobParams": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "StockReportJobParams": {
        "type": "object",
        "properties": {
            "threshold": {
                "type": "integer",
                "minimum": 0,
                "maximum": 1_000_000,
                "default": 5,
            }
        },
        "additionalProperties": False,
    },
    "FailJobParams": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "minLength": 1,
                "maxLength": 100,
                "default": "boom",
            }
        },
        "additionalProperties": False,
    },
    "SleepJobResult": {
        "type": "object",
        "required": ["slept_ms"],
        "properties": {"slept_ms": {"type": "integer", "minimum": 0, "maximum": 5000}},
        "additionalProperties": False,
    },
    "OrdersSummaryJobResult": {
        "type": "object",
        "required": ["orders", "by_status", "revenue_cents"],
        "properties": {
            "orders": {"type": "integer", "minimum": 0},
            "by_status": {
                "type": "object",
                "required": ["new", "paid", "shipped", "cancelled"],
                "properties": {
                    "new": {"type": "integer", "minimum": 0},
                    "paid": {"type": "integer", "minimum": 0},
                    "shipped": {"type": "integer", "minimum": 0},
                    "cancelled": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": False,
            },
            "revenue_cents": {"type": "integer", "minimum": 0},
        },
        "additionalProperties": False,
    },
    "StockReportJobResult": {
        "type": "object",
        "required": ["threshold", "low_stock"],
        "properties": {
            "threshold": {"type": "integer", "minimum": 0, "maximum": 1_000_000},
            "low_stock": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["product_id", "sku", "stock"],
                    "properties": {
                        "product_id": {"type": "integer", "minimum": 1},
                        "sku": {"type": "string", "minLength": 2, "maxLength": 32},
                        "stock": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 1_000_000,
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    },
    "JobError": {
        "type": "object",
        "nullable": True,
        "required": ["code", "message"],
        "properties": {
            "code": {"type": "string", "enum": ["job_failed", "job_crashed"]},
            "message": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        "additionalProperties": False,
    },
    "JobList": {
        "type": "object",
        "required": ["items", "total", "limit", "offset"],
        "properties": {
            "items": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/Job"},
            },
            "total": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "offset": {"type": "integer", "minimum": 0},
        },
        "additionalProperties": False,
    },
    "CreateOrdersBulk": {
        "type": "object",
        "required": ["items"],
        "properties": {
            # Item validation belongs to the normal per-item create path.
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {},
            },
            "atomic": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    },
    "CreateProductsBulk": {
        "type": "object",
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {},
            },
            "atomic": {"type": "boolean", "default": False},
        },
        "additionalProperties": False,
    },
    "BulkCreateResponse": {
        "type": "object",
        "required": ["results", "summary"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "oneOf": [
                        {
                            "type": "object",
                            "required": ["index", "status", "data"],
                            "properties": {
                                "index": {"type": "integer", "minimum": 0},
                                "status": {"type": "integer", "enum": [201]},
                                "data": {"type": "object"},
                            },
                            "additionalProperties": False,
                        },
                        {
                            "type": "object",
                            "required": ["index", "status", "error"],
                            "properties": {
                                "index": {"type": "integer", "minimum": 0},
                                "status": {
                                    "type": "integer",
                                    "enum": [400, 404, 409, 424],
                                },
                                "error": {
                                    "type": "object",
                                    "required": ["code", "message"],
                                    "properties": {
                                        "code": {"type": "string"},
                                        "message": {"type": "string"},
                                        "details": {},
                                    },
                                },
                            },
                            "additionalProperties": False,
                        },
                    ]
                },
            },
            "summary": {
                "type": "object",
                "required": ["total", "succeeded", "failed"],
                "properties": {
                    "total": {"type": "integer", "minimum": 0, "maximum": 50},
                    "succeeded": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 50,
                    },
                    "failed": {"type": "integer", "minimum": 0, "maximum": 50},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    },
    "CreateOrder": {
        "type": "object",
        "required": ["customer_id"],
        "properties": {
            "customer_id": _CUSTOMER_ID,
            "total_cents": _TOTAL_CENTS,
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_ORDER_ITEMS,
                "items": _ORDER_ITEM_INPUT,
            },
        },
        "oneOf": [
            {"required": ["items"]},
            {"required": ["total_cents"]},
        ],
        "x-exactlyOne": ["items", "total_cents"],
        "additionalProperties": False,
    },
    "UpdateOrder": {
        "type": "object",
        "properties": {"status": _STATUS, "total_cents": _TOTAL_CENTS},
        "minProperties": 1,
        "additionalProperties": False,
    },
    "CreateProduct": {
        "type": "object",
        "required": ["sku", "name", "category", "price_cents"],
        "properties": {
            "sku": _SKU,
            "name": _PRODUCT_NAME,
            "category": _CATEGORY,
            "price_cents": _PRICE_CENTS,
            "stock": _STOCK,
            "tags": _TAGS,
            "active": _ACTIVE,
        },
        "additionalProperties": False,
    },
    "UpdateProduct": {
        "type": "object",
        # Route-level schema validation must let the handler report the
        # immutable-SKU error for every submitted value.
        "properties": {"sku": {"readOnly": True}, **_PRODUCT_FIELDS},
        "minProperties": 1,
        "additionalProperties": False,
    },
    "Product": _PRODUCT,
    "ProductList": {
        "type": "object",
        "required": ["items", "total", "limit"],
        "properties": {
            "items": {"type": "array", "items": _PRODUCT},
            "total": {"type": "integer", "minimum": 0, "maximum": MAX_PRODUCTS},
            "limit": {"type": "integer", "minimum": MIN_LIMIT, "maximum": MAX_LIMIT},
            "offset": {
                "type": "integer",
                "minimum": MIN_OFFSET,
                "maximum": (1 << 63) - 1,
            },
            "next_cursor": {"type": "string", "nullable": True},
        },
        "additionalProperties": False,
    },
    "AdjustStock": {
        "type": "object",
        "required": ["delta"],
        "properties": {
            "delta": {
                "type": "integer",
                "minimum": -MAX_STOCK,
                "maximum": MAX_STOCK,
                "x-nonZero": True,
            }
        },
        "additionalProperties": False,
    },
    "Category": {
        "type": "object",
        "required": [
            "category",
            "products",
            "active_products",
            "in_stock",
            "min_price_cents",
            "max_price_cents",
        ],
        "properties": {
            "category": _CATEGORY,
            "products": {"type": "integer", "minimum": 1},
            "active_products": {"type": "integer", "minimum": 0},
            "in_stock": {"type": "integer", "minimum": 0},
            "min_price_cents": _PRICE_CENTS,
            "max_price_cents": _PRICE_CENTS,
        },
        "additionalProperties": False,
    },
    "CategoryList": {
        "type": "object",
        "required": ["items", "total"],
        "properties": {
            "items": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/Category"},
            },
            "total": {"type": "integer", "minimum": 0, "maximum": MAX_PRODUCTS},
        },
        "additionalProperties": False,
    },
    "Order": {
        "type": "object",
        "required": [
            "id",
            "customer_id",
            "total_cents",
            "status",
            "created_at",
            "items",
        ],
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "customer_id": _CUSTOMER_ID,
            "total_cents": _TOTAL_CENTS,
            "status": _STATUS,
            "created_at": {"type": "string", "format": "date-time"},
            "items": {"type": "array", "items": _ORDER_ITEM},
        },
        "additionalProperties": False,
    },
    "OrderList": {
        "type": "object",
        "required": ["items", "total", "limit"],
        "properties": {
            "items": {"type": "array", "items": {"$ref": "#/components/schemas/Order"}},
            "total": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": MIN_LIMIT, "maximum": MAX_LIMIT},
            "offset": {
                "type": "integer",
                "minimum": MIN_OFFSET,
                "maximum": (1 << 63) - 1,
            },
            "next_cursor": {"type": "string", "nullable": True},
        },
        "additionalProperties": False,
    },
    "SearchResult": {
        "type": "object",
        "required": ["items", "total", "limit", "offset", "query"],
        "properties": {
            "items": {"type": "array", "items": {"type": "object"}},
            "total": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": MIN_LIMIT, "maximum": MAX_LIMIT},
            "offset": {
                "type": "integer",
                "minimum": MIN_OFFSET,
                "maximum": (1 << 63) - 1,
            },
            "query": {
                "type": "object",
                "required": ["normalized", "terms"],
                "properties": {
                    "normalized": {"type": "string"},
                    "terms": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    },
    "SearchExplain": {
        "type": "object",
        "required": ["resource", "normalized", "ast"],
        "properties": {
            "resource": {"type": "string"},
            "normalized": {"type": "string"},
            "ast": {"type": "object"},
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
                                "position": {
                                    "type": "string",
                                    "pattern": "^[0-9]+$",
                                },
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
SCHEMAS["ProductList"]["properties"]["items"]["items"] = SCHEMAS["Product"]
SCHEMAS["CategoryList"]["properties"]["items"]["items"] = SCHEMAS["Category"]

_WEBHOOK_EVENT_TYPES = [
    f"{resource}.{action}"
    for resource in ("order", "product")
    for action in ("created", "updated", "deleted")
]
SCHEMAS.update(
    {
        "CreateWebhook": {
            "type": "object",
            "required": ["url", "events", "secret"],
            "properties": {
                "url": {"type": "string", "minLength": 1, "maxLength": 200},
                "events": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 9,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "enum": _WEBHOOK_EVENT_TYPES + ["order.*", "product.*", "*"],
                    },
                },
                "secret": {"type": "string", "minLength": 8, "maxLength": 64},
                "max_attempts": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "default": 5,
                },
                "backoff_base_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 60000,
                    "default": 1000,
                },
            },
            "additionalProperties": False,
        },
        "PatchWebhook": {
            "type": "object",
            "properties": {
                "active": {"type": "boolean"},
                "events": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 9,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "enum": _WEBHOOK_EVENT_TYPES + ["order.*", "product.*", "*"],
                    },
                },
            },
            "additionalProperties": False,
            "minProperties": 1,
        },
        "Webhook": {
            "type": "object",
            "required": [
                "id",
                "url",
                "events",
                "active",
                "max_attempts",
                "backoff_base_ms",
                "secret_set",
                "created_at",
            ],
            "properties": {
                "id": {"type": "integer", "minimum": 1},
                "url": {"type": "string"},
                "events": {"type": "array", "items": {"type": "string"}},
                "active": {"type": "boolean"},
                "max_attempts": {"type": "integer", "minimum": 1, "maximum": 8},
                "backoff_base_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
                "secret_set": {"type": "boolean", "enum": [True]},
                "created_at": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "WebhookList": {
            "type": "object",
            "required": ["items", "total"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/Webhook"},
                },
                "total": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        "ProcessOutbox": {
            "type": "object",
            "properties": {
                "ignore_schedule": {"type": "boolean", "default": False},
                "max": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
            },
            "additionalProperties": False,
        },
        "Dispatcher": {
            "type": "object",
            "required": ["enabled", "interval_ms"],
            "properties": {
                "enabled": {"type": "boolean"},
                "interval_ms": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 60000,
                },
            },
            "additionalProperties": False,
        },
        "UpdateDispatcher": {
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "default": True},
                "interval_ms": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 60000,
                    "default": 1000,
                },
            },
            "additionalProperties": False,
            "minProperties": 1,
        },
        "OutboxAttempt": {
            "type": "object",
            "required": [
                "n",
                "at",
                "outcome",
                "http_status",
                "timestamp",
                "signature",
            ],
            "properties": {
                "n": {"type": "integer", "minimum": 1},
                "at": {"type": "string"},
                "outcome": {"type": "string"},
                "http_status": {"type": "integer", "nullable": True},
                "timestamp": {"type": "integer", "minimum": 0},
                "signature": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "OutboxEntry": {
            "type": "object",
            "required": [
                "id",
                "webhook_id",
                "event_id",
                "event_type",
                "status",
                "attempts",
                "next_attempt_at",
                "created_at",
                "payload",
            ],
            "properties": {
                "id": {"type": "integer", "minimum": 1},
                "webhook_id": {"type": "integer", "minimum": 1},
                "event_id": {"type": "string"},
                "event_type": {"type": "string", "enum": _WEBHOOK_EVENT_TYPES},
                "status": {
                    "type": "string",
                    "enum": ["pending", "retrying", "delivered", "failed"],
                },
                "attempts": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/OutboxAttempt"},
                },
                "next_attempt_at": {"type": "string", "nullable": True},
                "created_at": {"type": "string"},
                "payload": {"type": "object"},
            },
            "additionalProperties": False,
        },
        "OutboxList": {
            "type": "object",
            "required": ["items", "total", "limit", "offset"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"$ref": "#/components/schemas/OutboxEntry"},
                },
                "total": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        "ProcessOutboxResult": {
            "type": "object",
            "required": ["processed", "delivered", "retrying", "failed"],
            "properties": {
                "processed": {"type": "integer", "minimum": 0},
                "delivered": {"type": "integer", "minimum": 0},
                "retrying": {"type": "integer", "minimum": 0},
                "failed": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
    }
)
