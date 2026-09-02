"""Bounded execution, retry, error, and rate-limit primitives for the API.

The HTTP layer is intentionally kept out of this module.  A route constructs an
``OperationEnvelope`` from server-authenticated identity, then hands it to a
``BoundedOperationRunner``.  This keeps concurrency and retry behaviour
identical for HTTP, command-line, and end-to-end test callers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from enum import Enum
import math
import threading
import time
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable


def _required_text(value: str, name: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    if any(character in normalized for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{name} contains a forbidden control character")
    return normalized


def _positive_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _finite_nonnegative(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return normalized


def _finite_positive(value: float, name: str) -> float:
    normalized = _finite_nonnegative(value, name)
    if normalized <= 0.0:
        raise ValueError(f"{name} must be positive")
    return normalized


class OperationKind(str, Enum):
    """Supported application operations and their retry safety boundary."""

    INGESTION = "ingestion"
    DELETION = "deletion"
    JOB_STATUS = "job_status"
    RETRIEVAL = "retrieval"
    ANSWER = "answer"
    HEALTH = "health"
    READINESS = "readiness"

    @property
    def is_write(self) -> bool:
        return self in {self.INGESTION, self.DELETION}

    @property
    def is_retry_safe(self) -> bool:
        """Only provider-free, side-effect-free reads retry automatically."""
        return self in {
            self.JOB_STATUS,
            self.HEALTH,
            self.READINESS,
        }


_OPERATION_SCOPES = MappingProxyType(
    {
        OperationKind.INGESTION: "documents:ingest",
        OperationKind.DELETION: "documents:delete",
        OperationKind.JOB_STATUS: "jobs:read",
        OperationKind.RETRIEVAL: "retrieval:read",
        OperationKind.ANSWER: "answers:generate",
        OperationKind.HEALTH: "health:probe",
        OperationKind.READINESS: "health:probe",
    }
)


def required_scope(operation: OperationKind) -> str:
    """Return the fixed action permission for one trusted operation kind."""
    if not isinstance(operation, OperationKind):
        raise ValueError("operation is not supported")
    return _OPERATION_SCOPES[operation]


@dataclass(frozen=True, slots=True)
class OperationEnvelope:
    """Server-owned identity and payload passed to a synchronous backend."""

    operation: OperationKind
    request_id: str
    trace_id: str
    principal_id: str
    tenant_id: str
    access_groups: frozenset[str] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.operation, OperationKind):
            try:
                object.__setattr__(self, "operation", OperationKind(self.operation))
            except (TypeError, ValueError) as error:
                raise ValueError("operation is not supported") from error
        for name in ("request_id", "trace_id", "principal_id", "tenant_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        groups = frozenset(
            _required_text(value, "access_group", maximum=128)
            for value in self.access_groups
        )
        object.__setattr__(self, "access_groups", groups)
        scopes = frozenset(
            _required_text(value, "scope", maximum=128) for value in self.scopes
        )
        object.__setattr__(self, "scopes", scopes)
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class UsageMetadata:
    """Non-content operational accounting safe to expose to observability."""

    total_ms: float = 0.0
    retrieval_ms: float = 0.0
    generation_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    estimated_cost_usd: float = 0.0
    stages: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        for name in ("total_ms", "retrieval_ms", "generation_ms"):
            object.__setattr__(
                self,
                name,
                _finite_nonnegative(getattr(self, name), name),
            )
        for name in ("input_tokens", "output_tokens", "model_calls"):
            _nonnegative_integer(getattr(self, name), name)
        object.__setattr__(
            self,
            "estimated_cost_usd",
            _finite_nonnegative(self.estimated_cost_usd, "estimated_cost_usd"),
        )
        normalized_stages: list[tuple[str, float]] = []
        if len(self.stages) > 32:
            raise ValueError("usage stages cannot contain more than 32 entries")
        seen: set[str] = set()
        for entry in self.stages:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("each usage stage must be a (name, milliseconds) tuple")
            name = _required_text(entry[0], "stage name", maximum=64)
            if name in seen:
                raise ValueError(f"duplicate usage stage: {name}")
            seen.add(name)
            normalized_stages.append(
                (name, _finite_nonnegative(entry[1], f"stage {name} duration"))
            )
        object.__setattr__(self, "stages", tuple(normalized_stages))


@dataclass(frozen=True, slots=True)
class BackendResult:
    """Result returned by a backend operation."""

    payload: Any
    usage: UsageMetadata = field(default_factory=UsageMetadata)

    def __post_init__(self) -> None:
        if not isinstance(self.usage, UsageMetadata):
            raise ValueError("usage must be UsageMetadata")


@runtime_checkable
class Backend(Protocol):
    """Synchronous backend contract executed inside the bounded worker pool."""

    def execute(self, envelope: OperationEnvelope, /) -> BackendResult: ...


class ErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    DEPENDENCY_TIMEOUT = "dependency_timeout"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    OVERLOADED = "overloaded"
    RUNTIME_CLOSED = "runtime_closed"
    INTERNAL = "internal_error"


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    code: ErrorCode
    status_code: int
    public_message: str
    retryable: bool = False
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, ErrorCode):
            raise ValueError("code must be ErrorCode")
        if not 400 <= self.status_code <= 599:
            raise ValueError("status_code must be an HTTP error status")
        object.__setattr__(
            self,
            "public_message",
            _required_text(self.public_message, "public_message", maximum=512),
        )
        if self.retry_after_seconds is not None:
            object.__setattr__(
                self,
                "retry_after_seconds",
                _finite_positive(self.retry_after_seconds, "retry_after_seconds"),
            )


class ApiRuntimeError(RuntimeError):
    """A deliberately public, classified application error."""

    code = ErrorCode.INTERNAL
    status_code = 500
    default_message = "the request could not be completed"
    retryable = False

    def __init__(
        self,
        public_message: str | None = None,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        message = _required_text(
            public_message or self.default_message,
            "public_message",
            maximum=512,
        )
        if retry_after_seconds is not None:
            retry_after_seconds = _finite_positive(
                retry_after_seconds,
                "retry_after_seconds",
            )
        self.public_message = message
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)

    @property
    def info(self) -> ErrorInfo:
        return ErrorInfo(
            code=self.code,
            status_code=self.status_code,
            public_message=self.public_message,
            retryable=self.retryable,
            retry_after_seconds=self.retry_after_seconds,
        )


class RequestValidationError(ApiRuntimeError):
    code = ErrorCode.INVALID_REQUEST
    status_code = 422
    default_message = "the request is invalid"


class AuthenticationError(ApiRuntimeError):
    code = ErrorCode.UNAUTHENTICATED
    status_code = 401
    default_message = "authentication is required"


class AuthorizationError(ApiRuntimeError):
    code = ErrorCode.FORBIDDEN
    status_code = 403
    default_message = "the operation is not permitted"


class ResourceNotFoundError(ApiRuntimeError):
    code = ErrorCode.NOT_FOUND
    status_code = 404
    default_message = "the requested resource was not found"


class ConflictError(ApiRuntimeError):
    code = ErrorCode.CONFLICT
    status_code = 409
    default_message = "the operation conflicts with current state"


class RateLimitExceeded(ApiRuntimeError):
    code = ErrorCode.RATE_LIMITED
    status_code = 429
    default_message = "the request rate limit was exceeded"
    retryable = True


class RetryableBackendError(ApiRuntimeError):
    """Explicit marker: only this family is eligible for read retries."""

    code = ErrorCode.DEPENDENCY_UNAVAILABLE
    status_code = 503
    default_message = "a dependency is temporarily unavailable"
    retryable = True


class DependencyUnavailableError(RetryableBackendError):
    pass


class DependencyTimeoutError(ApiRuntimeError):
    code = ErrorCode.DEPENDENCY_TIMEOUT
    status_code = 504
    default_message = "a dependency did not respond before the deadline"


class RuntimeOverloadedError(ApiRuntimeError):
    code = ErrorCode.OVERLOADED
    status_code = 503
    default_message = "the service is at its bounded concurrency limit"
    retryable = True


class RuntimeClosedError(ApiRuntimeError):
    code = ErrorCode.RUNTIME_CLOSED
    status_code = 503
    default_message = "the service is shutting down"


_PUBLIC_ERROR_MESSAGES = MappingProxyType(
    {
        ErrorCode.INVALID_REQUEST: RequestValidationError.default_message,
        ErrorCode.UNAUTHENTICATED: AuthenticationError.default_message,
        ErrorCode.FORBIDDEN: AuthorizationError.default_message,
        ErrorCode.NOT_FOUND: ResourceNotFoundError.default_message,
        ErrorCode.CONFLICT: ConflictError.default_message,
        ErrorCode.RATE_LIMITED: RateLimitExceeded.default_message,
        ErrorCode.DEPENDENCY_TIMEOUT: DependencyTimeoutError.default_message,
        ErrorCode.DEPENDENCY_UNAVAILABLE: DependencyUnavailableError.default_message,
        ErrorCode.OVERLOADED: RuntimeOverloadedError.default_message,
        ErrorCode.RUNTIME_CLOSED: RuntimeClosedError.default_message,
        ErrorCode.INTERNAL: ApiRuntimeError.default_message,
    }
)


def classify_exception(error: BaseException) -> ErrorInfo:
    """Return a stable public classification without exposing exception text."""

    if isinstance(error, ApiRuntimeError):
        info = error.info
        return replace(
            info,
            public_message=_PUBLIC_ERROR_MESSAGES[info.code],
        )
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return DependencyTimeoutError().info
    return ErrorInfo(
        code=ErrorCode.INTERNAL,
        status_code=500,
        public_message="the request could not be completed",
    )


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """Hard execution and retry bounds for synchronous backends."""

    max_workers: int = 8
    max_queue_size: int = 8
    timeout_seconds: float = 30.0
    max_attempts: int = 3
    initial_backoff_seconds: float = 0.05
    max_backoff_seconds: float = 1.0
    overload_retry_after_seconds: float = 1.0

    def __post_init__(self) -> None:
        _positive_integer(self.max_workers, "max_workers")
        _nonnegative_integer(self.max_queue_size, "max_queue_size")
        object.__setattr__(
            self,
            "timeout_seconds",
            _finite_positive(self.timeout_seconds, "timeout_seconds"),
        )
        _positive_integer(self.max_attempts, "max_attempts")
        object.__setattr__(
            self,
            "initial_backoff_seconds",
            _finite_nonnegative(
                self.initial_backoff_seconds,
                "initial_backoff_seconds",
            ),
        )
        object.__setattr__(
            self,
            "max_backoff_seconds",
            _finite_nonnegative(self.max_backoff_seconds, "max_backoff_seconds"),
        )
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError(
                "max_backoff_seconds must not be less than initial_backoff_seconds"
            )
        object.__setattr__(
            self,
            "overload_retry_after_seconds",
            _finite_positive(
                self.overload_retry_after_seconds,
                "overload_retry_after_seconds",
            ),
        )


Sleeper = Callable[[float], Awaitable[None]]


def _consume_async_future(future: asyncio.Future[Any]) -> None:
    """Retrieve a late exception after the caller has timed out or cancelled."""

    if future.cancelled():
        return
    try:
        future.exception()
    except (asyncio.CancelledError, RuntimeError):
        pass


class BoundedOperationRunner:
    """Run synchronous work with a hard worker+queue capacity.

    The capacity permit is released by the *concurrent* future's completion
    callback.  It is deliberately not released when an awaiting coroutine
    times out or is cancelled, because the underlying thread is still doing
    work at that point.
    """

    def __init__(
        self,
        backend: Backend,
        *,
        policy: RuntimePolicy | None = None,
        sleeper: Sleeper = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(backend, Backend):
            raise ValueError("backend must implement execute(envelope)")
        if not callable(sleeper):
            raise ValueError("sleeper must be callable")
        if not callable(monotonic):
            raise ValueError("monotonic must be callable")
        self.backend = backend
        self.policy = policy or RuntimePolicy()
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._executor = ThreadPoolExecutor(
            max_workers=self.policy.max_workers,
            thread_name_prefix="graphrag-api",
        )
        self._capacity = threading.BoundedSemaphore(
            self.policy.max_workers + self.policy.max_queue_size
        )
        self._state_lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def _ensure_open(self) -> None:
        if self.closed:
            raise RuntimeClosedError()

    def _release_capacity(self, future: Future[BackendResult]) -> None:
        del future
        self._capacity.release()

    async def _execute_once(
        self,
        envelope: OperationEnvelope,
        *,
        timeout_seconds: float | None = None,
    ) -> BackendResult:
        self._ensure_open()
        if not self._capacity.acquire(blocking=False):
            raise RuntimeOverloadedError(
                retry_after_seconds=self.policy.overload_retry_after_seconds
            )
        try:
            self._ensure_open()
            concurrent_future = self._executor.submit(self.backend.execute, envelope)
        except BaseException:
            self._capacity.release()
            raise

        concurrent_future.add_done_callback(self._release_capacity)
        async_future = asyncio.wrap_future(concurrent_future)
        try:
            result = await asyncio.wait_for(
                asyncio.shield(async_future),
                timeout=(
                    self.policy.timeout_seconds
                    if timeout_seconds is None
                    else timeout_seconds
                ),
            )
        except TimeoutError as error:
            # A task which has not started can be cancelled without side
            # effects.  A running task returns ``False`` here and remains
            # capacity-accounted until its real completion callback runs.
            concurrent_future.cancel()
            async_future.add_done_callback(_consume_async_future)
            raise DependencyTimeoutError() from error
        except asyncio.CancelledError:
            async_future.add_done_callback(_consume_async_future)
            raise
        if not isinstance(result, BackendResult):
            raise TypeError("backend execute() must return BackendResult")
        return result

    async def run(self, envelope: OperationEnvelope) -> BackendResult:
        if not isinstance(envelope, OperationEnvelope):
            raise RequestValidationError("operation envelope is invalid")
        self._ensure_open()
        started = self._monotonic()
        deadline = started + self.policy.timeout_seconds
        attempts = (
            self.policy.max_attempts
            if envelope.operation.is_retry_safe
            else 1
        )
        for attempt in range(1, attempts + 1):
            try:
                remaining = deadline - self._monotonic()
                if remaining <= 0.0:
                    raise DependencyTimeoutError()
                result = await self._execute_once(
                    envelope,
                    timeout_seconds=remaining,
                )
                elapsed_ms = max(0.0, (self._monotonic() - started) * 1000.0)
                return replace(
                    result,
                    usage=replace(result.usage, total_ms=elapsed_ms),
                )
            except RetryableBackendError:
                if attempt >= attempts:
                    raise
                delay = min(
                    self.policy.initial_backoff_seconds * (2 ** (attempt - 1)),
                    self.policy.max_backoff_seconds,
                )
                if delay > 0.0:
                    if self._monotonic() + delay >= deadline:
                        raise DependencyTimeoutError()
                    await self._sleeper(delay)
        raise AssertionError("operation retry loop exhausted without a result")

    def close(self, *, wait: bool = True) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    async def aclose(self, *, wait: bool = True) -> None:
        await asyncio.to_thread(self.close, wait=wait)

    def __enter__(self) -> BoundedOperationRunner:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()


class RateLimitAlgorithm(str, Enum):
    FIXED_WINDOW = "fixed_window"
    TOKEN_BUCKET = "token_bucket"


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    requests: int = 60
    window_seconds: float = 60.0
    algorithm: RateLimitAlgorithm = RateLimitAlgorithm.TOKEN_BUCKET
    burst_capacity: int | None = None
    max_principals: int = 10_000

    def __post_init__(self) -> None:
        _positive_integer(self.requests, "requests")
        object.__setattr__(
            self,
            "window_seconds",
            _finite_positive(self.window_seconds, "window_seconds"),
        )
        if not isinstance(self.algorithm, RateLimitAlgorithm):
            try:
                object.__setattr__(
                    self,
                    "algorithm",
                    RateLimitAlgorithm(self.algorithm),
                )
            except (TypeError, ValueError) as error:
                raise ValueError("rate-limit algorithm is not supported") from error
        capacity = self.requests if self.burst_capacity is None else self.burst_capacity
        _positive_integer(capacity, "burst_capacity")
        if (
            self.algorithm is RateLimitAlgorithm.FIXED_WINDOW
            and capacity != self.requests
        ):
            raise ValueError(
                "fixed_window burst_capacity must equal the requests limit"
            )
        object.__setattr__(self, "burst_capacity", capacity)
        _positive_integer(self.max_principals, "max_principals")


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: float
    reset_after_seconds: float


@dataclass(slots=True)
class _RateState:
    tokens_or_used: float
    updated_at: float


class PrincipalRateLimiter:
    """Thread-safe per-principal fixed-window or token-bucket limiter."""

    def __init__(
        self,
        policy: RateLimitPolicy | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(monotonic):
            raise ValueError("monotonic must be callable")
        self.policy = policy or RateLimitPolicy()
        self._monotonic = monotonic
        self._states: dict[str, _RateState] = {}
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _prune_inactive(self, now: float) -> None:
        stale: list[str] = []
        for principal_id, state in self._states.items():
            elapsed = max(0.0, now - state.updated_at)
            if self.policy.algorithm is RateLimitAlgorithm.FIXED_WINDOW:
                inactive = elapsed >= self.policy.window_seconds
            else:
                refill_rate = self.policy.requests / self.policy.window_seconds
                inactive = (
                    state.tokens_or_used + elapsed * refill_rate
                    >= self.policy.burst_capacity
                )
            if inactive:
                stale.append(principal_id)
        for principal_id in stale:
            self._states.pop(principal_id, None)

    def check(self, principal_id: str, *, cost: int = 1) -> RateLimitDecision:
        key = _required_text(principal_id, "principal_id")
        _positive_integer(cost, "cost")
        if cost > self.policy.burst_capacity:
            raise ValueError("cost must not exceed burst_capacity")
        now = _finite_nonnegative(self._monotonic(), "monotonic clock")
        with self._lock:
            if self._closed:
                raise RuntimeClosedError()
            state = self._states.get(key)
            if state is None:
                if len(self._states) >= self.policy.max_principals:
                    self._prune_inactive(now)
                if len(self._states) >= self.policy.max_principals:
                    raise RuntimeOverloadedError(
                        "the rate-limit identity capacity was reached",
                        retry_after_seconds=self.policy.window_seconds,
                    )
                if self.policy.algorithm is RateLimitAlgorithm.FIXED_WINDOW:
                    state = _RateState(tokens_or_used=0.0, updated_at=now)
                else:
                    state = _RateState(
                        tokens_or_used=float(self.policy.burst_capacity),
                        updated_at=now,
                    )
                self._states[key] = state

            if self.policy.algorithm is RateLimitAlgorithm.FIXED_WINDOW:
                elapsed = max(0.0, now - state.updated_at)
                if elapsed >= self.policy.window_seconds:
                    state.tokens_or_used = 0.0
                    state.updated_at = now
                    elapsed = 0.0
                reset_after = max(0.0, self.policy.window_seconds - elapsed)
                if state.tokens_or_used + cost <= self.policy.requests:
                    state.tokens_or_used += cost
                    return RateLimitDecision(
                        allowed=True,
                        limit=self.policy.requests,
                        remaining=max(
                            0,
                            self.policy.requests - int(state.tokens_or_used),
                        ),
                        retry_after_seconds=0.0,
                        reset_after_seconds=reset_after,
                    )
                return RateLimitDecision(
                    allowed=False,
                    limit=self.policy.requests,
                    remaining=0,
                    retry_after_seconds=reset_after,
                    reset_after_seconds=reset_after,
                )

            refill_rate = self.policy.requests / self.policy.window_seconds
            elapsed = max(0.0, now - state.updated_at)
            tokens = min(
                float(self.policy.burst_capacity),
                state.tokens_or_used + elapsed * refill_rate,
            )
            state.updated_at = now
            if tokens + 1e-12 >= cost:
                state.tokens_or_used = tokens - cost
                return RateLimitDecision(
                    allowed=True,
                    limit=self.policy.burst_capacity,
                    remaining=max(0, math.floor(state.tokens_or_used)),
                    retry_after_seconds=0.0,
                    reset_after_seconds=(
                        self.policy.burst_capacity - state.tokens_or_used
                    )
                    / refill_rate,
                )
            state.tokens_or_used = tokens
            retry_after = (cost - tokens) / refill_rate
            return RateLimitDecision(
                allowed=False,
                limit=self.policy.burst_capacity,
                remaining=0,
                retry_after_seconds=retry_after,
                reset_after_seconds=(self.policy.burst_capacity - tokens)
                / refill_rate,
            )

    def require(self, principal_id: str, *, cost: int = 1) -> RateLimitDecision:
        decision = self.check(principal_id, cost=cost)
        if not decision.allowed:
            raise RateLimitExceeded(
                retry_after_seconds=max(decision.retry_after_seconds, 1e-9)
            )
        return decision

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._states.clear()

    def __enter__(self) -> PrincipalRateLimiter:
        if self.closed:
            raise RuntimeClosedError()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()
