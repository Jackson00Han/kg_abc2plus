"""Single-line structured JSON logs with a fail-closed field allowlist."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
import json
import re
import sys
from threading import RLock
from typing import Any, TextIO

from graphrag_prod.observability.redaction import REDACTED, redact_sensitive, safe_label


LOG_FIELD_ALLOWLIST = frozenset(
    {
        "request_id",
        "trace_id",
        "route",
        "method",
        "status",
        "error_code",
        "duration_ms",
    }
)
_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_EVENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_ERROR_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9_./{}:-]{0,127}$")
_MAX_DURATION_MS = 86_400_000.0


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _timestamp(value: datetime | str | None, clock: Callable[[], datetime]) -> str:
    if value is None:
        value = clock()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return safe_label(value, max_length=64)


def _safe_field(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name == "duration_ms":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return REDACTED
        number = float(value)
        if not 0.0 <= number <= _MAX_DURATION_MS:
            return REDACTED
        if number != number:
            return REDACTED
        return round(number, 6)
    if name == "status":
        if isinstance(value, bool):
            return REDACTED
        if isinstance(value, int) and 100 <= value <= 599:
            return value
        return REDACTED
    if name == "method":
        method = safe_label(value, max_length=16).upper()
        return method if method in _METHODS else "OTHER"
    if name == "route":
        route = safe_label(value, max_length=128).split("?", 1)[0]
        return route if _ROUTE_RE.fullmatch(route) else "<unknown>"
    if name in {"request_id", "trace_id"}:
        identifier = safe_label(value, max_length=256)
        return identifier if _IDENTIFIER_RE.fullmatch(identifier) else REDACTED
    if name == "error_code":
        error_code = safe_label(value, max_length=64)
        return error_code if _ERROR_CODE_RE.fullmatch(error_code) else "unknown_error"
    return redact_sensitive(value, max_depth=3, max_items=10, max_string_length=256)


def build_log_record(
    level: str,
    event: str,
    *,
    service: str = "graphrag-prod",
    timestamp: datetime | str | None = None,
    clock: Callable[[], datetime] = _utc_now,
    fields: Mapping[str, Any] | None = None,
    **candidate_fields: Any,
) -> dict[str, Any]:
    """Build a sanitized log record.

    Unknown fields are intentionally discarded.  Consequently callers cannot
    accidentally log a query, request body, prompt, chunk, or source text.
    """

    normalized_level = safe_label(level, max_length=16).upper()
    if normalized_level not in _LEVELS:
        normalized_level = "INFO"
    safe_service = safe_label(service, max_length=64)
    if not _EVENT_RE.fullmatch(safe_service):
        safe_service = "graphrag-prod"
    safe_event = safe_label(event, max_length=128)
    if not _EVENT_RE.fullmatch(safe_event):
        safe_event = "unknown"
    record: dict[str, Any] = {
        "timestamp": _timestamp(timestamp, clock),
        "level": normalized_level,
        "service": safe_service,
        "event": safe_event,
    }
    supplied = dict(fields or {})
    supplied.update(candidate_fields)
    for name in sorted(LOG_FIELD_ALLOWLIST):
        if name in supplied and supplied[name] is not None:
            record[name] = _safe_field(name, supplied[name])
    return record


def json_log_line(
    level: str,
    event: str,
    *,
    service: str = "graphrag-prod",
    timestamp: datetime | str | None = None,
    clock: Callable[[], datetime] = _utc_now,
    fields: Mapping[str, Any] | None = None,
    **candidate_fields: Any,
) -> str:
    """Render one compact JSON record without literal control characters."""

    record = build_log_record(
        level,
        event,
        service=service,
        timestamp=timestamp,
        clock=clock,
        fields=fields,
        **candidate_fields,
    )
    return json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


class StructuredJsonLogger:
    """A minimal thread-safe JSON logger for the API boundary."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        service: str = "graphrag-prod",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        normalized_service = safe_label(service, max_length=64)
        self._service = (
            normalized_service
            if _EVENT_RE.fullmatch(normalized_service)
            else "graphrag-prod"
        )
        self._clock = clock
        self._lock = RLock()

    def log(self, level: str, event: str, **fields: Any) -> dict[str, Any]:
        record = build_log_record(
            level,
            event,
            service=self._service,
            clock=self._clock,
            fields=fields,
        )
        line = json.dumps(
            record, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        with self._lock:
            self._stream.write(f"{line}\n")
            self._stream.flush()
        return record

    def debug(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log("DEBUG", event, **fields)

    def info(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log("INFO", event, **fields)

    def warning(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log("WARNING", event, **fields)

    def error(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log("ERROR", event, **fields)

    def critical(self, event: str, **fields: Any) -> dict[str, Any]:
        return self.log("CRITICAL", event, **fields)
