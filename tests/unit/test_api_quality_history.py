"""API projection and failure boundaries for immutable quality history."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest

from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.api.quality_history_contracts import (
    PublishedGraphQualityRunListRequest,
    PublishedGraphQualityRunResponse,
)
from graphrag_prod.api.runtime import (
    AuthorizationError,
    ConflictError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    ResourceNotFoundError,
)
from graphrag_prod.domain.access import Principal
from graphrag_prod.graph.published_quality import (
    PublishedGraphQualityAuthorizationError,
    PublishedGraphQualityConflict,
    PublishedGraphQualityLimitExceeded,
    PublishedGraphQualityUnavailable,
)
from graphrag_prod.graph.published_quality_history import (
    PublishedGraphQualityHistoryConflict,
    PublishedGraphQualityHistoryUnavailable,
    PublishedGraphQualityRun,
    PublishedGraphQualityRunSummary,
)
from tests.unit.test_api_knowledge import _quality_report


NOW = datetime(2026, 9, 5, tzinfo=UTC)


def _principal(tenant: str = "tenant-alpha", subject: str = "expert-first") -> Principal:
    return Principal(subject, tenant, frozenset({"engineers"}), frozenset({"knowledge:quality"}))


def _summary(run: PublishedGraphQualityRun) -> PublishedGraphQualityRunSummary:
    report = run.report
    return PublishedGraphQualityRunSummary(
        run_id=report.run_id,
        tenant_id=report.tenant_id,
        publication_id=report.publication_id,
        publication_generation=report.publication_generation,
        ontology_version_id=report.ontology_version_id,
        corpus_revision=report.corpus_revision,
        graph_digest=report.graph_digest,
        ruleset_version=report.ruleset_version,
        passed=report.passed,
        total_issue_count=report.total_issue_count,
        total_error_count=report.total_error_count,
        issues_truncated=report.issues_truncated,
        counts=report.counts,
        recorded_by=run.recorded_by,
        recorded_at=run.recorded_at,
        record_hash=run.record_hash,
    )


class _History:
    """Stateful history double; real storage behavior has Neo4j integration tests."""

    def __init__(self) -> None:
        self.run: PublishedGraphQualityRun | None = None
        self.failure: Exception | None = None
        self.calls: list[tuple[str, Principal, object]] = []

    def audit_and_record(self, principal: Principal) -> PublishedGraphQualityRun:
        self.calls.append(("record", principal, None))
        if self.failure is not None:
            raise self.failure
        if self.run is None:
            self.run = PublishedGraphQualityRun(
                replace(_quality_report(), tenant_id=principal.tenant_id),
                principal.principal_id, NOW, "a" * 64,
            )
        return self.run

    def get_run(self, principal: Principal, run_id: str) -> PublishedGraphQualityRun | None:
        self.calls.append(("get", principal, run_id))
        if self.failure is not None:
            raise self.failure
        if self.run is not None and self.run.tenant_id == principal.tenant_id and self.run.run_id == run_id:
            return self.run
        return None

    def list_runs(self, principal: Principal, *, publication_id: str | None, limit: int) -> tuple[PublishedGraphQualityRunSummary, ...]:
        self.calls.append(("list", principal, (publication_id, limit)))
        if self.failure is not None:
            raise self.failure
        if self.run is None or self.run.tenant_id != principal.tenant_id:
            return ()
        if publication_id is not None and publication_id != self.run.report.publication_id:
            return ()
        return (_summary(self.run),)[:limit]


class _NoSessionDriver:
    def __init__(self) -> None:
        self.sessions = 0

    def session(self, **_: object) -> object:
        self.sessions += 1
        raise AssertionError("unexpected database access")


def _adapter(history: object | None = None, *, driver: object | None = None, report: object | None = None) -> Neo4jKnowledgeOperations:
    return Neo4jKnowledgeOperations(
        driver=driver or _NoSessionDriver(),
        construction=SimpleNamespace(run=lambda *_args, **_kwargs: None),
        quality_history_service=history,
        quality_service=SimpleNamespace(audit=lambda _principal: report or _quality_report()),
        clock=lambda: NOW,
    )


class QualityHistoryAdapterTests(unittest.TestCase):
    def test_record_list_get_preserve_provenance_without_source_or_tenant_fields(self) -> None:
        history = _History()
        adapter = _adapter(history)
        principal = _principal()
        record = adapter.record_quality(principal).payload
        self.assertIsInstance(record, PublishedGraphQualityRunResponse)
        self.assertEqual(record.recorded_by, principal.principal_id)
        self.assertEqual(record.report.review_sample[0].evidence_chunk_ids, ("chunk-1",))
        runs = adapter.quality_runs(principal, PublishedGraphQualityRunListRequest(publication_id="publication-1", limit=1)).payload
        self.assertEqual(runs.items[0].run_id, record.report.run_id)
        self.assertEqual(adapter.quality_run(principal, record.report.run_id).payload, record)
        self.assertEqual(history.calls[1][2], ("publication-1", 1))
        payload = str(record.model_dump(mode="json"))
        for forbidden in ("tenant_id", "source_text", "quoted_text", "evidence_text"):
            self.assertNotIn(forbidden, payload)

    def test_scope_and_missing_run_fail_before_protected_results(self) -> None:
        history = _History()
        adapter = _adapter(history)
        forbidden = replace(_principal(), capabilities=frozenset({"knowledge:review"}))
        for action in (
            lambda: adapter.record_quality(forbidden),
            lambda: adapter.quality_run(forbidden, "run-missing"),
            lambda: adapter.quality_runs(forbidden, PublishedGraphQualityRunListRequest()),
        ):
            with self.assertRaises(AuthorizationError):
                action()
        self.assertEqual(history.calls, [])
        with self.assertRaises(ResourceNotFoundError):
            adapter.quality_run(_principal(), "run-missing")

    def test_bad_tenant_filter_or_list_bound_from_service_fails_closed(self) -> None:
        history = _History()
        run = history.audit_and_record(_principal())
        adapter = _adapter(history)
        with self.assertRaises(DependencyUnavailableError):
            adapter.record_quality(_principal("tenant-other"))
        for values, request in (
            ((replace(_summary(run), tenant_id="tenant-other"),), PublishedGraphQualityRunListRequest()),
            ((_summary(run),), PublishedGraphQualityRunListRequest(publication_id="publication-other")),
            ((_summary(run), _summary(run)), PublishedGraphQualityRunListRequest(limit=1)),
        ):
            with self.subTest(request=request):
                history.list_runs = lambda *_args, **_kwargs: values
                with self.assertRaises(DependencyUnavailableError):
                    adapter.quality_runs(_principal(), request)

    def test_failure_taxonomy_is_consistent_for_all_history_operations(self) -> None:
        cases = (
            (PublishedGraphQualityAuthorizationError(), AuthorizationError),
            (PublishedGraphQualityHistoryConflict(), ConflictError),
            (PublishedGraphQualityConflict(), ConflictError),
            (PublishedGraphQualityLimitExceeded(), ConflictError),
            (PublishedGraphQualityHistoryUnavailable(), DependencyUnavailableError),
            (PublishedGraphQualityUnavailable(), DependencyUnavailableError),
            (TimeoutError("protected backend address"), DependencyTimeoutError),
            (RuntimeError("protected backend address"), DependencyUnavailableError),
        )
        for failure, expected in cases:
            history = _History()
            history.failure = failure
            adapter = _adapter(history)
            for operation in (
                lambda: adapter.record_quality(_principal()),
                lambda: adapter.quality_run(_principal(), "run-1"),
                lambda: adapter.quality_runs(_principal(), PublishedGraphQualityRunListRequest()),
            ):
                with self.subTest(failure=type(failure).__name__):
                    with self.assertRaises(expected) as caught:
                        operation()
                    self.assertNotIn("protected backend address", str(caught.exception))

    def test_get_rejects_service_returning_a_different_same_tenant_run(self) -> None:
        history = _History()
        run = history.audit_and_record(_principal())
        history.get_run = lambda *_args: run
        with self.assertRaises(DependencyUnavailableError):
            _adapter(history).quality_run(_principal(), "requested-but-different-run")

    def test_default_history_checks_http_projection_before_persistence(self) -> None:
        driver = _NoSessionDriver()
        report = replace(_quality_report(), counts=(*_quality_report().counts, ("unsupported_count", 1)))
        adapter = _adapter(driver=driver, report=report)
        with self.assertRaises(ConflictError):
            adapter.record_quality(_principal())
        self.assertEqual(driver.sessions, 0)
        self.assertFalse(hasattr(adapter.quality_history_service, "audit"))

    def test_history_injection_requires_explicit_read_and_write_methods(self) -> None:
        with self.assertRaisesRegex(TypeError, "audit_and_record, get_run and list_runs"):
            _adapter(SimpleNamespace(audit=lambda _: _quality_report()))


if __name__ == "__main__":
    unittest.main()
