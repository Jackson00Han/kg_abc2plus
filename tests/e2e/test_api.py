"""End-to-end tests for the authenticated FastAPI boundary."""

from __future__ import annotations

from datetime import UTC, datetime
import threading
import time
import unittest

from fastapi.testclient import TestClient
import jwt

from graphrag_prod.api.app import APISettings, create_app
from graphrag_prod.api.auth import JWTAuthConfig, JWTAuthenticator
from graphrag_prod.api.contracts import (
    AnswerResponse,
    DeleteResponse,
    IngestionResponse,
    JobResponse,
    ReadinessResponse,
    RetrievalLimitsRequest,
    RetrievalResponse,
    RetrievalTraceResponse,
    VersionFilterRequest,
)
from graphrag_prod.api.runtime import (
    Backend,
    BackendResult,
    OperationEnvelope,
    OperationKind,
    RateLimitAlgorithm,
    RateLimitPolicy,
    RuntimePolicy,
    UsageMetadata,
)
from graphrag_prod.generation import REFUSAL_ANSWER


_JWT_SECRET = "stage7-HS256-key-with-32-plus-diverse-bytes!"
_ISSUER = "https://identity.example.test"
_AUDIENCE = "graphrag-api"
_ALL_SCOPES = (
    "documents:ingest documents:delete jobs:read retrieval:read "
    "answers:generate metrics:read"
)


def _token(
    *,
    subject: str = "analyst-1",
    tenant_id: str = "tenant-alpha",
    groups: tuple[str, ...] = ("finance", "research"),
    scope: str = _ALL_SCOPES,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": _ISSUER,
            "aud": _AUDIENCE,
            "sub": subject,
            "tenant_id": tenant_id,
            "groups": list(groups),
            "scope": scope,
            "iat": now,
            "exp": now + 300,
        },
        _JWT_SECRET,
        algorithm="HS256",
        headers={"typ": "JWT"},
    )


def _headers(token: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token or _token()}"}


def _job(*, operation: str = "INGEST", job_id: str = "job-1") -> JobResponse:
    return JobResponse(
        job_id=job_id,
        operation=operation,
        status="SUCCEEDED",
        phase="COMPLETE",
        document_id="document-1",
        target_version_id=None,
        target_snapshot_id=None,
        expected_active_snapshot_id=None,
        source_generation=1,
        attempts=1,
        max_attempts=3,
        completed_tasks=4,
        expected_tasks=4,
        outcome="completed",
        last_error_code=None,
    )


def _retrieval_response(tenant_id: str) -> RetrievalResponse:
    return RetrievalResponse(
        chunks=(),
        trace=RetrievalTraceResponse(
            trace_id="retrieval-trace-1",
            method="vector+bm25+rrf+ra+adjacent",
            tenant_id=tenant_id,
            corpus_revision=7,
            embedding_generation_id="embedding-generation-1",
            embedding_space_id="embedding-space-1",
            vector_recall=(),
            bm25_recall=(),
            seed_ranking=(),
            graph_expansion=(),
            candidate_vector_ranking=(),
            final_ranking=(),
            decisions=(),
            selected_chunk_ids=(),
            context_chars=0,
            limits=RetrievalLimitsRequest(),
            version_filter=VersionFilterRequest(),
        ),
    )


