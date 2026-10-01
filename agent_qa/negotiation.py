"""Bounded parsers and matchers for HTTP content negotiation headers."""

from __future__ import annotations

import re
from collections.abc import Iterable

_MAX_HEADER_LENGTH = 8192
_MAX_ITEMS = 100
_MAX_PARAMS = 32
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_QVALUE = re.compile(r"^(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$")
_Parsed = list[tuple[str, float, dict[str, str]]]


def _split_quoted(value: str, delimiter: str) -> list[str] | None:
    parts: list[str] = []
    start = 0
    quoted = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == delimiter and not quoted:
            parts.append(value[start:index])
            start = index + 1
    if quoted or escaped:
        return None
    parts.append(value[start:])
    return parts


def _parse(header: str | None, *, encoding: bool = False) -> _Parsed:
    if not isinstance(header, str) or len(header) > _MAX_HEADER_LENGTH:
        return []
    result: _Parsed = []
    items = _split_quoted(header, ",")
    if items is None:
        items = header.split(",")
    for item in items[:_MAX_ITEMS]:
        parts = _split_quoted(item, ";")
        if parts is None:
            continue
        media_range = parts[0].strip().lower()
        valid_range = (
            _TOKEN.fullmatch(media_range) if encoding else _valid_range(media_range)
        )
        if not valid_range:
            continue
        quality = 1.0
        params: dict[str, str] = {}
        seen_params: set[str] = set()
        valid = True
        if len(parts) - 1 > _MAX_PARAMS:
            continue
        for raw_param in parts[1:]:
            name, separator, value = raw_param.partition("=")
            name = name.strip().lower()
            value = value.strip()
            if (
                not separator
                or not _TOKEN.fullmatch(name)
                or not value
                or len(value) > 256
                or name in seen_params
            ):
                valid = False
                break
            seen_params.add(name)
            if name == "q":
                if not _QVALUE.fullmatch(value):
                    valid = False
                    break
                quality = float(value)
            else:
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    value = re.sub(r"\\(.)", r"\1", value[1:-1])
                    if '"' in value or any(ord(char) < 0x20 for char in value):
                        valid = False
                        break
                elif not _TOKEN.fullmatch(value):
                    valid = False
                    break
                params[name] = value
        if valid:
            result.append((media_range, quality, params))
    return result


def _valid_range(value: str) -> bool:
    if value == "*/*":
        return True
    major, separator, minor = value.partition("/")
    return bool(
        separator
        and _TOKEN.fullmatch(major)
        and (minor == "*" or bool(_TOKEN.fullmatch(minor)))
        and "*" not in major
        and (minor == "*" or "*" not in minor)
    )


def parse_accept(header: str | None) -> _Parsed:
    """Parse valid Accept members as (media range, q, non-q parameters)."""
    return _parse(header)


def parse_accept_encoding(header: str | None) -> _Parsed:
    """Parse valid Accept-Encoding members as (coding, q, parameters)."""
    return _parse(header, encoding=True)


def _parsed(value: str | _Parsed | None) -> _Parsed:
    return parse_accept(value) if isinstance(value, str) or value is None else value


def _effective_quality(
    ranges: _Parsed, offered: str, *, encoding: bool = False
) -> tuple[int, float] | None:
    offered = offered.lower()
    major, _, _ = offered.partition("/")
    matches: list[tuple[int, float]] = []
    for value, quality, _ in ranges:
        value = value.lower()
        if encoding:
            if value == offered:
                matches.append((2, quality))
            elif value == "*":
                matches.append((0, quality))
        elif value == offered:
            matches.append((2, quality))
        elif value == f"{major}/*":
            matches.append((1, quality))
        elif value == "*/*":
            matches.append((0, quality))
    if not matches:
        return None
    specificity = max(rank for rank, _ in matches)
    return specificity, max(q for rank, q in matches if rank == specificity)


def best_match(accept: str | _Parsed | None, offered: Iterable[str]) -> str | None:
    """Choose by effective q, then specificity, then offered order."""
    ranges = _parsed(accept)
    choices = list(offered)
    if len(choices) > _MAX_ITEMS:
        choices = choices[:_MAX_ITEMS]
    best: str | None = None
    best_quality = -1.0
    best_specificity = -1
    for item in choices:
        if (
            not isinstance(item, str)
            or len(item) > 256
            or not _valid_range(item.lower())
        ):
            continue
        match = _effective_quality(ranges, item)
        if match is None:
            continue
        specificity, quality = match
        if (quality, specificity) > (best_quality, best_specificity):
            best = item
            best_quality = quality
            best_specificity = specificity
    return best if best_quality > 0 else None


def gzip_acceptable(header: str | None) -> bool:
    """Return whether gzip has a positive effective quality value."""
    ranges = parse_accept_encoding(header)
    match = _effective_quality(ranges, "gzip", encoding=True)
    return match is not None and match[1] > 0


def prefers_problem(header: str | None) -> bool:
    """Select problem errors on a positive quality tie, except wildcard-only Accept."""
    ranges = parse_accept(header)
    if not ranges or all(value == "*/*" for value, _, _ in ranges):
        return False
    problem = _effective_quality(ranges, "application/problem+json")
    legacy = _effective_quality(ranges, "application/json")
    return (
        problem is not None
        and problem[1] > 0
        and problem[1] >= (legacy[1] if legacy is not None else 0)
    )
