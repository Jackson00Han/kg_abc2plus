"""Negative/security scope cannot disappear from industrial acceptance."""

from copy import deepcopy
from pathlib import Path
import unittest

from graphrag_prod.industrial.contract import load_industrial_contract, validate_industrial_contract


class IndustrialContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_industrial_contract(
            Path(__file__).resolve().parents[2] / "contracts/industrial_knowledge.v1.json"
        )

    def test_contract_has_measurable_industrial_and_negative_cases(self) -> None:
        validate_industrial_contract(self.contract)
        self.assertEqual(self.contract["scope"]["manufacturer"], "Schneider Electric")

    def test_dropping_a_negative_class_is_rejected(self) -> None:
        for index in range(len(self.contract["question_classes"])):
            changed = deepcopy(self.contract)
            changed["question_classes"].pop(index)
            with self.assertRaises(ValueError):
                validate_industrial_contract(changed)

    def test_evidence_and_authorization_targets_cannot_be_relaxed(self) -> None:
        for metric_id, target in (("authorization_violations", 0.1), ("citation_location_accuracy", 0.99), ("product_scope_violations", 1)):
            changed = deepcopy(self.contract)
            next(item for item in changed["evaluation"]["metrics"] if item["id"] == metric_id)["target"] = target
            with self.assertRaises(ValueError):
                validate_industrial_contract(changed)

    def test_scale_cannot_remove_bounds_or_make_production_claims(self) -> None:
        modifications = [
            ("resources", "online_source_bytes", 100 * 1024 * 1024),
            ("resources", "maximum_graph_expansion_hops", 20),
            ("corpus", "maximum_published_records", 501),
            ("corpus", "synthetic_is_not_real_field_evidence", False),
            ("evaluation", "negative_cases_must_be_reported", False),
        ]
        for section, key, value in modifications:
            changed = deepcopy(self.contract)
            changed[section][key] = value
            with self.assertRaises(ValueError):
                validate_industrial_contract(changed)
        changed = deepcopy(self.contract)
        changed["production_candidate_eligible"] = True
        with self.assertRaises(ValueError):
            validate_industrial_contract(changed)
