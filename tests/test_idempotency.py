"""Unit and socket-free API tests for idempotency behavior."""

import json
import threading
import unittest
from email.message import Message
from io import BytesIO
from unittest.mock import patch

from agent_qa import config
from agent_qa.idempotency import IdempotencyStore, StoredResponse
from agent_qa.metrics import REGISTRY
from agent_qa.orders import OrderStore
from agent_qa.products import ProductStore
from agent_qa.server import Handler


class IdempotencyStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.store = IdempotencyStore(ttl_seconds=10, capacity=2, clock=self.clock)

    def clock(self):
        return self.now

    def test_ttl_configuration_is_bounded(self):
        with patch.dict("os.environ", {"AGENT_QA_IDEMPOTENCY_TTL_SECONDS": "1"}):
            self.assertEqual(config.idempotency_ttl_seconds(), 1)
        with patch.dict("os.environ", {"AGENT_QA_IDEMPOTENCY_TTL_SECONDS": "86400"}):
            self.assertEqual(config.idempotency_ttl_seconds(), 86400)
        for value in ("0", "86401", "not-a-number"):
            with self.subTest(value=value):
                with patch.dict(
                    "os.environ", {"AGENT_QA_IDEMPOTENCY_TTL_SECONDS": value}
                ):
                    with self.assertRaises(ValueError):
                        config.idempotency_ttl_seconds()

    def test_ttl_expires_saved_responses(self):
        scope = ("fingerprint", "POST", "/orders", "key")
        self.assertEqual(self.store.begin(scope, "body").kind, "new")
        response = StoredResponse(201, {"id": 1}, {"Location": "/orders/1"})
        self.store.complete(scope, response)
        self.assertEqual(self.store.begin(scope, "body").kind, "replay")

        self.now += 10
        self.assertEqual(self.store.begin(scope, "body").kind, "new")

    def test_lru_eviction_preserves_recently_used_response(self):
        first, second, third = (("key", str(value)) for value in range(3))
        for scope in (first, second):
            self.assertEqual(self.store.begin(scope, "body").kind, "new")
            self.store.complete(scope, StoredResponse(201, {}, {}))

        self.assertEqual(self.store.begin(first, "body").kind, "replay")
        self.assertEqual(self.store.begin(third, "body").kind, "new")
        self.store.complete(third, StoredResponse(201, {}, {}))
        self.assertEqual(self.store.begin(first, "body").kind, "replay")
        self.assertEqual(self.store.begin(second, "body").kind, "new")

    def test_mismatched_fingerprint_does_not_replace_saved_response(self):
        scope = ("key", "scope")
        self.store.begin(scope, "first")
        response = StoredResponse(201, {"id": 1}, {})
        self.store.complete(scope, response)

        self.assertEqual(self.store.begin(scope, "other").kind, "mismatch")
        replay = self.store.begin(scope, "first")
        self.assertEqual(replay.kind, "replay")
        self.assertEqual(replay.response, response)

    def test_parallel_reservation_is_in_progress(self):
        scope = ("key", "scope")
        self.store.begin(scope, "body")
        barrier = threading.Barrier(2)
        result = []

        def begin_again():
            barrier.wait(timeout=2)
            result.append(self.store.begin(scope, "body").kind)

        worker = threading.Thread(target=begin_again)
        worker.start()
        barrier.wait(timeout=2)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["in_progress"])

    def test_abort_allows_key_to_be_reused(self):
        scope = ("key", "scope")
        self.assertEqual(self.store.begin(scope, "body").kind, "new")
        self.store.abort(scope)
        self.assertEqual(self.store.begin(scope, "body").kind, "new")

    def test_saved_and_replayed_responses_are_isolated_snapshots(self):
        scope = ("key", "scope")
        self.store.begin(scope, "body")
        body = {"nested": {"value": 1}}
        headers = {"Location": "/orders/1"}
        self.store.complete(scope, StoredResponse(201, body, headers))
        body["nested"]["value"] = 2
        headers["Location"] = "/orders/2"

        replay = self.store.begin(scope, "body")
        replay.response.body["nested"]["value"] = 3
        replay.response.headers["Location"] = "/orders/3"
        next_replay = self.store.begin(scope, "body")
        self.assertEqual(next_replay.response.body, {"nested": {"value": 1}})
        self.assertEqual(next_replay.response.headers, {"Location": "/orders/1"})

    def test_active_reservations_are_not_evicted_at_capacity(self):
        store = IdempotencyStore(ttl_seconds=10, capacity=2, clock=self.clock)
        first, second, third = (("key", str(value)) for value in range(3))
        store.begin(first, "body")
        store.begin(second, "body")

        self.assertEqual(store.begin(third, "body").kind, "in_progress")
        self.assertEqual(store.begin(first, "body").kind, "in_progress")
        self.assertEqual(len(store._entries), 2)

        store.abort(first)
        self.assertEqual(store.begin(third, "body").kind, "new")


