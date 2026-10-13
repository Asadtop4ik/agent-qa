"""Acceptance tests for idempotent POST requests."""

import io
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agent_qa import orders, products, routes
from agent_qa.config import idempotency_ttl_seconds
from agent_qa.idempotency import BeginKind, IdempotencyStore, Response
from agent_qa.metrics import MetricsRegistry
from agent_qa.openapi import build_openapi
from agent_qa.routes import ROUTES
from agent_qa.server import Handler


API_KEY = "qa-synthetic-key"


def invoke(
    method,
    path,
    payload=None,
    headers=None,
    api_key=API_KEY,
    request_id="acceptance-test",
):
    encoded = b"" if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {
        "Content-Length": str(len(encoded)),
        "Content-Type": "application/json",
        "X-Request-Id": request_id,
    }
    if api_key is not None:
        request_headers["X-API-Key"] = api_key
    request_headers.update(headers or {})
    response = {}
    handler = object.__new__(Handler)
    handler.command = method
    handler.path = path
    handler.headers = request_headers
    handler.rfile = io.BytesIO(encoded)
    handler.request_id = request_id
    handler._json = lambda status, body, extra=None: response.update(
        status=status,
        body=body,
        headers={**(extra or {}), "X-Request-Id": request_id},
    )
    handler._handle()
    return response


def order_payload(customer_id="task-83-customer", total_cents=1500):
    return {"customer_id": customer_id, "total_cents": total_cents}


def product_payload(sku="TASK83-ITEM"):
    return {
        "sku": sku,
        "name": "Task 83 item",
        "category": "task-83",
        "price_cents": 125,
        "stock": 3,
    }


