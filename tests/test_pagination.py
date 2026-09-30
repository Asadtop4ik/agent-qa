"""Tests for signed cursor encoding and generic keyset boundaries."""

import base64
import hashlib
import hmac
import unittest
from unittest.mock import patch

from agent_qa.errors import ApiError
from agent_qa.pagination import (
    MAX_CURSOR_LENGTH,
    decode_cursor,
    encode_cursor,
    filter_fingerprint,
    keyset_page,
)


def _signed_raw_cursor(raw: bytes, key: bytes) -> str:
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    signature = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()[:16]
    return f"{encoded}.{signature}"


class CursorTests(unittest.TestCase):
    def test_round_trip_and_query_fingerprint(self):
        fingerprint = filter_fingerprint({"active": True, "category": "tools"})
        token = encode_cursor("-price_cents", fingerprint, (500, 12))
        self.assertEqual(decode_cursor(token, "-price_cents", fingerprint), (500, 12))
        self.assertEqual(
            filter_fingerprint(
                {
                    "active": True,
                    "category": "tools",
                    "limit": 3,
                    "offset": 9,
                    "pagination": "cursor",
                    "cursor": "ignored",
                    "sort": "-price_cents",
                }
            ),
            fingerprint,
        )

    def test_tampering_is_invalid_and_changed_query_is_mismatch(self):
        fingerprint = filter_fingerprint({"category": "tools"})
        token = encode_cursor("id", fingerprint, (5, 5))
        tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
        with self.assertRaises(ApiError) as error:
            decode_cursor(tampered, "id", fingerprint)
        self.assertEqual(error.exception.code, "invalid_cursor")
        with self.assertRaises(ApiError) as error:
            decode_cursor(token, "-id", fingerprint)
        self.assertEqual(error.exception.code, "cursor_mismatch")
        with self.assertRaises(ApiError) as error:
            decode_cursor(token, "id", filter_fingerprint({"category": "other"}))
        self.assertEqual(error.exception.code, "cursor_mismatch")

    def test_malformed_and_oversized_tokens_are_rejected(self):
        fingerprint = filter_fingerprint({})
        for token in (
            "",
            "not-a-cursor",
            "x" * (MAX_CURSOR_LENGTH + 1),
            "@@.0123456789abcdef",
        ):
            with self.subTest(token=token[:24]), self.assertRaises(ApiError) as error:
                decode_cursor(token, "id", fingerprint)
            self.assertEqual(error.exception.code, "invalid_cursor")

    def test_signed_malformed_payloads_are_invalid_cursors(self):
        signing_key = b"unit-test-signing-key"
        fingerprint = filter_fingerprint({})
        payloads = (
            b'{"v":1.0,"s":"id","f":"' + fingerprint.encode("ascii") + b'","k":[1,1]}',
            b'{"v":1,"s":"id","f":"' + fingerprint.encode("ascii") + b'","k":[[],1]}',
            b'{"v":1,"s":"id","f":"'
            + fingerprint.encode("ascii")
            + b'","k":[1,1],"extra":true}',
            b'{"v":1,"s":"id","f":"'
            + fingerprint.encode("ascii")
            + b'","k":[['
            + b"[" * 1200
            + b"0"
            + b"]" * 1200
            + b"],1]}",
            b"{" + b" " * 3072,
        )
        with patch("agent_qa.pagination._CURSOR_KEY", signing_key):
            for raw in payloads:
                token = _signed_raw_cursor(raw, signing_key)
                with self.subTest(raw_length=len(raw)):
                    with self.assertRaises(ApiError) as error:
                        decode_cursor(token, "id", fingerprint)
                    self.assertEqual(error.exception.code, "invalid_cursor")

    def test_cursor_from_previous_process_key_is_invalid(self):
        first_key = b"first-process-cursor-key"
        second_key = b"second-process-cursor-key"
        fingerprint = filter_fingerprint({})
        with patch("agent_qa.pagination._CURSOR_KEY", first_key):
            token = encode_cursor("id", fingerprint, (5, 5))
        with patch("agent_qa.pagination._CURSOR_KEY", second_key):
            with self.assertRaises(ApiError) as error:
                decode_cursor(token, "id", fingerprint)
        self.assertEqual(error.exception.code, "invalid_cursor")

    def test_maximum_unicode_name_cursor_round_trips(self):
        fingerprint = filter_fingerprint({})
        name = "💩" * 120
        token = encode_cursor("name", fingerprint, (name, 1))
        self.assertLessEqual(len(token), MAX_CURSOR_LENGTH)
        self.assertEqual(decode_cursor(token, "name", fingerprint), (name, 1))


class KeysetPageTests(unittest.TestCase):
    def test_ties_use_ascending_ids_for_ascending_and_descending_values(self):
        rows = [
            {"id": 4, "price_cents": 10, "name": "same"},
            {"id": 2, "price_cents": 10, "name": "same"},
            {"id": 3, "price_cents": 20, "name": "other"},
            {"id": 1, "price_cents": 20, "name": "other"},
        ]
        for sort, expected in (
            ("price_cents", [2, 4, 1, 3]),
            ("-price_cents", [1, 3, 2, 4]),
            ("name", [1, 3, 2, 4]),
            ("-name", [2, 4, 1, 3]),
        ):
            with self.subTest(sort=sort):
                cursor_key = None
                seen = []
                while True:
                    page, cursor_key = keyset_page(rows, sort, cursor_key, 2)
                    seen.extend(row["id"] for row in page)
                    if cursor_key is None:
                        break
                self.assertEqual(seen, expected)

    def test_id_sort_respects_direction(self):
        rows = [{"id": value} for value in (1, 3, 2)]
        ascending, _ = keyset_page(rows, "id", None, 3)
        descending, _ = keyset_page(rows, "-id", None, 3)
        self.assertEqual([row["id"] for row in ascending], [1, 2, 3])
        self.assertEqual([row["id"] for row in descending], [3, 2, 1])


if __name__ == "__main__":
    unittest.main()
