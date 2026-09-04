"""HTTP end-to-end contracts for governed industrial knowledge operations."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import time
import unittest

from fastapi.testclient import TestClient
import jwt

from graphrag_prod.api import (
    GraphRAGApplicationBackend,
    JWTAuthConfig,
    JWTAuthenticator,
    create_app,
)
from graphrag_prod.api.knowledge_contracts import (
    AuthoritativeImportResponse,
    ConstructionJobListResponse,
    ConstructionJobResponse,
    EntityResolutionApplyResponse,
    EntityResolutionResponse,
    KnowledgeConstructionResponse,
    OntologyListResponse,
    OntologyVersionResponse,
    PublicationHistoryResponse,
    PublicationCandidatesResponse,
    PublicationResponse,
    PublishedGraphQualityResponse,
    ReviewBatchResponse,
    ReviewQueueResponse,
)
from graphrag_prod.api.runtime import BackendResult, RequestValidationError


SECRET = "knowledge-api-e2e-key-with-32-plus-diverse-bytes!"
ISSUER = "https://identity.example.test"
AUDIENCE = "graphrag-api"
SCOPES = " ".join(
    (
        "ontology:read",
        "ontology:write",
        "ontology:publish",
        "knowledge:import",
        "knowledge:construct",
        "knowledge:review",
        "knowledge:publish",
        "knowledge:quality",
    )
)
NOW = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)


def _headers() -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + 300,
            "sub": "expert-1",
            "tenant_id": "tenant-industrial",
            "groups": ["engineers"],
            "scope": SCOPES,
        },
        SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _ontology(status: str = "DRAFT") -> OntologyVersionResponse:
    return OntologyVersionResponse(
        tbox_id="tbox-1",
        key="industrial-assets",
        version=1,
        status=status,
        checksum="a" * 64,
        entity_types=(
            {
                "name": "Asset",
                "canonical_key_namespaces": ("asset-id",),
            },
        ),
        relationship_types=(),
    )


def _publication() -> PublicationResponse:
    return PublicationResponse(
        publication_id="publication-1",
        ontology_version_id="tbox-1",
        generation=1,
        manifest_hash="b" * 64,
        source_revision_ids=("revision-1",),
        published_revision_ids=("published-revision-1",),
        removed_record_ids=(),
        replaced_record_ids=(),
        status="ACTIVE",
        created_by="expert-1",
        created_at=NOW,
        activated_at=NOW,
    )


def _quality() -> PublishedGraphQualityResponse:
    return PublishedGraphQualityResponse(
        run_id="published-graph-quality:" + "1" * 64,
        ruleset_version="published-governed-graph-quality-v1",
        publication_id="publication-1",
        publication_generation=1,
        manifest_hash="2" * 64,
        ontology_version_id="tbox-1",
        tbox_checksum="3" * 64,
        corpus_revision=7,
        graph_digest="4" * 64,
        counts={
            "revisions": 1,
            "entity_mentions": 1,
            "assertions": 0,
            "relationship_assertions": 0,
            "literal_assertions": 0,
            "canonical_entities": 1,
        },
        total_issue_count=0,
        total_error_count=0,
        issues_truncated=False,
        issues=(),
        review_sample=(),
        passed=True,
    )


def _resolution_suggestion() -> dict[str, object]:
    return {
        "target": {
            "entity_id": "target-entity-1",
            "entity_type": "Asset",
            "canonical_key": "asset-id:P-7",
            "canonical_name": "Pump-7",
            "aliases": [],
        },
        "ontology_version_id": "tbox-1",
        "rule_version": "authoritative-resolution-rules:v1",
        "matcher_version": "tbox-identity-properties:v1",
        "evidence": [
            {
                "match_kind": "EXACT_IDENTITY_PROPERTIES",
                "candidate_value": "serial_number=STRING:P-7",
                "target_value": "serial_number=STRING:P-7",
                "matcher_version": "tbox-identity-properties:v1",
                "authoritative_evidence": [
                    {
                        "mention_revision_id": "authority-revision-1",
                        "document_id": "authority-document-1",
                        "version_id": "authority-version-1",
                        "chunk_id": "authority-chunk-1",
                        "char_start": 0,
                        "char_end": 6,
                        "quoted_text": "Pump-7",
                    }
                ],
            }
        ],
        "confidence": 1.0,
        "outcome": "AUTO_LINK",
        "reason": "unique authoritative identity-property match",
    }


class _Documents:
    ingest = delete = get_job = lambda *args, **kwargs: None


class _Queries:
    retrieve = answer = lambda *args, **kwargs: None


class _Readiness:
    check = lambda *args, **kwargs: BackendResult(
        {"status": "ready", "checks": {"backend": "ok"}}
    )


class _Knowledge:
    def __init__(self, *, construction_status: str = "CANDIDATE") -> None:
        self.calls: list[tuple[str, object, object]] = []
        self.construction_status = construction_status

    def _record(self, name: str, principal: object, request: object) -> None:
        self.calls.append((name, principal, request))

    def ontology_list(self, principal: object, request: object) -> BackendResult:
        self._record("ontology_list", principal, request)
        return BackendResult(OntologyListResponse(items=(_ontology(),)))

    def ontology_import(self, principal: object, request: object) -> BackendResult:
        self._record("ontology_import", principal, request)
        return BackendResult(_ontology())

    def ontology_publish(
        self, principal: object, tbox_id: str, request: object
    ) -> BackendResult:
        self._record("ontology_publish", principal, (tbox_id, request))
        return BackendResult(_ontology("PUBLISHED"))

    def authoritative_import(
        self, principal: object, request: object
    ) -> BackendResult:
        self._record("authoritative_import", principal, request)
        return BackendResult(
            AuthoritativeImportResponse(
                ontology_version_id="tbox-1",
                mention_count=1,
                assertion_count=0,
                revision_ids=("revision-1",),
            )
        )

    def construct(self, principal: object, request: object) -> BackendResult:
        self._record("construct", principal, request)
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
                        "status": self.construction_status,
                        "finding_codes": (),
                        "mention_record_ids": (
                            ()
                            if self.construction_status == "EMPTY"
                            else ("mention-1",)
                        ),
                        "assertion_record_ids": (),
                        "replayed": False,
                    },
                ),
            )
        )

    def construction_job(self, principal: object, job_id: str) -> BackendResult:
        self._record("construction_job", principal, job_id)
        return BackendResult(
            ConstructionJobResponse(
                job_id="job-1",
                document_id="document-1",
                version_id="version-1",
                snapshot_id="snapshot-1",
                tbox_id="tbox-1",
                status="COMPLETED",
                expected_chunks=1,
                completed_chunks=1,
                created_at=NOW,
                updated_at=NOW,
                completed_at=NOW,
            )
        )

    def construction_jobs(self, principal: object, request: object) -> BackendResult:
        self._record("construction_jobs", principal, request)
        return BackendResult(ConstructionJobListResponse(items=()))

    def review_queue(self, principal: object, request: object) -> BackendResult:
        self._record("review_queue", principal, request)
        return BackendResult(ReviewQueueResponse(items=()))

    def revision_history(
        self, principal: object, record_id: str, request: object
    ) -> BackendResult:
        self._record("revision_history", principal, (record_id, request))
        raise RequestValidationError()

    def review_batch(self, principal: object, request: object) -> BackendResult:
        self._record("review_batch", principal, request)
        return BackendResult(
            ReviewBatchResponse(
                outcomes=(
                    {
                        "record_kind": "ENTITY_MENTION",
                        "record_id": "mention-1",
                        "previous_revision_id": "mention-revision-1",
                        "revision_id": "mention-revision-2",
                        "revision": 2,
                        "status": "REJECTED",
                    },
                )
            )
        )

    def resolution_suggestions(
        self, principal: object, request: object
    ) -> BackendResult:
        self._record("resolution_suggestions", principal, request)
        return BackendResult(
            EntityResolutionResponse(
                record_id="mention-1",
                revision_id="mention-revision-1",
                revision=1,
                candidate={
                    "entity_id": "candidate-entity-1",
                    "entity_type": "Asset",
                    "canonical_key": "llm-candidate:p-7",
                    "canonical_name": "Pump seven",
                    "aliases": [],
                },
                identity_properties=(
                    {
                        "name": "serial_number",
                        "datatype": "STRING",
                        "canonical_value": "P-7",
                    },
                ),
                suggestions=(_resolution_suggestion(),),
            )
        )

    def apply_resolution(self, principal: object, request: object) -> BackendResult:
        self._record("apply_resolution", principal, request)
        return BackendResult(
            EntityResolutionApplyResponse(
                outcomes=(
                    {
                        "record_kind": "ENTITY_MENTION",
                        "record_id": "mention-1",
                        "previous_revision_id": "mention-revision-1",
                        "revision_id": "mention-revision-2",
                        "revision": 2,
                        "status": "APPROVED",
                    },
                    {
                        "record_kind": "ASSERTION",
                        "record_id": "assertion-1",
                        "previous_revision_id": "assertion-revision-1",
                        "revision_id": "assertion-revision-2",
                        "revision": 2,
                        "status": "CANDIDATE",
                    },
                ),
                applied_suggestion=_resolution_suggestion(),
            )
        )

    def publish(self, principal: object, request: object) -> BackendResult:
        self._record("publish", principal, request)
        return BackendResult(_publication())

    def rollback(
        self, principal: object, publication_id: str, request: object
    ) -> BackendResult:
        self._record("rollback", principal, (publication_id, request))
        return BackendResult(_publication())

    def history(self, principal: object, request: object) -> BackendResult:
        self._record("history", principal, request)
        return BackendResult(PublicationHistoryResponse(items=(_publication(),)))

    def publication_candidates(
        self, principal: object, request: object
    ) -> BackendResult:
        self._record("publication_candidates", principal, request)
        return BackendResult(PublicationCandidatesResponse(items=()))

    def quality(self, principal: object) -> BackendResult:
        self._record("quality", principal, None)
        return BackendResult(_quality())


class KnowledgeAPIEndToEndTests(unittest.TestCase):
    def test_publication_accepts_removal_only_and_rejects_remove_replace_overlap(
        self,
    ) -> None:
        knowledge = _Knowledge()
        app = create_app(
            authenticator=JWTAuthenticator(
                JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)
            ),
            backend=GraphRAGApplicationBackend(
                documents=_Documents(),
                queries=_Queries(),
                readiness=_Readiness(),
                knowledge=knowledge,
            ),
        )
        with TestClient(app) as client:
            removal = client.post(
                "/v1/knowledge/publications:publish",
                headers=_headers(),
                json={"remove_record_ids": ["record-1"]},
            )
            overlap = client.post(
                "/v1/knowledge/publications:publish",
                headers=_headers(),
                json={
                    "approved_revision_ids": ["revision-2"],
                    "remove_record_ids": ["record-1"],
                    "replace_record_ids": ["record-1"],
                },
            )

        self.assertEqual(removal.status_code, 200)
        self.assertEqual(overlap.status_code, 422)
        self.assertEqual(len(knowledge.calls), 1)
        request = knowledge.calls[0][2]
        self.assertEqual(request.approved_revision_ids, ())
        self.assertEqual(request.remove_record_ids, ("record-1",))

    def test_invalid_declared_unit_returns_http_422(self) -> None:
        class _RejectingKnowledge(_Knowledge):
            def ontology_import(
                self, principal: object, request: object
            ) -> BackendResult:
                self._record("ontology_import", principal, request)
                raise RequestValidationError()

        knowledge = _RejectingKnowledge()
        app = create_app(
            authenticator=JWTAuthenticator(
                JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)
            ),
            backend=GraphRAGApplicationBackend(
                documents=_Documents(),
                queries=_Queries(),
                readiness=_Readiness(),
                knowledge=knowledge,
            ),
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/ontologies:import",
                headers=_headers(),
                json={
                    "key": "industrial-assets",
                    "version": 1,
                    "entity_types": [
                        {
                            "name": "Asset",
                            "canonical_key_namespaces": ["asset-id"],
                            "properties": [
                                {
                                    "name": "pressure",
                                    "datatype": "DECIMAL",
                                    "required": False,
                                    "cardinality": "ZERO_OR_ONE",
                                    "unit": "not_a_real_unit_xyz",
                                }
                            ],
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "invalid_request")
        self.assertEqual(len(knowledge.calls), 1)

    def test_valid_no_fact_upload_returns_http_200_and_empty_chunk(self) -> None:
        app = create_app(
            authenticator=JWTAuthenticator(
                JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)
            ),
            backend=GraphRAGApplicationBackend(
                documents=_Documents(),
                queries=_Queries(),
                readiness=_Readiness(),
                knowledge=_Knowledge(construction_status="EMPTY"),
            ),
        )
        with TestClient(app) as client:
            response = client.post(
                "/v1/knowledge:construct",
                headers=_headers(),
                json={
                    "operation_key": "construction-empty-000001",
                    "canonical_uri": "https://example.test/no-facts.txt",
                    "title": "No ontology facts",
                    "source_name": "controlled upload",
                    "mime_type": "text/plain",
                    "tbox_key": "industrial-assets",
                    "access_groups": ["engineers"],
                    "content_base64": base64.b64encode(
                        b"No ontology facts are stated."
                    ).decode(),
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["chunks"][0]["status"], "EMPTY")

    def test_literal_import_and_review_http_contracts_are_raw_only(self) -> None:
        knowledge = _Knowledge()
        app = create_app(
            authenticator=JWTAuthenticator(
                JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)
            ),
            backend=GraphRAGApplicationBackend(
                documents=_Documents(),
                queries=_Queries(),
                readiness=_Readiness(),
                knowledge=knowledge,
            ),
        )
        quote = "Pump-7 pressure was 100 psi at 2025-01-02T03:04:05Z"
        mention = {
            "source_key": "expert-pump-7",
            "entity": {
                "entity_type": "Asset",
                "canonical_key": "asset-id:P-7",
                "canonical_name": "Pump-7",
            },
            "evidence": {
                "document_id": "document-1",
                "version_id": "version-1",
                "chunk_id": "chunk-1",
                "char_start": 10,
                "char_end": 16,
                "quoted_text": "Pump-7",
            },
        }
        assertion = {
            "source_key": "expert-pressure-1",
            "subject_mention_source_key": "expert-pump-7",
            "predicate": "PRESSURE",
            "evidence": {
                "document_id": "document-1",
                "version_id": "version-1",
                "chunk_id": "chunk-1",
                "char_start": 10,
                "char_end": 10 + len(quote),
                "quoted_text": quote,
            },
            "literal": {
                "raw_literal": "100",
                "raw_unit": "psi",
                "raw_observed_at": "2025-01-02T03:04:05Z",
            },
        }
        auth = _headers()
        with TestClient(app) as client:
            accepted = client.post(
                "/v1/knowledge/authoritative:import",
                headers=auth,
                json={
                    "ontology_version_id": "tbox-1",
                    "mentions": [mention],
                    "assertions": [assertion],
                },
            )
            rejected = client.post(
                "/v1/knowledge/authoritative:import",
                headers=auth,
                json={
                    "ontology_version_id": "tbox-1",
                    "mentions": [mention],
                    "assertions": [
                        {
                            **assertion,
                            "literal": {
                                **assertion["literal"],
                                "canonical_value": "689.4757293168",
                            },
                        }
                    ],
                },
            )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(len(knowledge.calls), 1)
        parsed = knowledge.calls[0][2]
        self.assertEqual(parsed.assertions[0].literal.raw_literal, "100")
        self.assertEqual(parsed.assertions[0].literal.raw_unit, "psi")
        self.assertFalse(hasattr(parsed.assertions[0].literal, "canonical_value"))

    def test_all_governance_routes_share_auth_runner_and_typed_backend(self) -> None:
        knowledge = _Knowledge()
        backend = GraphRAGApplicationBackend(
            documents=_Documents(),
            queries=_Queries(),
            readiness=_Readiness(),
            knowledge=knowledge,
        )
        app = create_app(
            authenticator=JWTAuthenticator(
                JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)
            ),
            backend=backend,
        )
        auth = _headers()
        with TestClient(app) as client:
            responses = (
                client.get("/v1/ontologies?limit=10", headers=auth),
                client.post(
                    "/v1/ontologies:import",
                    headers=auth,
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
                ),
                client.post(
                    "/v1/ontologies/tbox-1:publish",
                    headers=auth,
                    json={"expected_active_tbox_id": None},
                ),
                client.post(
                    "/v1/knowledge/authoritative:import",
                    headers=auth,
                    json={
                        "ontology_version_id": "tbox-1",
                        "mentions": [
                            {
                                "source_key": "expert-pump-7",
                                "entity": {
                                    "entity_type": "Asset",
                                    "canonical_key": "asset-id:P-7",
                                    "canonical_name": "Pump-7",
                                },
                                "evidence": {
                                    "document_id": "document-1",
                                    "version_id": "version-1",
                                    "chunk_id": "chunk-1",
                                    "char_start": 10,
                                    "char_end": 16,
                                    "quoted_text": "Pump-7",
                                },
                            }
                        ],
                    },
                ),
                client.post(
                    "/v1/knowledge:construct",
                    headers=auth,
                    json={
                        "operation_key": "construction-000001",
                        "canonical_uri": "https://example.test/asset.txt",
                        "title": "Asset report",
                        "source_name": "controlled upload",
                        "mime_type": "text/plain",
                        "tbox_key": "industrial-assets",
                        "access_groups": ["engineers"],
                        "content_base64": base64.b64encode(
                            b"Acme owns Pump-7."
                        ).decode(),
                    },
                ),
                client.get(
                    "/v1/knowledge/construction-jobs/job-1",
                    headers=auth,
                ),
                client.get(
                    "/v1/knowledge/construction-jobs?status=COMPLETED&limit=10",
                    headers=auth,
                ),
                client.get("/v1/knowledge/review-queue?limit=10", headers=auth),
                client.get(
                    "/v1/knowledge/entity-resolution/mention-1?expected_revision=1",
                    headers=auth,
                ),
                client.post(
                    "/v1/knowledge/entity-resolution:apply",
                    headers=auth,
                    json={
                        "record_id": "mention-1",
                        "expected_revision": 1,
                        "target_entity_id": "target-entity-1",
                        "notes": "Expert verified exact identity properties.",
                    },
                ),
                client.post(
                    "/v1/knowledge/reviews:batch",
                    headers=auth,
                    json={
                        "decisions": [
                            {
                                "record_kind": "ENTITY_MENTION",
                                "record_id": "mention-1",
                                "expected_revision": 1,
                                "decision": "REJECTED",
                                "notes": "Incorrect identity",
                            }
                        ]
                    },
                ),
                client.post(
                    "/v1/knowledge/publications:publish",
                    headers=auth,
                    json={"approved_revision_ids": ["revision-1"]},
                ),
                client.get(
                    "/v1/knowledge/publication-candidates?limit=10",
                    headers=auth,
                ),
                client.post(
                    "/v1/knowledge/publications/publication-1:rollback",
                    headers=auth,
                    json={"expected_active_publication_id": "publication-2"},
                ),
                client.get("/v1/knowledge/publications?limit=10", headers=auth),
                client.get("/v1/knowledge/quality", headers=auth),
            )

        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len(knowledge.calls), 16)
        self.assertEqual(
            [name for name, _, _ in knowledge.calls],
            [
                "ontology_list",
                "ontology_import",
                "ontology_publish",
                "authoritative_import",
                "construct",
                "construction_job",
                "construction_jobs",
                "review_queue",
                "resolution_suggestions",
                "apply_resolution",
                "review_batch",
                "publish",
                "publication_candidates",
                "rollback",
                "history",
                "quality",
            ],
        )
        for _, principal, _ in knowledge.calls:
            self.assertEqual(principal.tenant_id, "tenant-industrial")
            self.assertEqual(principal.groups, frozenset({"engineers"}))
            self.assertEqual(principal.capabilities, frozenset(SCOPES.split()))


if __name__ == "__main__":
    unittest.main()
