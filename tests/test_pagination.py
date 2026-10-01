import base64
import hashlib
import hmac
import json
import re
import unittest
from unittest.mock import patch

from agent_qa.errors import ApiError
from agent_qa import pagination
from agent_qa.pagination import (
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
    keyset_page,
)


def signed_payload(payload, key):
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    signature = hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()[:16]
    return f"{body}.{signature}"


def signed_raw_payload(raw, key):
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    signature = hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()[:16]
    return f"{body}.{signature}"


class CursorTests(unittest.TestCase):
    def test_signed_cursor_round_trip_and_rejects_tampering(self):
        fingerprint = filter_fingerprint({"status": "new"})
        cursor = encode_cursor("id", fingerprint, [3, 3])
        self.assertEqual(decode_cursor(cursor, "id", fingerprint), [3, 3])
        self.assertRegex(cursor, re.compile(r"^[A-Za-z0-9_.-]+$"))
        forged = cursor[:-1] + ("0" if cursor[-1] != "0" else "1")
        with self.assertRaises(ApiError) as error:
            decode_cursor(forged, "id", fingerprint)
        self.assertEqual(error.exception.code, "invalid_cursor")

    def test_malformed_empty_oversized_and_old_key_cursors_are_rejected(self):
        fingerprint = filter_fingerprint({})
        cursor = encode_cursor("id", fingerprint, [3, 3])
        with patch.object(pagination, "CURSOR_KEY", b"r" * 32):
            expired = cursor
            malformed_tokens = (
                "",
                "!",
                "x" * (pagination.MAX_CURSOR_LENGTH + 1),
                expired,
            )
            for malformed in malformed_tokens:
                with self.subTest(cursor_length=len(malformed)):
                    with self.assertRaises(ApiError) as error:
                        decode_cursor(malformed, "id", fingerprint)
                    self.assertEqual(error.exception.code, "invalid_cursor")

    def test_signed_malformed_payloads_are_rejected(self):
        fingerprint = filter_fingerprint({})
        key = b"test signing key for malformed tokens"
        valid = {"v": 1, "s": "id", "f": fingerprint, "k": [3, 3]}
        invalid_payloads = (
            {**valid, "v": 2},
            {**valid, "v": True},
            {**valid, "s": "unsupported"},
            {**valid, "k": [True, 3]},
            {**valid, "k": [10**100, 3]},
            {**valid, "k": [[[[1]]], 3]},
            {**valid, "extra": "field"},
            {"v": 1, "s": "id", "f": fingerprint},
        )
        with patch.object(pagination, "CURSOR_KEY", key):
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    token = signed_payload(payload, key)
                    with self.assertRaises(ApiError) as error:
                        decode_cursor(token, "id", fingerprint)
                    self.assertEqual(error.exception.code, "invalid_cursor")
            nested = b'{"v":1,"s":"id","f":"' + fingerprint.encode("ascii")
            nested += b'","k":' + b"[" * 1100 + b"0" + b"]" * 1100 + b"}"
            with self.assertRaises(ApiError) as error:
                decode_cursor(signed_raw_payload(nested, key), "id", fingerprint)
            self.assertEqual(error.exception.code, "invalid_cursor")

    def test_fingerprint_is_deterministic_and_excludes_pagination(self):
        self.assertEqual(
            filter_fingerprint({"active": True, "limit": 1}),
            filter_fingerprint({"limit": 99, "active": True}),
        )
        self.assertEqual(
            filter_fingerprint({"active": True, "offset": 3}),
            filter_fingerprint({"active": True, "offset": 0}),
        )
        self.assertEqual(
            filter_fingerprint({"active": True, "sort": "id"}),
            filter_fingerprint({"active": True, "sort": "-id"}),
        )

    def test_maximum_unicode_name_and_lone_surrogate_round_trip(self):
        fingerprint = filter_fingerprint({})
        for name in ("😀" * 120, "\ud800"):
            cursor = encode_cursor("name", fingerprint, [name, 1])
            self.assertLessEqual(len(cursor), pagination.MAX_CURSOR_LENGTH)
            self.assertRegex(cursor, re.compile(r"^[A-Za-z0-9_.-]+$"))
            self.assertEqual(decode_cursor(cursor, "name", fingerprint), [name, 1])

    def test_keyset_order_ties_anchors_and_limit_plus_one(self):
        rows = [
            {"id": 4, "price_cents": 20},
            {"id": 2, "price_cents": 30},
            {"id": 3, "price_cents": 30},
            {"id": 1, "price_cents": 40},
        ]
        page, has_more = keyset_page(rows, ("price_cents", False), None, 2)
        self.assertEqual(
            [(row["price_cents"], row["id"]) for row in page], [(20, 4), (30, 2)]
        )
        self.assertTrue(has_more)
        page, has_more = keyset_page(rows, ("price_cents", True), None, 2)
        self.assertEqual(
            [(row["price_cents"], row["id"]) for row in page], [(40, 1), (30, 2)]
        )
        self.assertTrue(has_more)
        page, has_more = keyset_page(rows, ("price_cents", True), [30, 2], 2)
        self.assertEqual(
            [(row["price_cents"], row["id"]) for row in page], [(30, 3), (20, 4)]
        )
        self.assertFalse(has_more)

    def test_keyset_id_desc_and_missing_anchor_continue_by_value(self):
        rows = [{"id": row_id} for row_id in (1, 2, 4, 5, 6)]
        page, has_more = keyset_page(rows, ("id", True), None, 2)
        self.assertEqual([row["id"] for row in page], [6, 5])
        self.assertTrue(has_more)
        page, has_more = keyset_page(rows, ("id", True), [5, 5], 2)
        self.assertEqual([row["id"] for row in page], [4, 2])
        self.assertTrue(has_more)
        page, has_more = keyset_page(rows, ("id", True), [2, 2], 2)
        self.assertEqual([row["id"] for row in page], [1])
        self.assertFalse(has_more)
        page, has_more = keyset_page(rows, ("id", False), [3, 3], 2)
        self.assertEqual([row["id"] for row in page], [4, 5])
        self.assertTrue(has_more)


if __name__ == "__main__":
    unittest.main()
