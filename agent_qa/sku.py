"""Helpers for normalizing SKU values."""

import re


def normalize_sku(raw: str) -> str:
    """Return a normalized SKU with separators represented by hyphens."""
    if not isinstance(raw, str):
        raise ValueError("invalid sku")

    normalized = re.sub(r"-+", "-", re.sub(r"[\s_]+", "-", raw.strip().upper()))
    if not normalized:
        raise ValueError("invalid sku")
    return normalized
