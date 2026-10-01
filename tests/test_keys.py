"""KeyStore lifecycle and secret handling tests."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agent_qa.errors import ApiError
from agent_qa.keys import KeyStore, _digest


class Clock:
    def __init__(self):
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = KeyStore("bootstrap-secret", self.clock)

    def test_bootstrap_is_admin_and_cannot_be_rotated_or_revoked(self):
        self.assertEqual(
            self.store.authenticate("bootstrap-secret"),
            {"key_id": "bootstrap", "role": "admin", "label": "bootstrap"},
        )
        for action in (self.store.rotate, self.store.revoke):
            with self.subTest(action=action.__name__):
                with self.assertRaises(ApiError) as error:
                    action("bootstrap")
                self.assertEqual(error.exception.status, 409)
                self.assertEqual(error.exception.code, "bootstrap_key_immutable")

    def test_long_bootstrap_secrets_remain_authenticatable(self):
        for length in (4096, 4097, 16384):
            with self.subTest(length=length):
                secret = "b" * length
                store = KeyStore(secret, self.clock)
                self.assertEqual(
                    store.authenticate(secret),
                    {"key_id": "bootstrap", "role": "admin", "label": "bootstrap"},
                )
                self.assertIsNone(store.authenticate("x" * length))

    def test_overlong_authentication_candidate_is_rejected_with_bounded_hash(self):
        store = KeyStore("b" * 4097, self.clock)
        candidate = "x" * 4098

        with patch("agent_qa.keys._digest", wraps=_digest) as digest:
            self.assertIsNone(store.authenticate(candidate))

        digest.assert_called_once_with("")

    def test_grace_rotation_expires_old_key_at_boundary(self):
        created = self.store.create("write", "operator")
        rotated = self.store.rotate(created["key_id"], 30)
        self.assertEqual(self.store.authenticate(created["key"])["key_id"], "key_1")
        self.assertEqual(self.store.authenticate(rotated["key"])["key_id"], "key_1")
        self.clock.value += timedelta(seconds=30)
        self.assertIsNone(self.store.authenticate(created["key"]))
        self.assertEqual(self.store.authenticate(rotated["key"])["key_id"], "key_1")
        self.assertEqual(self.store.list_keys()["items"][1]["status"], "active")

    def test_rerotation_replaces_old_grace_secret(self):
        first = self.store.create("read", "reader")
        second = self.store.rotate(first["key_id"], 60)
        third = self.store.rotate(first["key_id"], 60)
        self.assertIsNone(self.store.authenticate(first["key"]))
        self.assertIsNotNone(self.store.authenticate(second["key"]))
        self.assertIsNotNone(self.store.authenticate(third["key"]))

    def test_revoked_key_is_invalid_and_ids_are_not_reused(self):
        first = self.store.create("read", "one")
        self.store.revoke(first["key_id"])
        self.assertIsNone(self.store.authenticate(first["key"]))
        self.assertEqual(self.store.create("read", "two")["key_id"], "key_2")
        with self.assertRaises(ApiError) as error:
            self.store.revoke("missing")
        self.assertEqual(
            (error.exception.status, error.exception.code), (404, "key_not_found")
        )

    def test_limit_counts_active_non_bootstrap_keys_only(self):
        created = [self.store.create("read", f"reader{i}") for i in range(20)]
        with self.assertRaises(ApiError) as error:
            self.store.create("read", "overflow")
        self.assertEqual(
            (error.exception.status, error.exception.code), (409, "key_limit")
        )
        self.store.revoke(created[0]["key_id"])
        self.assertEqual(self.store.create("read", "replacement")["key_id"], "key_21")

    def test_last_used_tracks_only_successful_authentication(self):
        created = self.store.create("read", "reader")
        self.assertIsNone(self.store.list_keys()["items"][1]["last_used_at"])
        self.assertIsNone(self.store.authenticate("wrong"))
        self.assertIsNone(self.store.list_keys()["items"][1]["last_used_at"])
        self.store.authenticate(created["key"])
        self.assertEqual(
            self.store.list_keys()["items"][1]["last_used_at"],
            "2026-01-01T00:00:00Z",
        )

    def test_invalid_inputs_are_rejected(self):
        for role, label in (
            ([], "label"),
            ("read", " "),
            ("read", "x" * 41),
            ("read", " " * 41),
        ):
            with self.subTest(role=role, label_length=len(label)):
                with self.assertRaises(ApiError) as error:
                    self.store.create(role, label)
                self.assertEqual(error.exception.status, 400)
        created = self.store.create("read", "reader")
        for grace in (True, -1, 301, 10**1000):
            with self.subTest(grace=type(grace).__name__):
                with self.assertRaises(ApiError) as error:
                    self.store.rotate(created["key_id"], grace)
                self.assertEqual(error.exception.status, 400)

    def test_authentication_compares_all_current_and_previous_hashes(self):
        created = self.store.create("read", "reader")
        rotated = self.store.rotate(created["key_id"], 5)

        for secret, expected_identity in (
            ("bootstrap-secret", "bootstrap"),
            (rotated["key"], "key_1"),
            (created["key"], "key_1"),
            ("not-a-key", None),
        ):
            with self.subTest(identity=expected_identity, secret=secret != "not-a-key"):
                with patch(
                    "agent_qa.keys.hmac.compare_digest",
                    wraps=__import__("hmac").compare_digest,
                ) as compare:
                    identity = self.store.authenticate(secret)
                self.assertEqual(compare.call_count, 3)
                self.assertEqual(
                    identity["key_id"] if identity is not None else None,
                    expected_identity,
                )

        self.clock.value += timedelta(seconds=5)
        with patch(
            "agent_qa.keys.hmac.compare_digest",
            wraps=__import__("hmac").compare_digest,
        ) as compare:
            self.assertIsNone(self.store.authenticate(created["key"]))
        self.assertEqual(compare.call_count, 3)

    def test_zero_grace_invalidates_old_secret_immediately(self):
        created = self.store.create("write", "operator")
        rotated = self.store.rotate(created["key_id"])
        self.assertIsNone(self.store.authenticate(created["key"]))
        self.assertIsNotNone(self.store.authenticate(rotated["key"]))

    def test_rotation_preserves_creation_and_last_used_timestamps(self):
        created = self.store.create("write", " operator ")
        self.store.authenticate(created["key"])
        before = self.store.list_keys()["items"][1]
        self.clock.value += timedelta(minutes=1)
        rotated = self.store.rotate(created["key_id"], 10)
        after = self.store.list_keys()["items"][1]
        self.assertEqual(created["label"], " operator ")
        self.assertEqual(rotated["created_at"], before["created_at"])
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertEqual(after["last_used_at"], before["last_used_at"])

    def test_only_hashes_are_retained_and_list_never_returns_secret(self):
        created = self.store.create("admin", "temporary")
        rotated = self.store.rotate(created["key_id"])
        listing = self.store.list_keys()
        self.assertNotIn(created["key"], repr(self.store._keys))
        self.assertNotIn(rotated["key"], repr(self.store._keys))
        self.assertNotIn(created["key"], repr(listing))
        self.assertNotIn(rotated["key"], repr(listing))
        self.assertEqual(listing["total"], 2)


if __name__ == "__main__":
    unittest.main()
