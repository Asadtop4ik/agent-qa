"""Bounded, process-local tenant registry and storage bundles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
import threading
import time
from typing import Any

from agent_qa import config
from agent_qa.errors import ApiError
from agent_qa.idempotency import IdempotencyStore
from agent_qa.jobs import JobRunner, make_builtin_handlers
from agent_qa.orders import OrderStore
from agent_qa.outbox import OUTBOX, OutboxStore
from agent_qa.products import ProductStore

MAX_TENANTS = 10
_TENANT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


def validate_name(value: Any) -> str:
    """Return a valid tenant name or a consistent request error."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 24
        or not _TENANT_PATTERN.fullmatch(value)
    ):
        raise ApiError(
            400,
            "invalid_tenant",
            "Tenant name is invalid",
            [{"field": "tenant", "message": "Must match the tenant name pattern"}],
        )
    return value


@dataclass
class TenantStores:
    tenant: str
    orders: OrderStore
    products: ProductStore
    jobs: JobRunner
    outbox: OutboxStore
    idempotency: IdempotencyStore
    fulfillment_lock: threading.RLock
    created_at: str

    def metadata(self) -> dict[str, object]:
        return {
            "tenant": self.tenant,
            "orders": self.orders.list(limit=1)[1],
            "products": self.products.list(limit=1)[1],
            "webhooks": self.outbox.list_webhooks()["total"],
            "jobs": self.jobs.list_jobs(limit=100)["total"],
            "created_at": self.created_at,
        }

    def stop(self) -> None:
        self.jobs.purge()
        self.outbox.purge()
        self.idempotency.clear()
        self.orders.clear()
        self.products.clear()


class TenantRegistry:
    """Keep at most ten tenant bundles; unknown reads stay ephemeral."""

    def __init__(self, *, default_outbox: OutboxStore | None = None) -> None:
        self._lock = threading.RLock()
        self._tenants: dict[str, TenantStores] = {}
        self._tenants["default"] = self._new_bundle(
            "default",
            outbox=default_outbox,
        )

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _new_bundle(
        tenant: str,
        *,
        outbox: OutboxStore | None = None,
        created_at: str | None = None,
    ) -> TenantStores:
        orders = OrderStore(tenant=tenant)
        products = ProductStore(tenant=tenant)
        return TenantStores(
            tenant=tenant,
            orders=orders,
            products=products,
            jobs=JobRunner(
                make_builtin_handlers(orders, products),
                workers=config.job_workers(),
                retention=config.job_retention(),
                tenant=tenant,
            ),
            outbox=outbox or OutboxStore(),
            idempotency=IdempotencyStore(config.idempotency_ttl_seconds()),
            fulfillment_lock=threading.RLock(),
            created_at=created_at or TenantRegistry._timestamp(),
        )

    def get(self, name: str) -> TenantStores:
        """Get stored resources or a throwaway empty bundle for read requests."""
        name = validate_name(name)
        with self._lock:
            bundle = self._tenants.get(name)
            if bundle is not None:
                return bundle
        return self._new_bundle(name)

    def ensure(self, name: str) -> TenantStores:
        """Create a bundle after the caller has completed authorization."""
        name = validate_name(name)
        with self._lock:
            bundle = self._tenants.get(name)
            if bundle is not None:
                return bundle
            if len(self._tenants) >= MAX_TENANTS:
                raise ApiError(409, "tenant_limit", "Tenant limit reached")
            bundle = self._new_bundle(name)
            self._tenants[name] = bundle
            return bundle

    def list_tenants(self) -> list[dict[str, object]]:
        with self._lock:
            bundles = [self._tenants[name] for name in sorted(self._tenants)]
        return [bundle.metadata() for bundle in bundles]

    def tenant_names(self) -> tuple[str, ...]:
        """Return a stable snapshot of registered names."""
        with self._lock:
            return tuple(sorted(self._tenants))

    def delete(self, name: str) -> None:
        name = validate_name(name)
        if name == "default":
            raise ApiError(
                409, "default_tenant_protected", "Default tenant is protected"
            )
        with self._lock:
            bundle = self._tenants.pop(name, None)
        if bundle is None:
            raise ApiError(404, "tenant_not_found", "Tenant not found")
        with bundle.fulfillment_lock:
            bundle.stop()

    def shutdown(self, timeout: float = 1.0) -> None:
        """Stop tenant workers within one shared deadline, retaining their data."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or (isinstance(timeout, float) and not math.isfinite(timeout))
            or timeout < 0
            or timeout > 10
        ):
            raise ValueError("timeout must be from 0 to 10 seconds")
        deadline = time.monotonic() + timeout
        with self._lock:
            bundles = tuple(self._tenants[name] for name in sorted(self._tenants))
        for bundle in bundles:
            remaining = max(0.0, deadline - time.monotonic())
            bundle.jobs.stop(timeout=remaining)
            remaining = max(0.0, deadline - time.monotonic())
            bundle.outbox.stop(timeout=remaining)

    def counts(self) -> dict[str, int]:
        """Return aggregate order/product counts and number of tenants."""
        with self._lock:
            bundles = tuple(self._tenants.values())
        return {
            "orders": sum(bundle.orders.list(limit=1)[1] for bundle in bundles),
            "products": sum(bundle.products.list(limit=1)[1] for bundle in bundles),
            "tenants": len(bundles),
        }


TENANTS = TenantRegistry(default_outbox=OUTBOX)


def current() -> TenantStores:
    """Return storage for the request's selected tenant, defaulting to default."""
    from agent_qa.context import get_context

    context = get_context()
    return TENANTS.get(context.tenant if context is not None else "default")


def get(name: str) -> TenantStores:
    return TENANTS.get(name)


def ensure(name: str) -> TenantStores:
    return TENANTS.ensure(name)


def list_tenants() -> list[dict[str, object]]:
    return TENANTS.list_tenants()


def delete(name: str) -> None:
    TENANTS.delete(name)


def shutdown(timeout: float = 1.0) -> None:
    TENANTS.shutdown(timeout)
