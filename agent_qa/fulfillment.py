"""Atomic order creation and inventory reservation."""

from __future__ import annotations

import logging
import threading
from copy import deepcopy
from typing import Any

from agent_qa.bulk import run_bulk, validate_bulk_input
from agent_qa.conditional import check_expected_version
from agent_qa.errors import ApiError
from agent_qa.orders import OrderError, OrderStore, validate_create, validate_patch
from agent_qa.products import ProductStore
from agent_qa.schemas import MAX_TOTAL_CENTS

logger = logging.getLogger(__name__)


class FulfillmentService:
    """Coordinate order mutations and product stock under a stable lock order."""

    def __init__(self, orders: OrderStore, products: ProductStore) -> None:
        self._orders = orders
        self._products = products
        self._lock = threading.RLock()

    def create(self, *, include_version: bool = False, **fields: Any) -> dict[str, Any]:
        normalized = validate_create(fields)
        if "total_cents" in normalized:
            with self._lock, self._products._lock, self._orders._lock:
                created = self._orders._create_locked({**normalized, "items": []})
                return self._orders._copy_order(
                    self._orders._orders[created["id"]], include_version
                )

        lines = normalized["items"]
        prepared: list[dict[str, Any]] = []
        seen: set[int] = set()
        products: list[dict[str, Any]] = []
        # Check all product references before checking availability or stock.
        with self._lock, self._products._lock, self._orders._lock:
            for index, line in enumerate(lines):
                product_id = line["product_id"]
                field = f"items[{index}].product_id"
                if product_id in seen:
                    raise ApiError(
                        400,
                        "validation_error",
                        "Request validation failed",
                        [{"field": field, "message": "Duplicate product"}],
                    )
                seen.add(product_id)
                product = self._products._products.get(product_id)
                if product is None:
                    raise ApiError(
                        400,
                        "validation_error",
                        "Request validation failed",
                        [{"field": field, "message": "Unknown product"}],
                    )
                products.append(product)

            for index, (line, product) in enumerate(zip(lines, products)):
                if not product["active"]:
                    raise ApiError(
                        409,
                        "product_unavailable",
                        "Product is unavailable",
                        [
                            {
                                "field": f"items[{index}].product_id",
                                "message": "Product is unavailable",
                            }
                        ],
                    )
                quantity = line["quantity"]
                line_total = product["price_cents"] * quantity
                prepared.append(
                    {
                        "product_id": product["id"],
                        "sku": product["sku"],
                        "name": product["name"],
                        "quantity": quantity,
                        "unit_price_cents": product["price_cents"],
                        "line_total_cents": line_total,
                    }
                )
            total_cents = sum(item["line_total_cents"] for item in prepared)
            if total_cents > MAX_TOTAL_CENTS:
                raise ApiError(
                    400,
                    "validation_error",
                    "Request validation failed",
                    [{"field": "items", "message": "Order total exceeds maximum"}],
                )

            reserve_lines = [
                {
                    "product_id": line["product_id"],
                    "quantity": line["quantity"],
                    "index": index,
                }
                for index, line in enumerate(lines)
            ]
            stock_snapshot = self._products._snapshot_stock_locked(seen)
            next_id = self._orders._next_id
            try:
                self._products.reserve(reserve_lines)
                created = self._orders._create_locked(
                    {
                        "customer_id": normalized["customer_id"],
                        "total_cents": total_cents,
                        "items": prepared,
                    }
                )
                return self._orders._copy_order(
                    self._orders._orders[created["id"]], include_version
                )
            except Exception as error:
                self._products._restore_stock_locked(stock_snapshot)
                self._orders._orders.pop(next_id, None)
                self._orders._next_id = next_id
                if not isinstance(error, (ApiError, OrderError)):
                    logger.exception("Unexpected failure creating item order")
                raise

    def create_bulk(
        self, items: list[Any], atomic: bool = False
    ) -> tuple[int, dict[str, Any]]:
        """Create orders sequentially, optionally restoring orders and stock."""
        validate_bulk_input(items, atomic)

        def apply_one(item: Any) -> dict[str, Any]:
            try:
                return self.create(**validate_create(item))
            except OrderError as error:
                status = {
                    "validation_error": 400,
                    "store_full": 409,
                }.get(error.code, 500)
                raise ApiError(
                    status, error.code, error.message, error.details
                ) from error

        with self._lock, self._products._lock, self._orders._lock:
            orders_snapshot = deepcopy(self._orders._orders)
            order_id_snapshot = self._orders._next_id
            products_snapshot = deepcopy(self._products._products)
            product_id_snapshot = self._products._next_id

            def rollback() -> None:
                self._orders._orders.clear()
                self._orders._orders.update(deepcopy(orders_snapshot))
                self._orders._next_id = order_id_snapshot
                self._products._products.clear()
                self._products._products.update(deepcopy(products_snapshot))
                self._products._next_id = product_id_snapshot

            return run_bulk(items, apply_one, rollback, atomic)

    def update(
        self,
        order_id: int,
        changes: dict[str, Any],
        *,
        expected_version: int | tuple[str, ...] | str | None = None,
        include_version: bool = False,
    ) -> dict[str, Any] | None:
        fields = validate_patch(changes)
        with self._lock, self._products._lock, self._orders._lock:
            current = self._orders._orders.get(order_id)
            if current is None:
                return None
            should_release = (
                fields.get("status") == "cancelled"
                and current["status"] != "cancelled"
                and current["status"] != "shipped"
                and bool(current.get("items"))
            )
            if not should_release:
                return self._orders.update(
                    order_id,
                    fields,
                    expected_version=expected_version,
                    include_version=include_version,
                )

            product_ids = {item["product_id"] for item in current["items"]}
            stock_snapshot = self._products._snapshot_stock_locked(product_ids)
            order_snapshot = self._orders._copy_order(current, include_version=True)
            try:
                updated = self._orders.update(
                    order_id,
                    fields,
                    expected_version=expected_version,
                    include_version=include_version,
                )
                self._products.release(self._stock_lines(current["items"]))
                return updated
            except Exception as error:
                self._products._restore_stock_locked(stock_snapshot)
                self._orders._orders[order_id] = order_snapshot
                if not isinstance(error, (ApiError, OrderError)):
                    logger.exception("Unexpected failure cancelling item order")
                raise

    def delete(
        self,
        order_id: int,
        *,
        expected_version: int | tuple[str, ...] | str | None = None,
    ) -> bool:
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id <= 0:
            return False
        with self._lock, self._products._lock, self._orders._lock:
            current = self._orders._orders.get(order_id)
            if current is None:
                return False
            check_expected_version(
                expected_version, "order", order_id, current["version"]
            )
            should_release = current["status"] not in {"cancelled", "shipped"} and bool(
                current.get("items")
            )
            if not should_release:
                return self._orders.delete(order_id, expected_version=expected_version)

            product_ids = {item["product_id"] for item in current["items"]}
            stock_snapshot = self._products._snapshot_stock_locked(product_ids)
            order_snapshot = self._orders._copy_order(current, include_version=True)
            try:
                self._products.release(self._stock_lines(current["items"]))
                return self._orders.delete(order_id, expected_version=expected_version)
            except Exception as error:
                self._products._restore_stock_locked(stock_snapshot)
                self._orders._orders[order_id] = order_snapshot
                if not isinstance(error, (ApiError, OrderError)):
                    logger.exception("Unexpected failure deleting item order")
                raise

    @staticmethod
    def _stock_lines(items: list[dict[str, Any]]) -> list[dict[str, int]]:
        return [
            {
                "product_id": item["product_id"],
                "quantity": item["quantity"],
                "index": index,
            }
            for index, item in enumerate(items)
        ]
