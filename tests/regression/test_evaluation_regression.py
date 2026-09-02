"""Stage 8 dataset and split-observation regression checks."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from graphrag_prod.evaluation.answers import calculate_answer_metrics
from graphrag_prod.evaluation.datasets import load_gold_dataset
from graphrag_prod.evaluation.metrics import (
    evaluate_graph_results,
    evaluate_operational_observations,
)
from scripts.capture_conflict_results import capture


ROOT = Path(__file__).parents[2]


class EvaluationRegressionTests(unittest.TestCase):
    def test_split_gold_graph_and_conflict_runtime_results_are_perfect(self) -> None:
        gold = load_gold_dataset(ROOT / "evaluation" / "gold-v1" / "manifest.json")
        graph_actual = json.loads(
            (ROOT / "evaluation" / "observations" / "graph-system-v1.json").read_text(
                encoding="utf-8"
            )
        )["items"]
        graph = evaluate_graph_results(gold.graph_items, graph_actual)
        self.assertEqual(graph.entity_precision, 1.0)
        self.assertEqual(graph.relationship_precision, 1.0)
        self.assertEqual(graph.entity_resolution_accuracy, 1.0)
        self.assertEqual(graph.case_outcome_accuracy, 1.0)

        actual = capture(gold.conflict_answers, gold.conflict_sources)
        answers = calculate_answer_metrics(gold.conflict_answers, actual)
        self.assertEqual(answers.answer_correctness, 1.0)
        self.assertEqual(answers.conflict_handling_rate, 1.0)
        self.assertEqual(answers.temporal_comparison_rate, 1.0)

    def test_stage8_operational_fixture_is_non_qualifying(self) -> None:
        path = ROOT / "evaluation" / "observations" / "dev-mini-operational-v1.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(payload["performance_qualification"])
        metrics = evaluate_operational_observations(payload)
        self.assertEqual(metrics.ingestion_success_rate, 1.0)
        self.assertEqual(metrics.idempotency_mismatch_count, 0)
        self.assertEqual(metrics.deletion_residue_count, 0)


if __name__ == "__main__":
    unittest.main()
