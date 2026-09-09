"""HTTP end-to-end contracts for governed industrial knowledge operations."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from importlib.resources import files
import json
import shutil
import subprocess
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
    ActivePublicationInventoryResponse,
    AuthoritativeImportResponse,
    ConstructionJobListResponse,
    ConstructionJobResponse,
    DocumentLifecycleListResponse,
    DocumentRetirementResponse,
    EntityResolutionApplyResponse,
    EntityResolutionResponse,
    KnowledgeConstructionResponse,
    OntologyListResponse,
    OntologyVersionResponse,
    PublicationHistoryResponse,
    PublicationCandidatesResponse,
    PublicationResponse,
    PublicationPreviewResponse,
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
        "knowledge:lifecycle",
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


def _documents() -> DocumentLifecycleListResponse:
    return DocumentLifecycleListResponse(
        items=(
            {
                "document_id": "document-1",
                "title": "Asset report",
                "source_name": "controlled upload",
                "canonical_uri": "https://example.test/asset.txt",
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


def _inventory() -> ActivePublicationInventoryResponse:
    return ActivePublicationInventoryResponse(
        publication_id="publication-1",
        publication_generation=1,
        manifest_hash="b" * 64,
        ontology_version_id="tbox-1",
        document_id="document-1",
        total_record_count=1,
        matching_record_count=1,
        truncated=False,
        items=(
            {
                "record_id": "record-1",
                "revision_id": "published-revision-1",
                "record_kind": "ENTITY_MENTION",
                "governance_status": "PUBLISHED",
                "origin": "EXPERT_IMPORT",
                "authority_level": "AUTHORITATIVE",
                "confidence": 1.0,
                "ontology_key": "Asset",
                "evidence": {
                    "document_id": "document-1",
                    "version_id": "version-1",
                    "chunk_id": "chunk-1",
                    "ordinal": 0,
                    "char_start": 0,
                    "char_end": 6,
                },
                "entity": {
                    "entity_id": "entity-1",
                    "entity_type": "Asset",
                    "canonical_key": "asset-id:P-7",
                    "display_name": "Pump-7",
                },
            },
        ),
    )


def _retirement() -> DocumentRetirementResponse:
    return DocumentRetirementResponse(
        retirement_id="retirement-1",
        document_id="document-1",
        retired_snapshot_id="snapshot-3",
        retired_version_id="version-3",
        source_generation_before=3,
        source_generation_after=4,
        corpus_revision=8,
        retired_at=NOW,
        status="RETIRED",
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
    record_quality = quality_runs = quality_run = lambda *args: None

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

    def publish(self, principal: object, request: object, *, preview_only: bool = False) -> BackendResult:
        self._record("preview" if preview_only else "publish", principal, request)
        if preview_only:
            from graphrag_prod.knowledge.publication_preview import publication_preview
            from tests.fixtures.knowledge import make_knowledge_batch
            batch = make_knowledge_batch()
            return BackendResult(PublicationPreviewResponse.model_validate(publication_preview(
                publication_id="publication", ontology_version_id="tbox-1", manifest_hash="a" * 64,
                base_publication_id=None, before=(), after=(*batch.mentions, *batch.assertions),
                source_revision_ids=request.approved_revision_ids, removed_record_ids=(), replaced_record_ids=())))
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

    def inventory(self, principal: object, request: object) -> BackendResult:
        self._record("inventory", principal, request)
        return BackendResult(_inventory())

    def documents(self, principal: object, request: object) -> BackendResult:
        self._record("documents", principal, request)
        return BackendResult(_documents())

    def retire_document(
        self, principal: object, document_id: str, request: object
    ) -> BackendResult:
        self._record("retire_document", principal, (document_id, request))
        return BackendResult(_retirement())


class KnowledgeAPIEndToEndTests(unittest.TestCase):
    def test_review_context_and_manual_target_requests_keep_auth_and_revision_contracts(self):
        from graphrag_prod.api.knowledge_contracts import ReviewEvidenceResponse

        class ContextKnowledge(_Knowledge):
            def review_evidence(self, principal, request):
                self._record('review_evidence', principal, request)
                return BackendResult(ReviewEvidenceResponse(
                    record_id=request.record_id, revision=request.expected_revision,
                    document_id='doc', version_id='version', chunk_id='chunk',
                    document_title='设备台账', source_uri='https://example.test/ledger',
                    text='设备 BC-P-101 的循环水泵。', context_start=0, context_end=18,
                    char_start=13, char_end=17, quoted_text='循环水泵', total_characters=18,
                    document_accessible=True, view=request.view, has_previous=False, has_next=False))

        knowledge = ContextKnowledge()
        app = create_app(backend=GraphRAGApplicationBackend(documents=_Documents(), queries=_Queries(),
            readiness=_Readiness(), knowledge=knowledge),
            authenticator=JWTAuthenticator(JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)))
        with TestClient(app) as client:
            body = {'record_id':'mention-1', 'expected_revision':1}
            self.assertEqual(client.post('/v1/knowledge/review-evidence', json=body).status_code, 401)
            headers = _headers()
            claims = jwt.decode(headers['Authorization'].split()[1], SECRET,
                algorithms=['HS256'], audience=AUDIENCE, issuer=ISSUER)
            claims['scope'] = 'knowledge:publish'
            denied = {'Authorization':'Bearer '+jwt.encode(claims, SECRET, algorithm='HS256')}
            self.assertEqual(client.post('/v1/knowledge/review-evidence', headers=denied, json=body).status_code, 403)
            response = client.post('/v1/knowledge/review-evidence', headers=headers, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('BC-P-101', response.json()['text'])
            self.assertEqual(client.post('/v1/knowledge/review-evidence', headers=headers,
                json={**body, 'offset':-1}).status_code, 422)
            target = {**body, 'target_entity_id':'entity-canonical', 'target_record_id':'confirmed',
                      'target_expected_revision':2, 'notes':'Verified both source contexts.'}
            response = client.post('/v1/knowledge/entity-resolution:apply', headers=headers, json=target)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(knowledge.calls[-1][2].target_expected_revision, 2)
            target.pop('target_expected_revision')
            self.assertEqual(client.post('/v1/knowledge/entity-resolution:apply', headers=headers, json=target).status_code, 422)

    def test_publication_preview_is_authenticated_complete_and_hash_bound(self):
        knowledge = _Knowledge()
        backend = GraphRAGApplicationBackend(documents=_Documents(), queries=_Queries(),
            readiness=_Readiness(), knowledge=knowledge)
        app = create_app(backend=backend, authenticator=JWTAuthenticator(JWTAuthConfig(
            issuer=ISSUER, audience=AUDIENCE, secret=SECRET)))
        with TestClient(app) as client:
            body = {"approved_revision_ids": ["revision-1"]}
            self.assertEqual(client.post("/v1/knowledge/publications:preview", json=body).status_code, 401)
            response = client.post("/v1/knowledge/publications:preview", headers=_headers(), json=body)
            self.assertEqual(response.status_code, 200, response.text)
            preview = response.json()
            self.assertEqual(preview["schema"], "graphrag-publication-preview-v1")
            self.assertEqual(len(preview["records_after"]), 3)
            self.assertEqual(len(preview["evidence"]), 3)
            body["expected_preview_hash"] = preview["preview_hash"]
            response = client.post("/v1/knowledge/publications:publish", headers=_headers(), json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(knowledge.calls[-1][2].expected_preview_hash, preview["preview_hash"])
            body["expected_preview_hash"] = "invalid"
            self.assertEqual(client.post("/v1/knowledge/publications:publish", headers=_headers(), json=body).status_code, 422)

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

        def build_app():
            return create_app(
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
        with TestClient(build_app()) as client:
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

        # Exercise the actual page serializer and submit handler against the
        # strict HTTP contract, including response-only entity IDs on all paths.
        entity = {**mention["entity"], "entity_id": "entity-1", "aliases": ["P7"]}
        semantics = {
            "datatype": "DECIMAL",
            "raw_value": "100",
            "raw_unit": "psi",
            "raw_observed_at": "2025-01-02T03:04:05Z",
            "canonical_value": "689.4757293168",
            "canonical_unit": "kPa",
        }
        records = [
            {
                "record_kind": "ENTITY_MENTION", "record_id": "mention-1",
                "revision": 1, "confidence": 0.99, "entity": entity,
            },
            {
                "record_kind": "ASSERTION", "record_id": "pressure-1",
                "revision": 1, "confidence": 0.99, "subject": entity,
                "subject_mention_revision_id": "mention-revision-1",
                "predicate": "PRESSURE", "literal_semantics": semantics,
            },
            {
                "record_kind": "ASSERTION", "record_id": "contains-1",
                "revision": 1, "confidence": 0.99, "subject": entity,
                "subject_mention_revision_id": "mention-revision-1",
                "predicate": "CONTAINS",
                "object_entity": {
                    **entity, "entity_id": "entity-2", "canonical_key": "asset-id:S-7",
                    "canonical_name": "Seal-7", "aliases": [],
                },
                "object_mention_revision_id": "mention-revision-2",
                "relationship_properties": [{
                    "property_value_id": "property-1", "name": "PRESSURE",
                    "literal_semantics": semantics, "evidence": assertion["evidence"],
                    "confidence": 0.98,
                }],
            },
        ]
        page = files("graphrag_prod.playground").joinpath("static/index.html").read_text()
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for the UI/API contract check")
        generated = subprocess.run(
            [node, "-e", r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {page, records} = JSON.parse(fs.readFileSync(0, 'utf8'));
const original = JSON.stringify(records);
const requests = [];
const editors = records.map(() => ({value: '', disabled: true, focus() {}}));
const panels = records.map(() => ({hidden: true}));
const toggles = records.map(() => ({setAttribute(key, value) {this[key] = value;}}));
const actions = records.map(() => ['APPROVED', 'REJECTED', 'QUARANTINED'].map(
  reviewAction => ({dataset: {reviewAction}, hidden: false})));
const toasts = [];
const context = {
  state: {reviews: records, approvedRevisions: new Set(), selectedCandidateRevisions: new Set(), publicationCandidates: [], identityEpoch: 0, reviewEpoch: 0, resolutions: new Map(), reviewAssessments: new Map()},
  elements: {
    publicationRevisions: {value: ''}, publicationRemovals: {value: ''},
    reviewList: {querySelector(selector) {
      const index = Number(selector.match(/="(\d+)"/)[1]);
      if (selector.includes('edit-panel')) return panels[index];
      if (selector.includes('edit-toggle')) return toggles[index];
      return editors[index];
    }, querySelectorAll(selector) {
      const match = selector.match(/="(\d+)"/);
      return match ? actions[Number(match[1])] : actions.flat();
    }},
  },
  parseJsonEditor: editor => JSON.parse(editor.value),
  apiRequest: async (path, options) => {
    assert.equal(path, '/v1/knowledge/reviews:batch');
    assert.equal(options.method, 'POST');
    requests.push(JSON.parse(options.body));
    return {outcomes: []};
  },
  showToast: text => toasts.push(text), loadReviews: async () => {}, loadActiveDocuments: async () => {},
  invalidatePublicationPreview() {}, loadPublicationCandidates: async () => {},
};
vm.createContext(context);
for (const [start, end] of [
  ['function literalSemantics(', 'function literalSemanticsMarkup('],
  ['function reviewEdit(', 'function resolutionMarkup('],
  ['function reviewModel(', 'function reviewTechnical('],
  ['function setReviewBusy(', 'async function keepExistingFact('],
  ['function publicationSelectedIds(', 'function publicationCandidateGroups('],
  ['function updatePublicationBusy(', 'function renderPublicationCandidates('],
  ['async function submitReviews(', 'function activePublication('],
]) vm.runInContext(page.slice(page.indexOf(start), page.indexOf(end)), context);
(async () => {
  context.state.resolutions.set(records[0].record_id,{revision:1,identityEpoch:0,reviewEpoch:0,status:'ready',suggestions:[{outcome:'NO_MATCH'}]});
  for (const record of records.slice(1)) context.state.reviewAssessments.set(record.record_id,{revision:1,identityEpoch:0,reviewEpoch:0,status:'READY'});
  for (let i = 0; i < records.length; i++) {
    vm.runInContext(`setReviewEditing(${i}, false)`, context);
  }
  const initial = editors[0].value;
  vm.runInContext('setReviewEditing(0, true)', context);
  assert.equal(panels[0].hidden, false);
  assert.equal(toggles[0].textContent, '取消编辑');
  assert.equal(actions[0][0].hidden, true, 'edited content must be checked before approval');
  assert.equal(actions[0][1].hidden, true);
  editors[0].value = '{invalid JSON';
  await vm.runInContext("submitReviews('QUARANTINED', [0], true)", context);
  assert.equal(requests.length, 0, 'invalid draft must not submit');
  await vm.runInContext("submitReviews('APPROVED', [0, 1])", context);
  assert.equal(requests.length, 0, 'batch review must not silently discard drafts');
  assert.ok(toasts.at(-1).includes('保存或取消'));
  vm.runInContext('setReviewEditing(0, false)', context);
  assert.equal(editors[0].value, initial);
  assert.equal(editors[0].disabled, true);
  assert.equal(panels[0].hidden, true);
  assert.equal(actions[0][0].textContent, '确认新实体');
  assert.equal(actions[0][1].hidden, false);
  assert.equal(requests.length, 0, 'cancel must not submit');
  for (let i = 0; i < records.length; i++) {
    vm.runInContext(`setReviewEditing(${i}, true)`, context);
    const edit = JSON.parse(editors[i].value);
    // Confidence and evidence are protected; edit business fields instead.
    edit.confidence = 0.87;
    editors[i].value = JSON.stringify(edit);
    const before=requests.length;
    await vm.runInContext(`submitReviews('QUARANTINED', [${i}], true)`, context);
    assert.equal(requests.length,before,'readonly metadata must not submit');
    delete edit.confidence;
    if(i===0) edit.standard_name='Pump-7 corrected';
    if(i===1) edit.value='101';
    if(i===2) edit.properties[0].value='101';
    editors[i].value = JSON.stringify(edit);
    await vm.runInContext(`submitReviews('QUARANTINED', [${i}], true)`, context);
    vm.runInContext(`setReviewEditing(${i}, false)`, context);
  }
  await vm.runInContext("submitReviews('APPROVED', [0, 1, 2])", context);
  assert.equal(requests.length, 4);
  assert.equal(JSON.stringify(records), original, 'edit generation mutated the queue');
  process.stdout.write(JSON.stringify(requests));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""],
            input=json.dumps({"page": page, "records": records}),
            text=True, capture_output=True, timeout=15, check=True,
        )
        payloads = json.loads(generated.stdout)
        edited = {"decisions": [payload["decisions"][0] for payload in payloads[:3]]}
        with TestClient(build_app()) as client:
            for index, payload in enumerate(payloads):
                with self.subTest(payload=index):
                    response = client.post(
                        "/v1/knowledge/reviews:batch", headers=auth, json=payload,
                    )
                    self.assertEqual(response.status_code, 200, response.text)
            # Explicitly supplied readonly fields must still be rejected.
            for index, field in (
                (0, "entity"), (1, "subject"), (2, "subject"), (2, "object_entity")
            ):
                invalid = json.loads(json.dumps(edited))
                edit_key = "mention_edit" if index == 0 else "assertion_edit"
                invalid["decisions"][index][edit_key][field]["entity_id"] = "entity-1"
                with self.subTest(index=index, field=field):
                    response = client.post(
                        "/v1/knowledge/reviews:batch", headers=auth, json=invalid,
                    )
                    self.assertEqual(response.status_code, 422)
        self.assertEqual(len(knowledge.calls), 5)
        decisions = [call[2].decisions[0] for call in knowledge.calls[1:4]]
        self.assertEqual(decisions[0].mention_edit.entity.aliases, ("P7",))
        self.assertEqual(decisions[0].mention_edit.confidence, 0.99)
        self.assertEqual(decisions[1].assertion_edit.confidence, 0.99)
        self.assertEqual(decisions[2].assertion_edit.confidence, 0.99)
        self.assertEqual(decisions[1].assertion_edit.literal.raw_unit, "psi")
        self.assertEqual(decisions[1].assertion_edit.literal.raw_literal, "101")
        self.assertEqual(
            decisions[1].assertion_edit.literal.raw_observed_at,
            "2025-01-02T03:04:05Z",
        )
        self.assertEqual(
            decisions[2].assertion_edit.object_entity.canonical_key, "asset-id:S-7"
        )
        prop = decisions[2].assertion_edit.relationship_properties[0]
        self.assertEqual(prop.literal.raw_unit, "psi")
        self.assertEqual(prop.evidence.quoted_text, quote)
        for decision in knowledge.calls[4][2].decisions:
            self.assertIsNone(decision.mention_edit)
            self.assertIsNone(decision.assertion_edit)

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
                client.get(
                    "/v1/knowledge/publication-inventory?document_id=document-1&limit=10",
                    headers=auth,
                ),
                client.get("/v1/knowledge/documents?limit=10", headers=auth),
                client.post(
                    "/v1/knowledge/documents/document-1:retire",
                    headers=auth,
                    json={
                        "operation_key": "retirement-operation-0001",
                        "expected_active_snapshot_id": "snapshot-3",
                        "source_generation": 3,
                    },
                ),
            )

        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len(knowledge.calls), 19)
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
                "inventory",
                "documents",
                "retire_document",
            ],
        )
        for _, principal, _ in knowledge.calls:
            self.assertEqual(principal.tenant_id, "tenant-industrial")
            self.assertEqual(principal.groups, frozenset({"engineers"}))
            self.assertEqual(principal.capabilities, frozenset(SCOPES.split()))
        inventory_call = next(call for call in knowledge.calls if call[0] == "inventory")
        self.assertEqual(inventory_call[2].document_id, "document-1")
        self.assertEqual(inventory_call[2].limit, 10)
        inventory_response = responses[16].json()
        self.assertEqual(inventory_response["items"][0]["entity"]["display_name"], "Pump-7")
        self.assertNotIn("tenant_id", str(inventory_response))
        self.assertNotIn("source_text", str(inventory_response))


if __name__ == "__main__":
    unittest.main()
