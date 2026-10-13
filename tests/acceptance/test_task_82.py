"""Acceptance tests for order items and inventory reservations."""

import io
import json
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agent_qa import orders, products
from agent_qa.openapi import build_openapi
from agent_qa.routes import ROUTES
from agent_qa.schemas import MAX_TOTAL_CENTS, SCHEMAS
from agent_qa.server import Handler


API_KEY = "qa-synthetic-key"


def invoke(method, path, payload=None, api_key=API_KEY):
    encoded = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Length": str(len(encoded)), "Content-Type": "application/json"}
    if api_key is not None:
        headers["X-API-Key"] = api_key
    response = {}
    handler = object.__new__(Handler)
    handler.command = method
    handler.path = path
    handler.headers = headers
    handler.rfile = io.BytesIO(encoded)
    handler.request_id = "acceptance-test"
    handler._json = lambda status, body, extra=None: response.update(
        status=status, body=body, headers=extra or {}
    )
    handler._handle()
    return response


def token():
    return uuid.uuid4().hex[:12].upper()


def create_product(
    test_case, *, name="Snapshot product", price=125, stock=10, active=True
):
    sku = "T-" + token()
    response = invoke(
        "POST",
        "/products",
        {
            "sku": sku,
            "name": name,
            "category": "acceptance",
            "price_cents": price,
            "stock": stock,
            "active": active,
        },
    )
    test_case.assertEqual(response["status"], 201)
    return response["body"]


