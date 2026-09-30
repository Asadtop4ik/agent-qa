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
            "offset": {"type": "integer", "minimum": MIN_OFFSET},
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
            "offset": {"type": "integer", "minimum": MIN_OFFSET},
            "next_cursor": {"type": "string", "nullable": True},
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
SCHEMAS["ProductList"]["properties"]["items"]["items"] = SCHEMAS["Product"]
SCHEMAS["CategoryList"]["properties"]["items"]["items"] = SCHEMAS["Category"]