class Task83AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.order_store = orders.OrderStore()
        self.product_store = products.ProductStore()
        self.stores_patch = patch.dict(
            routes.__dict__,
            {"ORDER_STORE": self.order_store, "PRODUCT_STORE": self.product_store},
        )
        self.stores_patch.start()
        self.addCleanup(self.stores_patch.stop)
        self.auth_patch = patch("agent_qa.server.is_valid_api_key", return_value=True)
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)

    def assert_api_error(self, response, status, code):
        self.assertEqual(response["status"], status)
        self.assertEqual(response["body"]["error"]["code"], code)

    def test_ac1_key_validation_and_missing_header_compatibility(self):
        payload = order_payload()
        for key in ("", "x" * 65, "invalid key", "bad/character"):
            with self.subTest(key=key):
                self.assert_api_error(
                    invoke("POST", "/orders", payload, {"Idempotency-Key": key}),
                    400,
                    "invalid_idempotency_key",
                )

        for key in ("A", "A" + "a._:-9" * 10 + "a._"):
            with self.subTest(valid_key=key):
                self.assertEqual(
                    invoke("POST", "/orders", payload, {"Idempotency-Key": key})[
                        "status"
                    ],
                    201,
                )
        self.assertEqual(invoke("POST", "/orders", payload)["status"], 201)
        self.assertEqual(invoke("POST", "/orders", payload)["status"], 201)
        self.assertEqual(self.order_store.list()[1], 4)

    def test_ac2_replay_is_scoped_by_api_key_path_and_canonical_body(self):
        payload = order_payload()
        first = invoke(
            "POST",
            "/orders",
            payload,
            {"Idempotency-Key": "order-key"},
            request_id="first-request",
        )
        replay = invoke(
            "POST",
            "/orders",
            {"total_cents": 1500, "customer_id": "task-83-customer"},
            {"Idempotency-Key": "order-key"},
            request_id="replay-request",
        )
        self.assertEqual(replay["status"], first["status"])
        self.assertEqual(replay["body"], first["body"])
        self.assertEqual(replay["headers"]["Location"], first["headers"]["Location"])
        self.assertEqual(replay["headers"]["Idempotent-Replay"], "true")
        self.assertEqual(replay["headers"]["Idempotency-Key"], "order-key")
        self.assertEqual(replay["headers"]["X-Request-Id"], "replay-request")
        self.assertEqual(self.order_store.list()[1], 1)

        separate_api_key = invoke(
            "POST",
            "/orders",
            payload,
            {"Idempotency-Key": "order-key"},
            api_key="another-api-key",
        )
        self.assertEqual(separate_api_key["status"], 201)
        separate_path = invoke(
            "POST",
            "/products",
            product_payload(),
            {"Idempotency-Key": "order-key"},
        )
        self.assertEqual(separate_path["status"], 201)

    def test_ac2_changed_body_returns_422_and_does_not_create_again(self):
        invoke("POST", "/orders", order_payload(), {"Idempotency-Key": "reuse"})
        self.assert_api_error(
            invoke(
                "POST",
                "/orders",
                order_payload(total_cents=1600),
                {"Idempotency-Key": "reuse"},
            ),
            422,
            "idempotency_key_reused",
        )
        self.assertEqual(self.order_store.list()[1], 1)

    def test_ac3_active_duplicate_returns_409_and_releases_after_completion(self):
        entered = threading.Event()
        release = threading.Event()
        route = next(
            item
            for item in ROUTES
            if item["method"] == "POST" and item["path"] == "/orders"
        )
        original = route["handler"]

        def slow_handler(query, path_params=None, payload=None):
            entered.set()
            release.wait(2)
            return original(query, path_params, payload)

        with patch.dict(route, {"handler": slow_handler}):
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(
                    invoke,
                    "POST",
                    "/orders",
                    order_payload(),
                    {"Idempotency-Key": "parallel"},
                )
                self.assertEqual(entered.wait(2), True)
                self.assert_api_error(
                    invoke(
                        "POST",
                        "/orders",
                        order_payload(),
                        {"Idempotency-Key": "parallel"},
                    ),
                    409,
                    "idempotency_in_progress",
                )
                release.set()
                first = pending.result(timeout=2)
        self.assertEqual(first["status"], 201)
        self.assertEqual(self.order_store.list()[1], 1)

    def test_ac3_unauthorized_and_client_error_do_not_consume_key(self):
        key = "retry-after-error"
        self.assert_api_error(
            invoke("POST", "/orders", order_payload(), {"Idempotency-Key": key}, None),
            401,
            "unauthorized",
        )
        self.assertEqual(
            invoke(
                "POST",
                "/orders",
                order_payload(),
                {"Idempotency-Key": key},
                API_KEY,
            )["status"],
            201,
        )

        route = next(
            item
            for item in ROUTES
            if item["method"] == "POST" and item["path"] == "/orders"
        )
        original = route["handler"]

        def server_error(query, path_params=None, payload=None):
            return 500, {}, {}

        with patch.dict(
            route,
            {"handler": server_error},
        ):
            self.assertEqual(
                invoke(
                    "POST",
                    "/orders",
                    order_payload(),
                    {"Idempotency-Key": "retry-after-server-error"},
                )["status"],
                500,
            )
        with patch.dict(route, {"handler": original}):
            self.assertEqual(
                invoke(
                    "POST",
                    "/orders",
                    order_payload(),
                    {"Idempotency-Key": "retry-after-server-error"},
                )["status"],
                201,
            )

        invalid_payload = {"customer_id": "task-83-customer", "total_cents": -1}
        self.assert_api_error(
            invoke(
                "POST",
                "/orders",
                invalid_payload,
                {"Idempotency-Key": "retry-after-validation"},
            ),
            400,
            "validation_error",
        )
        self.assertEqual(
            invoke(
                "POST",
                "/orders",
                order_payload(),
                {"Idempotency-Key": "retry-after-validation"},
            )["status"],
            201,
        )

    def test_ac3_twenty_threads_create_one_order_for_one_key(self):
        barrier = threading.Barrier(20)

        def submit():
            barrier.wait(timeout=3)
            return invoke(
                "POST",
                "/orders",
                order_payload("twenty-thread-customer"),
                {"Idempotency-Key": "twenty-thread-key"},
            )

        with ThreadPoolExecutor(max_workers=20) as executor:
            responses = list(executor.map(lambda _index: submit(), range(20)))
        self.assertEqual(self.order_store.list()[1], 1)
        self.assertIn(201, [response["status"] for response in responses])

    def test_ac4_ttl_configuration_expiration_and_lru_capacity(self):
        self.assertEqual(IdempotencyStore().ttl_seconds, 600)
        for value in ("1", "86400"):
            with self.subTest(ttl=value), patch.dict(
                "os.environ", {"AGENT_QA_IDEMPOTENCY_TTL_SECONDS": value}
            ):
                self.assertEqual(idempotency_ttl_seconds(), int(value))

        now = [0.0]
        ttl_store = IdempotencyStore(ttl_seconds=10, clock=lambda: now[0])
        ttl_scope = ("api", "POST", "/orders", "ttl-key")
        self.assertEqual(ttl_store.begin(ttl_scope, "fingerprint").kind, BeginKind.NEW)
        ttl_store.complete(ttl_scope, "fingerprint", Response(201, {"id": 1}, {}))
        now[0] = 9.0
        self.assertEqual(
            ttl_store.begin(ttl_scope, "fingerprint").kind, BeginKind.REPLAY
        )
        now[0] = 10.0
        self.assertEqual(ttl_store.begin(ttl_scope, "fingerprint").kind, BeginKind.NEW)

        lru_store = IdempotencyStore(capacity=500)
        scopes = [("api", "POST", "/orders", f"key-{index}") for index in range(501)]
        for scope in scopes[:500]:
            self.assertEqual(lru_store.begin(scope, "fingerprint").kind, BeginKind.NEW)
            lru_store.complete(scope, "fingerprint", Response(201, {}, {}))
        self.assertEqual(
            lru_store.begin(scopes[0], "fingerprint").kind, BeginKind.REPLAY
        )
        self.assertEqual(
            lru_store.begin(scopes[500], "fingerprint").kind, BeginKind.NEW
        )
        lru_store.complete(scopes[500], "fingerprint", Response(201, {}, {}))
        self.assertEqual(
            lru_store.begin(scopes[0], "fingerprint").kind, BeginKind.REPLAY
        )
        self.assertEqual(lru_store.begin(scopes[1], "fingerprint").kind, BeginKind.NEW)

    def test_ac5_openapi_and_idempotency_metrics(self):
        document = build_openapi(ROUTES, "task-83")
        for path in ("/orders", "/products"):
            operation = document["paths"][path]["post"]
            self.assertIn(
                {"name": "Idempotency-Key", "in": "header"},
                [
                    {"name": item["name"], "in": item["in"]}
                    for item in operation.get("parameters", [])
                ],
            )
            self.assertIn("422", operation["responses"])
            self.assertIn("409", operation["responses"])

        registry = MetricsRegistry()
        with patch("agent_qa.routes.REGISTRY", registry):
            self.assertNotIn("agent_qa_idempotency_total", registry.render(0, "test"))
            invoke(
                "POST",
                "/orders",
                order_payload(),
                {"Idempotency-Key": "metrics-key"},
            )
            invoke(
                "POST",
                "/orders",
                order_payload(),
                {"Idempotency-Key": "metrics-key"},
            )
            invoke(
                "POST",
                "/orders",
                order_payload(total_cents=1600),
                {"Idempotency-Key": "metrics-key"},
            )
            entered = threading.Event()
            release = threading.Event()
            route = next(
                item
                for item in ROUTES
                if item["method"] == "POST" and item["path"] == "/products"
            )
            original = route["handler"]

            def slow_product(query, path_params=None, payload=None):
                entered.set()
                release.wait(2)
                return original(query, path_params, payload)

            with patch.dict(route, {"handler": slow_product}):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    pending = executor.submit(
                        invoke,
                        "POST",
                        "/products",
                        product_payload("TASK83-METRICS"),
                        {"Idempotency-Key": "metrics-progress"},
                    )
                    self.assertEqual(entered.wait(2), True)
                    self.assert_api_error(
                        invoke(
                            "POST",
                            "/products",
                            product_payload("TASK83-METRICS"),
                            {"Idempotency-Key": "metrics-progress"},
                        ),
                        409,
                        "idempotency_in_progress",
                    )
                    release.set()
                    pending.result(timeout=2)
            rendered = registry.render(0, "test")
        self.assertIn('agent_qa_idempotency_total{result="stored"}', rendered)
        self.assertIn('agent_qa_idempotency_total{result="replayed"}', rendered)
        self.assertIn('agent_qa_idempotency_total{result="mismatch"}', rendered)
        self.assertIn('agent_qa_idempotency_total{result="in_progress"}', rendered)


if __name__ == "__main__":
    unittest.main()