class Task82AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.order_store_patch = patch(
            "agent_qa.routes.ORDER_STORE", orders.OrderStore()
        )
        self.product_store_patch = patch(
            "agent_qa.routes.PRODUCT_STORE", products.ProductStore()
        )
        self.auth_patch = patch(
            "agent_qa.server.is_valid_api_key",
            side_effect=lambda headers: headers.get("X-API-Key") == API_KEY,
        )
        self.order_store_patch.start()
        self.product_store_patch.start()
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.addCleanup(self.product_store_patch.stop)
        self.addCleanup(self.order_store_patch.stop)

    def api_error(self, response, status, code):
        self.assertEqual(response["status"], status)
        self.assertEqual(response["body"]["error"]["code"], code)
        return response["body"]["error"]

    def create_order(self, payload):
        return invoke("POST", "/orders", payload)

    def stock(self, product_id):
        return invoke("GET", f"/products/{product_id}")["body"]["stock"]

    def test_ac1_create_accepts_legacy_and_item_shapes_and_exactly_one(self):
        product = create_product(self)
        legacy = self.create_order({"customer_id": "legacy", "total_cents": 10})
        self.assertEqual(legacy["status"], 201)
        self.assertEqual(legacy["body"]["items"], [])
        current = self.create_order(
            {
                "customer_id": "items",
                "items": [{"product_id": product["id"], "quantity": 1}],
            }
        )
        self.assertEqual(current["status"], 201)

        both = self.api_error(
            self.create_order(
                {
                    "customer_id": "both",
                    "total_cents": 1,
                    "items": [{"product_id": product["id"], "quantity": 1}],
                }
            ),
            400,
            "validation_error",
        )
        self.assertEqual(
            both["details"],
            [{"field": "items", "message": "Cannot be combined with total_cents"}],
        )
        neither = self.api_error(
            self.create_order({"customer_id": "neither"}),
            400,
            "validation_error",
        )
        self.assertEqual(
            neither["details"],
            [{"field": "items", "message": "Either items or total_cents is required"}],
        )

        for items in ([], [{}] * 21):
            with self.subTest(item_count=len(items)):
                error = self.api_error(
                    self.create_order({"customer_id": "bounds", "items": items}),
                    400,
                    "validation_error",
                )
                self.assertIn("items", [detail["field"] for detail in error["details"]])
        for quantity in (0, 1001, True):
            with self.subTest(quantity=quantity):
                error = self.api_error(
                    self.create_order(
                        {
                            "customer_id": "quantity",
                            "items": [
                                {"product_id": product["id"], "quantity": quantity}
                            ],
                        }
                    ),
                    400,
                    "validation_error",
                )
                self.assertIn(
                    "items[0].quantity",
                    [detail["field"] for detail in error["details"]],
                )

    def test_ac2_order_items_keep_price_name_snapshot_and_sum(self):
        first = create_product(self, name="First snapshot", price=125, stock=5)
        second = create_product(self, name="Second snapshot", price=40, stock=5)
        response = self.create_order(
            {
                "customer_id": "snapshot",
                "items": [
                    {"product_id": first["id"], "quantity": 2},
                    {"product_id": second["id"], "quantity": 3},
                ],
            }
        )
        self.assertEqual(response["status"], 201)
        self.assertEqual(response["body"]["total_cents"], 370)
        self.assertEqual(
            response["body"]["items"],
            [
                {
                    "product_id": first["id"],
                    "sku": first["sku"],
                    "name": "First snapshot",
                    "quantity": 2,
                    "unit_price_cents": 125,
                    "line_total_cents": 250,
                },
                {
                    "product_id": second["id"],
                    "sku": second["sku"],
                    "name": "Second snapshot",
                    "quantity": 3,
                    "unit_price_cents": 40,
                    "line_total_cents": 120,
                },
            ],
        )
        invoke(
            "PATCH",
            f"/products/{first['id']}",
            {"name": "Changed", "price_cents": 999},
        )
        fetched = invoke("GET", f"/orders/{response['body']['id']}")
        self.assertEqual(fetched["body"]["items"], response["body"]["items"])
        self.assertEqual(fetched["body"]["total_cents"], 370)

        costly = create_product(self, price=MAX_TOTAL_CENTS, stock=2)
        overflow = self.api_error(
            self.create_order(
                {
                    "customer_id": "overflow",
                    "items": [{"product_id": costly["id"], "quantity": 2}],
                }
            ),
            400,
            "validation_error",
        )
        self.assertIn(
            {"field": "items", "message": "Order total exceeds maximum"},
            overflow["details"],
        )
        self.assertEqual(self.stock(costly["id"]), 2)

    def test_ac2_openapi_order_schemas_include_item_fields_and_limits(self):
        create_schema = SCHEMAS["CreateOrder"]
        item_schema = create_schema["properties"]["items"]["items"]
        self.assertEqual(create_schema["properties"]["items"]["minItems"], 1)
        self.assertEqual(create_schema["properties"]["items"]["maxItems"], 20)
        self.assertEqual(item_schema["properties"]["product_id"]["minimum"], 1)
        self.assertEqual(item_schema["properties"]["quantity"]["minimum"], 1)
        self.assertEqual(item_schema["properties"]["quantity"]["maximum"], 1000)
        order_schema = SCHEMAS["Order"]
        for name in (
            "product_id",
            "sku",
            "name",
            "quantity",
            "unit_price_cents",
            "line_total_cents",
        ):
            self.assertIn(
                name, order_schema["properties"]["items"]["items"]["properties"]
            )
        spec = build_openapi(ROUTES, "acceptance-sha")
        self.assertEqual(
            spec["paths"]["/orders"]["post"]["requestBody"]["content"][
                "application/json"
            ]["schema"],
            create_schema,
        )
        self.assertEqual(
            spec["components"]["schemas"]["Order"],
            order_schema,
        )

    def test_ac3_product_and_stock_errors_have_exact_details_without_side_effects(self):
        product = create_product(self, stock=2)
        unknown = self.api_error(
            self.create_order(
                {
                    "customer_id": "unknown",
                    "items": [{"product_id": 999, "quantity": 1}],
                }
            ),
            400,
            "validation_error",
        )
        self.assertEqual(
            unknown["details"],
            [{"field": "items[0].product_id", "message": "Unknown product"}],
        )
        duplicate = self.api_error(
            self.create_order(
                {
                    "customer_id": "duplicate",
                    "items": [
                        {"product_id": product["id"], "quantity": 1},
                        {"product_id": product["id"], "quantity": 1},
                    ],
                }
            ),
            400,
            "validation_error",
        )
        self.assertEqual(
            duplicate["details"],
            [{"field": "items[1].product_id", "message": "Duplicate product"}],
        )
        inactive = create_product(self, stock=2, active=False)
        unavailable = self.api_error(
            self.create_order(
                {
                    "customer_id": "inactive",
                    "items": [{"product_id": inactive["id"], "quantity": 1}],
                }
            ),
            409,
            "product_unavailable",
        )
        self.assertEqual(unavailable["details"][0]["field"], "items[0].product_id")

        other = create_product(self, stock=0)
        insufficient = self.api_error(
            self.create_order(
                {
                    "customer_id": "short",
                    "items": [
                        {"product_id": product["id"], "quantity": 3},
                        {"product_id": other["id"], "quantity": 1},
                    ],
                }
            ),
            409,
            "insufficient_stock",
        )
        self.assertEqual(
            insufficient["details"],
            [
                {"field": "items[0].quantity", "message": "Only 2 in stock"},
                {"field": "items[1].quantity", "message": "Only 0 in stock"},
            ],
        )
        self.assertEqual(self.stock(product["id"]), 2)
        self.assertEqual(self.stock(other["id"]), 0)

    def test_ac3_schema_validation_precedes_product_lookup(self):
        error = self.api_error(
            self.create_order(
                {
                    "customer_id": "precedence",
                    "items": [{"product_id": 987654, "quantity": 0}],
                }
            ),
            400,
            "validation_error",
        )
        self.assertIn(
            "items[0].quantity", [detail["field"] for detail in error["details"]]
        )

    def test_ac4_stock_is_reserved_cancelled_and_deleted_once(self):
        product = create_product(self, stock=4)
        created = self.create_order(
            {
                "customer_id": "cancel",
                "items": [{"product_id": product["id"], "quantity": 1}],
            }
        )
        self.assertEqual(self.stock(product["id"]), 3)
        cancelled = invoke(
            "PATCH", f"/orders/{created['body']['id']}", {"status": "cancelled"}
        )
        self.assertEqual(cancelled["status"], 200)
        self.assertEqual(self.stock(product["id"]), 4)
        self.assertEqual(
            invoke("DELETE", f"/orders/{created['body']['id']}")["status"], 204
        )
        self.assertEqual(self.stock(product["id"]), 4)

        deleted = self.create_order(
            {
                "customer_id": "delete",
                "items": [{"product_id": product["id"], "quantity": 1}],
            }
        )
        self.assertEqual(self.stock(product["id"]), 3)
        self.assertEqual(
            invoke("DELETE", f"/orders/{deleted['body']['id']}")["status"], 204
        )
        self.assertEqual(self.stock(product["id"]), 4)

        shipped = self.create_order(
            {
                "customer_id": "shipped",
                "items": [{"product_id": product["id"], "quantity": 1}],
            }
        )
        self.assertEqual(
            invoke("PATCH", f"/orders/{shipped['body']['id']}", {"status": "paid"})[
                "body"
            ]["status"],
            "paid",
        )
        self.assertEqual(
            invoke("PATCH", f"/orders/{shipped['body']['id']}", {"status": "shipped"})[
                "body"
            ]["status"],
            "shipped",
        )
        self.assertEqual(self.stock(product["id"]), 3)
        self.assertEqual(
            invoke("DELETE", f"/orders/{shipped['body']['id']}")["status"], 204
        )
        self.assertEqual(self.stock(product["id"]), 3)

    def test_ac4_item_total_patch_is_computed_and_legacy_total_still_updates(self):
        product = create_product(self)
        item_order = self.create_order(
            {
                "customer_id": "computed",
                "items": [{"product_id": product["id"], "quantity": 1}],
            }
        )["body"]
        self.api_error(
            invoke("PATCH", f"/orders/{item_order['id']}", {"total_cents": 200}),
            409,
            "total_computed",
        )
        legacy = self.create_order({"customer_id": "legacy-patch", "total_cents": 10})[
            "body"
        ]
        changed = invoke("PATCH", f"/orders/{legacy['id']}", {"total_cents": 20})
        self.assertEqual(changed["status"], 200)
        self.assertEqual(changed["body"]["total_cents"], 20)

    def test_ac5_store_full_rolls_back_stock_and_does_not_consume_id(self):
        store = orders.OrderStore(capacity=1)
        with patch("agent_qa.routes.ORDER_STORE", store):
            first = self.create_order({"customer_id": "fills", "total_cents": 1})
            product = create_product(self, stock=3)
            failed = self.api_error(
                self.create_order(
                    {
                        "customer_id": "full",
                        "items": [{"product_id": product["id"], "quantity": 1}],
                    }
                ),
                409,
                "store_full",
            )
            self.assertEqual(failed["code"], "store_full")
            self.assertEqual(self.stock(product["id"]), 3)
            self.assertEqual(
                invoke("DELETE", f"/orders/{first['body']['id']}")["status"], 204
            )
            next_order = self.create_order(
                {
                    "customer_id": "after-full",
                    "items": [{"product_id": product["id"], "quantity": 1}],
                }
            )
            self.assertEqual(next_order["status"], 201)
            self.assertEqual(next_order["body"]["id"], first["body"]["id"] + 1)

    def test_ac5_race_reserves_exactly_ten_units_for_thirty_requests(self):
        product = create_product(self, stock=10)
        barrier = threading.Barrier(30)

        def create_one(index):
            barrier.wait()
            return self.create_order(
                {
                    "customer_id": f"race-{index}",
                    "items": [{"product_id": product["id"], "quantity": 1}],
                }
            )["status"]

        with ThreadPoolExecutor(max_workers=30) as executor:
            statuses = list(executor.map(create_one, range(30)))
        self.assertEqual(statuses.count(201), 10)
        self.assertEqual(statuses.count(409), 20)
        self.assertEqual(self.stock(product["id"]), 0)


if __name__ == "__main__":
    unittest.main()
