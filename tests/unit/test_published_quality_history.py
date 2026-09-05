"""Unit checks for immutable published-quality history records."""

from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock

from graphrag_prod.domain.access import Principal
from graphrag_prod.graph.published_quality import (
    PublishedGraphQualityAuthorizationError,
    PublishedGraphQualityConflict,
    PublishedGraphQualityIssue,
    PublishedGraphQualityReport,
    PublishedGraphReviewSampleItem,
)
from graphrag_prod.graph.published_quality_history import (
    Neo4jPublishedGraphQualityHistoryService,
    PublishedGraphQualityHistoryConflict,
    PublishedGraphQualityHistoryUnavailable,
    _acl_payloads,
    _issue_payloads,
    _manifest,
    _report_document,
    _report_from_json,
    _sample_payloads,
)
from graphrag_prod.graph.quality import IssueSeverity


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _report(*, suffix: str = "one") -> PublishedGraphQualityReport:
    run_seed = _digest(f"run:{suffix}")
    issue_id = "published-quality-issue:" + _digest(f"issue:{suffix}")
    return PublishedGraphQualityReport(
        run_id="published-graph-quality:" + run_seed,
        ruleset_version="published-governed-graph-quality-v1",
        tenant_id="tenant-alpha",
        publication_id=f"publication:{suffix}",
        publication_generation=1,
        manifest_hash=_digest(f"manifest:{suffix}"),
        ontology_version_id=f"tbox:{suffix}",
        tbox_checksum=_digest(f"tbox:{suffix}"),
        corpus_revision=7,
        graph_digest=_digest(f"graph:{suffix}"),
        counts=(("assertions", 1), ("revisions", 1)),
        total_issue_count=1,
        total_error_count=1,
        issues_truncated=False,
        issues=(
            PublishedGraphQualityIssue(
                issue_id,
                "ACTIVE_ASSERTION_PROJECTION_INVALID",
                IssueSeverity.ERROR,
                "AssertionRevision",
                "revision:1",
                "published assertion projection differs from its revision",
            ),
        ),
        review_sample=(
            PublishedGraphReviewSampleItem(
                "AssertionRevision",
                "revision:1",
                ("ACTIVE_ASSERTION_PROJECTION_INVALID",),
                ("chunk:1",),
            ),
        ),
    )


class _Auditor:
    def __init__(self, value: PublishedGraphQualityReport | Exception) -> None:
        self.value = value
        self.calls: list[Principal] = []

    def audit(self, principal: Principal) -> PublishedGraphQualityReport:
        self.calls.append(principal)
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _NoSessionDriver:
    def __init__(self) -> None:
        self.sessions = 0

    def session(self, **_: object) -> object:
        self.sessions += 1
        raise AssertionError("a write/read session must not be opened")


class PublishedQualityHistoryUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.principal = Principal(
            "expert:alpha",
            "tenant-alpha",
            frozenset({"alpha-public"}),
            frozenset({"knowledge:quality"}),
        )

    def test_report_codec_is_canonical_and_metadata_only(self) -> None:
        report = _report()
        document = _report_document(report)
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

        self.assertEqual(_report_from_json(encoded), report)
        serialized = json.dumps(document, sort_keys=True)
        for forbidden_key in (
            '"source_text"',
            '"chunk_text"',
            '"evidence_text"',
            '"quoted_text"',
        ):
            self.assertNotIn(forbidden_key, serialized)
        self.assertIn('"evidence_chunk_ids"', serialized)

    def test_report_codec_rejects_derived_or_payload_tampering(self) -> None:
        document = _report_document(_report())
        document["passed"] = True
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self.assertRaises(PublishedGraphQualityHistoryConflict):
            _report_from_json(encoded)

        document = _report_document(_report())
        document["ruleset_version"] = "untrusted-ruleset"
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self.assertRaises(PublishedGraphQualityHistoryConflict):
            _report_from_json(encoded)

        document = _report_document(_report())
        document["unexpected"] = "field"
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self.assertRaises(PublishedGraphQualityHistoryConflict):
            _report_from_json(encoded)

    def test_child_and_acl_manifests_are_deterministic(self) -> None:
        report = _report()
        first_issues = _issue_payloads(report)
        first_samples = _sample_payloads(report)
        first_acl = _acl_payloads(
            report,
            (("alpha-board", "alpha-public"), ("alpha-public",)),
        )

        self.assertEqual(first_issues, _issue_payloads(report))
        self.assertEqual(first_samples, _sample_payloads(report))
        self.assertEqual(
            first_acl,
            _acl_payloads(
                report,
                (("alpha-board", "alpha-public"), ("alpha-public",)),
            ),
        )
        self.assertEqual(
            _manifest(first_issues, "issue_id"),
            _manifest(_issue_payloads(report), "issue_id"),
        )

    def test_missing_quality_scope_does_not_call_auditor_or_open_session(self) -> None:
        driver = _NoSessionDriver()
        auditor = _Auditor(_report())
        service = Neo4jPublishedGraphQualityHistoryService(
            driver,
            auditor=auditor,
        )
        principal = Principal(
            "reviewer:alpha",
            "tenant-alpha",
            frozenset({"alpha-public"}),
            frozenset({"knowledge:review"}),
        )

        with self.assertRaises(PublishedGraphQualityAuthorizationError):
            service.audit_and_record(principal)

        self.assertEqual(auditor.calls, [])
        self.assertEqual(driver.sessions, 0)

    def test_audit_exception_happens_before_any_write_session(self) -> None:
        driver = _NoSessionDriver()
        auditor = _Auditor(PublishedGraphQualityConflict())
        service = Neo4jPublishedGraphQualityHistoryService(
            driver,
            auditor=auditor,
            clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        )

        with self.assertRaises(PublishedGraphQualityConflict):
            service.audit_and_record(self.principal)

        self.assertEqual(auditor.calls, [self.principal])
        self.assertEqual(driver.sessions, 0)

    def test_transport_projection_is_validated_before_clock_or_write(self) -> None:
        report = replace(
            _report(),
            review_sample=tuple(
                PublishedGraphReviewSampleItem(
                    "AssertionRevision", f"revision:{index}", (), ()
                )
                for index in range(21)
            ),
        )
        # This is a valid reusable history report but exceeds a transport's
        # smaller review-sample contract. It must not be silently truncated or
        # committed before the transport can reject its response.
        self.assertEqual(len(_report_document(report)["review_sample"]), 21)
        observed: list[PublishedGraphQualityReport] = []

        def validate_projection(value: PublishedGraphQualityReport) -> None:
            observed.append(value)
            if len(value.review_sample) > 20:
                raise ValueError("transport review sample exceeds 20")

        clock = MagicMock(side_effect=AssertionError("clock must not be called"))
        driver = _NoSessionDriver()
        service = Neo4jPublishedGraphQualityHistoryService(
            driver,
            auditor=_Auditor(report),
            report_validator=validate_projection,
            clock=clock,
        )
        self.assertFalse(hasattr(service, "audit"))
        with self.assertRaises(PublishedGraphQualityHistoryConflict):
            service.audit_and_record(self.principal)
        self.assertEqual(observed, [report])
        self.assertEqual(driver.sessions, 0)
        clock.assert_not_called()

    def test_timeouts_keep_their_identity_across_history_boundaries(self) -> None:
        for boundary in ("audit", "validator", "clock", "write", "get", "list"):
            with self.subTest(boundary=boundary):
                timeout = TimeoutError("deadline exceeded")
                driver = MagicMock()
                session = driver.session.return_value.__enter__.return_value
                session.execute_write.side_effect = timeout
                session.execute_read.side_effect = timeout
                validator = MagicMock(
                    side_effect=timeout if boundary == "validator" else None
                )
                clock = MagicMock(
                    return_value=datetime(2026, 1, 1, tzinfo=UTC),
                    side_effect=timeout if boundary == "clock" else None,
                )
                service = Neo4jPublishedGraphQualityHistoryService(
                    driver,
                    auditor=_Auditor(timeout if boundary == "audit" else _report()),
                    report_validator=validator,
                    clock=clock,
                )
                with self.assertRaises(TimeoutError) as caught:
                    if boundary == "get":
                        service.get_run(self.principal, _report().run_id)
                    elif boundary == "list":
                        service.list_runs(self.principal)
                    else:
                        service.audit_and_record(self.principal)
                self.assertIs(caught.exception, timeout)
                if boundary in {"audit", "validator", "clock"}:
                    driver.session.assert_not_called()

    def test_projection_validator_failure_is_redacted_before_write(self) -> None:
        driver = _NoSessionDriver()
        validator = MagicMock(side_effect=RuntimeError("internal projection detail"))
        service = Neo4jPublishedGraphQualityHistoryService(
            driver, auditor=_Auditor(_report()), report_validator=validator
        )
        with self.assertRaises(PublishedGraphQualityHistoryUnavailable) as caught:
            service.audit_and_record(self.principal)
        self.assertNotIn("internal projection detail", str(caught.exception))
        self.assertEqual(driver.sessions, 0)

    def test_constructor_and_list_bounds_are_strict(self) -> None:
        with self.assertRaisesRegex(TypeError, "auditor must implement audit"):
            Neo4jPublishedGraphQualityHistoryService(
                _NoSessionDriver(),
                auditor=object(),
            )
        with self.assertRaisesRegex(TypeError, "report_validator must be callable"):
            Neo4jPublishedGraphQualityHistoryService(
                _NoSessionDriver(), report_validator=object()
            )
        service = Neo4jPublishedGraphQualityHistoryService(
            _NoSessionDriver(),
            auditor=_Auditor(_report()),
        )
        with self.assertRaisesRegex(ValueError, "between 1 and 50"):
            service.list_runs(self.principal, limit=51)
        with self.assertRaises(TypeError):
            service.list_runs(self.principal, limit=True)


if __name__ == "__main__":
    unittest.main()
