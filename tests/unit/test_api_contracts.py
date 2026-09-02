"""Tests for strict and bounded Stage 7 HTTP payload contracts."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import unittest

from pydantic import ValidationError

from graphrag_prod.api.contracts import (
    AnswerRequest,
    ErrorResponse,
    GenerationLimitsRequest,
    HealthResponse,
    IngestionRequest,
    JobResponse,
    MAX_DOCUMENT_BYTES,
    MetricsResponse,
    ReadinessResponse,
    RetrievalLimitsRequest,
    RetrievalRequest,
    VersionFilterRequest,
)
from graphrag_prod.observability.metrics import MetricsRegistry


def _ingestion_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "operation_key": "request-00000001",
        "canonical_uri": "https://example.test/reports/fy2024.txt",
        "title": "FY2024 filing",
        "source_name": "regulatory filing",
        "mime_type": "text/plain",
        "language": "en",
        "published_at": datetime(2024, 9, 28, tzinfo=UTC),
        "content": "Exact source text.\n",
        "access_policy_id": "policy-finance",
        "access_policy_version": 1,
        "access_groups": ("finance-readers",),
    }
    payload.update(changes)
    return payload


def _job_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": "job-1",
        "operation": "UPSERT",
        "status": "SUCCEEDED",
        "phase": "COMPLETE",
        "document_id": "document-1",
        "target_version_id": "version-1",
        "target_snapshot_id": "snapshot-1",
        "expected_active_snapshot_id": None,
        "source_generation": 0,
        "attempts": 1,
        "max_attempts": 3,
        "completed_tasks": 2,
        "expected_tasks": 2,
        "outcome": "published",
        "last_error_code": None,
    }
    payload.update(changes)
    return payload


class APIRequestContractTests(unittest.TestCase):
    def test_ingestion_preserves_exact_content_and_validates_acl_shape(self) -> None:
        request = IngestionRequest.model_validate(_ingestion_payload())
        self.assertEqual(request.content, "Exact source text.\n")
        self.assertEqual(request.access_groups, ("finance-readers",))

        for groups in ((), ("public", "public"), ("bad group",)):
            with self.subTest(groups=groups):
                with self.assertRaises(ValidationError):
                    IngestionRequest.model_validate(_ingestion_payload(access_groups=groups))

    def test_ingestion_rejects_unknown_fields_naive_time_and_non_utf8_bounds(self) -> None:
        invalid_payloads = (
            _ingestion_payload(tenant_id="attacker-tenant"),
            _ingestion_payload(published_at=datetime(2024, 1, 1)),
            _ingestion_payload(content="safe\x00unsafe"),
            _ingestion_payload(content="界" * (MAX_DOCUMENT_BYTES // 3 + 1)),
            _ingestion_payload(canonical_uri="https://user:secret@example.test/source"),
            _ingestion_payload(canonical_uri="https://example.test/source?token=secret"),
            _ingestion_payload(canonical_uri="https://example.test/source#section"),
            _ingestion_payload(title="unsafe\r\ntitle"),
            _ingestion_payload(source_name="unsafe\x1fname"),
            _ingestion_payload(mime_type="application/pdf"),
            _ingestion_payload(language="fr"),
        )
        for payload in invalid_payloads:
            with self.subTest(fields=tuple(payload)):
                with self.assertRaises(ValidationError):
                    IngestionRequest.model_validate(payload)

    def test_ingestion_canonicalizes_stable_source_uri(self) -> None:
        request = IngestionRequest.model_validate(
            _ingestion_payload(canonical_uri="HTTPS://EXAMPLE.TEST:443/reports/")
        )
        self.assertEqual(request.canonical_uri, "https://example.test/reports")

    def test_retrieval_and_answer_never_accept_identity_or_vector_fields(self) -> None:
        for model in (RetrievalRequest, AnswerRequest):
            schema_fields = model.model_fields
            self.assertNotIn("tenant_id", schema_fields)
            self.assertNotIn("query_vector", schema_fields)
            self.assertNotIn("access_groups", schema_fields)
            for forbidden in ("tenant_id", "query_vector", "access_groups"):
                with self.subTest(model=model.__name__, forbidden=forbidden):
                    with self.assertRaises(ValidationError):
                        model.model_validate(
                            {"query_text": "What were net sales?", forbidden: "forged"}
                        )

    def test_query_text_and_all_numeric_limits_are_bounded_and_strict(self) -> None:
        self.assertEqual(RetrievalRequest(query_text="  What changed?  ").query_text, "What changed?")
        invalid_requests = (
            {"query_text": ""},
            {"query_text": "x" * 2_001},
            {"query_text": "safe\x00unsafe"},
            {"query_text": "q", "limits": {"top_k": 21}},
            {"query_text": "q", "limits": {"top_k": "5"}},
            {"query_text": "q", "limits": {"top_k": True}},
            {"query_text": "q", "limits": {"adjacent_window": 4}},
            {"query_text": "q", "limits": {"minimum_vector_score": float("nan")}},
        )
        for payload in invalid_requests:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    RetrievalRequest.model_validate(payload)

    def test_relational_retrieval_limits_cannot_escape_domain_invariants(self) -> None:
        invalid_limits = (
            {"bm25_recall_k": 20, "bm25_scan_k": 19},
            {"seed_k": 20, "candidate_limit": 10},
            {"top_k": 2, "anchor_k": 3},
        )
        for values in invalid_limits:
            with self.subTest(values=values):
                with self.assertRaises(ValidationError):
                    RetrievalLimitsRequest.model_validate(values)
        domain = RetrievalLimitsRequest(top_k=10, anchor_k=5).to_domain()
        self.assertEqual(domain.top_k, 10)
        self.assertEqual(domain.anchor_k, 5)

    def test_version_filters_are_unique_small_and_timezone_aware(self) -> None:
        for payload in (
            {"document_ids": ("document-1", "document-1")},
            {"document_ids": tuple(f"d-{index}" for index in range(101))},
            {"published_at_or_before": datetime(2024, 1, 1)},
            {"unknown": "value"},
        ):
            with self.subTest(payload=tuple(payload)):
                with self.assertRaises(ValidationError):
                    VersionFilterRequest.model_validate(payload)
        value = VersionFilterRequest(
            document_ids=("document-1",),
            published_at_or_before=datetime(2024, 1, 1, tzinfo=UTC),
        ).to_domain()
        self.assertEqual(value.document_ids, frozenset({"document-1"}))

    def test_generation_limits_are_independently_bounded(self) -> None:
        self.assertEqual(GenerationLimitsRequest().to_domain().max_claims, 20)
        for payload in (
            {"max_context_chunks": 11},
            {"max_claims": 21},
            {"max_citations_per_claim": 6},
            {"max_prompt_chars": 50_001},
            {"max_claims": "20"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    GenerationLimitsRequest.model_validate(payload)

    def test_json_request_accepts_arrays_and_timezone_strings_without_coercing_numbers(self) -> None:
        encoded = json.dumps(
            {
                **_ingestion_payload(
                    published_at="2024-09-28T00:00:00Z",
                    access_groups=["finance-readers"],
                )
            },
            default=str,
        )
        parsed = IngestionRequest.model_validate_json(encoded)
        self.assertEqual(parsed.published_at, datetime(2024, 9, 28, tzinfo=UTC))
        self.assertEqual(parsed.access_groups, ("finance-readers",))


class APIResponseContractTests(unittest.TestCase):
    def test_job_response_omits_internal_lease_and_rejects_extra_fields(self) -> None:
        response = JobResponse.model_validate(_job_payload())
        serialized = response.model_dump()
        self.assertNotIn("lease_token", serialized)
        self.assertNotIn("lease_owner", serialized)
        with self.assertRaises(ValidationError):
            JobResponse.model_validate(_job_payload(lease_token="protected"))

    def test_job_status_phase_and_counts_are_strict(self) -> None:
        for changes in (
            {"status": "UNKNOWN"},
            {"phase": "UNKNOWN"},
            {"attempts": -1},
            {"attempts": "1"},
            {"completed_tasks": True},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError):
                    JobResponse.model_validate(_job_payload(**changes))

    def test_health_readiness_metrics_and_errors_are_strict(self) -> None:
        health = HealthResponse(service="sample-graphrag", version="0.1.0")
        self.assertEqual(health.status, "ok")
        readiness = ReadinessResponse(status="ready", checks={"neo4j": "ok"})
        self.assertEqual(readiness.checks, {"neo4j": "ok"})
        registry = MetricsRegistry()
        registry.record_request("/v1/retrieval", "POST", 200, 12.5)
        registry.record_error("dependency_unavailable")
        registry.record_retrieval_stage("vector_recall", 4.0)
        registry.record_model_call(10, 5, 0.001)
        metrics = MetricsResponse.model_validate(registry.snapshot())
        self.assertEqual(metrics.requests.total, 1)
        self.assertEqual(metrics.retrieval.by_stage["vector_recall"].count, 1)
        self.assertEqual(metrics.model.input_tokens, 10)
        error = ErrorResponse(code="invalid_request", message="Request rejected", request_id="request-1")
        self.assertEqual(error.code, "invalid_request")

        for constructor in (
            lambda: ReadinessResponse(status="ready", checks={}),
            lambda: ReadinessResponse(status="ready", checks={"neo4j": "unknown"}),
            lambda: MetricsResponse.model_validate(
                {
                    **registry.snapshot(),
                    "requests": {
                        **registry.snapshot()["requests"],
                        "total": "1",
                    },
                }
            ),
            lambda: ErrorResponse(code="INVALID REQUEST", message="bad", request_id="r"),
        ):
            with self.assertRaises(ValidationError):
                constructor()


if __name__ == "__main__":
    unittest.main()
