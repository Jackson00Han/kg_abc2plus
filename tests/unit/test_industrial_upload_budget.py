"""Uploaded evidence remains exact while fitting the locked contextual renderer."""
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from graphrag_prod.construction.parser import BoundedDocumentParser
from graphrag_prod.industrial.construction import (
    INDUSTRIAL_TENANT, INDUSTRIAL_TBOX_KEY, IndustrialUploadBudgetExceeded,
    IndustrialUploadContext, Neo4jIndustrialUploadPolicy, industrial_upload_parser,
)
from tests.unit.test_construction_workflow import _Extractor, _tbox, _workflow
from tests.unit.test_industrial_runtime import PRINCIPAL, metadata


class IndustrialUploadBudgetTests(unittest.TestCase):
    def setUp(self):
        self.parser = industrial_upload_parser()
        self.policy = Neo4jIndustrialUploadPolicy(object())
        self.policy.resolver = Mock()

    def test_chinese_multichunk_source_preserves_exact_text_without_provider_call(self):
        text = "母线槽检查记录尚未确认故障。" * 180
        parsed = self.parser.parse(text.encode(), mime_type="text/plain")
        self.assertGreater(len(parsed.chunks), 1)
        self.assertLessEqual(len(parsed.chunks), 4)
        self.assertEqual("".join(chunk.text for chunk in parsed.chunks), text)
        with patch("subprocess.Popen", side_effect=AssertionError("preflight must be local")):
            self.policy.validate_parsed(metadata(), parsed)
        for chunk in parsed.chunks:
            self.assertLessEqual(len(chunk.text), 900)
            self.assertEqual(chunk.text, text[chunk.char_start:chunk.char_end])
        self.assertEqual(BoundedDocumentParser().chunking.max_chars, 1200)

    def test_actual_title_and_section_rendering_rejects_bytes_not_character_count(self):
        parsed = self.parser.parse(("测" * 900).encode(), mime_type="text/plain")
        self.policy.validate_parsed(replace(metadata(), title="t" * 512), parsed)
        for value, candidate in (
            (replace(metadata(), title="题" * 512), parsed),
            (metadata(), replace(parsed, chunks=(replace(parsed.chunks[0], section="节" * 512),))),
            (metadata(), self.parser.parse(("🔧" * 900).encode(), mime_type="text/plain")),
        ):
            with self.subTest(title_length=len(value.title)), self.assertRaises(IndustrialUploadBudgetExceeded):
                self.policy.validate_parsed(value, candidate)

    def test_upload_budget_failure_precedes_job_ingestion_and_extraction(self):
        extractor = _Extractor(replace(_tbox(INDUSTRIAL_TENANT), key=INDUSTRIAL_TBOX_KEY))
        workflow, audit, _, pipeline = _workflow(extractor=extractor)
        workflow.industrial_upload_policy = self.policy
        workflow.industrial_parser = self.parser
        with self.assertRaises(IndustrialUploadBudgetExceeded):
            workflow.run(PRINCIPAL, ("测" * 900).encode(), replace(metadata(), title="题" * 512))
        self.assertFalse(audit.jobs)
        self.assertFalse(pipeline.requests)
        self.assertEqual(extractor.calls, 0)

    def test_exact_asset_context_is_part_of_local_provider_input_validation(self):
        parsed = self.parser.parse("检查记录".encode(), mime_type="text/plain")
        value = replace(metadata(), industrial_context=IndustrialUploadContext("canalis-kt", ("asset-bkt-a01",)))
        from graphrag_prod.retrieval.rerank_provider import rerank_cache_identity
        with patch("graphrag_prod.retrieval.rerank_provider.rerank_cache_identity", wraps=rerank_cache_identity) as call:
            self.policy.validate_parsed(value, parsed)
        self.assertIn("User-selected equipment scope: BKT-A01", call.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
