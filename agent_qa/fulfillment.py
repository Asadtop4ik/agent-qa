"""Atomic order creation and inventory reservation."""

from __future__ import annotations

import logging
import threading
from typing import Any

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

    def create(self, **fields: Any) -> dict[str, Any]:
        normalized = validate_create(fields)
        if "total_cents" in normalized:
            with self._lock, self._products._lock, self._orders._lock:
                return self._orders._create_locked({**normalized, "items": []})

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
                return self._orders._create_locked(
                    {
                        "customer_id": normalized["customer_id"],
                        "total_cents": total_cents,
                        "items": prepared,
                    }
                )
            except Exception as error:
                self._products._restore_stock_locked(stock_snapshot)
                self._orders._orders.pop(next_id, None)
                self._orders._next_id = next_id
                if not isinstance(error, (ApiError, OrderError)):
                    logger.exception("Unexpected failure creating item order")
                raise

    def update(self, order_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
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
                return self._orders.update(order_id, fields)

            product_ids = {item["product_id"] for item in current["items"]}
            stock_snapshot = self._products._snapshot_stock_locked(product_ids)
            order_snapshot = self._orders._copy_order(current)
            try:
                updated = self._orders.update(order_id, fields)
                self._products.release(self._stock_lines(current["items"]))
                return updated
            except Exception as error:
                self._products._restore_stock_locked(stock_snapshot)
                self._orders._orders[order_id] = order_snapshot
                if not isinstance(error, (ApiError, OrderError)):
                    logger.exception("Unexpected failure cancelling item order")
                raise

    def delete(self, order_id: int) -> bool:
        with self._lock, self._products._lock, self._orders._lock:
            current = self._orders._orders.get(order_id)
            if current is None:
                return False
            should_release = current["status"] not in {"cancelled", "shipped"} and bool(
                current.get("items")
            )
            if not should_release:
                return self._orders.delete(order_id)

            product_ids = {item["product_id"] for item in current["items"]}
            stock_snapshot = self._products._snapshot_stock_locked(product_ids)
            order_snapshot = self._orders._copy_order(current)
            try:
                self._products.release(self._stock_lines(current["items"]))
                return self._orders.delete(order_id)
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
