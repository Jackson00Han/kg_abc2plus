"""Unit tests for validated Neo4j resource construction and lifecycle."""

from __future__ import annotations

import math
import unittest
from typing import Any

from neo4j import Query
from neo4j.exceptions import (
    ConnectionAcquisitionTimeoutError,
    Neo4jError,
    ServiceUnavailable,
)

from graphrag_prod.api.resources import (
    Neo4jResource,
    Neo4jSettings,
    create_neo4j_resource,
)
from graphrag_prod.api.runtime import (
    ApiRuntimeError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    ErrorCode,
    RuntimeClosedError,
)


class _FakeResult:
    def __init__(self, record: Any) -> None:
        self.record = record

    def single(self) -> Any:
        if isinstance(self.record, BaseException):
            raise self.record
        return self.record


class _FakeSession:
    def __init__(self, driver: _FakeDriver, database: str) -> None:
        self.driver = driver
        self.database = database

    def __enter__(self) -> _FakeSession:
        self.driver.entered_sessions += 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.driver.exited_sessions += 1

    def run(self, query: str | Query) -> _FakeResult:
        self.driver.queries.append((self.database, str(query)))
        if isinstance(query, Query):
            self.driver.query_configs.append((query.metadata, query.timeout))
        if self.driver.readiness_error is not None:
            raise self.driver.readiness_error
        return _FakeResult(self.driver.readiness_record)

    def execute_read(self, work: Any, *args: Any, **kwargs: Any) -> Any:
        self.driver.managed_calls.append(("read", self.database))
        self.driver.transaction_configs.append((work.metadata, work.timeout))
        return work("read-tx", *args, **kwargs)

    def execute_write(self, work: Any, *args: Any, **kwargs: Any) -> Any:
        self.driver.managed_calls.append(("write", self.database))
        return work("write-tx", *args, **kwargs)

    def begin_transaction(
        self,
        *,
        metadata: dict[str, str],
        timeout: float,
    ) -> _FakeTransaction:
        self.driver.transaction_configs.append((metadata, timeout))
        return _FakeTransaction(self.driver)


class _FakeTransaction:
    def __init__(self, driver: _FakeDriver) -> None:
        self.driver = driver

    def __enter__(self) -> _FakeTransaction:
        self.driver.explicit_transactions += 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del traceback
        if exc_type is not None or exc is not None:
            self.driver.rolled_back_transactions += 1

    def commit(self) -> None:
        self.driver.committed_transactions += 1

    def __str__(self) -> str:
        return "write-tx"


class _FakeDriver:
    def __init__(self) -> None:
        self.closed_count = 0
        self.entered_sessions = 0
        self.exited_sessions = 0
        self.session_databases: list[str] = []
        self.queries: list[tuple[str, str]] = []
        self.managed_calls: list[tuple[str, str]] = []
        self.transaction_configs: list[tuple[dict[str, str] | None, float | None]] = []
        self.query_configs: list[tuple[dict[str, str] | None, float | None]] = []
        self.explicit_transactions = 0
        self.committed_transactions = 0
        self.rolled_back_transactions = 0
        self.readiness_record: Any = {"ready": 1}
        self.readiness_error: BaseException | None = None

    def session(self, *, database: str) -> _FakeSession:
        self.session_databases.append(database)
        return _FakeSession(self, database)

    def close(self) -> None:
        self.closed_count += 1


def _settings(**overrides: Any) -> Neo4jSettings:
    values = {
        "uri": "neo4j+s://graph.example.test:7687",
        "database": "production-graph",
        "username": "graph-service",
        "password": "unit-test-password",
        "max_connection_pool_size": 17,
        "connection_acquisition_timeout": 8,
        "connection_timeout": 4,
        "max_connection_lifetime": 900,
        "max_transaction_retry_time": 6,
        "transaction_timeout_seconds": 12,
        "readiness_timeout_seconds": 3,
    }
    values.update(overrides)
    return Neo4jSettings(**values)


