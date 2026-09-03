"""Adversarial tests for the Stage 7 HTTP security boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
import json
import threading
import unittest

from fastapi.testclient import TestClient
import jwt

from graphrag_prod.api.app import APISettings, create_app
from graphrag_prod.api.auth import JWTAuthConfig, JWTAuthenticator
from graphrag_prod.api.contracts import MAX_DOCUMENT_BYTES
from graphrag_prod.api.runtime import (
    BackendResult,
    DependencyUnavailableError,
    OperationEnvelope,
    OperationKind,
    ResourceNotFoundError,
)
from graphrag_prod.observability.logging import StructuredJsonLogger


SECRET = "stage7-security-fixture-4Rr!6pQ9xV2mN8cL5sT1"
ISSUER = "https://identity.example.test"
AUDIENCE = "graphrag-api"
ALL_SCOPES = (
    "documents:ingest documents:delete jobs:read retrieval:read "
    "answers:generate metrics:read"
)


def _claims(
    *,
    subject: str = "reader-1",
    tenant_id: str = "tenant-alpha",
    groups: tuple[str, ...] = ("public",),
    scope: str = ALL_SCOPES,
    issued_at: int | None = None,
    expires_at: int | None = None,
) -> dict[str, object]:
    now = int(datetime.now(UTC).timestamp())
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now if issued_at is None else issued_at,
        "exp": now + 600 if expires_at is None else expires_at,
        "sub": subject,
        "tenant_id": tenant_id,
        "groups": list(groups),
        "scope": scope,
    }


def _token(
    *,
    subject: str = "reader-1",
    tenant_id: str = "tenant-alpha",
    groups: tuple[str, ...] = ("public",),
    scope: str = ALL_SCOPES,
    claims: dict[str, object] | None = None,
    algorithm: str = "HS256",
    key: str = SECRET,
) -> str:
    return jwt.encode(
        claims
        or _claims(
            subject=subject,
            tenant_id=tenant_id,
            groups=groups,
            scope=scope,
        ),
        key,
        algorithm=algorithm,
    )


def _authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _delete_body() -> dict[str, object]:
    return {
        "operation_key": "delete-request-000001",
        "source_generation": 1,
        "max_attempts": 3,
    }


def _ingestion_body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "operation_key": "ingest-request-000001",
        "canonical_uri": "https://source.example.test/filing.txt",
        "title": "Authoritative filing",
        "source_name": "regulatory filing",
        "mime_type": "text/plain",
        "language": "en",
        "published_at": "2024-09-28T00:00:00Z",
        "content": "Exact source evidence.",
        "access_policy_id": "policy-public",
        "access_policy_version": 1,
        "access_groups": ["public"],
        "source_generation": 1,
        "max_attempts": 3,
    }
    body.update(changes)
    return body


class _ScopedFakeBackend:
    """A small fake that deliberately hides cross-tenant resource existence."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.envelopes: list[OperationEnvelope] = []

    def reset(self) -> None:
        with self._lock:
            self.envelopes.clear()

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.envelopes)

    def execute(self, envelope: OperationEnvelope, /) -> BackendResult:
        with self._lock:
            self.envelopes.append(envelope)
        if envelope.operation is OperationKind.READINESS:
            return BackendResult({"status": "ready", "checks": {"backend": "ok"}})
        if envelope.operation is OperationKind.JOB_STATUS:
            if (
                envelope.tenant_id != "tenant-alpha"
                or envelope.payload.get("job_id") != "owned-job"
            ):
                raise ResourceNotFoundError()
        if envelope.operation is OperationKind.DELETION:
            if (
                envelope.tenant_id != "tenant-alpha"
                or envelope.payload.get("document_id") != "owned-document"
            ):
                raise ResourceNotFoundError()
        raise ResourceNotFoundError()


class APISecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = _ScopedFakeBackend()
        self.log_stream = StringIO()
        self.authenticator = JWTAuthenticator(
            JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)
        )
        self.app = create_app(
            authenticator=self.authenticator,
            backend=self.backend,
            settings=APISettings(max_request_body_bytes=MAX_DOCUMENT_BYTES),
            logger=StructuredJsonLogger(self.log_stream, service="stage7-security"),
        )
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def test_missing_expired_wrong_algorithm_and_duplicate_authorization_fail_closed(
        self,
    ) -> None:
        now = int(datetime.now(UTC).timestamp())
        expired = _token(
            claims=_claims(issued_at=now - 601, expires_at=now - 61)
        )
        hs512 = _token(
            algorithm="HS512",
            key=(
                "stage7-security-hs512-fixture-key-8Tz!5xC2vB7nM4qL1sP9"
                "-separate-64-byte-key"
            ),
        )
        unsigned = _token(algorithm="none", key="")
        valid = _token()
        attempts: tuple[object, ...] = (
            {},
            _authorization(expired),
            _authorization(hs512),
            _authorization(unsigned),
            [
                ("Authorization", f"Bearer {valid}"),
                ("Authorization", f"Bearer {valid}"),
            ],
        )
        for headers in attempts:
            with self.subTest(header_kind=type(headers).__name__):
                response = self.client.get("/v1/jobs/owned-job", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                body = response.json()
                self.assertEqual(body["code"], "unauthenticated")
                self.assertEqual(body["message"], "authentication is required")
                self.assertNotIn("reader-1", response.text)
                self.assertNotIn(str(headers), response.text)
        self.assertEqual(self.backend.call_count, 0)

    def test_client_cannot_supply_identity_scope_or_query_vector(self) -> None:
        headers = _authorization(_token(groups=("public", "finance-readers")))
        attacks = (
            {"query_text": "What were sales?", "tenant_id": "tenant-victim"},
            {"query_text": "What were sales?", "access_groups": ["executives"]},
            {"query_text": "What were sales?", "query_vector": [1.0, 0.0]},
            {
                "query_text": "What were sales?",
                "graph": {"tenant_id": "tenant-victim"},
            },
            {
                "query_text": "What were sales?",
                "selected_chunk_ids": ["victim-private-chunk"],
            },
        )
        for body in attacks:
            with self.subTest(field=tuple(body)[-1]):
                response = self.client.post(
                    "/v1/retrieval", json=body, headers=headers
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "invalid_request")
        ingestion = _ingestion_body(tenant_id="tenant-victim")
        response = self.client.post(
            "/v1/documents:ingest", json=ingestion, headers=headers
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "invalid_request")
        self.assertEqual(self.backend.call_count, 0)

    def test_ingestion_acl_escalation_is_rejected_before_backend_submission(self) -> None:
        response = self.client.post(
            "/v1/documents:ingest",
            json=_ingestion_body(access_groups=["executives"]),
            headers=_authorization(_token(groups=("public",))),
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "forbidden")
        self.assertEqual(self.backend.call_count, 0)

    def test_validation_errors_do_not_echo_protected_input(self) -> None:
        protected_query = "quarterly-password-Q7! do not disclose"
        response = self.client.post(
            "/v1/retrieval",
            json={"query_text": protected_query, "tenant_id": "tenant-victim"},
            headers=_authorization(_token()),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            {"code", "message", "request_id"},
            set(response.json()),
        )
        self.assertEqual(response.json()["message"], "the request is invalid")
        self.assertNotIn(protected_query, response.text)
        self.assertNotIn("tenant-victim", response.text)
        self.assertNotIn("query_text", response.text)
        self.assertEqual(self.backend.call_count, 0)

    def test_duplicate_and_injected_request_ids_are_replaced(self) -> None:
        duplicate = self.client.get(
            "/health/live",
            headers=[
                ("X-Request-ID", "chosen-request-id"),
                ("X-Request-ID", "attacker-request-id"),
            ],
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertNotIn(
            duplicate.headers["x-request-id"],
            {"chosen-request-id", "attacker-request-id"},
        )
        for injected in (
            "request-id,attacker-id",
            "request-id%0d%0aX-Forged:yes",
            "request id with spaces",
            "x" * 65,
        ):
            with self.subTest(injected=injected):
                response = self.client.get(
                    "/health/live", headers={"X-Request-ID": injected}
                )
                self.assertEqual(response.status_code, 200)
                self.assertNotEqual(response.headers["x-request-id"], injected)
                self.assertRegex(response.headers["x-request-id"], r"^[0-9a-f]{32}$")
                self.assertNotIn("x-forged", json.dumps(dict(response.headers)).lower())

    def test_oversized_content_length_and_chunked_body_are_rejected(self) -> None:
        headers = {
            **_authorization(_token()),
            "Content-Type": "application/json",
            "Content-Length": str(MAX_DOCUMENT_BYTES + 1),
        }
        declared = self.client.post(
            "/v1/documents:ingest", content=b"{}", headers=headers
        )
        self.assertEqual(declared.status_code, 413)
        self.assertEqual(declared.json()["code"], "invalid_request")

        def oversized_chunks():
            block = b"x" * (1024 * 1024)
            for _ in range(6):
                yield block

        chunked = self.client.post(
            "/v1/documents:ingest",
            content=oversized_chunks(),
            headers={
                **_authorization(_token()),
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
        )
        self.assertEqual(chunked.status_code, 413)
        self.assertEqual(chunked.json()["code"], "invalid_request")
        self.assertEqual(self.backend.call_count, 0)

    def test_logs_never_contain_token_query_content_or_uri_credentials(self) -> None:
        sensitive_token = _token(subject="protected-reader")
        sensitive_query = "M&A forecast password S3cr3t-query-value"
        source_content = "Highly protected source content 7719-XY"
        source_uri = "https://api-user:uri-password@source.example.test/secret.txt"
        headers = _authorization(sensitive_token)

        self.client.post(
            "/v1/retrieval",
            json={"query_text": sensitive_query, "tenant_id": "tenant-victim"},
            headers=headers,
        )
        self.client.post(
            "/v1/documents:ingest",
            json=_ingestion_body(
                canonical_uri=source_uri,
                content=source_content,
                access_groups=["executives"],
            ),
            headers=headers,
        )
        logs = self.log_stream.getvalue()
        for protected in (
            sensitive_token,
            "protected-reader",
            sensitive_query,
            source_content,
            source_uri,
            "api-user",
            "uri-password",
        ):
            with self.subTest(protected=protected):
                self.assertNotIn(protected, logs)
        for line in logs.splitlines():
            record = json.loads(line)
            self.assertLessEqual(
                set(record),
                {
                    "timestamp",
                    "level",
                    "service",
                    "event",
                    "request_id",
                    "trace_id",
                    "route",
                    "method",
                    "status",
                    "error_code",
                    "duration_ms",
                },
            )

    def test_backend_custom_error_message_never_crosses_http_boundary(self) -> None:
        protected = "password=audit-leak-value"

        class LeakyBackend:
            def execute(self, envelope: OperationEnvelope, /) -> BackendResult:
                del envelope
                raise DependencyUnavailableError(protected)

        app = create_app(
            authenticator=self.authenticator,
            backend=LeakyBackend(),
            logger=StructuredJsonLogger(StringIO(), service="stage7-error-test"),
        )
        with TestClient(app) as client:
            response = client.get(
                "/v1/jobs/owned-job",
                headers=_authorization(_token()),
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "dependency_unavailable")
        self.assertNotIn(protected, response.text)

    def test_cross_tenant_job_and_delete_match_same_tenant_unknown_responses(self) -> None:
        alpha_headers = _authorization(_token(tenant_id="tenant-alpha"))
        beta_headers = _authorization(_token(tenant_id="tenant-beta"))

        job_cross_tenant = self.client.get(
            "/v1/jobs/owned-job", headers=beta_headers
        )
        job_unknown = self.client.get(
            "/v1/jobs/unknown-job", headers=alpha_headers
        )
        delete_cross_tenant = self.client.request(
            "DELETE",
            "/v1/documents/owned-document",
            json=_delete_body(),
            headers=beta_headers,
        )
        delete_unknown = self.client.request(
            "DELETE",
            "/v1/documents/unknown-document",
            json=_delete_body(),
            headers=alpha_headers,
        )
        for hidden, unknown in (
            (job_cross_tenant, job_unknown),
            (delete_cross_tenant, delete_unknown),
        ):
            self.assertEqual(hidden.status_code, 404)
            self.assertEqual(unknown.status_code, 404)
            self.assertEqual(hidden.json()["code"], unknown.json()["code"])
            self.assertEqual(hidden.json()["message"], unknown.json()["message"])
            self.assertEqual(hidden.json()["code"], "not_found")
        tenants = [envelope.tenant_id for envelope in self.backend.envelopes]
        self.assertEqual(tenants, ["tenant-beta", "tenant-alpha"] * 2)

    def test_metrics_require_both_system_tenant_and_observer_group(self) -> None:
        attempts = (
            (_token(tenant_id="tenant-alpha", groups=("system-observer",)), 403),
            (_token(tenant_id="system", groups=("public",)), 403),
            (_token(tenant_id="system", groups=("system-observer",)), 200),
        )
        for token, expected in attempts:
            with self.subTest(expected=expected):
                response = self.client.get(
                    "/v1/metrics", headers=_authorization(token)
                )
                self.assertEqual(response.status_code, expected)
        self.assertEqual(self.backend.call_count, 0)

    def test_unknown_routes_collapse_to_one_low_cardinality_metric_label(self) -> None:
        for index in range(40):
            response = self.client.get(
                f"/untrusted/path-{index}-secret-customer-{index}"
            )
            self.assertEqual(response.status_code, 404)
        routes = self.app.state.metrics.snapshot()["requests"]["by_route"]
        unknown_routes = {
            key: value for key, value in routes.items() if key == "GET <unknown>"
        }
        self.assertEqual(tuple(unknown_routes), ("GET <unknown>",))
        self.assertGreaterEqual(unknown_routes["GET <unknown>"]["count"], 40)
        for key in routes:
            self.assertNotIn("secret-customer", key)


if __name__ == "__main__":
    unittest.main()
