"""HTTP wiring for read-only review guidance and explicit duplicate dismissal."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
import time
import unittest

from fastapi.testclient import TestClient
import jwt

from graphrag_prod.api import GraphRAGApplicationBackend, JWTAuthConfig, JWTAuthenticator, create_app
from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.knowledge.review import ReviewBatchResult, ReviewOutcome, ReviewRecordKind
from graphrag_prod.knowledge.review_assessment import ReviewAssessment, ReviewDependency
from graphrag_prod.knowledge.trust import GovernanceStatus
from tests.fixtures.knowledge import make_knowledge_batch


class ReviewAssessmentHTTPTests(unittest.TestCase):
    def test_assessment_route_scope_revision_and_duplicate_request_contract(self):
        batch = make_knowledge_batch()
        record = batch.assertions[0]
        assessment = ReviewAssessment(
            record.record_id, record.revision_id, 1, "BLOCKED", "ENDPOINTS_REQUIRE_REVIEW", "请先确认实体。",
            (ReviewDependency("subject", batch.mentions[0].record_id, batch.mentions[0].revision_id,
                              "Apple", "CANDIDATE", False, batch.mentions[0].evidence),),
        )
        calls = []

        def assess(principal, record_id, expected_revision):
            calls.append((principal, record_id, expected_revision))
            return assessment

        def review_batch(principal, requests):
            calls.append(requests)
            return ReviewBatchResult(principal.tenant_id, (ReviewOutcome(
                ReviewRecordKind.ASSERTION, record.record_id, record.revision_id,
                "reviewed-revision", 2, GovernanceStatus.REJECTED,
            ),))

        knowledge = Neo4jKnowledgeOperations(
            driver=object(), database="neo4j", construction=SimpleNamespace(run=lambda: None),
            assessment_service=SimpleNamespace(assess=assess), reviews=SimpleNamespace(review_batch=review_batch),
            clock=lambda: datetime.now(UTC),
        )
        unused = lambda *args: None
        backend = GraphRAGApplicationBackend(
            documents=SimpleNamespace(ingest=unused, delete=unused, get_job=unused),
            queries=SimpleNamespace(retrieve=unused, answer=unused),
            readiness=SimpleNamespace(check=unused), knowledge=knowledge,
        )
        secret = "review-assessment-local-test-key-with-diverse-bytes!"
        auth = JWTAuthenticator(JWTAuthConfig(issuer="https://identity.test", audience="review-api", secret=secret))

        def headers(scope):
            now = int(time.time())
            token = jwt.encode(dict(iss="https://identity.test", aud="review-api", sub="expert", iat=now,
                                    exp=now + 300, tenant_id=batch.tenant_id, groups=["finance-readers"], scope=scope),
                               secret, algorithm="HS256")
            return {"Authorization": f"Bearer {token}"}

        with TestClient(create_app(authenticator=auth, backend=backend)) as client:
            url = f"/v1/knowledge/review-assessments/{record.record_id}"
            denied = client.get(url, params={"expected_revision": 1}, headers=headers("ontology:read"))
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(calls, [])
            invalid = client.get(url, params={"expected_revision": 0}, headers=headers("knowledge:review"))
            self.assertEqual(invalid.status_code, 422)
            response = client.get(url, params={"expected_revision": 1}, headers=headers("knowledge:review"))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "BLOCKED")
            self.assertEqual(calls[0][1:], (record.record_id, 1))
            self.assertIn("knowledge:review", calls[0][0].capabilities)
            decision = dict(record_kind="ASSERTION", record_id=record.record_id, expected_revision=1,
                            decision="REJECTED", notes="Keep the authority and source audit.",
                            duplicate_of_revision_id="authority-revision")
            response = client.post("/v1/knowledge/reviews:batch", json={"decisions": [decision]}, headers=headers("knowledge:review"))
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(calls[-1][0].duplicate_of_revision_id, "authority-revision")
            decision["decision"] = "APPROVED"
            response = client.post("/v1/knowledge/reviews:batch", json={"decisions": [decision]}, headers=headers("knowledge:review"))
            self.assertEqual(response.status_code, 422)
