"""Version metadata and sunset behavior."""

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from agent_qa import settings
from agent_qa.versioning import (
    DEPRECATED_AT,
    SUNSET_AT,
    map_v2_error_body,
    map_v2_field_path,
    response_headers,
    sunset_reached,
    versions_document,
)


class VersioningTests(unittest.TestCase):
    def test_versions_document_matches_the_public_contract(self):
        self.assertEqual(
            versions_document(),
            {
                "versions": [
                    {
                        "version": "1",
                        "status": "deprecated",
                        "deprecated_at": "2026-10-01T00:00:00Z",
                        "sunset": "2026-12-31T23:59:59Z",
                        "base_path": "/orders",
                    },
                    {
                        "version": "2",
                        "status": "current",
                        "base_path": "/v2/orders",
                    },
                ]
            },
        )

    def test_response_headers_come_from_route_metadata_and_concrete_params(self):
        route = {
            "api_version": "1",
            "deprecated": True,
            "successor": "/v2/orders/{id}",
        }
        self.assertEqual(
            response_headers(route, {"id": "42"}),
            {
                "Deprecation": "@1790812800",
                "Sunset": "Thu, 31 Dec 2026 23:59:59 GMT",
                "Link": '</v2/orders/42>; rel="successor-version"',
                "X-API-Version": "1",
            },
        )
        self.assertEqual(
            response_headers({"api_version": "2"}, {}),
            {"X-API-Version": "2"},
        )

    def test_sunset_is_opt_in_and_uses_injected_clock(self):
        before = datetime(2026, 12, 31, 23, 59, 58, tzinfo=timezone.utc)
        at_sunset = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
        after_sunset = datetime(2027, 1, 1, tzinfo=timezone.utc)
        route = {"api_version": "1", "deprecated": True}
        self.assertFalse(sunset_reached(route, now=at_sunset, enforce=False))
        self.assertFalse(sunset_reached(route, now=before, enforce=True))
        self.assertTrue(sunset_reached(route, now=at_sunset, enforce=True))
        self.assertTrue(sunset_reached(route, now=after_sunset, enforce=True))

    def test_default_setting_keeps_sunset_inactive_after_the_date(self):
        route = {"api_version": "1", "deprecated": True}
        after_sunset = datetime(2027, 1, 1, tzinfo=timezone.utc)
        with patch(
            "agent_qa.versioning.settings.current", return_value=settings.load({})
        ):
            self.assertFalse(sunset_reached(route, now=after_sunset))

    def test_default_clock_is_patchable_for_dispatch_tests(self):
        route = {"api_version": "1", "deprecated": True}
        with patch("agent_qa.versioning.utcnow", return_value=SUNSET_AT):
            self.assertTrue(sunset_reached(route, enforce=True))
        self.assertEqual(DEPRECATED_AT.isoformat(), "2026-10-01T00:00:00+00:00")

    def test_shared_store_fields_are_mapped_to_v2_names(self):
        self.assertEqual(map_v2_field_path("customer_id"), "customer.id")
        self.assertEqual(map_v2_field_path("body.total_cents"), "amount.total_cents")
        mapped = map_v2_error_body(
            {
                "error": {
                    "code": "validation_error",
                    "details": [
                        {"field": "customer_id", "message": "Required"},
                        {"field": "items[0].quantity", "message": "Too many"},
                    ],
                }
            }
        )
        self.assertEqual(
            [item["field"] for item in mapped["error"]["details"]],
            ["customer.id", "items[0].quantity"],
        )


if __name__ == "__main__":
    unittest.main()