class FakeBackend:
    """Thread-safe backend returning strict route-contract fixtures."""

    def __init__(self) -> None:
        self.envelopes: list[OperationEnvelope] = []
        self.ready = True
        self._lock = threading.Lock()

    def execute(self, envelope: OperationEnvelope, /) -> BackendResult:
        with self._lock:
            self.envelopes.append(envelope)
            ready = self.ready

        if envelope.operation is OperationKind.INGESTION:
            return BackendResult(
                IngestionResponse(
                    job=_job(),
                    snapshot_id="snapshot-1",
                    active_snapshot_id="snapshot-1",
                )
            )
        if envelope.operation is OperationKind.DELETION:
            return BackendResult(
                DeleteResponse(job=_job(operation="DELETE", job_id="job-delete-1"))
            )
        if envelope.operation is OperationKind.JOB_STATUS:
            return BackendResult(_job())
        if envelope.operation is OperationKind.RETRIEVAL:
            return BackendResult(
                _retrieval_response(envelope.tenant_id),
                UsageMetadata(
                    retrieval_ms=3.5,
                    input_tokens=8,
                    model_calls=1,
                    estimated_cost_usd=0.00001,
                    stages=(("query_embedding", 1.0), ("retrieval", 2.5)),
                ),
            )
        if envelope.operation is OperationKind.ANSWER:
            return BackendResult(
                AnswerResponse(
                    status="insufficient_context",
                    answer=REFUSAL_ANSWER,
                    claims=(),
                    citations=(),
                    conflicts=(),
                    prompt_version="grounded-answer-v1.3.0",
                    output_schema_version="grounded-answer-schema-v1.0.0",
                    failure_code=None,
                ),
                UsageMetadata(
                    retrieval_ms=2.0,
                    generation_ms=1.0,
                    input_tokens=12,
                    output_tokens=7,
                    model_calls=2,
                    estimated_cost_usd=0.00002,
                    stages=(
                        ("query_embedding", 0.5),
                        ("retrieval", 1.5),
                        ("generation", 1.0),
                    ),
                ),
            )
        if envelope.operation is OperationKind.READINESS:
            return BackendResult(
                ReadinessResponse(
                    status="ready" if ready else "not_ready",
                    checks={"neo4j": "ok" if ready else "error"},
                )
            )
        raise AssertionError(f"unexpected operation: {envelope.operation}")


def _app(
    backend: Backend,
    *,
    rate_limit_policy: RateLimitPolicy | None = None,
    runtime_policy: RuntimePolicy | None = None,
    shutdown_callbacks: tuple[object, ...] = (),
):
    return create_app(
        authenticator=JWTAuthenticator(
            JWTAuthConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                secret=_JWT_SECRET,
                leeway_seconds=0,
            )
        ),
        backend=backend,
        runtime_policy=runtime_policy,
        settings=APISettings(
            service_name="stage7-test-api",
            version="7.0.0-test",
        ),
        rate_limit_policy=rate_limit_policy,
        shutdown_callbacks=shutdown_callbacks,  # type: ignore[arg-type]
    )