class IdempotencyApiTests(unittest.TestCase):
    def setUp(self):
        self.store = IdempotencyStore()
        self.store_patch = patch("agent_qa.server.IDEMPOTENCY_STORE", self.store)
        self.store_patch.start()
        self.orders = OrderStore()
        self.products = ProductStore()
        self.orders_patch = patch("agent_qa.routes.ORDER_STORE", self.orders)
        self.products_patch = patch("agent_qa.routes.PRODUCT_STORE", self.products)
        self.orders_patch.start()
        self.products_patch.start()
        self.auth_patch = patch("agent_qa.server.is_valid_api_key", return_value=True)
        self.auth_patch.start()

    def tearDown(self):
        self.auth_patch.stop()
        self.products_patch.stop()
        self.orders_patch.stop()
        self.store_patch.stop()

    @staticmethod
    def dispatch(
        method,
        path,
        payload,
        *,
        key="retry-key",
        api_key="scope-a",
        request_id="api-request",
        raw_body=None,
        fail_output=False,
        extra_idempotency_keys=(),
    ):
        raw = raw_body if raw_body is not None else json.dumps(payload).encode("utf-8")
        headers = Message()
        headers["Content-Length"] = str(len(raw))
        headers["Content-Type"] = "application/json"
        headers["X-API-Key"] = api_key
        if key is not None:
            headers["Idempotency-Key"] = key
        for extra_key in extra_idempotency_keys:
            headers.add_header("Idempotency-Key", extra_key)
        responses = []
        handler = object.__new__(Handler)
        handler.path = path
        handler.command = method
        handler.headers = headers
        handler.rfile = BytesIO(raw)
        handler.request_id = request_id

        def capture(status, body, response_headers=None):
            nonlocal fail_output
            if fail_output:
                fail_output = False
                raise OSError("simulated client disconnect")
            result_headers = dict(response_headers or {})
            result_headers["X-Request-Id"] = handler.request_id
            responses.append((status, body, result_headers))

        handler._json = capture
        with patch("agent_qa.server.LOGGER.exception"):
            Handler._handle(handler)
        return responses[0]

    @staticmethod
    def order_payload(customer="idempotency-customer"):
        return {"customer_id": customer, "total_cents": 1500}

    def test_order_replay_returns_saved_response_without_duplicate_creation(self):
        payload = self.order_payload()
        first = self.dispatch("POST", "/orders", payload)
        second = self.dispatch("POST", "/orders", payload, request_id="replay-request")

        self.assertEqual(first[0], 201)
        self.assertEqual(second[0], 201)
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[2]["Location"], second[2]["Location"])
        self.assertEqual(first[2]["Idempotency-Key"], "retry-key")
        self.assertEqual(second[2]["Idempotency-Key"], "retry-key")
        self.assertEqual(second[2]["Idempotent-Replay"], "true")
        self.assertNotIn("Idempotent-Replay", first[2])
        self.assertEqual(first[2]["X-Request-Id"], "api-request")
        self.assertEqual(second[2]["X-Request-Id"], "replay-request")
        self.assertEqual(self.orders.list()[1], 1)

    def test_canonical_object_key_order_replays_same_order(self):
        first_payload = {"customer_id": "canonical", "total_cents": 1200}
        reordered_payload = {"total_cents": 1200, "customer_id": "canonical"}

        first = self.dispatch("POST", "/orders", first_payload)
        replay = self.dispatch("POST", "/orders", reordered_payload)

        self.assertEqual(replay[0], 201)
        self.assertEqual(replay[1], first[1])
        self.assertEqual(replay[2]["Idempotent-Replay"], "true")
        self.assertEqual(self.orders.list()[1], 1)

    def test_product_replay_creates_one_product(self):
        payload = {
            "sku": "PRODUCT-REPLAY",
            "name": "Widget",
            "category": "tools",
            "price_cents": 100,
            "stock": 1,
        }

        first = self.dispatch("POST", "/products", payload)
        replay = self.dispatch("POST", "/products", payload)

        self.assertEqual(first[0], 201)
        self.assertEqual(replay[0], 201)
        self.assertEqual(replay[1], first[1])
        self.assertEqual(self.products.list()[1], 1)

    def test_schema_invalid_mismatch_precedes_validation_and_fresh_key_retries(self):
        valid = {"customer_id": "schema-order", "total_cents": 100}
        invalid = {"customer_id": "schema-order", "total_cents": True}
        self.dispatch("POST", "/orders", valid)

        mismatch = self.dispatch("POST", "/orders", invalid)
        fresh_invalid = self.dispatch("POST", "/orders", invalid, key="fresh-key")
        retried = self.dispatch("POST", "/orders", valid, key="fresh-key")

        self.assertEqual(mismatch[0], 422)
        self.assertEqual(mismatch[1]["error"]["code"], "idempotency_key_reused")
        self.assertEqual(fresh_invalid[0], 400)
        self.assertEqual(fresh_invalid[1]["error"]["code"], "validation_error")
        self.assertEqual(retried[0], 201)
        self.assertEqual(self.orders.list()[1], 2)

    def test_item_order_replay_reserves_product_stock_once(self):
        product = self.products.create(
            sku="ITEM-1",
            name="Widget",
            category="tools",
            price_cents=123,
            stock=5,
        )
        payload = {
            "customer_id": "item-order-customer",
            "items": [{"product_id": product["id"], "quantity": 2}],
        }

        first = self.dispatch("POST", "/orders", payload)
        replay = self.dispatch("POST", "/orders", payload)

        self.assertEqual(first[0], 201)
        self.assertEqual(replay[0], 201)
        self.assertEqual(first[1], replay[1])
        self.assertEqual(self.products.get(product["id"])["stock"], 3)
        self.assertEqual(self.orders.list()[1], 1)

    def test_mismatch_invalid_key_auth_and_distinct_path(self):
        payload = self.order_payload()
        self.dispatch("POST", "/orders", payload)
        mismatch = self.dispatch(
            "POST", "/orders", self.order_payload("different-customer")
        )
        self.assertEqual(mismatch[0], 422)
        self.assertEqual(mismatch[1]["error"]["code"], "idempotency_key_reused")

        invalid = self.dispatch("POST", "/orders", payload, key="bad key")
        self.assertEqual(invalid[0], 400)
        self.assertEqual(invalid[1]["error"]["code"], "invalid_idempotency_key")
        missing = self.dispatch("POST", "/orders", payload, key=None)
        self.assertEqual(missing[0], 201)

        with patch("agent_qa.server.is_valid_api_key", return_value=False):
            unauthorized = self.dispatch("POST", "/orders", payload, key="bad key")
        self.assertEqual(unauthorized[0], 401)
        self.assertEqual(unauthorized[1]["error"]["code"], "unauthorized")

        product_payload = {
            "sku": "DIFFERENT-PATH",
            "name": "Widget",
            "category": "tools",
            "price_cents": 1,
            "stock": 1,
        }
        different_path = self.dispatch("POST", "/products", product_payload)
        self.assertEqual(different_path[0], 201)
        different_api_key = self.dispatch("POST", "/orders", payload, api_key="scope-b")
        self.assertEqual(different_api_key[0], 201)
        self.assertEqual(self.orders.list()[1], 3)

    def test_metrics_record_store_replay_and_mismatch_outcomes(self):
        payload = self.order_payload("metrics-customer")
        with patch.object(
            REGISTRY,
            "record_idempotency",
            wraps=REGISTRY.record_idempotency,
        ) as metric:
            self.dispatch("POST", "/orders", payload)
            self.dispatch("POST", "/orders", payload)
            self.dispatch("POST", "/orders", self.order_payload("other"))

        self.assertEqual(
            [call.args[0] for call in metric.call_args_list],
            ["stored", "replayed", "mismatch"],
        )

    def test_non_success_response_releases_reservation_and_deep_json_is_rejected(self):
        attempts = []

        def sometimes_fails(query, path_params, payload):
            attempts.append(payload)
            if len(attempts) == 1:
                return 500, {"error": "temporary"}, {}
            return 201, {"created": True}, {"Location": "/synthetic/1"}

        route = {
            "method": "POST",
            "path": "/synthetic",
            "handler": sometimes_fails,
            "body": True,
            "auth_required": True,
            "idempotent": True,
        }
        with patch("agent_qa.server.ROUTES", (route,)):
            first = self.dispatch("POST", "/synthetic", {"value": 1})
            second = self.dispatch("POST", "/synthetic", {"value": 1})
        self.assertEqual(first[0], 500)
        self.assertEqual(second[0], 201)
        self.assertEqual(len(attempts), 2)

        nested = 0
        for _ in range(34):
            nested = [nested]
        rejected = self.dispatch("POST", "/orders", {"nested": nested})
        self.assertEqual(rejected[0], 400)
        self.assertEqual(rejected[1]["error"]["code"], "invalid_json")

        malformed = self.dispatch("POST", "/orders", None, raw_body=b"{bad")
        self.assertEqual(malformed[0], 400)
        self.assertEqual(malformed[1]["error"]["code"], "invalid_json")
        huge_integer = self.dispatch(
            "POST", "/orders", {"customer_id": "large", "total_cents": 10**1000}
        )
        self.assertEqual(huge_integer[0], 400)

    def test_invalid_key_boundaries_and_duplicate_headers(self):
        payload = self.order_payload("key-boundaries")
        for key in ("a", "x" * 64):
            with self.subTest(key_length=len(key)):
                response = self.dispatch("POST", "/orders", payload, key=key)
                self.assertEqual(response[0], 201)
        for key in ("", "x" * 65, "bad key"):
            with self.subTest(key_length=len(key)):
                response = self.dispatch("POST", "/orders", payload, key=key)
                self.assertEqual(response[0], 400)
                self.assertEqual(
                    response[1]["error"]["code"], "invalid_idempotency_key"
                )
        duplicate = self.dispatch(
            "POST",
            "/orders",
            payload,
            key=None,
            extra_idempotency_keys=("first", "second"),
        )
        self.assertEqual(duplicate[0], 400)
        self.assertEqual(duplicate[1]["error"]["code"], "invalid_idempotency_key")

    def test_idempotency_flag_does_not_apply_to_non_post_methods(self):
        attempts = []

        def create(query, path_params, payload):
            attempts.append(payload)
            return 200, {"created": len(attempts)}, {}

        route = {
            "method": "PUT",
            "path": "/flagged-put",
            "handler": create,
            "body": True,
            "auth_required": True,
            "idempotent": True,
        }
        with patch("agent_qa.server.ROUTES", (route,)):
            first = self.dispatch("PUT", "/flagged-put", {"value": 1})
            second = self.dispatch("PUT", "/flagged-put", {"value": 1})
        self.assertEqual(first[1], {"created": 1})
        self.assertEqual(second[1], {"created": 2})
        self.assertEqual(len(attempts), 2)

    def test_json_writer_suppresses_handler_request_id_header(self):
        handler = object.__new__(Handler)
        handler.command = "POST"
        handler.path = "/orders"
        handler.request_id = "current-request"
        handler.request_version = "HTTP/1.1"
        handler._headers_buffer = []
        handler.wfile = BytesIO()
        handler.log_request = lambda *args: None
        handler._record_response = lambda status: None

        Handler._json(
            handler,
            201,
            {"created": True},
            {"Location": "/orders/1", "x-request-id": "stale-request"},
        )

        response = handler.wfile.getvalue().decode("iso-8859-1")
        header_lines = response.split("\r\n\r\n", 1)[0].split("\r\n")
        request_id_headers = [
            line for line in header_lines if line.lower().startswith("x-request-id:")
        ]
        self.assertEqual(request_id_headers, ["X-Request-Id: current-request"])

    def test_raised_handler_errors_release_reservation(self):
        attempts = []

        def raises_before_success(query, path_params, payload):
            attempts.append(payload)
            if len(attempts) == 1:
                from agent_qa.errors import ApiError

                raise ApiError(409, "temporary_conflict", "Try again")
            if len(attempts) == 2:
                raise RuntimeError("unexpected test failure")
            return 201, {"created": True}, {}

        route = {
            "method": "POST",
            "path": "/raises",
            "handler": raises_before_success,
            "body": True,
            "auth_required": True,
            "idempotent": True,
        }
        with patch("agent_qa.server.ROUTES", (route,)):
            with patch("agent_qa.server.LOGGER.exception"):
                responses = [
                    self.dispatch("POST", "/raises", {"value": 1}) for _ in range(3)
                ]
        self.assertEqual([response[0] for response in responses], [409, 500, 201])
        self.assertEqual(len(attempts), 3)

    def test_disconnect_after_success_keeps_response_for_replay(self):
        payload = self.order_payload("disconnect-customer")
        first = self.dispatch("POST", "/orders", payload, fail_output=True)
        replay = self.dispatch("POST", "/orders", payload)

        self.assertEqual(first[0], 500)
        self.assertEqual(replay[0], 201)
        self.assertEqual(self.orders.list()[1], 1)

    def test_http_parallel_request_gets_in_progress(self):
        entered = threading.Event()
        release = threading.Event()
        attempts = []

        def blocking_create(query, path_params, payload):
            attempts.append(payload)
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return 201, {"created": True}, {}

        route = {
            "method": "POST",
            "path": "/blocking",
            "handler": blocking_create,
            "body": True,
            "auth_required": True,
            "idempotent": True,
        }
        first_response = []
        with patch("agent_qa.server.ROUTES", (route,)):
            with patch.object(
                REGISTRY,
                "record_idempotency",
                wraps=REGISTRY.record_idempotency,
            ) as metric:
                first = threading.Thread(
                    target=lambda: first_response.append(
                        self.dispatch("POST", "/blocking", {"value": 1})
                    )
                )
                first.start()
                self.assertTrue(entered.wait(timeout=2))
                parallel = self.dispatch("POST", "/blocking", {"value": 1})
                release.set()
                first.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertEqual(parallel[0], 409)
        self.assertEqual(parallel[1]["error"]["code"], "idempotency_in_progress")
        self.assertEqual(first_response[0][0], 201)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(
            [call.args[0] for call in metric.call_args_list],
            ["in_progress", "stored"],
        )

    def test_twenty_concurrent_requests_create_one_order(self):
        count = 20
        start = threading.Barrier(count)
        results = []
        lock = threading.Lock()
        payload = self.order_payload("concurrent-customer")

        def send():
            start.wait(timeout=2)
            response = self.dispatch("POST", "/orders", payload)
            with lock:
                results.append(response)

        threads = [threading.Thread(target=send) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=4)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(self.orders.list()[1], 1)
        self.assertEqual(len(results), count)
        self.assertTrue(all(result[0] in {201, 409} for result in results))


if __name__ == "__main__":
    unittest.main()
