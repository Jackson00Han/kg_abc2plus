"""Validated Neo4j driver lifecycle and transaction resource boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import math
import re
import threading
from typing import Any, Literal
from urllib.parse import urlsplit

from neo4j import GraphDatabase, Query, unit_of_work
from neo4j.exceptions import (
    ConnectionAcquisitionTimeoutError,
    DriverError,
    Neo4jError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

from .runtime import (
    ApiRuntimeError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    ErrorCode,
    RuntimeClosedError,
)


_URI_SCHEMES = frozenset(
    {
        "neo4j",
        "neo4j+s",
        "neo4j+ssc",
        "bolt",
        "bolt+s",
        "bolt+ssc",
    }
)
_DATABASE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_TRANSACTION_TIMEOUT_CODES = frozenset(
    {
        "Neo.ClientError.Transaction.TransactionTimedOut",
        "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration",
        "Neo.TransientError.Transaction.TransactionTimedOut",
        "Neo.TransientError.Transaction.TransactionTimedOutClientConfiguration",
        "Neo.ClientError.Transaction.LockAcquisitionTimeout",
        "Neo.TransientError.Transaction.LockAcquisitionTimeout",
    }
)


def _required_text(value: str, name: str, *, maximum: int) -> str:
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


def _finite_positive(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError(f"{name} must be a finite positive number")
    return normalized


def _finite_nonnegative(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return normalized


def _bounded_positive(value: float, name: str, *, maximum: float) -> float:
    normalized = _finite_positive(value, name)
    if normalized > maximum:
        raise ValueError(f"{name} must not exceed {maximum:g}")
    return normalized


def _public_driver_error(error: BaseException) -> ApiRuntimeError:
    """Classify a Neo4j failure without retaining protected driver detail."""

    if isinstance(error, ConnectionAcquisitionTimeoutError):
        return DependencyTimeoutError()
    if (
        isinstance(error, Neo4jError)
        and getattr(error, "code", None) in _TRANSACTION_TIMEOUT_CODES
    ):
        return DependencyTimeoutError()
    if isinstance(error, (ServiceUnavailable, SessionExpired, TransientError)):
        return DependencyUnavailableError()
    if isinstance(error, DriverError):
        return DependencyUnavailableError()
    # Syntax, constraint, and other deterministic server failures are not
    # retryable.  The stable generic 500 response avoids leaking query text,
    # graph identifiers, server addresses, or credentials.
    return ApiRuntimeError()


def _validate_uri(value: str) -> str:
    uri = _required_text(value, "uri", maximum=2048)
    try:
        parsed = urlsplit(uri)
        port = parsed.port
    except ValueError as error:
        raise ValueError("uri is not a valid Neo4j URI") from error
    if parsed.scheme.lower() not in _URI_SCHEMES:
        raise ValueError("uri must use an official Neo4j or Bolt scheme")
    if not parsed.hostname:
        raise ValueError("uri must include a host")
    if "%" in parsed.hostname or any(
        character.isspace() or ord(character) < 32
        for character in parsed.hostname
    ):
        raise ValueError("uri host contains a forbidden character")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("uri must not contain credentials")
    if parsed.path not in ("", "/"):
        raise ValueError("uri must not contain a path")
    if parsed.query or parsed.fragment:
        raise ValueError("uri must not contain query parameters or a fragment")
    if port is not None and not 1 <= port <= 65_535:
        raise ValueError("uri port must be between 1 and 65535")
    return uri


@dataclass(frozen=True, slots=True)
class Neo4jSettings:
    """Strict, secret-safe settings mapped directly to official driver options."""

    uri: str
    database: str
    username: str
    password: str = field(repr=False)
    max_connection_pool_size: int = 50
    connection_acquisition_timeout: float = 30.0
    connection_timeout: float = 10.0
    max_connection_lifetime: float = 3600.0
    max_transaction_retry_time: float = 15.0
    transaction_timeout_seconds: float = 25.0
    readiness_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "uri", _validate_uri(self.uri))
        database = _required_text(self.database, "database", maximum=63)
        if _DATABASE_PATTERN.fullmatch(database) is None:
            raise ValueError(
                "database must start with an alphanumeric character and contain "
                "only alphanumerics, dot, underscore, or hyphen"
            )
        object.__setattr__(self, "database", database)
        object.__setattr__(
            self,
            "username",
            _required_text(self.username, "username", maximum=256),
        )
        if not isinstance(self.password, str):
            raise ValueError("password must be text")
        if not self.password:
            raise ValueError("password must not be empty")
        if len(self.password) > 4096:
            raise ValueError("password must be at most 4096 characters")
        if any(character in self.password for character in ("\x00", "\r", "\n")):
            raise ValueError("password contains a forbidden control character")
        _positive_integer(
            self.max_connection_pool_size,
            "max_connection_pool_size",
        )
        if self.max_connection_pool_size > 1_000:
            raise ValueError("max_connection_pool_size must not exceed 1000")
        for name, maximum in (
            ("connection_acquisition_timeout", 300.0),
            ("connection_timeout", 120.0),
            ("max_connection_lifetime", 86_400.0),
            ("transaction_timeout_seconds", 300.0),
            ("readiness_timeout_seconds", 60.0),
        ):
            object.__setattr__(
                self,
                name,
                _bounded_positive(getattr(self, name), name, maximum=maximum),
            )
        object.__setattr__(
            self,
            "max_transaction_retry_time",
            _finite_nonnegative(
                self.max_transaction_retry_time,
                "max_transaction_retry_time",
            ),
        )
        if self.max_transaction_retry_time > 300.0:
            raise ValueError("max_transaction_retry_time must not exceed 300")
        if self.readiness_timeout_seconds > self.transaction_timeout_seconds:
            raise ValueError(
                "readiness_timeout_seconds must not exceed "
                "transaction_timeout_seconds"
            )


@dataclass(frozen=True, slots=True)
class ResourceProbe:
    component: str
    ok: bool
    status: Literal["healthy", "unhealthy", "ready", "not_ready"]
    error_code: ErrorCode | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "ok": self.ok,
            "status": self.status,
            "error_code": self.error_code.value if self.error_code else None,
        }


class Neo4jResource:
    """Own one configured driver and expose safe managed transactions."""

    def __init__(self, driver: Any, settings: Neo4jSettings) -> None:
        if driver is None:
            raise ValueError("driver must not be None")
        if not isinstance(settings, Neo4jSettings):
            raise ValueError("settings must be Neo4jSettings")
        self._driver = driver
        self.settings = settings
        self._state_lock = threading.Lock()
        self._closed = False

    _DRIVER_ERRORS = (DriverError, Neo4jError)

    @property
    def driver(self) -> Any:
        self._ensure_open()
        return self._driver

    @property
    def database(self) -> str:
        return self.settings.database

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def _ensure_open(self) -> None:
        if self.closed:
            raise RuntimeClosedError("the Neo4j resource is closed")

    def execute_read(
        self,
        work: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute a managed, driver-retryable read transaction."""

        self._ensure_open()
        if not callable(work):
            raise ValueError("work must be callable")
        timed_work = unit_of_work(
            metadata={"component": "graphrag-api", "operation": "read"},
            timeout=self.settings.transaction_timeout_seconds,
        )(work)
        try:
            with self._driver.session(database=self.database) as session:
                return session.execute_read(timed_work, *args, **kwargs)
        except self._DRIVER_ERRORS as error:
            raise _public_driver_error(error) from error

    def execute_write(
        self,
        work: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute exactly one explicit write transaction with a server timeout.

        Durable ingestion operations are idempotent, but an HTTP write is never
        implicitly replayed by this resource.  The caller decides whether a
        recorded operation may be resumed.
        """

        self._ensure_open()
        if not callable(work):
            raise ValueError("work must be callable")
        try:
            with self._driver.session(database=self.database) as session:
                with session.begin_transaction(
                    metadata={"component": "graphrag-api", "operation": "write"},
                    timeout=self.settings.transaction_timeout_seconds,
                ) as transaction:
                    result = work(transaction, *args, **kwargs)
                    transaction.commit()
                    return result
        except self._DRIVER_ERRORS as error:
            raise _public_driver_error(error) from error

    def health_probe(self) -> ResourceProbe:
        """Process liveness: no dependency I/O and no protected detail."""

        if self.closed:
            return ResourceProbe(
                component="neo4j",
                ok=False,
                status="unhealthy",
                error_code=ErrorCode.RUNTIME_CLOSED,
            )
        return ResourceProbe(component="neo4j", ok=True, status="healthy")

    def readiness_probe(self) -> ResourceProbe:
        """Dependency readiness: authenticate, select the DB, and run a query."""

        if self.closed:
            return ResourceProbe(
                component="neo4j",
                ok=False,
                status="not_ready",
                error_code=ErrorCode.RUNTIME_CLOSED,
            )
        try:
            with self._driver.session(database=self.database) as session:
                record = session.run(
                    Query(
                        "RETURN 1 AS ready",
                        metadata={
                            "component": "graphrag-api",
                            "operation": "readiness",
                        },
                        timeout=self.settings.readiness_timeout_seconds,
                    )
                ).single()
            if record is None or record["ready"] != 1:
                raise RuntimeError("Neo4j readiness query returned no marker")
        except Exception:
            return ResourceProbe(
                component="neo4j",
                ok=False,
                status="not_ready",
                error_code=ErrorCode.DEPENDENCY_UNAVAILABLE,
            )
        return ResourceProbe(component="neo4j", ok=True, status="ready")

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._driver.close()

    def __enter__(self) -> Neo4jResource:
        self._ensure_open()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()

    def __repr__(self) -> str:
        return (
            f"Neo4jResource(uri={self.settings.uri!r}, "
            f"database={self.settings.database!r}, closed={self.closed!r})"
        )


DriverFactory = Callable[..., Any]


def create_neo4j_resource(
    settings: Neo4jSettings,
    *,
    driver_factory: DriverFactory = GraphDatabase.driver,
) -> Neo4jResource:
    """Create a resource using documented Neo4j pool and timeout settings."""

    if not isinstance(settings, Neo4jSettings):
        raise ValueError("settings must be Neo4jSettings")
    if not callable(driver_factory):
        raise ValueError("driver_factory must be callable")
    driver = driver_factory(
        settings.uri,
        auth=(settings.username, settings.password),
        max_connection_pool_size=settings.max_connection_pool_size,
        connection_acquisition_timeout=settings.connection_acquisition_timeout,
        connection_timeout=settings.connection_timeout,
        max_connection_lifetime=settings.max_connection_lifetime,
        max_transaction_retry_time=settings.max_transaction_retry_time,
    )
    return Neo4jResource(driver, settings)
