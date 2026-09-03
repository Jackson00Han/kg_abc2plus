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
    KnowledgeConstructionResponse,
    OntologyListResponse,
    OntologyVersionResponse,
    PublicationHistoryResponse,
    PublicationResponse,
    ReviewBatchResponse,
    ReviewQueueResponse,
)
from graphrag_prod.api.runtime import BackendResult


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


class _Documents:
    ingest = delete = get_job = lambda *args, **kwargs: None


class _Queries:
    retrieve = answer = lambda *args, **kwargs: None


class _Readiness:
    check = lambda *args, **kwargs: BackendResult(
        {"status": "ready", "checks": {"backend": "ok"}}
    )


class _Knowledge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []

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
                        "status": "CANDIDATE",
                        "finding_codes": (),
                        "mention_record_ids": ("mention-1",),
                        "assertion_record_ids": (),
                        "replayed": False,
                    },
                ),
            )
        )

    def review_queue(self, principal: object, request: object) -> BackendResult:
        self._record("review_queue", principal, request)
        return BackendResult(ReviewQueueResponse(items=()))

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


class KnowledgeAPIEndToEndTests(unittest.TestCase):
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
                        "content_base64": base64.b64encode(
                            b"Acme owns Pump-7."
                        ).decode(),
                    },
                ),
                client.get("/v1/knowledge/review-queue?limit=10", headers=auth),
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
                client.post(
                    "/v1/knowledge/publications/publication-1:rollback",
                    headers=auth,
                    json={"expected_active_publication_id": "publication-2"},
                ),
                client.get("/v1/knowledge/publications?limit=10", headers=auth),
            )

        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len(knowledge.calls), 10)
        self.assertEqual(
            [name for name, _, _ in knowledge.calls],
            [
                "ontology_list",
                "ontology_import",
                "ontology_publish",
                "authoritative_import",
                "construct",
                "review_queue",
                "review_batch",
                "publish",
                "rollback",
                "history",
            ],
        )
        for _, principal, _ in knowledge.calls:
            self.assertEqual(principal.tenant_id, "tenant-industrial")
            self.assertEqual(principal.groups, frozenset({"engineers"}))
            self.assertEqual(principal.capabilities, frozenset(SCOPES.split()))


if __name__ == "__main__":
    unittest.main()
