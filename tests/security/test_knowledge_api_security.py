"""Adversarial HTTP checks for industrial knowledge-governance routes."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import threading
import unittest

from fastapi.testclient import TestClient
import jwt

from graphrag_prod.api import JWTAuthConfig, JWTAuthenticator, create_app
from graphrag_prod.api.knowledge_contracts import (
    ConstructionJobListResponse,
    DocumentLifecycleListResponse,
    DocumentRetirementResponse,
    KnowledgeConstructionResponse,
    OntologyListResponse,
    PublicationCandidatesResponse,
    PublishedGraphQualityResponse,
)
from graphrag_prod.api.runtime import (
    AuthorizationError,
    BackendResult,
    OperationEnvelope,
    OperationKind,
    ResourceNotFoundError,
)


SECRET = "knowledge-api-security-key-with-32-diverse-bytes!"
ISSUER = "https://identity.example.test"
AUDIENCE = "graphrag-api"


def _token(
    *,
    tenant_id: str = "tenant-alpha",
    scope: str = "ontology:read",
    groups: tuple[str, ...] = ("engineers",),
) -> str:
    now = int(datetime.now(UTC).timestamp())
    return jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + 300,
            "sub": "industrial-expert",
            "tenant_id": tenant_id,
            "groups": list(groups),
            "scope": scope,
        },
        SECRET,
        algorithm="HS256",
    )


def _headers(**kwargs: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(**kwargs)}"}


def _construct_body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "operation_key": "construction-000001",
        "canonical_uri": "https://example.test/asset.txt",
        "title": "Asset report",
        "source_name": "controlled upload",
        "mime_type": "text/plain",
        "language": "en",
        "tbox_key": "industrial-assets",
        "access_groups": ["engineers"],
        "content_base64": base64.b64encode(b"Acme owns Pump-7.").decode(),
    }
    body.update(changes)
    return body


class _Backend:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.envelopes: list[OperationEnvelope] = []

    def execute(self, envelope: OperationEnvelope, /) -> BackendResult:
        with self._lock:
            self.envelopes.append(envelope)
        if envelope.operation is OperationKind.ONTOLOGY_LIST:
            return BackendResult(OntologyListResponse(items=()))
        if envelope.operation is OperationKind.KNOWLEDGE_CONSTRUCT:
            return BackendResult(
                KnowledgeConstructionResponse(
                    job_id="job-1",
                    document_id="document-1",
                    version_id="version-1",
                    snapshot_id="snapshot-1",
                    tbox_id="tbox-1",
                    chunks=(
                        {
                            "chunk_id": "chunk-1",
                            "artifact_id": "artifact-1",
                            "status": "REJECTED",
                            "finding_codes": ("NO_RELATIONSHIPS",),
                            "mention_record_ids": (),
                            "assertion_record_ids": (),
                            "replayed": False,
                        },
                    ),
                )
            )
        if envelope.operation is OperationKind.KNOWLEDGE_CONSTRUCTION_JOBS:
            return BackendResult(ConstructionJobListResponse(items=()))
        if envelope.operation is OperationKind.KNOWLEDGE_PUBLICATION_CANDIDATES:
            return BackendResult(PublicationCandidatesResponse(items=()))
        if envelope.operation is OperationKind.KNOWLEDGE_QUALITY:
            return BackendResult(
                PublishedGraphQualityResponse(
                    run_id="published-graph-quality:" + "1" * 64,
                    ruleset_version="published-governed-graph-quality-v1",
                    publication_id="publication-1",
                    publication_generation=1,
                    manifest_hash="2" * 64,
                    ontology_version_id="tbox-1",
                    tbox_checksum="3" * 64,
                    corpus_revision=4,
                    graph_digest="4" * 64,
                    counts={
                        "revisions": 0,
                        "entity_mentions": 0,
                        "assertions": 0,
                        "relationship_assertions": 0,
                        "literal_assertions": 0,
                        "canonical_entities": 0,
                    },
                    total_issue_count=0,
                    total_error_count=0,
                    issues_truncated=False,
                    issues=(),
                    review_sample=(),
                    passed=True,
                )
            )
        if envelope.operation is OperationKind.KNOWLEDGE_DOCUMENTS:
            return BackendResult(
                DocumentLifecycleListResponse(
                    items=(
                        {
                            "document_id": "document-1",
                            "title": "Asset report",
                            "source_name": "controlled upload",
                            "canonical_uri": "urn:industrial:asset-report:1",
                            "source_generation": 3,
                            "active_snapshot_id": "snapshot-3",
                            "active_version_id": "version-3",
                            "chunk_count": 2,
                            "access_policy_id": "policy-engineering",
                            "access_policy_version": 4,
                            "access_groups": ("engineers",),
                            "blocked": False,
                            "blocker_codes": (),
                        },
                    )
                )
            )
        if envelope.operation is OperationKind.KNOWLEDGE_DOCUMENT_RETIRE:
            return BackendResult(
                DocumentRetirementResponse(
                    retirement_id="retirement-1",
                    document_id="document-1",
                    retired_snapshot_id="snapshot-3",
                    retired_version_id="version-3",
                    source_generation_before=3,
                    source_generation_after=4,
                    corpus_revision=8,
                    retired_at=datetime.now(UTC),
                    status="RETIRED",
                )
            )
        if envelope.operation is OperationKind.READINESS:
            return BackendResult({"status": "ready", "checks": {"backend": "ok"}})
        raise ResourceNotFoundError()


class KnowledgeAPISecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = _Backend()
        app = create_app(
            authenticator=JWTAuthenticator(
                JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)
            ),
            backend=self.backend,
        )
        self.context = TestClient(app)
        self.client = self.context.__enter__()

    def tearDown(self) -> None:
        self.context.__exit__(None, None, None)

    def test_each_action_requires_its_independent_verified_scope(self) -> None:
        allowed = self.client.get("/v1/ontologies", headers=_headers())
        denied = self.client.post(
            "/v1/ontologies:import",
            headers=_headers(),
            json={
                "key": "industrial-assets",
                "version": 1,
                "entity_types": [
                    {
                        "name": "Asset",
                        "canonical_key_namespaces": ["asset-id"],
                    }
                ],
            },
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["code"], "forbidden")
        self.assertEqual(len(self.backend.envelopes), 1)

    def test_published_quality_requires_dedicated_scope_and_returns_no_source_text(
        self,
    ) -> None:
        denied = self.client.get(
            "/v1/knowledge/quality",
            headers=_headers(scope="knowledge:review"),
        )
        allowed = self.client.get(
            "/v1/knowledge/quality",
            headers=_headers(
                tenant_id="tenant-alpha",
                scope="knowledge:quality",
                groups=("engineers", "public"),
            ),
        )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["code"], "forbidden")
        self.assertEqual(allowed.status_code, 200)
        self.assertNotIn("tenant_id", allowed.json())
        self.assertNotIn("source_text", allowed.text)
        self.assertNotIn("quoted_text", allowed.text)
        self.assertEqual(len(self.backend.envelopes), 1)
        envelope = self.backend.envelopes[0]
        self.assertEqual(envelope.operation, OperationKind.KNOWLEDGE_QUALITY)
        self.assertEqual(envelope.tenant_id, "tenant-alpha")
        self.assertEqual(envelope.access_groups, frozenset({"engineers", "public"}))
        self.assertEqual(envelope.payload, {})

    def test_document_lifecycle_requires_dedicated_scope_and_is_metadata_only(
        self,
    ) -> None:
        denied = self.client.get(
            "/v1/knowledge/documents?limit=10",
            headers=_headers(scope="knowledge:review"),
        )
        listed = self.client.get(
            "/v1/knowledge/documents?limit=10",
            headers=_headers(
                tenant_id="tenant-alpha",
                scope="knowledge:lifecycle",
                groups=("engineers", "public"),
            ),
        )
        retired = self.client.post(
            "/v1/knowledge/documents/document-1:retire",
            headers=_headers(
                tenant_id="tenant-alpha",
                scope="knowledge:lifecycle",
                groups=("engineers", "public"),
            ),
            json={
                "operation_key": "retirement-operation-0001",
                "expected_active_snapshot_id": "snapshot-3",
                "source_generation": 3,
            },
        )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(retired.status_code, 200)
        for response in (listed, retired):
            self.assertNotIn("tenant_id", response.json())
            self.assertNotIn("source_text", response.text)
            self.assertNotIn("quoted_text", response.text)
        self.assertEqual(len(self.backend.envelopes), 2)
        list_envelope, retire_envelope = self.backend.envelopes
        self.assertEqual(list_envelope.operation, OperationKind.KNOWLEDGE_DOCUMENTS)
        self.assertEqual(list_envelope.payload, {"limit": 10})
        self.assertEqual(
            retire_envelope.operation,
            OperationKind.KNOWLEDGE_DOCUMENT_RETIRE,
        )
        self.assertEqual(retire_envelope.tenant_id, "tenant-alpha")
        self.assertNotIn("tenant_id", retire_envelope.payload)
        self.assertNotIn("principal_id", retire_envelope.payload)
        self.assertEqual(
            retire_envelope.payload["request"]["expected_active_snapshot_id"],
            "snapshot-3",
        )

    def test_document_retirement_missing_cross_tenant_and_partial_acl_are_one_error(
        self,
    ) -> None:
        class _NonEnumeratingBackend(_Backend):
            def execute(self, envelope: OperationEnvelope, /) -> BackendResult:
                if envelope.operation is OperationKind.KNOWLEDGE_DOCUMENT_RETIRE:
                    raise AuthorizationError()
                return super().execute(envelope)

        backend = _NonEnumeratingBackend()
        app = create_app(
            authenticator=JWTAuthenticator(
                JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)
            ),
            backend=backend,
        )
        cases = (
            ("tenant-alpha", ("engineers",), "missing-document"),
            ("tenant-other", ("engineers",), "document-1"),
            ("tenant-alpha", ("public",), "document-1"),
        )
        with TestClient(app) as client:
            responses = tuple(
                client.post(
                    f"/v1/knowledge/documents/{document_id}:retire",
                    headers=_headers(
                        tenant_id=tenant_id,
                        scope="knowledge:lifecycle",
                        groups=groups,
                    ),
                    json={
                        "operation_key": "retirement-operation-0001",
                        "expected_active_snapshot_id": "snapshot-3",
                        "source_generation": 3,
                    },
                )
                for tenant_id, groups, document_id in cases
            )
        public = tuple(
            (response.status_code, response.json()["code"], response.json()["message"])
            for response in responses
        )
        self.assertEqual(len(set(public)), 1)
        self.assertEqual(
            public[0],
            (403, "forbidden", "the operation is not permitted"),
        )
        self.assertNotIn("missing-document", "".join(response.text for response in responses))

    def test_document_lifecycle_input_bounds_fail_before_backend(self) -> None:
        invalid_list = self.client.get(
            "/v1/knowledge/documents?limit=101",
            headers=_headers(scope="knowledge:lifecycle"),
        )
        injected = self.client.post(
            "/v1/knowledge/documents/document-1:retire",
            headers=_headers(scope="knowledge:lifecycle"),
            json={
                "operation_key": "retirement-operation-0001",
                "expected_active_snapshot_id": "snapshot-3",
                "source_generation": 3,
                "tenant_id": "tenant-victim",
            },
        )
        malformed = self.client.post(
            "/v1/knowledge/documents/document-1:retire",
            headers=_headers(scope="knowledge:lifecycle"),
            json={
                "operation_key": "short",
                "expected_active_snapshot_id": "snapshot-3",
                "source_generation": 3,
            },
        )
        self.assertEqual(
            tuple(item.status_code for item in (invalid_list, injected, malformed)),
            (422, 422, 422),
        )
        self.assertEqual(self.backend.envelopes, [])

    def test_partial_acl_quality_denial_is_generic_and_non_leaking(self) -> None:
        class _CompletePublicationBackend(_Backend):
            def execute(self, envelope: OperationEnvelope, /) -> BackendResult:
                if (
                    envelope.operation is OperationKind.KNOWLEDGE_QUALITY
                    and "public" not in envelope.access_groups
                ):
                    raise AuthorizationError()
                return super().execute(envelope)

        backend = _CompletePublicationBackend()
        app = create_app(
            authenticator=JWTAuthenticator(
                JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)
            ),
            backend=backend,
        )
        with TestClient(app) as client:
            response = client.get(
                "/v1/knowledge/quality",
                headers=_headers(
                    scope="knowledge:quality",
                    groups=("engineers",),
                ),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["message"], "the operation is not permitted")
        self.assertNotIn("public", response.text)
        self.assertNotIn("revision", response.text)

    def test_identity_and_capability_injection_never_reaches_backend(self) -> None:
        for forbidden in (
            "tenant_id",
            "principal_id",
            "capabilities",
        ):
            with self.subTest(forbidden=forbidden):
                response = self.client.post(
                    "/v1/knowledge:construct",
                    headers=_headers(scope="knowledge:construct"),
                    json=_construct_body(**{forbidden: "tenant-victim"}),
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "invalid_request")
                self.assertNotIn("tenant-victim", response.text)
        self.assertEqual(self.backend.envelopes, [])

    def test_construction_acl_is_bounded_before_worker_submission(self) -> None:
        for groups in ([], ["engineers", "engineers"]):
            with self.subTest(groups=groups):
                response = self.client.post(
                    "/v1/knowledge:construct",
                    headers=_headers(scope="knowledge:construct"),
                    json=_construct_body(access_groups=groups),
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "invalid_request")
        unauthorized = self.client.post(
            "/v1/knowledge:construct",
            headers=_headers(scope="knowledge:construct"),
            json=_construct_body(access_groups=["board"]),
        )
        self.assertEqual(unauthorized.status_code, 403)
        self.assertEqual(unauthorized.json()["code"], "forbidden")
        self.assertEqual(self.backend.envelopes, [])

    def test_multi_group_identity_preserves_the_selected_narrow_acl(self) -> None:
        response = self.client.post(
            "/v1/knowledge:construct",
            headers=_headers(
                scope="knowledge:construct",
                groups=("engineers", "public"),
            ),
            json=_construct_body(access_groups=["engineers"]),
        )
        self.assertEqual(response.status_code, 200)
        envelope = self.backend.envelopes[0]
        self.assertEqual(envelope.access_groups, frozenset({"engineers", "public"}))
        self.assertEqual(envelope.payload["access_groups"], ("engineers",))

    def test_upload_mime_and_base64_fail_closed_before_worker_submission(self) -> None:
        for changes in (
            {"mime_type": "application/pdf"},
            {"content_base64": "not base64"},
            {"content_base64": "eA"},
        ):
            with self.subTest(changes=changes):
                response = self.client.post(
                    "/v1/knowledge:construct",
                    headers=_headers(scope="knowledge:construct"),
                    json=_construct_body(**changes),
                )
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.backend.envelopes, [])

    def test_valid_upload_uses_only_jwt_identity_in_bounded_envelope(self) -> None:
        response = self.client.post(
            "/v1/knowledge:construct",
            headers=_headers(
                tenant_id="tenant-industrial",
                scope="knowledge:construct ontology:read",
            ),
            json=_construct_body(),
        )
        self.assertEqual(response.status_code, 200)
        envelope = self.backend.envelopes[0]
        self.assertEqual(envelope.operation, OperationKind.KNOWLEDGE_CONSTRUCT)
        self.assertEqual(envelope.tenant_id, "tenant-industrial")
        self.assertEqual(envelope.access_groups, frozenset({"engineers"}))
        self.assertEqual(
            envelope.scopes,
            frozenset({"knowledge:construct", "ontology:read"}),
        )
        for forbidden in (
            "tenant_id",
            "principal_id",
            "capabilities",
        ):
            self.assertNotIn(forbidden, envelope.payload)
        self.assertEqual(envelope.payload["access_groups"], ("engineers",))

    def test_missing_and_cross_tenant_ids_have_identical_public_response(self) -> None:
        responses = tuple(
            self.client.post(
                "/v1/ontologies/unknown-tbox:publish",
                headers=_headers(tenant_id=tenant, scope="ontology:publish"),
                json={"expected_active_tbox_id": None},
            )
            for tenant in ("tenant-alpha", "tenant-other")
        )
        self.assertEqual(
            tuple(
                (item.status_code, item.json()["code"], item.json()["message"])
                for item in responses
            ),
            (
                (404, "not_found", "the requested resource was not found"),
                (404, "not_found", "the requested resource was not found"),
            ),
        )

    def test_recovery_reads_require_independent_scopes_and_keep_no_existence_boundary(self) -> None:
        jobs = self.client.get(
            "/v1/knowledge/construction-jobs?status=RETRY_WAIT&limit=25",
            headers=_headers(scope="knowledge:construct"),
        )
        candidates = self.client.get(
            "/v1/knowledge/publication-candidates?limit=100",
            headers=_headers(scope="knowledge:publish"),
        )
        denied = self.client.get(
            "/v1/knowledge/construction-jobs",
            headers=_headers(scope="knowledge:review"),
        )
        invalid = self.client.get(
            "/v1/knowledge/construction-jobs?status=UNKNOWN",
            headers=_headers(scope="knowledge:construct"),
        )
        self.assertEqual(jobs.status_code, 200)
        self.assertEqual(jobs.json(), {"items": []})
        self.assertEqual(candidates.status_code, 200)
        self.assertEqual(candidates.json(), {"items": []})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(invalid.status_code, 422)

        for path, scope in (
            ("/v1/knowledge/construction-jobs/missing-job", "knowledge:construct"),
            ("/v1/knowledge/records/missing-record/revisions", "knowledge:review"),
        ):
            responses = tuple(
                self.client.get(
                    path,
                    headers=_headers(tenant_id=tenant, scope=scope),
                )
                for tenant in ("tenant-alpha", "tenant-other")
            )
            self.assertEqual(
                tuple(
                    (item.status_code, item.json()["code"], item.json()["message"])
                    for item in responses
                ),
                (
                    (404, "not_found", "the requested resource was not found"),
                    (404, "not_found", "the requested resource was not found"),
                ),
            )

    def test_entity_resolution_requires_review_scope_and_hides_target_existence(self) -> None:
        denied = self.client.get(
            "/v1/knowledge/entity-resolution/candidate-1?expected_revision=1",
            headers=_headers(scope="ontology:read"),
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.backend.envelopes, [])

        responses = tuple(
            self.client.get(
                "/v1/knowledge/entity-resolution/candidate-1?expected_revision=1",
                headers=_headers(
                    tenant_id=tenant,
                    scope="knowledge:review",
                ),
            )
            for tenant in ("tenant-alpha", "tenant-other")
        )
        self.assertEqual(
            tuple((item.status_code, item.json()["code"]) for item in responses),
            ((404, "not_found"), (404, "not_found")),
        )
        self.assertEqual(
            [item.operation for item in self.backend.envelopes],
            [
                OperationKind.ENTITY_RESOLUTION_SUGGEST,
                OperationKind.ENTITY_RESOLUTION_SUGGEST,
            ],
        )
        self.assertEqual(
            [item.tenant_id for item in self.backend.envelopes],
            ["tenant-alpha", "tenant-other"],
        )
        self.assertNotIn("tenant_id", self.backend.envelopes[0].payload)

        before = len(self.backend.envelopes)
        forged = self.client.post(
            "/v1/knowledge/entity-resolution:apply",
            headers=_headers(scope="knowledge:review"),
            json={
                "record_id": "candidate-1",
                "expected_revision": 1,
                "target_entity_id": "target-1",
                "notes": "Forged boundary test",
                "tenant_id": "tenant-victim",
            },
        )
        self.assertEqual(forged.status_code, 422)
        self.assertEqual(len(self.backend.envelopes), before)

    def test_relationship_property_identity_and_canonical_values_are_server_owned(
        self,
    ) -> None:
        text = "Pump-7 supplied by Acme at 40 percent."
        base_property = {
            "name": "SupplyShare",
            "literal": {"raw_literal": "40", "raw_unit": "percent"},
            "evidence": {
                "document_id": "document-1",
                "version_id": "version-1",
                "chunk_id": "chunk-1",
                "char_start": 27,
                "char_end": 37,
                "quoted_text": "40 percent",
            },
        }
        body = {
            "ontology_version_id": "tbox-1",
            "mentions": [
                {
                    "source_key": "asset",
                    "entity": {
                        "entity_type": "Asset",
                        "canonical_key": "asset-id:P-7",
                        "canonical_name": "Pump-7",
                    },
                    "evidence": {
                        "document_id": "document-1",
                        "version_id": "version-1",
                        "chunk_id": "chunk-1",
                        "char_start": 0,
                        "char_end": 6,
                        "quoted_text": "Pump-7",
                    },
                },
                {
                    "source_key": "supplier",
                    "entity": {
                        "entity_type": "Organization",
                        "canonical_key": "org-id:ACME",
                        "canonical_name": "Acme",
                    },
                    "evidence": {
                        "document_id": "document-1",
                        "version_id": "version-1",
                        "chunk_id": "chunk-1",
                        "char_start": 19,
                        "char_end": 23,
                        "quoted_text": "Acme",
                    },
                },
            ],
            "assertions": [
                {
                    "source_key": "supply",
                    "subject_mention_source_key": "asset",
                    "object_mention_source_key": "supplier",
                    "predicate": "SUPPLIED_BY",
                    "evidence": {
                        "document_id": "document-1",
                        "version_id": "version-1",
                        "chunk_id": "chunk-1",
                        "char_start": 0,
                        "char_end": len(text),
                        "quoted_text": text,
                    },
                    "relationship_properties": [base_property],
                }
            ],
        }
        before = len(self.backend.envelopes)
        forged_values = (
            {**base_property, "property_value_id": "client-chosen"},
            {
                **base_property,
                "literal": {
                    **base_property["literal"],
                    "canonical_value": "0.4",
                },
            },
        )
        for forged in forged_values:
            with self.subTest(forged=forged):
                body["assertions"][0]["relationship_properties"] = [forged]  # type: ignore[index]
                response = self.client.post(
                    "/v1/knowledge/authoritative:import",
                    headers=_headers(scope="knowledge:import"),
                    json=body,
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "invalid_request")
        self.assertEqual(len(self.backend.envelopes), before)


if __name__ == "__main__":
    unittest.main()
