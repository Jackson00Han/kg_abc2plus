"""Tests for the machine-readable acceptance contract."""

from __future__ import annotations

import copy
import unittest

from scripts.validate_acceptance_contract import load_contract, validate_contract
from scripts.validate_acceptance_contract import (
    DEFAULT_PROFILE_ID,
    ROOT,
    load_profile,
    load_profiles,
    resolve_validation_profile,
    validate_profile,
    validate_profiles,
)


class AcceptanceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = load_contract()
        self.profiles = load_profiles()

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

    def test_repository_profiles_are_valid_and_dev_mini_is_default(self) -> None:
        self.assertEqual(DEFAULT_PROFILE_ID, "dev-mini")
        self.assertEqual(validate_profiles(self.contract, self.profiles), [])

    def test_dev_mini_reduces_only_scale_without_mutating_the_base(self) -> None:
        original = copy.deepcopy(self.contract)
        effective = resolve_validation_profile(
            self.contract,
            self.profiles["dev-mini"],
        )

        self.assertEqual(self.contract, original)
        self.assertEqual(effective["scope"]["maximum_document_bytes"], 262144)
        self.assertEqual(effective["scope"]["minimum_validation_chunks"], 100)
        self.assertEqual(effective["scope"]["retrieval_concurrency"], 2)
        self.assertEqual(
            {item["id"]: item["minimum_items"] for item in effective["datasets"]},
            {"gold-v1": 14, "graph-review-v1": 14, "load-v1": 100},
        )
        self.assertTrue(
            all(
                item["minimum_success_cases"] == 1
                and item["minimum_boundary_cases"] == 1
                for item in effective["question_classes"]
            )
        )
        self.assertEqual(effective["metrics"], self.contract["metrics"])
        self.assertFalse(
            effective["validation_profile"]["production_candidate_eligible"]
        )

    def test_production_reference_preserves_every_contract_value(self) -> None:
        effective = resolve_validation_profile(
            self.contract,
            self.profiles["production-reference"],
        )
        effective.pop("validation_profile")
        self.assertEqual(effective, self.contract)
        self.assertTrue(
            self.profiles["production-reference"][
                "production_candidate_eligible"
            ]
        )

    def test_reduced_profile_cannot_claim_production_eligibility(self) -> None:
        invalid = copy.deepcopy(self.profiles["dev-mini"])
        invalid["production_candidate_eligible"] = True
        self.assertIn(
            "profile[dev-mini] reduced profiles cannot be production eligible",
            validate_profile(self.contract, invalid),
        )

    def test_profile_rejects_semantic_or_metric_overrides(self) -> None:
        invalid = copy.deepcopy(self.profiles["dev-mini"])
        invalid["overrides"]["scope"]["authorization"] = "none"
        invalid["metric_overrides"] = {"unauthorized_exposure_count": 1}
        invalid["metric_policy"]["thresholds"] = "relaxed"
        errors = validate_profile(self.contract, invalid)
        self.assertTrue(any("unknown fields" in error for error in errors))
        self.assertTrue(
            any("cannot override semantic scope field" in error for error in errors)
        )
        self.assertTrue(
            any("metric thresholds cannot be overridden" in error for error in errors)
        )

    def test_profile_rejects_unknown_ids_and_base_version_drift(self) -> None:
        invalid = copy.deepcopy(self.profiles["dev-mini"])
        invalid["base_contract_version"] = "0.0.0"
        invalid["overrides"]["datasets"]["unknown-v1"] = {"minimum_items": 1}
        errors = validate_profile(self.contract, invalid)
        self.assertTrue(any("base_contract_version" in error for error in errors))
        self.assertTrue(any("unknown dataset" in error for error in errors))

    def test_profile_rejects_nonpositive_boolean_or_oversized_scale(self) -> None:
        invalid = copy.deepcopy(self.profiles["dev-mini"])
        invalid["overrides"]["scope"]["retrieval_concurrency"] = True
        invalid["overrides"]["datasets"]["gold-v1"]["minimum_items"] = 0
        invalid["overrides"]["question_classes"][
            "minimum_boundary_cases"
        ] = 99
        errors = validate_profile(self.contract, invalid)
        self.assertTrue(any("retrieval_concurrency" in error for error in errors))
        self.assertTrue(any("gold-v1 minimum_items" in error for error in errors))
        self.assertTrue(any("exceeds the production reference" in error for error in errors))

    def test_profile_rejects_cross_field_dataset_underallocation(self) -> None:
        invalid = copy.deepcopy(load_profile("dev-mini"))
        invalid["overrides"]["datasets"]["gold-v1"]["minimum_items"] = 13
        invalid["overrides"]["datasets"]["load-v1"]["minimum_items"] = 99
        errors = validate_profile(self.contract, invalid)
        self.assertTrue(any("gold-v1 is smaller" in error for error in errors))
        self.assertTrue(any("load-v1 is smaller" in error for error in errors))

    def test_disposable_neo4j_runners_match_dev_resource_caps(self) -> None:
        limits = self.profiles["dev-mini"]["execution"]["neo4j"]
        expected_lines = {
            f'container_memory="{limits["container_memory_mb"]}m"',
            f'container_cpus="{limits["container_cpus"]}"',
            f'heap_initial="{limits["heap_initial_mb"]}m"',
            f'heap_max="{limits["heap_max_mb"]}m"',
            f'pagecache="{limits["pagecache_mb"]}m"',
            f'readiness_attempts="{limits["readiness_timeout_seconds"]}"',
        }
        for runner_name in (
            "run_stage2_neo4j_tests.sh",
            "run_stage3_neo4j_tests.sh",
            "run_stage4_neo4j_tests.sh",
            "run_stage5_neo4j_tests.sh",
            "run_stage5a_neo4j_tests.sh",
            "run_stage6_neo4j_tests.sh",
            "run_stage7_neo4j_tests.sh",
        ):
            with self.subTest(runner=runner_name):
                runner = (ROOT / "scripts" / runner_name).read_text(encoding="utf-8")
                for expected in expected_lines:
                    self.assertIn(expected, runner)
                    variable = expected.split("=", 1)[0]
                    assignments = [
                        line
                        for line in runner.splitlines()
                        if line.startswith(f"{variable}=")
                    ]
                    self.assertEqual(assignments, [expected])

    def test_dev_resource_caps_and_mode_cannot_be_removed(self) -> None:
        unbounded = copy.deepcopy(self.profiles["dev-mini"])
        unbounded["execution"]["neo4j"] = {"mode": "deployment_sized"}
        self.assertTrue(
            any(
                "must use local_capped" in error
                for error in validate_profile(self.contract, unbounded)
            )
        )

        oversized = copy.deepcopy(self.profiles["dev-mini"])
        oversized["execution"]["neo4j"]["container_memory_mb"] = 1_000_000
        oversized["execution"]["neo4j"]["container_cpus"] = 10_000
        errors = validate_profile(self.contract, oversized)
        self.assertTrue(any("container_memory_mb exceeds" in error for error in errors))
        self.assertTrue(any("container_cpus exceeds" in error for error in errors))

    def test_production_reference_execution_cannot_be_weakened(self) -> None:
        invalid = copy.deepcopy(self.profiles["production-reference"])
        invalid["execution"]["answer_latency_samples"] = 1
        invalid["execution"]["sustained_load_seconds"] = 1
        invalid["execution"]["neo4j"] = copy.deepcopy(
            self.profiles["dev-mini"]["execution"]["neo4j"]
        )
        errors = validate_profile(self.contract, invalid)
        self.assertTrue(any("deployment_sized" in error for error in errors))
        self.assertTrue(any("answer_latency_samples is below" in error for error in errors))
        self.assertTrue(any("sustained_load_seconds is below" in error for error in errors))

    def test_resolver_fails_before_applying_semantic_overrides(self) -> None:
        invalid = copy.deepcopy(self.profiles["dev-mini"])
        invalid["overrides"]["scope"]["authorization"] = "none"
        original = copy.deepcopy(self.contract)
        with self.assertRaisesRegex(ValueError, "invalid validation profile"):
            resolve_validation_profile(self.contract, invalid)
        self.assertEqual(self.contract, original)

    def test_malformed_profile_inputs_return_errors_instead_of_tracebacks(self) -> None:
        invalid_id = copy.deepcopy(self.profiles["dev-mini"])
        invalid_id["profile_id"] = []
        self.assertTrue(validate_profile(self.contract, invalid_id))

        invalid_policy = copy.deepcopy(self.profiles["dev-mini"])
        invalid_policy["metric_policy"]["quality_results"] = ["smoke_only"]
        self.assertTrue(validate_profile(self.contract, invalid_policy))

        malformed_profiles = copy.deepcopy(self.profiles)
        malformed_profiles["dev-mini"] = None
        self.assertIn(
            "profile[dev-mini] must be an object",
            validate_profiles(self.contract, malformed_profiles),
        )

        invalid_contract = copy.deepcopy(self.contract)
        del invalid_contract["scope"]["retrieval_concurrency"]
        self.assertEqual(
            validate_profiles(invalid_contract, self.profiles),
            ["base contract is invalid; profiles cannot be validated"],
        )
        with self.assertRaisesRegex(ValueError, "unknown validation profile"):
            load_profile([])

    def test_profile_version_drift_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.profiles["dev-mini"])
        invalid["profile_version"] = "999.999.999"
        self.assertIn(
            "profile[dev-mini] profile_version is unsupported",
            validate_profile(self.contract, invalid),
        )

    def test_malformed_nested_contract_ids_are_rejected_without_traceback(self) -> None:
        mutations = (
            ("dataset id", "datasets", 0, "id"),
            ("question id", "question_classes", 0, "id"),
            ("metric id", "metrics", 0, "id"),
            ("metric area", "metrics", 0, "area"),
            ("metric operator", "metrics", 0, "operator"),
            ("metric dataset", "metrics", 0, "dataset"),
        )
        for name, collection, index, field in mutations:
            with self.subTest(field=name):
                invalid = copy.deepcopy(self.contract)
                invalid[collection][index][field] = ["bad"]
                self.assertTrue(validate_contract(invalid))
                with self.assertRaisesRegex(ValueError, "invalid validation profile"):
                    resolve_validation_profile(invalid, self.profiles["dev-mini"])

    def test_nontext_semantic_fields_and_nonfinite_targets_are_rejected(self) -> None:
        invalid = copy.deepcopy(self.contract)
        invalid["owner"] = ["repository-maintainers"]
        invalid["scope"]["authorization"] = ["tenant_and_access_groups"]
        invalid["metrics"][0]["target"] = float("nan")
        errors = validate_contract(invalid)
        self.assertTrue(any("top-level field: owner" in error for error in errors))
        self.assertTrue(any("scope is missing authorization" in error for error in errors))
        self.assertTrue(any("target must be numeric" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
