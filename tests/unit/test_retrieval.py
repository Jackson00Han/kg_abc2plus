"""Unit tests for Stage 5 ranking, bounds, metrics, and query contracts."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
import math
from pathlib import Path
import unittest

from graphrag_prod.domain import Principal
from graphrag_prod.retrieval.engine import (
    ADJACENT_QUERY,
    BM25_RECALL_QUERY,
    CANDIDATE_VECTOR_QUERY,
    GRAPH_EXPANSION_QUERY,
    HYDRATE_QUERY,
    VECTOR_RECALL_QUERY,
    _content_deduplication_key,
    _query_terms,
)
from graphrag_prod.retrieval.metrics import (
    evaluate_retrieval_dataset,
    evaluate_retrieval_items,
)
from graphrag_prod.retrieval.models import (
    Citation,
    RetrievalLimits,
    RetrievalRequest,
    VersionFilter,
)
from graphrag_prod.retrieval.ranking import (
    reciprocal_rank_fusion,
    resource_allocation_score,
    select_context,
    stable_deduplicate,
)


ROOT = Path(__file__).parents[2]


class RetrievalRankingTests(unittest.TestCase):
    def test_rrf_uses_standard_formula_and_ignores_channel_duplicates(self) -> None:
        order, scores, positions = reciprocal_rank_fusion(
            {"vector": ["a", "b", "a"], "bm25": ["b", "c"]},
            rank_constant=60,
        )
        self.assertEqual(order, ("b", "a", "c"))
        self.assertAlmostEqual(scores["a"], 1 / 61)
        self.assertAlmostEqual(scores["b"], 1 / 62 + 1 / 61)
        self.assertEqual(positions["a"], {"vector": 1})

    def test_rrf_ties_break_on_stable_chunk_id(self) -> None:
        order, _, _ = reciprocal_rank_fusion({"vector": ["b"], "bm25": ["a"]})
        self.assertEqual(order, ("a", "b"))

    def test_resource_allocation_is_the_established_inverse_degree_sum(self) -> None:
        self.assertAlmostEqual(resource_allocation_score([2, 4]), 0.75)
        with self.assertRaisesRegex(ValueError, "positive"):
            resource_allocation_score([0])
        with self.assertRaisesRegex(ValueError, "positive"):
            resource_allocation_score([2.5])  # type: ignore[list-item]

    def test_deduplication_keeps_first_rank_and_exact_content(self) -> None:
        kept, removed = stable_deduplicate(
            ["a", "b", "a", "c"],
            {"a": "same", "b": "same", "c": "different"},
        )
        self.assertEqual(kept, ("a", "c"))
        self.assertEqual(removed, ("b", "a"))

    def test_content_deduplication_never_erases_version_provenance(self) -> None:
        records = {
            "first": {"version_id": "version-one", "chunk_checksum": "same"},
            "local-copy": {
                "version_id": "version-one",
                "chunk_checksum": "same",
            },
            "other-version": {
                "version_id": "version-two",
                "chunk_checksum": "same",
            },
        }
        kept, removed = stable_deduplicate(
            tuple(records),
            {
                chunk_id: _content_deduplication_key(record)
                for chunk_id, record in records.items()
            },
        )
        self.assertEqual(kept, ("first", "other-version"))
        self.assertEqual(removed, ("local-copy",))

    def test_context_budget_never_truncates_a_chunk(self) -> None:
        selected = select_context(
            ranked_ids=["a", "b", "c"],
            anchor_ids=["a"],
            adjacent_ids=["b"],
            char_lengths={"a": 6, "b": 5, "c": 4},
            max_chunks=3,
            max_chars=10,
        )
        self.assertEqual(selected.chunk_ids, ("a", "c"))
        self.assertEqual(selected.total_chars, 10)
        self.assertIn(("b", "character_budget"), selected.skipped)


class RetrievalContractTests(unittest.TestCase):
    def test_citation_adds_optional_document_provenance_compatibly(self) -> None:
        citation = Citation(
            chunk_id="chunk",
            chunk_checksum="chunk-checksum",
            document_id="document",
            canonical_uri="https://example.com/document",
            source_name="fixture",
            version_id="version",
            version_checksum="version-checksum",
            version_number=1,
            ordinal=0,
            char_start=0,
            char_end=5,
            page_number=None,
            section=None,
        )
        self.assertIsNone(citation.document_title)
        self.assertIsNone(citation.published_at)

        enriched = dataclasses.replace(
            citation,
            document_title="Authoritative title",
            published_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
        self.assertEqual(enriched.document_title, "Authoritative title")
        self.assertEqual(
            enriched.published_at,
            datetime(2024, 1, 1, tzinfo=UTC),
        )

    def test_limits_validate_cross_field_and_score_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "bm25_scan_k"):
            RetrievalLimits(bm25_recall_k=10, bm25_scan_k=9)
        for value in (-0.1, 1.1):
            with self.subTest(minimum_vector_score=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "minimum_vector_score must be between zero and one",
                ):
                    RetrievalLimits(minimum_vector_score=value)
        with self.assertRaisesRegex(ValueError, "finite number"):
            RetrievalLimits(minimum_vector_score=True)
        with self.assertRaisesRegex(ValueError, "anchor_k"):
            RetrievalLimits(top_k=2, anchor_k=3)
        with self.assertRaisesRegex(ValueError, "boolean"):
            RetrievalLimits(deduplicate_content=1)  # type: ignore[arg-type]

    def test_request_requires_nonzero_finite_vector(self) -> None:
        principal = Principal("reader", "tenant", frozenset({"readers"}))
        with self.assertRaisesRegex(ValueError, "non-zero"):
            RetrievalRequest("query", (0.0, 0.0), principal, "space-v1")
        with self.assertRaisesRegex(ValueError, "finite"):
            RetrievalRequest("query", (1.0, math.inf), principal, "space-v1")
        with self.assertRaisesRegex(ValueError, "query_embedding_space_id"):
            RetrievalRequest("query", (1.0, 0.0), principal, " ")

    def test_version_cutoff_must_be_aware(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            VersionFilter(published_at_or_before=datetime(2024, 1, 1))
        cutoff = datetime(2024, 1, 1, tzinfo=UTC)
        self.assertEqual(VersionFilter(published_at_or_before=cutoff).published_at_or_before, cutoff)

    def test_lucene_input_is_reduced_to_literal_terms(self) -> None:
        self.assertEqual(_query_terms('revenue +(margin): "cash"'), "revenue margin cash")
        self.assertEqual(_query_terms("???"), "")

    def test_every_data_path_has_tenant_acl_active_version_and_stable_ids(self) -> None:
        for query in (
            VECTOR_RECALL_QUERY,
            BM25_RECALL_QUERY,
            GRAPH_EXPANSION_QUERY,
            CANDIDATE_VECTOR_QUERY,
            ADJACENT_QUERY,
            HYDRATE_QUERY,
        ):
            self.assertIn("$tenant_id", query)
            self.assertIn("access_groups", query)
            self.assertIn("ACTIVE_SNAPSHOT", query)
            self.assertIn("ACTIVE_VERSION", query)
            self.assertIn("version_ids", query)
            self.assertIn("chunk_id", query)
            self.assertNotIn("elementId", query)
        self.assertIn("document.title AS document_title", HYDRATE_QUERY)
        self.assertIn("version.published_at AS published_at", HYDRATE_QUERY)


class RetrievalMetricTests(unittest.TestCase):
    def test_hand_computable_metric_fixture(self) -> None:
        items = [
            {
                "id": "one",
                "answerable": True,
                "relevance": {"a": 3},
                "ranking": ["a"],
                "unauthorized_exposures": [],
            },
            {
                "id": "two",
                "answerable": True,
                "relevance": {"b": 3},
                "ranking": ["x", "b"],
                "unauthorized_exposures": [],
            },
            {
                "id": "denied",
                "answerable": False,
                "relevance": {},
                "ranking": [],
                "unauthorized_exposures": ["protected"],
            },
        ]
        result = evaluate_retrieval_items(items)
        self.assertEqual(result.recall_at_5, 1.0)
        self.assertEqual(result.mrr, 0.75)
        expected_ndcg = (1.0 + 1.0 / math.log2(3)) / 2
        self.assertAlmostEqual(result.ndcg_at_5, expected_ndcg)
        self.assertEqual(result.unauthorized_exposure_count, 1)

    def test_repository_gold_dataset_meets_stage_1_targets(self) -> None:
        result = evaluate_retrieval_dataset(
            ROOT / "evaluation" / "retrieval-gold-v1.json"
        )
        self.assertEqual(result.item_count, 49)
        self.assertGreaterEqual(result.recall_at_5, 0.9)
        self.assertGreaterEqual(result.mrr, 0.8)
        self.assertGreaterEqual(result.ndcg_at_5, 0.85)
        self.assertEqual(result.unauthorized_exposure_count, 0)

    def test_dataset_quota_validation_counts_all_cases(self) -> None:
        path = ROOT / "evaluation" / "retrieval-gold-v1.json"
        payload_result = evaluate_retrieval_dataset(path)
        self.assertEqual(payload_result.item_count, 49)


if __name__ == "__main__":
    unittest.main()
