"""Tests for the bounded Neo4j transaction boundary in retrieval."""

from __future__ import annotations

import math
import unittest
from typing import Any

from neo4j.exceptions import (
    ConnectionAcquisitionTimeoutError,
    Neo4jError,
    ServiceUnavailable,
    SessionExpired,
)

from graphrag_prod.domain import Principal
from graphrag_prod.retrieval import (
    Neo4jRetrievalEngine,
    RetrievalBackendError,
    RetrievalBackendTimeout,
    RetrievalBackendUnavailable,
    RetrievalRequest,
    RetrievalUnavailable,
)


def _request() -> RetrievalRequest:
    return RetrievalRequest(
        query_text="What were net sales?",
        query_vector=(1.0, 0.5),
        principal=Principal(
            principal_id="reader-1",
            tenant_id="tenant-alpha",
            groups=frozenset({"finance-readers"}),
        ),
        query_embedding_space_id="embedding-space-v1",
    )


class _FailingSession:
    def __init__(self, driver: _FailingDriver) -> None:
        self.driver = driver

    def __enter__(self) -> _FailingSession:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback

    def execute_read(self, work: Any, request: RetrievalRequest) -> Any:
        self.driver.work_metadata = work.metadata
        self.driver.work_timeout = work.timeout
        self.driver.requests.append(request)
        raise self.driver.error


class _FailingDriver:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.databases: list[str] = []
        self.requests: list[RetrievalRequest] = []
        self.work_metadata: dict[str, str] | None = None
        self.work_timeout: float | None = None

    def session(self, *, database: str) -> _FailingSession:
        self.databases.append(database)
        return _FailingSession(self)


class Neo4jRetrievalResourceTests(unittest.TestCase):
    def test_managed_transaction_has_bounded_server_timeout_and_safe_metadata(
        self,
    ) -> None:
        driver = _FailingDriver(RetrievalUnavailable("tenant state unavailable"))
        engine = Neo4jRetrievalEngine(
            driver,
            " production-graph ",
            transaction_timeout_seconds=7.5,
        )
        with self.assertRaises(RetrievalUnavailable):
            engine.retrieve(_request())
        self.assertEqual(driver.databases, ["production-graph"])
        self.assertEqual(driver.work_timeout, 7.5)
        self.assertEqual(
            driver.work_metadata,
            {"component": "graphrag-retrieval", "operation": "retrieve"},
        )
        self.assertNotIn("tenant", str(driver.work_metadata))
        self.assertNotIn("query", str(driver.work_metadata))

    def test_transaction_timeout_configuration_is_strict_and_bounded(self) -> None:
        driver = _FailingDriver(RuntimeError("unused"))
        cases = (0, -1, 301, True, math.inf, math.nan, "5")
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Neo4jRetrievalEngine(
                        driver,
                        transaction_timeout_seconds=value,  # type: ignore[arg-type]
                    )
        for database in ("", " \n", "bad\x00database"):
            with self.subTest(database=database):
                with self.assertRaises(ValueError):
                    Neo4jRetrievalEngine(driver, database)
        with self.assertRaises(ValueError):
            Neo4jRetrievalEngine(None)

    def test_driver_failures_map_to_sanitized_stable_categories(self) -> None:
        protected = "bolt://user:password@private-host:7687 secret Cypher"
        server_timeout = Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Transaction.TransactionTimedOut",
            message=protected,
        )
        configured_timeout = Neo4jError._hydrate_neo4j(
            code=(
                "Neo.ClientError.Transaction."
                "TransactionTimedOutClientConfiguration"
            ),
            message=protected,
        )
        transient = Neo4jError._hydrate_neo4j(
            code="Neo.TransientError.General.DatabaseUnavailable",
            message=protected,
        )
        syntax = Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Statement.SyntaxError",
            message=protected,
        )
        failures = (
            (ConnectionAcquisitionTimeoutError(protected), RetrievalBackendTimeout),
            (server_timeout, RetrievalBackendTimeout),
            (configured_timeout, RetrievalBackendTimeout),
            (ServiceUnavailable(protected), RetrievalBackendUnavailable),
            (SessionExpired(protected), RetrievalBackendUnavailable),
            (transient, RetrievalBackendUnavailable),
            (syntax, RetrievalBackendError),
        )
        for driver_error, expected in failures:
            with self.subTest(expected=expected.__name__):
                engine = Neo4jRetrievalEngine(_FailingDriver(driver_error))
                with self.assertRaises(expected) as caught:
                    engine.retrieve(_request())
                if expected is RetrievalBackendError:
                    self.assertIs(type(caught.exception), RetrievalBackendError)
                self.assertNotIn(protected, str(caught.exception))
                self.assertEqual(
                    str(caught.exception),
                    "the retrieval store could not complete the query",
                )


if __name__ == "__main__":
    unittest.main()
