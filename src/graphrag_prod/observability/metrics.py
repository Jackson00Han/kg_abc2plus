"""Thread-safe, bounded in-process operational metrics.

The registry deliberately accepts only low-cardinality operational labels.  It
stores fixed-bucket aggregates, never individual observations or user content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import math
import re
from threading import RLock
from typing import Any

from graphrag_prod.observability.redaction import safe_label


_OVERFLOW = "<overflow>"
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_DURATION_BUCKETS_MS = (
    1.0,
    5.0,
    10.0,
    25.0,
    50.0,
    100.0,
    250.0,
    500.0,
    1_000.0,
    5_000.0,
)
_MAX_DURATION_MS = 86_400_000.0
_MAX_TOKENS_PER_CALL = 1_000_000_000
_MAX_MODEL_CALLS_PER_REPORT = 10_000
_MAX_COST_PER_CALL_USD = Decimal("1000000")
_METRIC_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_ROUTE_RE = re.compile(r"^/[A-Za-z0-9_./{}:-]{0,127}$")


def _positive_limit(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_integer(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _duration(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("duration_ms must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= _MAX_DURATION_MS:
        raise ValueError("duration_ms must be a finite non-negative number")
    return 0.0 if result == 0.0 else result


def _cost(value: int | float | Decimal) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("estimated_cost_usd must be finite and non-negative")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("estimated_cost_usd must be finite and non-negative") from exc
    if not result.is_finite() or not Decimal("0") <= result <= _MAX_COST_PER_CALL_USD:
        raise ValueError("estimated_cost_usd must be finite and non-negative")
    return result


@dataclass(slots=True)
class _DurationAggregate:
    count: int = 0
    total_ms: float = 0.0
    minimum_ms: float | None = None
    maximum_ms: float | None = None
    buckets: list[int] = field(
        default_factory=lambda: [0] * (len(_DURATION_BUCKETS_MS) + 1)
    )

    def observe(self, duration_ms: float) -> None:
        self.count += 1
        self.total_ms += duration_ms
        self.minimum_ms = (
            duration_ms if self.minimum_ms is None else min(self.minimum_ms, duration_ms)
        )
        self.maximum_ms = (
            duration_ms if self.maximum_ms is None else max(self.maximum_ms, duration_ms)
        )
        matched = False
        for index, bound in enumerate(_DURATION_BUCKETS_MS):
            if duration_ms <= bound:
                self.buckets[index] += 1
                matched = True
        if not matched:
            self.buckets[-1] += 1

    def snapshot(self) -> dict[str, Any]:
        labels = [f"le_{bound:g}" for bound in _DURATION_BUCKETS_MS] + ["gt_5000"]
        return {
            "count": self.count,
            "total_ms": round(self.total_ms, 6),
            "min_ms": None if self.minimum_ms is None else round(self.minimum_ms, 6),
            "max_ms": None if self.maximum_ms is None else round(self.maximum_ms, 6),
            "buckets": {
                label: count for label, count in zip(labels, self.buckets, strict=True)
            },
        }


@dataclass(slots=True)
class _RequestAggregate:
    count: int = 0
    error_count: int = 0
    durations: _DurationAggregate = field(default_factory=_DurationAggregate)

    def snapshot(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "error_count": self.error_count,
            "latency_ms": self.durations.snapshot(),
        }


class MetricsRegistry:
    """Bounded operational counters and duration histograms.

    Routes must be route templates (for example ``/jobs/{job_id}``), never raw
    request URLs.  Independent hard caps still prevent accidental unbounded
    cardinality: unseen labels spill into a single ``<overflow>`` bucket.
    """

    def __init__(
        self,
        *,
        max_routes: int = 64,
        max_error_codes: int = 32,
        max_retrieval_stages: int = 16,
    ) -> None:
        self._max_routes = _positive_limit("max_routes", max_routes)
        self._max_error_codes = _positive_limit("max_error_codes", max_error_codes)
        self._max_retrieval_stages = _positive_limit(
            "max_retrieval_stages", max_retrieval_stages
        )
        self._lock = RLock()
        self._request_total = 0
        self._request_error_total = 0
        self._route_labels: set[str] = set()
        self._requests: dict[str, _RequestAggregate] = {}
        self._error_total = 0
        self._errors: dict[str, int] = {}
        self._retrieval_total = _DurationAggregate()
        self._retrieval_stages: dict[str, _DurationAggregate] = {}
        self._model_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._estimated_cost_usd = Decimal("0")

    @staticmethod
    def _bounded_key(
        values: dict[str, Any], raw_key: object, capacity: int, *, fallback: str
    ) -> str:
        key = safe_label(raw_key, max_length=128) or fallback
        if not _METRIC_LABEL_RE.fullmatch(key):
            key = fallback
        if key in values or len(values) < capacity:
            return key
        return _OVERFLOW

    def record_request(
        self,
        route: str,
        method: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        """Record one completed request without retaining request data."""

        if isinstance(status_code, bool) or not isinstance(status_code, int):
            raise ValueError("status_code must be an integer from 100 through 599")
        if not 100 <= status_code <= 599:
            raise ValueError("status_code must be an integer from 100 through 599")
        normalized_method = safe_label(method, max_length=16).upper()
        if normalized_method not in _METHODS:
            normalized_method = "OTHER"
        elapsed = _duration(duration_ms)
        # A route label is a route template only.  Drop a raw query string if a
        # framework integration accidentally supplies the full target.
        route_label = safe_label(route, max_length=128).split("?", 1)[0]
        if not _ROUTE_RE.fullmatch(route_label):
            route_label = "<unknown>"
        route_label = route_label or "<unknown>"
        with self._lock:
            if route_label in self._route_labels:
                route_key = route_label
            elif len(self._route_labels) < self._max_routes:
                self._route_labels.add(route_label)
                route_key = route_label
            else:
                route_key = _OVERFLOW
            key = f"{normalized_method} {route_key}"
            # Capacity is governed by route, not method.  If many unexpected
            # methods appear they are normalized to OTHER above.
            aggregate = self._requests.setdefault(key, _RequestAggregate())
            aggregate.count += 1
            aggregate.durations.observe(elapsed)
            self._request_total += 1
            if status_code >= 400:
                aggregate.error_count += 1
                self._request_error_total += 1

    def record_error(self, error_code: str) -> None:
        """Record a categorized application/dependency error."""

        with self._lock:
            key = self._bounded_key(
                self._errors,
                error_code,
                self._max_error_codes,
                fallback="unknown_error",
            )
            self._errors[key] = self._errors.get(key, 0) + 1
            self._error_total += 1

    def record_retrieval_stage(self, stage: str, duration_ms: float) -> None:
        """Record one bounded retrieval-stage duration."""

        elapsed = _duration(duration_ms)
        with self._lock:
            key = self._bounded_key(
                self._retrieval_stages,
                stage,
                self._max_retrieval_stages,
                fallback="unknown",
            )
            self._retrieval_stages.setdefault(key, _DurationAggregate()).observe(elapsed)
            self._retrieval_total.observe(elapsed)

    def record_model_call(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_usd: int | float | Decimal = 0,
    ) -> None:
        """Record provider usage without model prompts, outputs, or model IDs."""

        self._record_model_usage(
            model_calls=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )

    def _record_model_usage(
        self,
        *,
        model_calls: int,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: int | float | Decimal,
    ) -> None:
        checked_calls = _non_negative_integer("model_calls", model_calls)
        checked_input = _non_negative_integer("input_tokens", input_tokens)
        checked_output = _non_negative_integer("output_tokens", output_tokens)
        if (
            checked_calls > _MAX_MODEL_CALLS_PER_REPORT
            or checked_input > _MAX_TOKENS_PER_CALL
            or checked_output > _MAX_TOKENS_PER_CALL
        ):
            raise ValueError("model usage exceeds the per-report telemetry bound")
        checked_cost = _cost(estimated_cost_usd)
        if checked_calls == 0 and (
            checked_input != 0 or checked_output != 0 or checked_cost != 0
        ):
            raise ValueError("zero model calls cannot report tokens or cost")
        with self._lock:
            self._model_calls += checked_calls
            self._input_tokens += checked_input
            self._output_tokens += checked_output
            self._estimated_cost_usd += checked_cost

    def record_model_usage(
        self,
        *,
        model_calls: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_usd: int | float | Decimal = 0,
    ) -> None:
        """Keyword-only alias for integrations that report usage objects."""

        self._record_model_usage(
            model_calls=model_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )

    def snapshot(self) -> dict[str, Any]:
        """Return a deterministic, JSON-ready point-in-time snapshot."""

        with self._lock:
            return {
                "requests": {
                    "total": self._request_total,
                    "error_count": self._request_error_total,
                    "by_route": {
                        key: self._requests[key].snapshot()
                        for key in sorted(self._requests)
                    },
                },
                "errors": {
                    "total": self._error_total,
                    "by_code": {key: self._errors[key] for key in sorted(self._errors)},
                },
                "retrieval": {
                    "total": self._retrieval_total.snapshot(),
                    "by_stage": {
                        key: self._retrieval_stages[key].snapshot()
                        for key in sorted(self._retrieval_stages)
                    },
                },
                "model": {
                    "calls": self._model_calls,
                    "input_tokens": self._input_tokens,
                    "output_tokens": self._output_tokens,
                    "estimated_cost_usd": float(
                        self._estimated_cost_usd.quantize(Decimal("0.000000000001"))
                    ),
                },
            }
