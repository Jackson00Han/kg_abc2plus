"""Automatic review shares the authenticated, bounded governance HTTP boundary."""
import unittest

import jwt
from fastapi.testclient import TestClient
from pydantic import ValidationError

from graphrag_prod.api import GraphRAGApplicationBackend, JWTAuthConfig, JWTAuthenticator, create_app
from graphrag_prod.api.auto_review_contracts import AutoReviewResponse
from graphrag_prod.api.knowledge_contracts import ReviewEvidenceResponse
from graphrag_prod.api.runtime import BackendResult, OperationKind, required_scope
from tests.e2e.test_knowledge_api import (
    _Documents, _Queries, _Readiness, _Knowledge, _headers, SECRET, ISSUER, AUDIENCE, NOW,
)


def receipt():
    return dict(job_id="job-1", run_id="auto-1", status="PARTIAL", stage="DONE",
                policy_version="auto-review:v1", initiated_by="expert-1", reviewed_by="service:auto-review",
                counts=dict(approved_groups=1, approved_mentions=3, manual_groups=1),
                items=[], updated_at=NOW)


class Knowledge(_Knowledge):
    def auto_review(self, principal, job_id, request=None):
        self._record("auto_review", principal, (job_id, request))
        return BackendResult(AutoReviewResponse(**receipt()))


class AutoReviewAPITests(unittest.TestCase):
    def setUp(self):
        self.knowledge = Knowledge()
        self.client = TestClient(create_app(
            authenticator=JWTAuthenticator(JWTAuthConfig(issuer=ISSUER, audience=AUDIENCE, secret=SECRET)),
            backend=GraphRAGApplicationBackend(documents=_Documents(), queries=_Queries(),
                                              readiness=_Readiness(), knowledge=self.knowledge)))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_get_and_explicit_retry_keep_authenticated_identity(self):
        url = "/v1/knowledge/construction-jobs/job-1/auto-review"
        got = self.client.get(url, headers=_headers())
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["counts"]["approved_mentions"], 3)
        retried = self.client.post(url + ":run", headers=_headers(), json={"retry": True})
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(self.knowledge.calls[-1][1].principal_id, "expert-1")
        self.assertTrue(self.knowledge.calls[-1][2][1].retry)
        self.assertFalse(OperationKind.KNOWLEDGE_AUTO_REVIEW_RUN.is_retry_safe)
        self.assertTrue(OperationKind.KNOWLEDGE_AUTO_REVIEW_RUN.is_write)
        self.assertEqual(required_scope(OperationKind.KNOWLEDGE_AUTO_REVIEW_RUN), "knowledge:review")

    def test_missing_review_permission_cannot_start_or_read(self):
        auth = _headers()
        claims = jwt.decode(auth["Authorization"].split()[1], SECRET, algorithms=["HS256"], audience=AUDIENCE)
        claims["scope"] = "knowledge:construct"
        auth = {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}
        url = "/v1/knowledge/construction-jobs/job-1/auto-review"
        self.assertEqual(self.client.get(url, headers=auth).status_code, 403)
        self.assertEqual(self.client.post(url + ":run", headers=auth, json={}).status_code, 403)
        self.assertEqual(self.knowledge.calls, [])

    def test_request_cannot_supply_tenant_service_identity_or_approval(self):
        response = self.client.post("/v1/knowledge/construction-jobs/job-1/auto-review:run", headers=_headers(),
                                    json={"tenant_id": "other", "reviewed_by": "admin", "approve_all": True})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.knowledge.calls, [])

    def test_review_only_scope_cannot_bypass_construction_job_visibility(self):
        auth = _headers()
        claims = jwt.decode(auth["Authorization"].split()[1], SECRET, algorithms=["HS256"], audience=AUDIENCE)
        claims["scope"] = "knowledge:review"
        auth = {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}
        url = "/v1/knowledge/construction-jobs/job-1/auto-review"
        self.assertEqual(self.client.get(url, headers=auth).status_code, 403)
        self.assertEqual(self.client.post(url + ":run", headers=auth, json={}).status_code, 403)
        self.assertEqual(self.knowledge.calls, [])

    def test_receipt_rejects_raw_provider_output_and_unbounded_items(self):
        parsed = AutoReviewResponse.model_validate({**receipt(), "updated_at": NOW.isoformat()})
        self.assertEqual(parsed.updated_at, NOW)
        with self.assertRaises(ValidationError):
            AutoReviewResponse(**receipt(), raw_response="sensitive prompt")
        row = dict(record_id="r1", record_kind="ENTITY_MENTION", decision="NEEDS_HUMAN",
                   reason_code="IDENTITY_AMBIGUOUS", reason="需要核对来源")
        with self.assertRaises(ValidationError):
            AutoReviewResponse(**{**receipt(), "items": [row] * 201})

    def test_mapping_issues_are_bounded_and_keep_source_paths_separate(self):
        issue = dict(code="PROPERTY_REQUIRED", property_name="project_id", entity_count=24,
                     entity_ids=["e-1"], record_ids=["m-1"],
                     reason="必填属性尚未形成可审核事实。", source_paths=["$.metadata.project_id"])
        parsed = AutoReviewResponse(**receipt(), issues=[issue])
        self.assertEqual(parsed.issues[0].entity_count, 24)
        self.assertEqual(parsed.counts.manual_groups, 1)
        self.assertEqual(AutoReviewResponse(**receipt()).issues, [])
        with self.assertRaises(ValidationError):
            AutoReviewResponse(**receipt(), issues=[{**issue, "raw_source": "not public"}])
        with self.assertRaises(ValidationError):
            AutoReviewResponse(**receipt(), issues=[issue] * 101)

    def test_review_evidence_retains_whitespace_for_exact_highlights(self):
        text = '      "owner": "设备-A",\n'
        quote = "设备-A"
        start = 100 + text.index(quote)
        response = ReviewEvidenceResponse(record_id="m-1", revision=2, document_id="d-1",
            version_id="v-1", chunk_id="c-1", document_title="拓扑", source_uri=None,
            text=text, context_start=100, context_end=100+len(text), char_start=start,
            char_end=start+len(quote), quoted_text=quote, total_characters=500,
            document_accessible=True, view="paragraph", has_previous=True, has_next=True)
        self.assertEqual(response.text, text)
        self.assertEqual(response.text[response.char_start-response.context_start:
            response.char_end-response.context_start], response.quoted_text)
