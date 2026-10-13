"""Atomic order creation and inventory reservation operations."""

from __future__ import annotations

from typing import Any

from agent_qa.orders import OrderError, OrderStore, validate_create, validate_patch
from agent_qa.products import ProductStore
from agent_qa.schemas import MAX_TOTAL_CENTS


def _validation_error(details: list[dict[str, str]]) -> OrderError:
    return OrderError("validation_error", "Request validation failed", details)


class Fulfillment:
    """Coordinate order and inventory mutations under product-then-order locks."""

    def __init__(self, order_store: OrderStore, product_store: ProductStore) -> None:
        self.order_store = order_store
        self.product_store = product_store

    def create(self, **payload: Any) -> dict[str, Any]:
        fields = validate_create(payload)
        if "total_cents" in fields:
            with self.product_store._lock, self.order_store._lock:
                return self.order_store._create_locked(fields)

        source_lines = fields["items"]
        with self.product_store._lock, self.order_store._lock:
            seen: set[int] = set()
            products: list[dict[str, Any]] = []
            errors: list[dict[str, str]] = []
            unavailable: list[dict[str, str]] = []
            for index, line in enumerate(source_lines):
                product_id = line["product_id"]
                field = f"items[{index}].product_id"
                if product_id in seen:
                    errors.append({"field": field, "message": "Duplicate product"})
                    continue
                seen.add(product_id)
                product = self.product_store._products.get(product_id)
                if product is None:
                    errors.append({"field": field, "message": "Unknown product"})
                else:
                    products.append(product)
                    if not product["active"]:
                        unavailable.append(
                            {"field": field, "message": "Product unavailable"}
                        )
            if errors:
                raise _validation_error(errors)
            if unavailable:
                raise OrderError(
                    "product_unavailable", "Product is unavailable", unavailable
                )

            snapshots: list[dict[str, Any]] = []
            total_cents = 0
            product_by_id = {product["id"]: product for product in products}
            for line in source_lines:
                product = product_by_id[line["product_id"]]
                line_total = product["price_cents"] * line["quantity"]
                total_cents += line_total
                snapshots.append(
                    {
                        "product_id": product["id"],
                        "sku": product["sku"],
                        "name": product["name"],
                        "quantity": line["quantity"],
                        "unit_price_cents": product["price_cents"],
                        "line_total_cents": line_total,
                    }
                )
            if total_cents > MAX_TOTAL_CENTS:
                raise _validation_error(
                    [{"field": "items", "message": "Order total exceeds maximum"}]
                )

            reservation_snapshot = self.product_store._reservation_snapshot(
                source_lines
            )
            try:
                self.product_store.reserve(source_lines)
                return self.order_store._create_locked(
                    {
                        "customer_id": fields["customer_id"],
                        "total_cents": total_cents,
                        "items": snapshots,
                    }
                )
            except Exception:
                self.product_store._restore_reservation_snapshot(reservation_snapshot)
                raise

    def update(self, order_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
        fields = validate_patch(changes)
        with self.product_store._lock, self.order_store._lock:
            current = self.order_store._orders.get(order_id)
            if current is None:
                return None
            items = current.get("items", [])
            transition = fields.get("status")
            lines = [
                {"product_id": item["product_id"], "quantity": item["quantity"]}
                for item in items
            ]
            updated = self.order_store.update(order_id, fields)
            if updated is None:
                return None
            try:
                if transition == "cancelled":
                    self.product_store.release(lines)
                elif transition == "shipped":
                    self.product_store.consume(lines)
            except Exception:
                self.order_store._orders[order_id] = current
                raise
            return updated

    def delete(self, order_id: int) -> bool:
        with self.product_store._lock, self.order_store._lock:
            current = self.order_store._orders.get(order_id)
            if current is None:
                return False
            lines = [
                {"product_id": item["product_id"], "quantity": item["quantity"]}
                for item in current.get("items", [])
            ]
            if current["status"] != "shipped" and current["status"] != "cancelled":
                self.product_store.release(lines)
            return self.order_store.delete(order_id)
