"""HTTP representation negotiation helpers with bounded header parsing."""

import re
from collections.abc import Iterable


MIN_GZIP_BYTES = 256
_MAX_HEADER_LENGTH = 8192
_MAX_ITEMS = 100
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_QVALUE = re.compile(r"^(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)$")


def _split_quoted(value: str, delimiter: str) -> list[str] | None:
    """Split a bounded header value while respecting quoted strings."""
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
            if len(parts) >= _MAX_ITEMS:
                return None
            parts.append(value[start:index])
            start = index + 1
    if quoted or escaped:
        return None
    parts.append(value[start:])
    if len(parts) > _MAX_ITEMS:
        return None
    return parts


def _header_items(header: str | None) -> list[str]:
    if not isinstance(header, str) or len(header) > _MAX_HEADER_LENGTH:
        return []
    if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in header):
        return []
    items = _split_quoted(header, ",")
    return items if items is not None else []


def _quality(raw: str) -> float | None:
    if not _QVALUE.fullmatch(raw.strip()):
        return None
    try:
        return float(raw)
    except (OverflowError, ValueError):
        return None


def _parse_parameters(pieces: list[str]) -> tuple[float, dict[str, str]] | None:
    quality = 1.0
    params: dict[str, str] = {}
    seen_quality = False
    for piece in pieces:
        if not piece.strip():
            continue
        if "=" not in piece:
            return None
        name, value = (part.strip() for part in piece.split("=", 1))
        if not _TOKEN.fullmatch(name) or len(value) > 256 or not value:
            return None
        if name.lower() == "q":
            if seen_quality:
                return None
            seen_quality = True
            parsed = _quality(value)
            if parsed is None:
                return None
            quality = parsed
        else:
            if value.startswith('"') or value.endswith('"'):
                if not _valid_quoted_string(value):
                    return None
                value = value[1:-1]
            elif not _TOKEN.fullmatch(value):
                return None
            params[name.lower()] = value
    return quality, params


def _valid_quoted_string(value: str) -> bool:
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return False
    escaped = False
    for char in value[1:-1]:
        codepoint = ord(char)
        if (codepoint < 32 and char != "\t") or codepoint == 127:
            return False
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return False
    return not escaped


def parse_accept(header: str | None) -> list[tuple[str, float, dict[str, str]]]:
    """Parse Accept members, ignoring malformed items and bounding work."""
    parsed: list[tuple[str, float, dict[str, str]]] = []
    for item in _header_items(header):
        segments = _split_quoted(item, ";")
        if segments is None:
            continue
        media_range = segments[0].strip().lower()
        if "/" not in media_range:
            continue
        major, minor = media_range.split("/", 1)
        valid_media_range = (major == "*" and minor == "*") or (
            major != "*"
            and _TOKEN.fullmatch(major)
            and (minor == "*" or (minor != "*" and _TOKEN.fullmatch(minor)))
        )
        if not valid_media_range:
            continue
        result = _parse_parameters(segments[1:])
        if result is None:
            continue
        quality, params = result
        parsed.append((media_range, quality, params))
    return parsed


def _specificity(media_range: str, media_type: str) -> int | None:
    if media_range == media_type:
        return 2
    major = media_type.partition("/")[0]
    if media_range == f"{major}/*":
        return 1
    if media_range == "*/*":
        return 0
    return None


def _effective_quality(
    accept: list[tuple[str, float, dict[str, str]]], media_type: str
) -> tuple[float, int] | None:
    matches = [
        (specificity, quality)
        for media_range, quality, _ in accept
        if (specificity := _specificity(media_range, media_type)) is not None
    ]
    if not matches:
        return None
    specificity = max(item[0] for item in matches)
    return (
        max(
            quality
            for match_specificity, quality in matches
            if match_specificity == specificity
        ),
        specificity,
    )


def best_match(
    accept: list[tuple[str, float, dict[str, str]]], offered: Iterable[str]
) -> str | None:
    """Choose an offered type by effective quality, then specificity/order."""
    best: tuple[float, int, int, str] | None = None
    for order, media_type in enumerate(offered):
        if not isinstance(media_type, str) or "/" not in media_type:
            continue
        effective = _effective_quality(accept, media_type.lower())
        if effective is None:
            continue
        quality, specificity = effective
        candidate = (quality, specificity, -order, media_type)
        if best is None or candidate[:3] > best[:3]:
            best = candidate
    return best[3] if best is not None and best[0] > 0 else None


def prefers_problem(header: str | None) -> bool:
    """Return whether problem+json is preferred at least as much as JSON."""
    parsed = parse_accept(header)
    if not parsed:
        return False
    if all(media_range == "*/*" for media_range, _, _ in parsed):
        return False
    problem = _effective_quality(parsed, "application/problem+json")
    if problem is None or problem[0] <= 0:
        return False
    json_quality = _effective_quality(parsed, "application/json")
    return json_quality is None or problem[0] >= json_quality[0]


def parse_accept_encoding(header: str | None) -> list[tuple[str, float]]:
    """Parse Accept-Encoding into bounded (coding, quality) pairs."""
    parsed: list[tuple[str, float]] = []
    for item in _header_items(header):
        segments = _split_quoted(item, ";")
        if segments is None:
            continue
        coding = segments[0].strip().lower()
        if coding != "*" and not _TOKEN.fullmatch(coding):
            continue
        parameters = _parse_parameters(segments[1:])
        if parameters is None:
            continue
        quality, params = parameters
        if params:
            continue
        parsed.append((coding, quality))
    return parsed


def gzip_acceptable(header: str | None) -> bool:
    """Return whether gzip is explicitly or wildcard accepted (q > 0)."""
    encodings = parse_accept_encoding(header)
    gzip = [quality for coding, quality in encodings if coding == "gzip"]
    if gzip:
        return all(quality > 0 for quality in gzip)
    wildcard = [quality for coding, quality in encodings if coding == "*"]
    return bool(wildcard and max(wildcard) > 0)
