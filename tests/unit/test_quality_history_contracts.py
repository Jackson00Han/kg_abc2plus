"""Response bounds and identity rejection for recorded quality reports."""

import unittest

from pydantic import ValidationError

from graphrag_prod.api.quality_history_contracts import (
    PublishedGraphQualityRecordRequest,
    PublishedGraphQualityRunListRequest,
    PublishedGraphQualityRunListResponse,
    PublishedGraphQualityRunResponse,
    PublishedGraphQualityRunSummaryResponse,
)
from tests.e2e.test_knowledge_api import NOW, _quality


def _summary() -> dict:
    report = _quality().model_dump(mode="python")
    for name in ("manifest_hash", "tbox_checksum", "issues", "review_sample"):
        report.pop(name)
    return {**report, "recorded_by": "expert-1", "recorded_at": NOW, "record_hash": "a" * 64}


class QualityHistoryContractTests(unittest.TestCase):
    def test_record_request_accepts_only_empty_server_owned_input(self):
        self.assertEqual(PublishedGraphQualityRecordRequest().model_dump(), {})
        for name in ("tenant_id", "publication_id", "report", "recorded_by", "passed"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                PublishedGraphQualityRecordRequest.model_validate({name: "forged"})

    def test_list_bounds_and_filters_are_strict(self):
        self.assertEqual(PublishedGraphQualityRunListRequest(limit=50).limit, 50)
        for value in (0, 51, True, "10"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                PublishedGraphQualityRunListRequest(limit=value)
        for value in ("bad id", "a\nb", "a" * 257):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                PublishedGraphQualityRunListRequest(publication_id=value)

    def test_summary_rejects_mismatched_totals_duplicate_runs_and_secrets(self):
        summary = _summary()
        valid = PublishedGraphQualityRunSummaryResponse.model_validate(summary)
        response = PublishedGraphQualityRunListResponse(items=(valid,))
        self.assertEqual(response.items[0].recorded_at, NOW)
        for changes in ({"passed": False}, {"total_error_count": 1}, {"tenant_id": "victim"}, {"source_text": "private"}, {"record_hash": "wrong"}, {"recorded_at": "2026-09-05T12:00:00"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                PublishedGraphQualityRunSummaryResponse.model_validate({**summary, **changes})
        with self.assertRaises(ValidationError):
            PublishedGraphQualityRunListResponse(items=(valid, valid))
        with self.assertRaises(ValidationError):
            PublishedGraphQualityRunListResponse(items=(valid,) * 51)

    def test_detail_round_trip_contains_metadata_only(self):
        response = PublishedGraphQualityRunResponse(
            report=_quality(), recorded_by="expert-1", recorded_at=NOW, record_hash="a" * 64
        )
        serialized = response.model_dump_json()
        self.assertEqual(PublishedGraphQualityRunResponse.model_validate_json(serialized), response)
        for forbidden in ("tenant_id", "quoted_text", "evidence_text", "source_text"):
            self.assertNotIn(forbidden, serialized)
        with self.assertRaises(ValidationError):
            PublishedGraphQualityRunResponse.model_validate({**response.model_dump(), "tenant_id": "victim"})
