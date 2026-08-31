"""Tests for the machine-readable acceptance contract."""

from __future__ import annotations

import copy
import unittest

from scripts.validate_acceptance_contract import load_contract, validate_contract


class AcceptanceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_contract()

    def test_repository_contract_is_valid(self) -> None:
        self.assertEqual(validate_contract(self.contract), [])

    def test_every_question_class_has_success_and_boundary_cases(self) -> None:
        for question_class in self.contract["question_classes"]:
            self.assertGreater(question_class["minimum_success_cases"], 0)
            self.assertGreater(question_class["minimum_boundary_cases"], 0)

    def test_every_metric_is_measurable_and_owned(self) -> None:
        for metric in self.contract["metrics"]:
            self.assertTrue(metric["method"].strip())
            self.assertTrue(metric["dataset_owner"].strip())
            self.assertIn(metric["operator"], {">=", "<=", "="})
            self.assertIsInstance(metric["target"], (int, float))

    def test_missing_dataset_owner_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.contract)
        invalid["metrics"][0]["dataset_owner"] = ""
        self.assertIn("metrics[0] is missing dataset_owner", validate_contract(invalid))

    def test_missing_boundary_cases_are_rejected(self) -> None:
        invalid = copy.deepcopy(self.contract)
        invalid["question_classes"][0]["minimum_boundary_cases"] = 0
        self.assertIn(
            "question_classes[0] minimum_boundary_cases must be a positive integer",
            validate_contract(invalid),
        )


if __name__ == "__main__":
    unittest.main()