class APIEndToEndTests(unittest.TestCase):
    def test_all_endpoint_contracts_and_trusted_envelopes(self) -> None:
        backend = FakeBackend()
        auth = _headers()
        with TestClient(_app(backend)) as client:
            ingestion = client.post(
                "/v1/documents:ingest",
                headers=auth,
                json={
                    "operation_key": "ingestion-operation-0001",
                    "canonical_uri": "s3://trusted-bucket/document-1.txt",
                    "title": "Quarterly report",
                    "source_name": "controlled-upload",
                    "mime_type": "text/plain",
                    "language": "en",
                    "published_at": "2026-08-31T00:00:00Z",
                    "content": "Authorized source text with an exact value of USD 42.",
                    "access_policy_id": "policy-1",
                    "access_policy_version": 2,
                    "access_groups": ["finance"],
                    "source_generation": 1,
                    "max_attempts": 3,
                },
            )
            deletion = client.request(
                "DELETE",
                "/v1/documents/document-1",
                headers=auth,
                json={
                    "operation_key": "deletion-operation-0001",
                    "source_generation": 2,
                },
            )
            job = client.get("/v1/jobs/job-1", headers=auth)
            retrieval = client.post(
                "/v1/retrieval",
                headers=auth,
                json={"query_text": "What was the exact authorized value?"},
            )
            answer = client.post(
                "/v1/answers",
                headers=auth,
                json={"query_text": "What was the exact authorized value?"},
            )
            live = client.get("/health/live")
            ready = client.get("/health/ready")

            observer = _headers(
                _token(
                    subject="observer-1",
                    tenant_id="system",
                    groups=("system-observer",),
                )
            )
            metrics = client.get("/v1/metrics", headers=observer)

        self.assertEqual(ingestion.status_code, 200)
        self.assertEqual(ingestion.json()["job"]["operation"], "INGEST")
        self.assertEqual(deletion.status_code, 200)
        self.assertEqual(deletion.json()["job"]["operation"], "DELETE")
        self.assertEqual(job.status_code, 200)
        self.assertEqual(job.json()["job_id"], "job-1")
        self.assertEqual(retrieval.status_code, 200)
        self.assertEqual(retrieval.json()["trace"]["tenant_id"], "tenant-alpha")
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["status"], "insufficient_context")
        self.assertEqual(
            live.json(),
            {"status": "ok", "service": "stage7-test-api", "version": "7.0.0-test"},
        )
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["status"], "ready")
        self.assertEqual(metrics.status_code, 200)
        for response in (
            ingestion,
            deletion,
            job,
            retrieval,
            answer,
            live,
            ready,
            metrics,
        ):
            self.assertRegex(response.headers["x-request-id"], r"^[A-Za-z0-9._:-]+$")
            self.assertRegex(response.headers["x-trace-id"], r"^[0-9a-f]{32}$")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")

        protected = backend.envelopes[:5]
        self.assertEqual(
            [item.operation for item in protected],
            [
                OperationKind.INGESTION,
                OperationKind.DELETION,
                OperationKind.JOB_STATUS,
                OperationKind.RETRIEVAL,
                OperationKind.ANSWER,
            ],
        )
        for envelope in protected:
            self.assertEqual(envelope.principal_id, "analyst-1")
            self.assertEqual(envelope.tenant_id, "tenant-alpha")
            self.assertEqual(envelope.access_groups, frozenset({"finance", "research"}))
            self.assertNotIn("tenant_id", envelope.payload)
            self.assertNotIn("query_vector", envelope.payload)
        self.assertEqual(
            protected[0].payload["access_groups"],
            ("finance",),
        )
        self.assertEqual(protected[1].payload["document_id"], "document-1")
        self.assertEqual(protected[2].payload, {"job_id": "job-1"})
        self.assertEqual(
            backend.envelopes[-1].operation,
            OperationKind.READINESS,
        )
        self.assertEqual(backend.envelopes[-1].principal_id, "service-health-probe")

    def test_headers_authentication_authorization_and_validation_statuses(self) -> None:
        backend = FakeBackend()
        with TestClient(_app(backend)) as client:
            accepted = client.post(
                "/v1/retrieval",
                headers={**_headers(), "X-Request-ID": "caller-request-123"},
                json={"query_text": "bounded question"},
            )
            unauthenticated = client.post(
                "/v1/retrieval",
                json={"query_text": "bounded question"},
            )
            forbidden = client.post(
                "/v1/documents:ingest",
                headers=_headers(),
                json={
                    "operation_key": "ingestion-operation-0002",
                    "canonical_uri": "s3://trusted-bucket/document-2.txt",
                    "title": "Restricted report",
                    "source_name": "controlled-upload",
                    "content": "restricted text",
                    "access_policy_id": "policy-2",
                    "access_policy_version": 1,
                    "access_groups": ["executive-only"],
                },
            )
            invalid = client.post(
                "/v1/retrieval",
                headers=_headers(),
                json={
                    "query_text": "bounded question",
                    "tenant_id": "attacker-tenant",
                    "query_vector": [1.0, 0.0],
                },
            )
            denied_metrics = client.get("/v1/metrics", headers=_headers())

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.headers["x-request-id"], "caller-request-123")
        self.assertRegex(accepted.headers["x-trace-id"], r"^[0-9a-f]{32}$")
        self.assertEqual(accepted.headers["cache-control"], "no-store")
        self.assertEqual(accepted.headers["x-content-type-options"], "nosniff")
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(unauthenticated.headers["www-authenticate"], "Bearer")
        self.assertEqual(unauthenticated.json()["code"], "unauthenticated")
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json()["code"], "forbidden")
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["code"], "invalid_request")
        self.assertNotIn("attacker-tenant", invalid.text)
        self.assertEqual(denied_metrics.status_code, 403)
        self.assertEqual(len(backend.envelopes), 1)

    def test_timed_out_write_requires_same_operation_key_retry(self) -> None:
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()

        class TimeoutResolutionBackend:
            def __init__(self) -> None:
                self.envelopes: list[OperationEnvelope] = []
                self._lock = threading.Lock()

            def execute(self, envelope: OperationEnvelope, /) -> BackendResult:
                if envelope.operation is not OperationKind.INGESTION:
                    raise AssertionError("only ingestion is expected")
                with self._lock:
                    self.envelopes.append(envelope)
                    call_number = len(self.envelopes)
                if call_number == 1:
                    started.set()
                    release.wait(timeout=2)
                    completed.set()
                return BackendResult(
                    IngestionResponse(
                        job=_job(),
                        snapshot_id="snapshot-1",
                        active_snapshot_id="snapshot-1",
                    )
                )

        backend = TimeoutResolutionBackend()
        body = {
            "operation_key": "timeout-operation-0001",
            "canonical_uri": "s3://trusted-bucket/timeout-document.txt",
            "title": "Timeout resolution report",
            "source_name": "controlled-upload",
            "content": "Authorized source text.",
            "access_policy_id": "policy-1",
            "access_policy_version": 1,
            "access_groups": ["finance"],
        }
        try:
            with TestClient(
                _app(
                    backend,
                    runtime_policy=RuntimePolicy(
                        max_workers=1,
                        max_queue_size=0,
                        timeout_seconds=0.03,
                        max_attempts=3,
                    ),
                )
            ) as client:
                timed_out = client.post(
                    "/v1/documents:ingest",
                    headers=_headers(),
                    json=body,
                )
                self.assertTrue(started.is_set())
                self.assertEqual(timed_out.status_code, 504)
                self.assertEqual(timed_out.json()["code"], "dependency_timeout")

                release.set()
                self.assertTrue(completed.wait(timeout=2))
                resolved = client.post(
                    "/v1/documents:ingest",
                    headers=_headers(),
                    json=body,
                )
        finally:
            release.set()

        self.assertEqual(resolved.status_code, 200)
        self.assertEqual(resolved.json()["job"]["status"], "SUCCEEDED")
        self.assertEqual(len(backend.envelopes), 2)
        self.assertEqual(
            [envelope.payload["operation_key"] for envelope in backend.envelopes],
            [body["operation_key"], body["operation_key"]],
        )

    def test_rate_limit_and_readiness_failure_are_bounded(self) -> None:
        backend = FakeBackend()
        policy = RateLimitPolicy(
            requests=1,
            window_seconds=60.0,
            algorithm=RateLimitAlgorithm.FIXED_WINDOW,
        )
        with TestClient(_app(backend, rate_limit_policy=policy)) as client:
            first = client.get("/v1/jobs/job-1", headers=_headers())
            limited = client.get("/v1/jobs/job-1", headers=_headers())
            backend.ready = False
            unavailable = client.get("/health/ready")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["code"], "rate_limited")
        self.assertGreaterEqual(int(limited.headers["retry-after"]), 1)
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(
            unavailable.json(),
            {"status": "not_ready", "checks": {"neo4j": "error"}},
        )
        self.assertEqual(
            [item.operation for item in backend.envelopes],
            [OperationKind.JOB_STATUS, OperationKind.READINESS],
        )

    def test_metrics_aggregate_usage_and_lifecycle_shutdown(self) -> None:
        backend = FakeBackend()
        shutdown_called: list[datetime] = []

        def shutdown() -> None:
            shutdown_called.append(datetime.now(UTC))

        app = _app(backend, shutdown_callbacks=(shutdown,))
        observer = _headers(
            _token(
                subject="observer-1",
                tenant_id="system",
                groups=("system-observer",),
            )
        )
        with TestClient(app) as client:
            retrieval = client.post(
                "/v1/retrieval",
                headers=_headers(),
                json={"query_text": "observable retrieval"},
            )
            answer = client.post(
                "/v1/answers",
                headers=_headers(),
                json={"query_text": "observable answer"},
            )
            snapshot = client.get("/v1/metrics", headers=observer)
            self.assertFalse(app.state.runner.closed)
            self.assertFalse(app.state.rate_limiter.closed)

        self.assertEqual(retrieval.status_code, 200)
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(snapshot.status_code, 200)
        metrics = snapshot.json()
        self.assertEqual(metrics["requests"]["total"], 2)
        self.assertEqual(metrics["retrieval"]["total"]["count"], 5)
        self.assertEqual(
            set(metrics["retrieval"]["by_stage"]),
            {"generation", "query_embedding", "retrieval"},
        )
        self.assertEqual(metrics["model"]["calls"], 3)
        self.assertEqual(metrics["model"]["input_tokens"], 20)
        self.assertEqual(metrics["model"]["output_tokens"], 7)
        self.assertAlmostEqual(metrics["model"]["estimated_cost_usd"], 0.00003)
        self.assertTrue(app.state.runner.closed)
        self.assertTrue(app.state.rate_limiter.closed)
        self.assertEqual(len(shutdown_called), 1)


if __name__ == "__main__":
    unittest.main()
