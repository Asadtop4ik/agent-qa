"""Named JSON schemas and limits shared by the API and in-memory stores."""

STATUSES = ("new", "paid", "shipped", "cancelled")
MIN_LIMIT = 1
MAX_LIMIT = 100
DEFAULT_LIMIT = 20
MIN_OFFSET = 0
DEFAULT_OFFSET = 0
MIN_TOTAL_CENTS = 0
MAX_TOTAL_CENTS = 100_000_000
MIN_CUSTOMER_ID_LENGTH = 1
MAX_CUSTOMER_ID_LENGTH = 64
MIN_ORDER_ITEMS = 1
MAX_ORDER_ITEMS = 20
MIN_PRODUCT_ID = 1
MIN_ORDER_QUANTITY = 1
MAX_ORDER_QUANTITY = 1000
MAX_ORDERS = 1000
MAX_PRODUCTS = 500
MAX_NAME_LENGTH = 120
MIN_PRICE_CENTS = 0
MAX_PRICE_CENTS = 100_000_000
MIN_STOCK = 0
MAX_STOCK = 1_000_000
MAX_TAGS = 10
MAX_TAG_LENGTH = 24
MAX_SKU_LENGTH = 32
MAX_CATEGORY_LENGTH = 32
SKU_PATTERN = r"^[A-Z0-9][A-Z0-9-]{1,31}$"
CATEGORY_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
TAG_PATTERN = r"^[a-z0-9-]{1,24}$"
PRODUCT_SORTS = (
    "id",
    "-id",
    "price_cents",
    "-price_cents",
    "name",
    "-name",
    "created_at",
)

_SKU = {
    "type": "string",
    "pattern": SKU_PATTERN,
    "x-fullMatch": True,
    "maxLength": MAX_SKU_LENGTH,
}
_NAME = {
    "type": "string",
    "minLength": 1,
    "maxLength": MAX_NAME_LENGTH,
    "x-nonBlank": True,
}
_CATEGORY = {
    "type": "string",
    "pattern": CATEGORY_PATTERN,
    "x-fullMatch": True,
    "maxLength": MAX_CATEGORY_LENGTH,
}
_PRICE_CENTS = {
    "type": "integer",
    "minimum": MIN_PRICE_CENTS,
    "maximum": MAX_PRICE_CENTS,
}
_STOCK = {"type": "integer", "minimum": MIN_STOCK, "maximum": MAX_STOCK}
_TAGS = {
    "type": "array",
    "minItems": 0,
    "maxItems": MAX_TAGS,
    "uniqueItems": True,
    "items": {
        "type": "string",
        "pattern": TAG_PATTERN,
        "x-fullMatch": True,
        "maxLength": MAX_TAG_LENGTH,
    },
}
_ACTIVE = {"type": "boolean"}
_PRODUCT_SCHEMA = {
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
        "name": _NAME,
        "category": _CATEGORY,
        "price_cents": _PRICE_CENTS,
        "stock": _STOCK,
        "tags": _TAGS,
        "active": _ACTIVE,
        "created_at": {"type": "string", "format": "date-time"},
        "updated_at": {"type": "string", "format": "date-time"},
    },
    "additionalProperties": False,
}

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
        "product_id": {"type": "integer", "minimum": MIN_PRODUCT_ID},
        "sku": _SKU,
        "name": _NAME,
        "quantity": {
            "type": "integer",
            "minimum": MIN_ORDER_QUANTITY,
            "maximum": MAX_ORDER_QUANTITY,
        },
        "unit_price_cents": _PRICE_CENTS,
        "line_total_cents": _TOTAL_CENTS,
    },
    "additionalProperties": False,
}
_ORDER_SCHEMA = {
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
        "items": {
            "type": "array",
            "maxItems": MAX_ORDER_ITEMS,
            "items": _ORDER_ITEM,
        },
    },
    "additionalProperties": False,
}

SCHEMAS = {
    "CreateOrder": {
        "type": "object",
        "required": ["customer_id"],
        "x-exactlyOne": {
            "fields": ["items", "total_cents"],
            "errorField": "items",
            "bothMessage": "Cannot be combined with total_cents",
            "neitherMessage": "Either items or total_cents is required",
        },
        "properties": {
            "customer_id": _CUSTOMER_ID,
            "total_cents": _TOTAL_CENTS,
            "items": {
                "type": "array",
                "minItems": MIN_ORDER_ITEMS,
                "maxItems": MAX_ORDER_ITEMS,
                "items": {
                    "type": "object",
                    "required": ["product_id", "quantity"],
                    "properties": {
                        "product_id": {
                            "type": "integer",
                            "minimum": MIN_PRODUCT_ID,
                        },
                        "quantity": {
                            "type": "integer",
                            "minimum": MIN_ORDER_QUANTITY,
                            "maximum": MAX_ORDER_QUANTITY,
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
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
            "name": _NAME,
            "category": _CATEGORY,
            "price_cents": _PRICE_CENTS,
            "stock": {**_STOCK, "default": 0},
            "tags": {**_TAGS, "default": []},
            "active": {**_ACTIVE, "default": True},
        },
        "additionalProperties": False,
    },
    "UpdateProduct": {
        "type": "object",
        "x-immutableFields": ["sku"],
        "properties": {
            "name": _NAME,
            "category": _CATEGORY,
            "price_cents": _PRICE_CENTS,
            "stock": _STOCK,
            "tags": _TAGS,
            "active": _ACTIVE,
        },
        "minProperties": 1,
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
                "not": {"enum": [0]},
            }
        },
        "additionalProperties": False,
    },
    "Product": _PRODUCT_SCHEMA,
    "ProductList": {
        "type": "object",
        "required": ["items", "total", "limit", "offset"],
        "properties": {
            "items": {"type": "array", "items": _PRODUCT_SCHEMA},
            "total": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": MIN_LIMIT, "maximum": MAX_LIMIT},
            "offset": {"type": "integer", "minimum": MIN_OFFSET},
        },
        "additionalProperties": False,
    },
    "CategoryList": {
        "type": "object",
        "required": ["items", "total"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
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
            },
            "total": {"type": "integer", "minimum": 0},
        },
        "additionalProperties": False,
    },
    "Order": _ORDER_SCHEMA,
    "OrderList": {
        "type": "object",
        "required": ["items", "total", "limit", "offset"],
        "properties": {
            "items": {
                "type": "array",
                "items": _ORDER_SCHEMA,
            },
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
                "required": ["code", "message"],
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
