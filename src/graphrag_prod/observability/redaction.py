"""Fail-closed redaction helpers for operational telemetry.

Observability is not an alternate data export path.  These helpers therefore
prefer losing diagnostic detail over retaining request, source, or credential
material.  The returned values are bounded and JSON-serializable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import re
from typing import Any


REDACTED = "[REDACTED]"
TRUNCATED = "[TRUNCATED]"

_MAX_DEPTH = 8
_MAX_ITEMS = 100
_MAX_STRING_LENGTH = 2_048

# Key matching is deliberately broader than credential matching.  A false
# positive only removes a field from telemetry; a false negative can leak it.
_SECRET_KEY_RE = re.compile(
    r"(?:^|[_\-.])(?:"
    r"authorization|proxy[_-]?authorization|cookie|set[_-]?cookie|"
    r"password|passwd|pwd|secret|client[_-]?secret|private[_-]?key|"
    r"api[_-]?key|access[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|auth[_-]?token|token|session(?:[_-]?id)?|credential|bearer"
    r")(?:$|[_\-.])",
    re.IGNORECASE,
)

# Raw protected material is excluded even when it is not itself a credential.
# Identifiers and checksums are safe when named precisely (for example
# ``chunk_id``); broad payload fields are not.
_PROTECTED_CONTENT_KEY_RE = re.compile(
    r"(?:^|[_\-.])(?:"
    r"quer(?:y|ies)|questions?|request[_-]?body|response[_-]?body|body|"
    r"payload|prompts?|messages?|completion|contexts?|contents?|text|answers?|"
    r"sources?|source[_-]?text|chunks?|chunk[_-]?text|documents?|"
    r"document[_-]?text|embeddings?|vectors?|user[_-]?input|model[_-]?output|"
    r"claims?|conflicts?|evidence|quotes?|citations?|exception|stack|traceback"
    r")(?:$|[_\-.])",
    re.IGNORECASE,
)

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SAFE_IDENTIFIER_KEY_RE = re.compile(
    r"^(?:chunk|document|source|version|citation)_(?:id|checksum)$",
    re.IGNORECASE,
)
_URI_CREDENTIAL_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s/:@]+):([^\s/@]+)@"
)
_URI_USERINFO_RE = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s/@]+)@"
)
_AUTH_SCHEME_RE = re.compile(
    r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}"
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"
)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-(?:proj-)?|gh[opsu]_|xox[baprs]-|AKIA|ASIA)"
    r"[A-Za-z0-9_\-]{8,}\b",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|password|passwd|pwd|secret|client[_-]?secret|"
    r"api[_-]?key|access[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"auth[_-]?token|token)"
    r"(\s*[:=]\s*)([^\s,;&]+)"
)
_SECRET_QUERY_PARAMETER_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|password|secret)=)"
    r"([^&#\s]+)"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
    r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.DOTALL,
)


def _clean_control_characters(value: str) -> str:
    """Replace controls so one record cannot forge another log line."""

    return _CONTROL_RE.sub(" ", value)


def _redact_text(value: str, *, max_length: int) -> str:
    value = _clean_control_characters(value)
    value = _URI_CREDENTIAL_RE.sub(r"\1[REDACTED]@", value)
    value = _URI_USERINFO_RE.sub(r"\1[REDACTED]@", value)
    value = _AUTH_SCHEME_RE.sub(r"\1 [REDACTED]", value)
    value = _JWT_RE.sub(REDACTED, value)
    value = _KNOWN_TOKEN_RE.sub(REDACTED, value)
    value = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", value)
    value = _SECRET_QUERY_PARAMETER_RE.sub(r"\1[REDACTED]", value)
    value = _PRIVATE_KEY_RE.sub(REDACTED, value)
    if len(value) > max_length:
        return f"{value[:max_length]}{TRUNCATED}"
    return value


def is_sensitive_key(key: object) -> bool:
    """Return whether a mapping key names credentials or protected content."""

    original = str(key)
    if _CONTROL_RE.search(original):
        return True
    normalized = original.strip()
    normalized = _CONTROL_RE.sub("_", normalized)
    # Normalize camelCase/PascalCase names such as ``clientSecret`` and
    # ``accessToken`` before matching the delimiter-aware patterns.
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_")
    if _SAFE_IDENTIFIER_KEY_RE.fullmatch(normalized):
        return False
    return bool(
        _SECRET_KEY_RE.search(normalized)
        or _PROTECTED_CONTENT_KEY_RE.search(normalized)
    )


def redact_sensitive(
    value: Any,
    *,
    max_depth: int = _MAX_DEPTH,
    max_items: int = _MAX_ITEMS,
    max_string_length: int = _MAX_STRING_LENGTH,
) -> Any:
    """Recursively redact and bound an arbitrary value for telemetry.

    Mapping keys are sorted to make the result deterministic.  Unknown object
    types are represented by their type name rather than calling ``str`` or
    ``repr`` because those methods commonly include protected state.
    """

    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise ValueError("max_depth must be a non-negative integer")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
        raise ValueError("max_items must be a positive integer")
    if (
        isinstance(max_string_length, bool)
        or not isinstance(max_string_length, int)
        or max_string_length < 1
    ):
        raise ValueError("max_string_length must be a positive integer")

    seen: set[int] = set()

    def visit(item: Any, depth: int) -> Any:
        if item is None or isinstance(item, bool | int):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else str(item)
        if isinstance(item, str):
            return _redact_text(item, max_length=max_string_length)
        if isinstance(item, bytes | bytearray | memoryview):
            return REDACTED
        if depth >= max_depth:
            return TRUNCATED

        item_id = id(item)
        if item_id in seen:
            return REDACTED

        if isinstance(item, Mapping):
            seen.add(item_id)
            try:
                pairs: list[tuple[str, Any, bool]] = []
                for raw_key, raw_value in item.items():
                    key = _redact_text(str(raw_key), max_length=128).strip()
                    if not key:
                        key = "[EMPTY_KEY]"
                    pairs.append((key, raw_value, is_sensitive_key(raw_key)))
                pairs.sort(key=lambda pair: pair[0])
                result: dict[str, Any] = {}
                for key, raw_value, sensitive in pairs[:max_items]:
                    # Duplicate normalized keys are collapsed safely.
                    safe_value = REDACTED if sensitive else visit(raw_value, depth + 1)
                    result[key] = REDACTED if key in result else safe_value
                if len(pairs) > max_items:
                    result[TRUNCATED] = len(pairs) - max_items
                return result
            finally:
                seen.remove(item_id)

        if isinstance(item, set | frozenset):
            seen.add(item_id)
            try:
                safe_items = [visit(child, depth + 1) for child in item]
                safe_items.sort(key=lambda child: repr(child))
                result = safe_items[:max_items]
                if len(safe_items) > max_items:
                    result.append(TRUNCATED)
                return result
            finally:
                seen.remove(item_id)

        if isinstance(item, Sequence):
            seen.add(item_id)
            try:
                result = [visit(child, depth + 1) for child in item[:max_items]]
                if len(item) > max_items:
                    result.append(TRUNCATED)
                return result
            finally:
                seen.remove(item_id)

        return f"<{type(item).__name__}>"

    return visit(value, 0)


def safe_label(value: object, *, max_length: int = 128) -> str:
    """Return a bounded, single-line label with embedded credentials removed."""

    if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 1:
        raise ValueError("max_length must be a positive integer")
    return _redact_text(str(value), max_length=max_length).strip()
