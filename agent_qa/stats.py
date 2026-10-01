"""Pure aggregation helpers for the admin statistics endpoint."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


_ORDER_STATUSES = ("new", "paid", "shipped", "cancelled")
_JOB_STATUSES = (
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
)
_OUTBOX_STATUSES = ("pending", "retrying", "delivered", "failed")


def _validate_top(top: int) -> None:
    if type(top) is not int or not 1 <= top <= 20:
        raise ValueError("top must be an integer from 1 to 20")


def aggregate_orders(orders: list[dict[str, Any]], top: int = 5) -> dict[str, Any]:
    """Aggregate one detached tenant order snapshot."""
    _validate_top(top)
    by_status = dict.fromkeys(_ORDER_STATUSES, 0)
    customers: dict[str, dict[str, int]] = {}
    total_value = 0
    revenue = 0
    itemized = 0
    for order in orders:
        status = order["status"]
        by_status[status] += 1
        total_value += order["total_cents"]
        if status in {"paid", "shipped"}:
            revenue += order["total_cents"]
        if order.get("items"):
            itemized += 1
        if status != "cancelled":
            customer_id = order["customer_id"]
            entry = customers.setdefault(customer_id, {"orders": 0, "spent_cents": 0})
            entry["orders"] += 1
            entry["spent_cents"] += order["total_cents"]
    top_customers = [
        {"customer_id": customer_id, **values}
        for customer_id, values in customers.items()
    ]
    top_customers.sort(key=lambda row: (-row["spent_cents"], row["customer_id"]))
    return {
        "total": len(orders),
        "by_status": by_status,
        "revenue_cents": revenue,
        "average_total_cents": total_value // len(orders) if orders else 0,
        "itemized": itemized,
        "top_customers": top_customers[:top],
    }


def aggregate_products(
    products: list[dict[str, Any]], orders: list[dict[str, Any]], top: int = 5
) -> dict[str, Any]:
    """Aggregate tenant products and sales represented in their orders."""
    _validate_top(top)
    categories: dict[str, dict[str, int]] = {}
    active_value = 0
    active = 0
    out_of_stock = 0
    low_stock = 0
    for product in products:
        category = product["category"]
        category_row = categories.setdefault(
            category, {"products": 0, "inventory_value_cents": 0}
        )
        category_row["products"] += 1
        stock = product["stock"]
        if stock == 0:
            out_of_stock += 1
        elif 1 <= stock <= 5:
            low_stock += 1
        if product["active"]:
            active += 1
            inventory_value = product["price_cents"] * stock
            active_value += inventory_value
            category_row["inventory_value_cents"] += inventory_value

    sold: dict[int, dict[str, Any]] = {}
    for order in orders:
        if order["status"] == "cancelled":
            continue
        for item in order.get("items", []):
            product_id = item["product_id"]
            row = sold.setdefault(
                product_id,
                {
                    "product_id": product_id,
                    "sku": item["sku"],
                    "units_sold": 0,
                    "revenue_cents": 0,
                },
            )
            row["units_sold"] += item["quantity"]
            row["revenue_cents"] += item["line_total_cents"]
    top_products = list(sold.values())
    top_products.sort(key=lambda row: (-row["revenue_cents"], row["product_id"]))
    return {
        "total": len(products),
        "active": active,
        "out_of_stock": out_of_stock,
        "low_stock": low_stock,
        "inventory_value_cents": active_value,
        "by_category": [
            {"category": category, **categories[category]}
            for category in sorted(categories)
        ],
        "top_products": top_products[:top],
    }


def aggregate_requests(snapshot: dict[str, Any], top: int = 5) -> dict[str, Any]:
    """Aggregate global request and duration snapshots by route."""
    _validate_top(top)
    total = errors_4xx = errors_5xx = 0
    route_counts: dict[str, int] = defaultdict(int)
    for row in snapshot["requests"]:
        count = row["count"]
        status = int(row["status"])
        total += count
        if 400 <= status < 500:
            errors_4xx += count
        elif 500 <= status < 600:
            errors_5xx += count
        route_counts[row["route"]] += count
    durations: dict[str, dict[str, float]] = {}
    for row in snapshot["durations"]:
        duration = durations.setdefault(row["route"], {"count": 0, "total": 0.0})
        duration["count"] += row["count"]
        duration["total"] += row["total_seconds"]
    by_route = []
    for route, count in route_counts.items():
        duration = durations.get(route, {"count": 0, "total": 0.0})
        duration_count = duration["count"]
        if duration_count:
            average_ms = duration["total"] * 1000 / duration_count
        else:
            average_ms = 0.0
        by_route.append(
            {"route": route, "count": count, "avg_ms": round(average_ms, 3)}
        )
    by_route.sort(key=lambda row: (-row["count"], row["route"]))
    return {
        "total": total,
        "errors_4xx": errors_4xx,
        "errors_5xx": errors_5xx,
        "by_route": by_route[:top],
    }


def build_stats(
    orders: list[dict[str, Any]],
    products: list[dict[str, Any]],
    requests: dict[str, Any],
    jobs: dict[str, int],
    outbox: dict[str, int],
    audit: dict[str, int],
    *,
    top: int = 5,
) -> dict[str, Any]:
    """Build aggregate sections from detached store and metrics snapshots."""
    _validate_top(top)
    return {
        "orders": aggregate_orders(orders, top),
        "products": aggregate_products(products, orders, top),
        "requests": aggregate_requests(requests, top),
        "jobs": {status: jobs.get(status, 0) for status in _JOB_STATUSES},
        "outbox": {status: outbox.get(status, 0) for status in _OUTBOX_STATUSES},
        "audit": {"entries": audit["entries"], "dropped": audit["dropped"]},
    }
