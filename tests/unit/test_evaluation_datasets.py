"""Prediction separation, provenance, coverage, and manifest tests."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from graphrag_prod.evaluation.datasets import (
    PREDICTION_FIELDS,
    _artifact_paths,
    _has_hitting_set,
    load_gold_dataset,
)
from graphrag_prod.evaluation.metrics import evaluate_graph_results


ROOT = Path(__file__).parents[2]
MANIFEST = ROOT / "evaluation" / "gold-v1" / "manifest.json"


class EvaluationDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.gold = load_gold_dataset(MANIFEST)

    def test_gold_manifest_binds_all_cases_exact_evidence_and_conflicts(self) -> None:
        self.assertEqual(self.gold.manifest["dataset_id"], "gold-v1")
        self.assertEqual(self.gold.manifest["version"], "2.0.0")
        self.assertEqual(len(self.gold.questions), 49)
        self.assertEqual(len(self.gold.answers), 49)
        self.assertEqual(len(self.gold.chunks), 120)
        self.assertEqual(len(self.gold.graph_items), 60)
        self.assertEqual(len(self.gold.conflict_answers), 2)
        self.assertEqual(len(self.gold.conflict_sources), 4)
        self.assertTrue(
            all(not (PREDICTION_FIELDS & set(item)) for item in self.gold.questions)
        )
        self.assertTrue(
            all(
                bool(item["required_evidence_groups"]) == item["answerable"]
                for item in self.gold.questions
            )
        )
        self.assertTrue(
            all(not (PREDICTION_FIELDS & set(item)) for item in self.gold.graph_items)
        )
        self.assertEqual(
            {item["expected_status"] for item in self.gold.conflict_answers},
            {"answered", "conflict"},
        )

    def test_negative_and_security_cases_cannot_be_excluded(self) -> None:
        coverage = self.gold.manifest["coverage"]
        self.assertEqual(len(coverage["unauthorized_case_ids"]), 7)
        self.assertEqual(len(coverage["unanswerable_case_ids"]), 7)
        self.assertEqual(len(coverage["required_case_ids"]), 49)
        questions = {item["id"]: item for item in self.gold.questions}
        for item_id in coverage["unauthorized_case_ids"]:
            self.assertTrue(questions[item_id]["forbidden_chunk_ids"])

    def test_manifest_checksum_tamper_fails_closed(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        manifest["artifacts"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            _artifact_paths(manifest, ROOT)

    def test_graph_actual_coverage_must_be_exact(self) -> None:
        actual = json.loads(
            (ROOT / "evaluation" / "observations" / "graph-system-v1.json").read_text(
                encoding="utf-8"
            )
        )["items"]
        self.assertEqual(
            evaluate_graph_results(self.gold.graph_items, actual).case_outcome_accuracy,
            1.0,
        )
        with self.assertRaisesRegex(ValueError, "coverage mismatch"):
            evaluate_graph_results(self.gold.graph_items, actual[:-1])

    def test_required_evidence_must_fit_inside_top_five(self) -> None:
        possible = tuple(
            frozenset(group)
            for group in ({"a", "alt-a"}, {"b"}, {"c"}, {"d"}, {"e"})
        )
        impossible = tuple(frozenset({value}) for value in "abcdef")
        self.assertTrue(_has_hitting_set(possible, maximum_size=5))
        self.assertFalse(_has_hitting_set(impossible, maximum_size=5))


if __name__ == "__main__":
    unittest.main()
