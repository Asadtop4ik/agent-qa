#!/usr/bin/env python3
"""Copy an incoming Docker archive to disk without exceeding its byte limit."""

from __future__ import annotations

import argparse
import sys
from typing import BinaryIO

CHUNK_SIZE = 64 * 1024


def copy_limited(source: BinaryIO, destination: BinaryIO, limit: int) -> int:
    """Copy at most ``limit`` bytes, raising before writing an oversized chunk."""
    if limit < 0:
        raise ValueError("archive size limit cannot be negative")

    total = 0
    while True:
        chunk = source.read(min(CHUNK_SIZE, limit - total + 1))
        if not chunk:
            return total
        if total + len(chunk) > limit:
            raise ValueError(f"Image archive exceeds {limit} bytes")
        destination.write(chunk)
        total += len(chunk)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination")
    parser.add_argument("limit", type=int)
    args = parser.parse_args()

    try:
        with open(args.destination, "wb") as output:
            copy_limited(sys.stdin.buffer, output, args.limit)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
