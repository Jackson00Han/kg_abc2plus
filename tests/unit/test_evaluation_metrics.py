"""Hand-computable Stage 8 metric tests."""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import unittest

from graphrag_prod.evaluation.datasets import load_json
from graphrag_prod.evaluation.metrics import (
    evaluate_graph_results,
    evaluate_operational_observations,
    nearest_rank_percentile,
)
from graphrag_prod.retrieval.metrics import evaluate_retrieval_results


ROOT = Path(__file__).parents[2]


class EvaluationMetricTests(unittest.TestCase):
    def test_complete_and_fractional_recall_are_not_any_hit_rate(self) -> None:
        gold = [
            {
                "answerable": True,
                "forbidden_chunk_ids": [],
                "id": "complete",
                "relevance": {"a": 3},
            },
            {
                "answerable": True,
                "forbidden_chunk_ids": [],
                "id": "partial",
                "relevance": {"b": 3, "c": 2},
            },
        ]
        actual = [
            {"id": "complete", "ranking": ["a"], "visible_resources": []},
            {
                "id": "partial",
                "ranking": ["x", "c", "y", "z", "q", "b"],
                "visible_resources": [],
            },
        ]
        metrics = evaluate_retrieval_results(gold, actual)
        self.assertEqual(metrics.recall_at_5, 0.5)
        self.assertEqual(metrics.evidence_recall_at_5, 0.75)
        self.assertEqual(metrics.mrr, 0.75)
        self.assertAlmostEqual(
            metrics.ndcg_at_5,
            (1.0 + (2**2 - 1) / math.log2(3) / ((2**3 - 1) + (2**2 - 1) / math.log2(3))) / 2,
        )

    def test_retrieval_gold_and_actual_must_be_independent_and_complete(self) -> None:
        gold = [
            {
                "answerable": False,
                "forbidden_chunk_ids": ["secret"],
                "id": "denied",
                "relevance": {},
            }
        ]
        actual = [
            {
                "id": "denied",
                "ranking": [],
                "visible_resources": [
                    {"id": "secret", "kind": "chunk", "stage": "adjacency"}
                ],
            }
        ]
        with self.assertRaisesRegex(ValueError, "answerable items"):
            evaluate_retrieval_results(gold, actual)
        contaminated = [{**gold[0], "ranking": []}]
        with self.assertRaisesRegex(ValueError, "actual-result fields"):
            evaluate_retrieval_results(contaminated, actual)

    def test_required_evidence_groups_allow_alternatives_per_fact(self) -> None:
        gold = [
            {
                "answerable": True,
                "forbidden_chunk_ids": [],
                "id": "alternatives",
                "relevance": {"a": 3, "b": 3, "c": 3},
                "required_evidence_groups": [["a", "b"], ["c"]],
            }
        ]
        actual = [
            {
                "id": "alternatives",
                "ranking": ["a", "c"],
                "visible_resources": [],
            }
        ]
        metrics = evaluate_retrieval_results(gold, actual)
        self.assertEqual(metrics.recall_at_5, 1.0)
        self.assertEqual(metrics.evidence_recall_at_5, 2 / 3)

    def test_required_groups_and_top_five_boundary_fail_closed(self) -> None:
        gold = [
            {
                "answerable": True,
                "forbidden_chunk_ids": [],
                "id": "and-groups",
                "relevance": {"a": 3, "b": 3},
                "required_evidence_groups": [["a"], ["b"]],
            }
        ]
        rank_five = [
            {
                "id": "and-groups",
                "ranking": ["x1", "x2", "x3", "a", "b"],
                "visible_resources": [],
            }
        ]
        self.assertEqual(
            evaluate_retrieval_results(gold, rank_five).recall_at_5, 1.0
        )
        rank_six = deepcopy(rank_five)
        rank_six[0]["ranking"] = ["x1", "x2", "x3", "x4", "a", "b"]
        self.assertEqual(
            evaluate_retrieval_results(gold, rank_six).recall_at_5, 0.0
        )

    def test_direct_retrieval_inputs_reject_invalid_gold_types(self) -> None:
        actual = [{"id": "bad", "ranking": [], "visible_resources": []}]
        for answerable, grade in ((1, 3), (True, True), (True, math.inf), (True, 4)):
            gold = [
                {
                    "answerable": answerable,
                    "forbidden_chunk_ids": [],
                    "id": "bad",
                    "relevance": {"a": grade},
                }
            ]
            with self.subTest(answerable=answerable, grade=grade), self.assertRaises(
                ValueError
            ):
                evaluate_retrieval_results(gold, actual)

    def test_graph_metrics_use_separate_gold_and_actual_decisions(self) -> None:
        gold = [
            {"id": "e1", "kind": "entity", "expected": {"adjudicated_correct": True}},
            {"id": "e2", "kind": "entity", "expected": {"adjudicated_correct": False}},
            {"id": "r1", "kind": "relationship", "expected": {"adjudicated_supported": True}},
            {"id": "r2", "kind": "relationship", "expected": {"adjudicated_supported": False}},
            {"id": "p1", "kind": "resolution", "expected": {"outcome": "MERGE"}},
            {"id": "p2", "kind": "resolution", "expected": {"outcome": "KEEP_SEPARATE"}},
            {"id": "p3", "kind": "resolution", "expected": {"outcome": "HUMAN_REVIEW"}},
        ]
        actual = [
            {"id": "e1", "kind": "entity", "accepted": True},
            {"id": "e2", "kind": "entity", "accepted": True},
            {"id": "r1", "kind": "relationship", "accepted": True},
            {"id": "r2", "kind": "relationship", "accepted": True},
            {"id": "p1", "kind": "resolution", "predicted_outcome": "MERGE"},
            {"id": "p2", "kind": "resolution", "predicted_outcome": "MERGE"},
            {"id": "p3", "kind": "resolution", "predicted_outcome": "HUMAN_REVIEW"},
        ]
        metrics = evaluate_graph_results(gold, actual)
        self.assertEqual(metrics.entity_precision, 0.5)
        self.assertEqual(metrics.relationship_precision, 0.5)
        self.assertEqual(metrics.entity_resolution_accuracy, 2 / 3)
        self.assertEqual(metrics.case_outcome_accuracy, 4 / 7)

    def test_nearest_rank_latency_and_operational_costs_are_exact(self) -> None:
        self.assertEqual(nearest_rank_percentile(list(range(1, 21)), 0.95), 19)
        self.assertEqual(nearest_rank_percentile([10, 20, 30, 40, 50], 0.95), 50)
        payload = load_json(
            ROOT / "evaluation" / "observations" / "dev-mini-operational-v2.json"
        )
        metrics = evaluate_operational_observations(payload)
        self.assertEqual(metrics.retrieval_p95_ms, 190)
        self.assertEqual(metrics.answer_p95_ms, 1200)
        self.assertAlmostEqual(metrics.retrieval_throughput_rps, 250 / 30)
        self.assertEqual(metrics.estimated_cost_usd, 0.0006)
        self.assertEqual(metrics.mean_answer_cost_usd, 0.00012)
        self.assertEqual(metrics.recovery_success_rate, 1.0)

    def test_latency_and_cost_reject_empty_nonfinite_and_negative_values(self) -> None:
        for value in ([], [True], [-1], [math.inf], [math.nan]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                nearest_rank_percentile(value, 0.95)
        payload = load_json(
            ROOT / "evaluation" / "observations" / "dev-mini-operational-v2.json"
        )
        for invalid in ("NaN", "Infinity", "-0.01", True):
            changed = deepcopy(payload)
            changed["usage"]["answer_cost_usd"][0] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                evaluate_operational_observations(changed)


if __name__ == "__main__":
    unittest.main()
