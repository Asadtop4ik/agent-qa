import io
import unittest

from scripts.receive_limited_archive import copy_limited


class ReceiveLimitedArchiveTests(unittest.TestCase):
    def test_accepts_archive_at_limit(self):
        source = io.BytesIO(b"123456")
        destination = io.BytesIO()

        copied = copy_limited(source, destination, 6)

        self.assertEqual(copied, 6)
        self.assertEqual(destination.getvalue(), b"123456")

    def test_rejects_oversized_archive_before_writing_beyond_limit(self):
        class ChunkedReader(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 3) if size >= 0 else 3)

        source = ChunkedReader(b"123456" + b"x" * 100_000)
        destination = io.BytesIO()

        with self.assertRaisesRegex(ValueError, "exceeds 6 bytes"):
            copy_limited(source, destination, 6)

        self.assertEqual(destination.getvalue(), b"123456")
        self.assertEqual(source.tell(), 7)

    def test_rejects_negative_limit(self):
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            copy_limited(io.BytesIO(b"1"), io.BytesIO(), -1)


if __name__ == "__main__":
    unittest.main()
