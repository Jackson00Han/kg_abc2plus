"""Review failures must never turn a durable construction into a failed upload."""
from types import SimpleNamespace
import unittest

from graphrag_prod.api.knowledge_contracts import KnowledgeConstructionRequest
from graphrag_prod.domain import Principal
from tests.unit import test_api_knowledge as fixture
from tests.unit.test_api_knowledge import (
    _Driver, _KnowledgeStore, _Construction, _ConstructionAudit,
    _Publications, _construct_payload,
)
from tests.e2e.test_auto_review_api import receipt


class Reviewer:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    async def run(self, principal, **kwargs):
        self.calls.append((principal, kwargs))
        if self.fail:
            raise RuntimeError("protected provider payload must not appear in response")
        return receipt()

    def get(self, principal, job_id):
        raise RuntimeError("review database unavailable")


class AutoReviewAdapterTests(unittest.TestCase):
    _adapter = fixture.KnowledgeAdapterTests._adapter

    def setUp(self):
        self.principal = Principal("expert-1", "tenant-alpha", frozenset({"engineers"}),
                                   frozenset({"knowledge:construct", "knowledge:review"}))

    def test_failed_review_preserves_construction_and_never_publishes(self):
        construction, publications, reviewer = _Construction(), _Publications(), Reviewer(True)
        adapter = self._adapter(_Driver(), _KnowledgeStore(), construction=construction, publications=publications)
        adapter.auto_reviews = reviewer
        result = adapter.construct(self.principal, KnowledgeConstructionRequest.model_validate(_construct_payload())).payload
        self.assertEqual(result.job_id, "job-1")
        self.assertEqual(result.chunks[0].status, "CANDIDATE")
        self.assertEqual(result.auto_review.status, "FAILED")
        self.assertNotIn("protected", result.model_dump_json())
        self.assertEqual(publications.calls, [])

    def test_review_receives_remaining_request_budget_and_real_principal(self):
        construction, reviewer = _Construction(), Reviewer()
        construction.config = SimpleNamespace(deadline_seconds=30)
        adapter = self._adapter(_Driver(), _KnowledgeStore(), construction=construction)
        adapter.auto_reviews = reviewer
        result = adapter.construct(self.principal, KnowledgeConstructionRequest.model_validate(_construct_payload())).payload
        self.assertEqual(result.auto_review.status, "PARTIAL")
        self.assertIs(reviewer.calls[0][0], self.principal)
        self.assertGreater(reviewer.calls[0][1]["deadline_seconds"], 0)
        self.assertLessEqual(reviewer.calls[0][1]["deadline_seconds"], 30)

    def test_source_only_never_calls_review(self):
        reviewer = Reviewer()
        adapter = self._adapter(_Driver(), _KnowledgeStore(), construction=_Construction(status="SOURCE_ONLY"))
        adapter.auto_reviews = reviewer
        result = adapter.construct(self.principal,
            KnowledgeConstructionRequest.model_validate(_construct_payload(extraction_mode="SOURCE_ONLY"))).payload
        self.assertIsNone(result.auto_review)
        self.assertEqual(reviewer.calls, [])

    def test_review_receipt_failure_does_not_hide_completed_job(self):
        audit = _ConstructionAudit()
        adapter = self._adapter(_Driver(), _KnowledgeStore(), construction_audit=audit)
        adapter.auto_reviews = Reviewer()
        result = adapter.construction_job(self.principal, audit.value.job_id).payload
        self.assertEqual(result.job_id, audit.value.job_id)
        self.assertEqual(result.auto_review.status, "FAILED")

    def test_identity_resume_passes_bounded_scope_without_changing_principal(self):
        from graphrag_prod.api.auto_review_contracts import AutoReviewRunRequest, AutoReviewItem
        from pydantic import ValidationError
        adapter = self._adapter(_Driver(), _KnowledgeStore())
        reviewer = Reviewer()
        adapter.auto_reviews = reviewer
        request = AutoReviewRunRequest(retry=True, resume_identity_record_ids=["mention-1"])
        adapter.auto_review(self.principal, "job-1", request)
        self.assertIs(reviewer.calls[0][0], self.principal)
        self.assertEqual(reviewer.calls[0][1], {"job_id": "job-1", "retry": True,
                                              "resume_identity_record_ids": ("mention-1",)})
        with self.assertRaises(ValidationError):
            AutoReviewRunRequest(resume_identity_record_ids=[f"m-{i}" for i in range(61)])
        with self.assertRaises(ValidationError):
            AutoReviewItem(record_id="r", record_kind="ASSERTION", decision="INCOMPLETE",
                           reason_code="NOT_DONE", reason="尚未处理", input_revision=0)
