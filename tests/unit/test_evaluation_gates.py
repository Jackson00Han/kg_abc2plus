"""Contract, invariant, and exact-baseline gate tests."""

from __future__ import annotations

from pathlib import Path
import unittest

from graphrag_prod.evaluation.datasets import load_json
from graphrag_prod.evaluation.gates import (
    EVALUATION_BASELINE_VERSION,
    baseline_candidate,
    baseline_failures,
    contract_metric_rows,
    invariant_failures,
    validate_policy,
)


ROOT = Path(__file__).parents[2]


class EvaluationGateTests(unittest.TestCase):
    def test_contract_operator_and_performance_profile_policy(self) -> None:
        contract = {
            "metrics": [
                {"area": "quality", "id": "quality", "operator": ">=", "target": 0.9, "unit": "ratio"},
                {"area": "performance", "id": "latency", "operator": "<=", "target": 10, "unit": "milliseconds"},
            ]
        }
        rows, failures = contract_metric_rows(
            contract, {"quality": 1.0, "latency": 20}, performance_gates=False
        )
        self.assertEqual(failures, [])
        self.assertFalse(next(row for row in rows if row["id"] == "latency")["passed"])
        _, production_failures = contract_metric_rows(
            contract, {"quality": 1.0, "latency": 20}, performance_gates=True
        )
        self.assertEqual(len(production_failures), 1)

    def test_hard_invariants_and_baseline_are_zero_tolerance(self) -> None:
        policy = load_json(ROOT / "evaluation" / "regression-policy.v1.json")
        validate_policy(policy, "dev-mini")
        observed = dict(policy["hard_invariants"])
        self.assertEqual(invariant_failures(policy, observed), [])
        observed["answer_correctness"] = 0.99
        self.assertTrue(invariant_failures(policy, observed))

        projection = {"metrics": {"recall": 1.0}, "cases": {"one": "abc"}}
        candidate = baseline_candidate(
            profile_id="dev-mini",
            gold_version="2.0.0",
            deterministic_projection=projection,
            semantic_digest="digest",
            rationale="reviewed baseline",
        )
        self.assertEqual(candidate["version"], EVALUATION_BASELINE_VERSION)
        self.assertEqual(
            baseline_failures(
                candidate,
                profile_id="dev-mini",
                gold_version="2.0.0",
                deterministic_projection=projection,
                semantic_digest="digest",
            ),
            [],
        )
        self.assertTrue(
            baseline_failures(
                candidate,
                profile_id="dev-mini",
                gold_version="2.0.0",
                deterministic_projection={"metrics": {"recall": 0.99}},
                semantic_digest="changed",
            )
        )
        stale = dict(candidate)
        stale["version"] = "1.0.0"
        with self.assertRaisesRegex(ValueError, "version is stale"):
            baseline_failures(
                stale,
                profile_id="dev-mini",
                gold_version="2.0.0",
                deterministic_projection=projection,
                semantic_digest="digest",
            )


if __name__ == "__main__":
    unittest.main()