class Neo4jSettingsTests(unittest.TestCase):
    def test_valid_settings_normalize_and_keep_secret_out_of_repr(self) -> None:
        settings = _settings(database=" graph_1 ", username=" service ")
        rendered = repr(settings)
        self.assertEqual(settings.database, "graph_1")
        self.assertEqual(settings.username, "service")
        self.assertNotIn(settings.password, rendered)
        self.assertNotIn("password=", rendered)

    def test_supported_official_uri_schemes(self) -> None:
        for scheme in (
            "neo4j",
            "neo4j+s",
            "neo4j+ssc",
            "bolt",
            "bolt+s",
            "bolt+ssc",
        ):
            with self.subTest(scheme=scheme):
                settings = _settings(uri=f"{scheme}://localhost:7687")
                self.assertEqual(settings.uri, f"{scheme}://localhost:7687")

    def test_uri_rejects_ambiguous_or_credential_bearing_values(self) -> None:
        invalid = (
            "http://localhost:7687",
            "neo4j://",
            "neo4j://user:unit-test-password@localhost:7687",
            "neo4j://localhost:7687/database",
            "neo4j://localhost:7687?token=value",
            "neo4j://localhost:7687#fragment",
            "neo4j://localhost:99999",
            "neo4j://local host:7687",
            "neo4j://localhost%2Fevil:7687",
        )
        for uri in invalid:
            with self.subTest(uri=uri):
                with self.assertRaises(ValueError):
                    _settings(uri=uri)

    def test_database_user_and_password_are_strictly_validated(self) -> None:
        cases = (
            {"database": ""},
            {"database": "bad/name"},
            {"database": "-leading"},
            {"username": ""},
            {"username": "line\nbreak"},
            {"password": ""},
            {"password": "line\nbreak"},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    _settings(**kwargs)

    def test_pool_and_timeout_settings_reject_bool_nonfinite_and_bad_bounds(
        self,
    ) -> None:
        cases = (
            {"max_connection_pool_size": 0},
            {"max_connection_pool_size": True},
            {"connection_acquisition_timeout": 0},
            {"connection_timeout": math.inf},
            {"max_connection_lifetime": -1},
            {"max_transaction_retry_time": -1},
            {"max_transaction_retry_time": math.nan},
            {"transaction_timeout_seconds": 0},
            {"transaction_timeout_seconds": 301},
            {"readiness_timeout_seconds": 61},
            {
                "transaction_timeout_seconds": 4,
                "readiness_timeout_seconds": 5,
            },
            {"max_connection_pool_size": 1_001},
            {"connection_acquisition_timeout": 301},
            {"connection_timeout": 121},
            {"max_connection_lifetime": 86_401},
            {"max_transaction_retry_time": 301},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    _settings(**kwargs)
        self.assertEqual(
            _settings(max_transaction_retry_time=0).max_transaction_retry_time,
            0,
        )


class Neo4jResourceTests(unittest.TestCase):
    def test_factory_maps_all_official_driver_pool_and_timeout_kwargs(self) -> None:
        captured: dict[str, Any] = {}
        driver = _FakeDriver()

        def factory(uri: str, **kwargs: Any) -> _FakeDriver:
            captured["uri"] = uri
            captured.update(kwargs)
            return driver

        settings = _settings()
        resource = create_neo4j_resource(settings, driver_factory=factory)
        self.assertIs(resource.driver, driver)
        self.assertEqual(captured["uri"], settings.uri)
        self.assertEqual(captured["auth"], (settings.username, settings.password))
        self.assertEqual(captured["max_connection_pool_size"], 17)
        self.assertEqual(captured["connection_acquisition_timeout"], 8.0)
        self.assertEqual(captured["connection_timeout"], 4.0)
        self.assertEqual(captured["max_connection_lifetime"], 900.0)
        self.assertEqual(captured["max_transaction_retry_time"], 6.0)
        self.assertNotIn("transaction_timeout_seconds", captured)
        self.assertNotIn("readiness_timeout_seconds", captured)

    def test_managed_read_and_write_use_the_configured_database(self) -> None:
        driver = _FakeDriver()
        resource = Neo4jResource(driver, _settings())

        def work(transaction: str, value: int, *, suffix: str) -> str:
            return f"{transaction}:{value}:{suffix}"

        self.assertEqual(
            resource.execute_read(work, 3, suffix="r"),
            "read-tx:3:r",
        )
        self.assertEqual(
            resource.execute_write(work, 4, suffix="w"),
            "write-tx:4:w",
        )
        self.assertEqual(
            driver.managed_calls,
            [
                ("read", "production-graph"),
            ],
        )
        self.assertEqual(driver.explicit_transactions, 1)
        self.assertEqual(driver.committed_transactions, 1)
        self.assertEqual(driver.rolled_back_transactions, 0)
        self.assertEqual(
            driver.transaction_configs,
            [
                ({"component": "graphrag-api", "operation": "read"}, 12.0),
                ({"component": "graphrag-api", "operation": "write"}, 12.0),
            ],
        )
        self.assertEqual(driver.entered_sessions, 2)
        self.assertEqual(driver.exited_sessions, 2)

    def test_write_failure_is_rolled_back_and_never_replayed(self) -> None:
        driver = _FakeDriver()
        resource = Neo4jResource(driver, _settings())
        calls = 0

        def work(transaction: _FakeTransaction) -> None:
            nonlocal calls
            self.assertEqual(str(transaction), "write-tx")
            calls += 1
            raise ServiceUnavailable("protected write failure")

        with self.assertRaises(DependencyUnavailableError):
            resource.execute_write(work)
        self.assertEqual(calls, 1)
        self.assertEqual(driver.explicit_transactions, 1)
        self.assertEqual(driver.committed_transactions, 0)
        self.assertEqual(driver.rolled_back_transactions, 1)

    def test_retryable_driver_failures_are_publicly_redacted(self) -> None:
        driver = _FakeDriver()
        resource = Neo4jResource(driver, _settings())
        protected_detail = "private graph endpoint"

        def failed_work(transaction: str) -> None:
            del transaction
            raise ServiceUnavailable(protected_detail)

        for operation in (resource.execute_read, resource.execute_write):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(DependencyUnavailableError) as caught:
                    operation(failed_work)
                self.assertNotIn(protected_detail, str(caught.exception))

    def test_timeout_and_deterministic_driver_failures_map_safely(self) -> None:
        driver = _FakeDriver()
        resource = Neo4jResource(driver, _settings())
        timeout_detail = "private timeout endpoint"
        server_timeout = Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Transaction.TransactionTimedOut",
            message=timeout_detail,
        )
        configured_timeout = Neo4jError._hydrate_neo4j(
            code=(
                "Neo.ClientError.Transaction."
                "TransactionTimedOutClientConfiguration"
            ),
            message=timeout_detail,
        )
        syntax_detail = "MATCH (secret query syntax"
        syntax_error = Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Statement.SyntaxError",
            message=syntax_detail,
        )
        failures = (
            (
                ConnectionAcquisitionTimeoutError(timeout_detail),
                DependencyTimeoutError,
            ),
            (server_timeout, DependencyTimeoutError),
            (configured_timeout, DependencyTimeoutError),
            (syntax_error, ApiRuntimeError),
        )
        for error, public_type in failures:
            with self.subTest(public_type=public_type.__name__):
                with self.assertRaises(public_type) as caught:
                    resource.execute_read(
                        lambda transaction, failure=error: (_ for _ in ()).throw(
                            failure
                        )
                    )
                rendered = str(caught.exception)
                self.assertNotIn(timeout_detail, rendered)
                self.assertNotIn(syntax_detail, rendered)

    def test_health_is_local_and_readiness_checks_database_access(self) -> None:
        driver = _FakeDriver()
        resource = Neo4jResource(driver, _settings())
        health = resource.health_probe()
        self.assertTrue(health.ok)
        self.assertEqual(health.status, "healthy")
        self.assertEqual(driver.queries, [])

        readiness = resource.readiness_probe()
        self.assertTrue(readiness.ok)
        self.assertEqual(readiness.status, "ready")
        self.assertEqual(
            driver.queries,
            [("production-graph", "RETURN 1 AS ready")],
        )
        self.assertEqual(
            driver.query_configs,
            [
                (
                    {"component": "graphrag-api", "operation": "readiness"},
                    3.0,
                )
            ],
        )

    def test_readiness_fails_closed_without_leaking_dependency_details(self) -> None:
        driver = _FakeDriver()
        secret = "private-host-and-credential-detail"
        driver.readiness_error = RuntimeError(secret)
        resource = Neo4jResource(driver, _settings())
        probe = resource.readiness_probe()
        self.assertFalse(probe.ok)
        self.assertEqual(probe.status, "not_ready")
        self.assertEqual(probe.error_code, ErrorCode.DEPENDENCY_UNAVAILABLE)
        self.assertNotIn(secret, repr(probe))

    def test_missing_readiness_marker_fails_closed(self) -> None:
        driver = _FakeDriver()
        driver.readiness_record = None
        resource = Neo4jResource(driver, _settings())
        self.assertFalse(resource.readiness_probe().ok)
        driver.readiness_record = {"ready": 0}
        self.assertFalse(resource.readiness_probe().ok)

    def test_close_is_idempotent_and_all_operations_fail_after_close(self) -> None:
        driver = _FakeDriver()
        resource = Neo4jResource(driver, _settings())
        resource.close()
        resource.close()
        self.assertEqual(driver.closed_count, 1)
        self.assertFalse(resource.health_probe().ok)
        self.assertFalse(resource.readiness_probe().ok)
        with self.assertRaises(RuntimeClosedError):
            _ = resource.driver
        with self.assertRaises(RuntimeClosedError):
            resource.execute_read(lambda transaction: transaction)
        with self.assertRaises(RuntimeClosedError):
            resource.execute_write(lambda transaction: transaction)

    def test_resource_repr_excludes_password_and_username(self) -> None:
        resource = Neo4jResource(_FakeDriver(), _settings())
        rendered = repr(resource)
        self.assertNotIn(resource.settings.password, rendered)
        self.assertNotIn(resource.settings.username, rendered)
        self.assertIn("production-graph", rendered)

    def test_probe_serialization_uses_stable_string_error_codes(self) -> None:
        resource = Neo4jResource(_FakeDriver(), _settings())
        resource.close()
        self.assertEqual(
            resource.health_probe().as_dict(),
            {
                "component": "neo4j",
                "ok": False,
                "status": "unhealthy",
                "error_code": "runtime_closed",
            },
        )


if __name__ == "__main__":
    unittest.main()
