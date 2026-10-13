"""Tests for the bounded idempotency response store."""

import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agent_qa import config, routes
from agent_qa.idempotency import BeginKind, IdempotencyStore, Response
from agent_qa.orders import OrderStore
from agent_qa.products import ProductStore
from agent_qa.server import Handler


def invoke(
    method,
    path,
    payload,
    key,
    store,
    request_id="idempotency-unit",
    product_store=None,
):
    encoded = json.dumps(payload).encode("utf-8")
    response = {}
    handler = object.__new__(Handler)
    handler.command = method
    handler.path = path
    handler.headers = {
        "Content-Length": str(len(encoded)),
        "Content-Type": "application/json",
        "X-API-Key": "unit-api-key",
        "Idempotency-Key": key,
    }
    handler.rfile = io.BytesIO(encoded)
    handler.request_id = request_id
    handler._json = lambda status, body, extra=None: response.update(
        status=status,
        body=body,
        headers={**(extra or {}), "X-Request-Id": request_id},
    )
    with (
        patch("agent_qa.server.is_valid_api_key", return_value=True),
        patch("agent_qa.server.IDEMPOTENCY_STORE", store),
        patch.dict(
            routes.__dict__,
            {
                "ORDER_STORE": OrderStore(),
                "PRODUCT_STORE": product_store or ProductStore(),
            },
        ),
    ):
        handler._handle()
    return response


class IdempotencyStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.store = IdempotencyStore(
            ttl_seconds=10, capacity=2, clock=lambda: self.now
        )
        self.api_store = IdempotencyStore()

    def test_ttl_expiration_allows_key_to_be_claimed_again(self):
        scope = ("api", "POST", "/orders", "key")
        self.assertIs(self.store.begin(scope, "fingerprint").kind, BeginKind.NEW)
        self.store.complete(scope, "fingerprint", Response(201, {"id": 1}, {}))
        self.assertIs(self.store.begin(scope, "fingerprint").kind, BeginKind.REPLAY)
        self.now = 10.0
        self.assertIs(self.store.begin(scope, "fingerprint").kind, BeginKind.NEW)

    def test_least_recently_used_completed_entry_is_evicted(self):
        scopes = [("api", "POST", "/orders", key) for key in "abc"]
        for scope in scopes[:2]:
            self.store.begin(scope, "fp")
            self.store.complete(scope, "fp", Response(201, {}, {}))
        self.assertIs(self.store.begin(scopes[0], "fp").kind, BeginKind.REPLAY)
        self.store.begin(scopes[2], "fp")
        self.store.complete(scopes[2], "fp", Response(201, {}, {}))
        self.assertIs(self.store.begin(scopes[0], "fp").kind, BeginKind.REPLAY)
        self.assertIs(self.store.begin(scopes[1], "fp").kind, BeginKind.NEW)

    def test_mismatch_and_abort_allow_a_later_retry(self):
        scope = ("api", "POST", "/orders", "key")
        self.store.begin(scope, "first")
        self.store.complete(scope, "first", Response(201, {}, {}))
        self.assertIs(self.store.begin(scope, "changed").kind, BeginKind.MISMATCH)
        other_scope = ("api", "POST", "/orders", "other")
        self.store.begin(other_scope, "bad")
        self.store.abort(other_scope, "bad")
        self.assertIs(self.store.begin(other_scope, "fixed").kind, BeginKind.NEW)

    def test_active_duplicate_is_reported_without_waiting(self):
        scope = ("api", "POST", "/orders", "key")
        claimed = threading.Event()
        release = threading.Event()

        def owner():
            self.store.begin(scope, "fp")
            claimed.set()
            release.wait(timeout=2)
            self.store.complete(scope, "fp", Response(201, {}, {}))

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(owner)
            self.assertTrue(claimed.wait(timeout=2))
            self.assertIs(self.store.begin(scope, "fp").kind, BeginKind.IN_PROGRESS)
            release.set()
            pending.result(timeout=2)

    def test_full_store_does_not_grow_when_every_slot_is_active(self):
        first = ("api", "POST", "/orders", "one")
        second = ("api", "POST", "/orders", "two")
        third = ("api", "POST", "/orders", "three")
        self.store.begin(first, "fp")
        self.store.begin(second, "fp")
        self.assertIs(self.store.begin(third, "fp").kind, BeginKind.IN_PROGRESS)

    def test_ttl_configuration_is_bounded(self):
        for value in ("0", "86401", "invalid", "9" * 5000):
            with self.subTest(value=value[:20]), patch.dict(
                "os.environ", {"AGENT_QA_IDEMPOTENCY_TTL_SECONDS": value}
            ):
                with self.assertRaises(ValueError):
                    config.idempotency_ttl_seconds()
        for value in ("1", "86400"):
            with patch.dict("os.environ", {"AGENT_QA_IDEMPOTENCY_TTL_SECONDS": value}):
                self.assertEqual(config.idempotency_ttl_seconds(), int(value))

    def test_invalid_key_is_rejected_before_json_parsing(self):
        response = {}
        handler = object.__new__(Handler)
        handler.command = "POST"
        handler.path = "/orders"
        handler.headers = {
            "Content-Length": "1",
            "Content-Type": "application/json",
            "X-API-Key": "unit-api-key",
            "Idempotency-Key": "bad key",
        }
        handler.rfile = io.BytesIO(b"{")
        handler.request_id = "bad-key-first"
        handler._json = lambda status, body, extra=None: response.update(
            status=status, body=body
        )
        with patch("agent_qa.server.is_valid_api_key", return_value=True):
            handler._handle()
        self.assertEqual(response["body"]["error"]["code"], "invalid_idempotency_key")

    def test_different_invalid_body_mismatches_before_schema_validation(self):
        valid_payload = {"customer_id": "idempotency-mismatch", "total_cents": 100}
        invoke("POST", "/orders", valid_payload, "invalid-body-reuse", self.api_store)
        response = invoke(
            "POST",
            "/orders",
            {"customer_id": "idempotency-mismatch", "total_cents": -1},
            "invalid-body-reuse",
            self.api_store,
        )
        self.assertEqual(response["status"], 422)
        self.assertEqual(response["body"]["error"]["code"], "idempotency_key_reused")

    def test_transport_failure_keeps_successful_result_cached(self):
        payload = {"customer_id": "idempotency-transport", "total_cents": 100}
        store = IdempotencyStore()
        order_store = OrderStore()
        product_store = ProductStore()
        first = object.__new__(Handler)
        body = json.dumps(payload).encode("utf-8")
        first.command = "POST"
        first.path = "/orders"
        first.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-API-Key": "unit-api-key",
            "Idempotency-Key": "transport-key",
        }
        first.rfile = io.BytesIO(body)
        first.request_id = "transport-first"
        first._json = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write"))
        with (
            patch("agent_qa.server.is_valid_api_key", return_value=True),
            patch("agent_qa.server.IDEMPOTENCY_STORE", store),
            patch.dict(
                routes.__dict__,
                {"ORDER_STORE": order_store, "PRODUCT_STORE": product_store},
            ),
        ):
            with self.assertRaises(OSError):
                first._dispatch()

            replay = {}
            second = object.__new__(Handler)
            second.command = "POST"
            second.path = "/orders"
            second.headers = first.headers
            second.rfile = io.BytesIO(body)
            second.request_id = "transport-replay"
            second._json = lambda status, value, extra=None: replay.update(
                status=status,
                body=value,
                headers={**(extra or {}), "X-Request-Id": second.request_id},
            )
            second._dispatch()
        self.assertEqual(replay["status"], 201)
        self.assertEqual(replay["headers"]["Idempotent-Replay"], "true")
        self.assertEqual(order_store.list()[1], 1)

    def test_product_create_replay_preserves_initial_stock(self):
        products_store = ProductStore()
        first = invoke(
            "POST",
            "/products",
            {
                "sku": "IDEMPOTENT-STOCK",
                "name": "Stock item",
                "category": "test",
                "price_cents": 100,
                "stock": 3,
            },
            "stock-key",
            self.api_store,
            product_store=products_store,
        )
        replay = invoke(
            "POST",
            "/products",
            {
                "sku": "IDEMPOTENT-STOCK",
                "name": "Stock item",
                "category": "test",
                "price_cents": 100,
                "stock": 3,
            },
            "stock-key",
            self.api_store,
            request_id="stock-replay-request",
            product_store=products_store,
        )
        self.assertEqual(first["body"], replay["body"])
        self.assertEqual(replay["headers"]["Idempotent-Replay"], "true")
        self.assertEqual(replay["headers"]["Idempotency-Key"], "stock-key")
        self.assertEqual(replay["headers"]["X-Request-Id"], "stock-replay-request")
        self.assertEqual(products_store.list()[0][0]["stock"], 3)


if __name__ == "__main__":
    unittest.main()
