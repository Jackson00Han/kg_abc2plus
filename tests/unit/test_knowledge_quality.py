"""Offline tests for versioned knowledge-extraction quality gates."""

from __future__ import annotations

import unittest
from copy import deepcopy

from graphrag_prod.evaluation.knowledge_quality import (
    KNOWLEDGE_BASELINE_SCHEMA_VERSION,
    KNOWLEDGE_GATE_POLICY_SCHEMA_VERSION,
    KNOWLEDGE_GOLD_SCHEMA_VERSION,
    KNOWLEDGE_PREDICTION_SCHEMA_VERSION,
    KNOWLEDGE_REPORT_SCHEMA_VERSION,
    build_knowledge_quality_report,
    knowledge_baseline_candidate,
)


def _evidence(text: str, quote: str, *, occurrence: int = 0) -> dict[str, object]:
    start = -1
    cursor = 0
    for _ in range(occurrence + 1):
        start = text.index(quote, cursor)
        cursor = start + 1
    return {
        "document_id": "doc-positive",
        "chunk_id": "chunk-positive",
        "start": start,
        "end": start + len(quote),
        "quote": quote,
    }


def knowledge_gold() -> dict[str, object]:
    text = (
        "Pump P-1 feeds Line L-1 at 12 bar from "
        "2025-01-01T00:00:00Z. Pump P-1 is the same asset."
    )
    return {
        "schema_version": KNOWLEDGE_GOLD_SCHEMA_VERSION,
        "dataset_id": "industrial-pump-gold",
        "version": "1.0.0",
        "contains_predictions": False,
        "adjudication": {
            "status": "approved",
            "protocol_version": "industrial-review-v1",
            "approved_case_ids": ["positive-01", "negative-01", "security-01"],
        },
        "ontology": {
            "entity_types": ["Pump", "Line"],
            "relationship_types": [
                {
                    "type": "FEEDS",
                    "source_entity_types": ["Pump"],
                    "target_entity_types": ["Line"],
                }
            ],
            "property_types": [
                {
                    "type": "operating_pressure",
                    "owner_entity_types": ["Pump"],
                    "datatype": "DECIMAL",
                    "canonical_unit": "bar",
                }
            ],
        },
        "cases": [
            {
                "case_id": "positive-01",
                "case_class": "positive",
                "document_id": "doc-positive",
                "chunk_id": "chunk-positive",
                "chunk_text": text,
                "entities": [
                    {
                        "id": "mention-pump-primary",
                        "entity_type": "Pump",
                        "canonical_name": "P-1",
                        "mention_text": "Pump P-1",
                        "evidence": _evidence(text, "Pump P-1"),
                    },
                    {
                        "id": "mention-line",
                        "entity_type": "Line",
                        "canonical_name": "L-1",
                        "mention_text": "Line L-1",
                        "evidence": _evidence(text, "Line L-1"),
                    },
                    {
                        "id": "mention-pump-alias",
                        "entity_type": "Pump",
                        "canonical_name": "P-1",
                        "mention_text": "Pump P-1",
                        "evidence": _evidence(text, "Pump P-1", occurrence=1),
                    },
                ],
                "relationships": [
                    {
                        "id": "relationship-feeds",
                        "relationship_type": "FEEDS",
                        "source_mention_id": "mention-pump-primary",
                        "target_mention_id": "mention-line",
                        "evidence": _evidence(text, "Pump P-1 feeds Line L-1"),
                    }
                ],
                "property_facts": [
                    {
                        "id": "property-pressure",
                        "property_type": "operating_pressure",
                        "owner_mention_id": "mention-pump-primary",
                        "datatype": "DECIMAL",
                        "typed_value": "12",
                        "raw_value": "12",
                        "raw_unit": "bar",
                        "canonical_value": "12",
                        "canonical_unit": "bar",
                        "valid_from": "2025-01-01T00:00:00Z",
                        "valid_to": None,
                        "observed_at": None,
                        "raw_valid_from": "2025-01-01T00:00:00Z",
                        "raw_valid_to": None,
                        "raw_observed_at": None,
                        "evidence": _evidence(text, "12 bar from 2025-01-01T00:00:00Z"),
                    }
                ],
                "resolution_pairs": [
                    {
                        "id": "resolution-positive",
                        "left_mention_id": "mention-pump-primary",
                        "right_mention_id": "mention-pump-alias",
                        "should_merge": True,
                    },
                    {
                        "id": "resolution-negative",
                        "left_mention_id": "mention-pump-primary",
                        "right_mention_id": "mention-line",
                        "should_merge": False,
                    },
                ],
            },
            {
                "case_id": "negative-01",
                "case_class": "negative",
                "document_id": "doc-negative",
                "chunk_id": "chunk-negative",
                "chunk_text": "No governed equipment facts are asserted.",
                "entities": [],
                "relationships": [],
                "property_facts": [],
                "resolution_pairs": [],
            },
            {
                "case_id": "security-01",
                "case_class": "security",
                "document_id": "doc-security",
                "chunk_id": "chunk-security",
                "chunk_text": "Restricted tenant data must not be extracted.",
                "entities": [],
                "relationships": [],
                "property_facts": [],
                "resolution_pairs": [],
            },
        ],
    }


def knowledge_predictions(gold: dict[str, object] | None = None) -> dict[str, object]:
    source = deepcopy(gold or knowledge_gold())
    cases = []
    for gold_case in source["cases"]:
        prediction_case = {
            "case_id": gold_case["case_id"],
            "entities": deepcopy(gold_case["entities"]),
            "relationships": deepcopy(gold_case["relationships"]),
            "property_facts": deepcopy(gold_case["property_facts"]),
            "resolution_pairs": [
                {"id": item["id"], "predicted_merge": item["should_merge"]}
                for item in gold_case["resolution_pairs"]
            ],
        }
        for family in ("entities", "relationships", "property_facts"):
            for artifact in prediction_case[family]:
                artifact.update(
                    {
                        "origin": "llm",
                        "authority_level": "secondary",
                        "review_status": "approved",
                    }
                )
        cases.append(prediction_case)
    return {
        "schema_version": KNOWLEDGE_PREDICTION_SCHEMA_VERSION,
        "dataset_id": source["dataset_id"],
        "gold_version": source["version"],
        "extractor_version": "extractor-2026-09-04",
        "cases": cases,
    }


def knowledge_policy() -> dict[str, object]:
    return {
        "schema_version": KNOWLEDGE_GATE_POLICY_SCHEMA_VERSION,
        "version": "1.0.0",
        "limits": {
            "max_cases": 100,
            "max_artifacts_per_case": 100,
            "max_resolution_pairs_per_case": 100,
            "max_total_text_chars": 100_000,
        },
        "thresholds": {
            "min_overall_f1": 1.0,
            "min_entity_f1": 1.0,
            "min_relationship_f1": 1.0,
            "min_property_f1": 1.0,
            "max_schema_violation_rate": 0.0,
            "max_evidence_violation_rate": 0.0,
            "max_authority_contamination_rate": 0.0,
            "max_resolution_false_merge_rate": 0.0,
            "max_resolution_missed_merge_rate": 0.0,
            "min_low_risk_review_sample_rate": 0.5,
            "per_type_min_f1": {
                "entity:Pump": 1.0,
                "relationship:FEEDS": 1.0,
                "property:operating_pressure": 1.0,
            },
        },
        "drift": {
            "max_f1_drop": 0.0,
            "max_per_type_f1_drop": 0.0,
            "max_schema_violation_rate_increase": 0.0,
            "max_evidence_violation_rate_increase": 0.0,
            "max_authority_contamination_rate_increase": 0.0,
            "max_resolution_false_merge_rate_increase": 0.0,
            "max_resolution_missed_merge_rate_increase": 0.0,
            "max_review_reject_rate_increase": 0.0,
            "max_review_quarantine_rate_increase": 0.0,
        },
        "high_risk_types": {
            "entity": [],
            "relationship": ["FEEDS"],
            "property": ["operating_pressure"],
        },
    }


def all_datatype_gold() -> dict[str, object]:
    gold = knowledge_gold()
    text = (
        "Pump P-1 feeds Line L-1. alpha 7 1.5 12345678901234567890.123 true "
        '2025-02-03 2025-02-03T00:00:00Z P3D https://example.com/a {"a": 1}. '
        "Pump P-1 repeated."
    )
    positive = gold["cases"][0]
    positive["chunk_text"] = text
    positive["entities"][0]["evidence"] = _evidence(text, "Pump P-1")
    positive["entities"][1]["evidence"] = _evidence(text, "Line L-1")
    positive["entities"][2]["evidence"] = _evidence(text, "Pump P-1", occurrence=1)
    positive["relationships"][0]["evidence"] = _evidence(
        text, "Pump P-1 feeds Line L-1"
    )

    definitions: list[dict[str, object]] = []
    facts: list[dict[str, object]] = []
    values = [
        ("string_value", "STRING", "alpha", "alpha", "alpha"),
        ("integer_value", "INTEGER", "7", 7, "7"),
        ("float_value", "FLOAT", "1.5", 1.5, "1.5"),
        (
            "decimal_value",
            "DECIMAL",
            "12345678901234567890.123",
            "12345678901234567890.123",
            "12345678901234567890.123",
        ),
        ("boolean_value", "BOOLEAN", "true", True, "true"),
        ("date_value", "DATE", "2025-02-03", "2025-02-03", "2025-02-03"),
        (
            "datetime_value",
            "DATETIME",
            "2025-02-03T00:00:00Z",
            "2025-02-03T00:00:00Z",
            "2025-02-03T00:00:00Z",
        ),
        ("duration_value", "DURATION", "P3D", "P3D", "P3D"),
        (
            "uri_value",
            "URI",
            "https://example.com/a",
            "https://example.com/a",
            "https://example.com/a",
        ),
        ("json_value", "JSON", '{"a": 1}', '{"a":1}', '{"a":1}'),
    ]
    for name, datatype, raw_value, typed_value, canonical_value in values:
        definitions.append(
            {
                "type": name,
                "owner_entity_types": ["Pump"],
                "datatype": datatype,
                "canonical_unit": None,
            }
        )
        facts.append(
            {
                "id": f"property-{name}",
                "property_type": name,
                "owner_mention_id": "mention-pump-primary",
                "datatype": datatype,
                "typed_value": typed_value,
                "raw_value": raw_value,
                "raw_unit": None,
                "canonical_value": canonical_value,
                "canonical_unit": None,
                "valid_from": None,
                "valid_to": None,
                "observed_at": None,
                "raw_valid_from": None,
                "raw_valid_to": None,
                "raw_observed_at": None,
                "evidence": _evidence(text, raw_value),
            }
        )
    gold["ontology"]["property_types"] = definitions
    positive["property_facts"] = facts
    return gold


def locked_baseline(
    gold: dict[str, object] | None = None,
    predictions: dict[str, object] | None = None,
    policy: dict[str, object] | None = None,
) -> dict[str, object]:
    source_gold = gold or knowledge_gold()
    source_predictions = predictions or knowledge_predictions(source_gold)
    source_policy = policy or knowledge_policy()
    initial = build_knowledge_quality_report(
        gold=source_gold,
        predictions=source_predictions,
        policy=source_policy,
        baseline=None,
    )
    candidate = knowledge_baseline_candidate(initial)
    candidate["locked"] = True
    return candidate


class KnowledgeQualityTests(unittest.TestCase):
    def test_raw_and_canonical_units_are_distinct_and_conversion_is_checked(
        self,
    ) -> None:
        gold = knowledge_gold()
        definition = gold["ontology"]["property_types"][0]
        fact = gold["cases"][0]["property_facts"][0]
        definition["canonical_unit"] = "kPa"
        fact["canonical_unit"] = "kPa"
        fact["typed_value"] = "1200"
        fact["canonical_value"] = "1200"
        predictions = knowledge_predictions(gold)
        policy = knowledge_policy()
        baseline = locked_baseline(gold, predictions, policy)
        report = build_knowledge_quality_report(
            gold=gold, predictions=predictions, policy=policy, baseline=baseline
        )
        self.assertTrue(report["passed"], report["failures"])

        incorrect = deepcopy(predictions)
        incorrect["cases"][0]["property_facts"][0]["canonical_value"] = "12"
        incorrect["cases"][0]["property_facts"][0]["typed_value"] = "12"
        degraded = build_knowledge_quality_report(
            gold=gold, predictions=incorrect, policy=policy, baseline=baseline
        )
        self.assertEqual(degraded["violations"]["schema_violation_count"], 1)
        self.assertFalse(degraded["passed"])

    def test_all_core_datatypes_and_lossless_decimal_are_supported(self) -> None:
        gold = all_datatype_gold()
        predictions = knowledge_predictions(gold)
        policy = knowledge_policy()
        policy["high_risk_types"]["property"] = []
        policy["thresholds"]["per_type_min_f1"] = {}
        baseline = locked_baseline(gold, predictions, policy)
        report = build_knowledge_quality_report(
            gold=gold, predictions=predictions, policy=policy, baseline=baseline
        )
        self.assertTrue(report["passed"], report["failures"])
        property_types = {
            key
            for key in report["extraction"]["per_type"]
            if key.startswith("property:")
        }
        self.assertEqual(len(property_types), 10)

        lossy = deepcopy(predictions)
        decimal_fact = next(
            item
            for item in lossy["cases"][0]["property_facts"]
            if item["datatype"] == "DECIMAL"
        )
        decimal_fact["canonical_value"] = 1.2345678901234567e19
        degraded = build_knowledge_quality_report(
            gold=gold, predictions=lossy, policy=policy, baseline=baseline
        )
        self.assertFalse(degraded["passed"])
        self.assertEqual(degraded["violations"]["schema_violation_count"], 1)

    def test_perfect_report_is_complete_serializable_and_deterministic(self) -> None:
        gold = knowledge_gold()
        predictions = knowledge_predictions(gold)
        policy = knowledge_policy()
        baseline = locked_baseline(gold, predictions, policy)

        first = build_knowledge_quality_report(
            gold=gold, predictions=predictions, policy=policy, baseline=baseline
        )
        second = build_knowledge_quality_report(
            gold=deepcopy(gold),
            predictions=deepcopy(predictions),
            policy=deepcopy(policy),
            baseline=deepcopy(baseline),
        )

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], KNOWLEDGE_REPORT_SCHEMA_VERSION)
        self.assertTrue(first["passed"])
        self.assertRegex(first["report_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(first["extraction"]["overall"]["f1"], 1.0)
        self.assertEqual(first["extraction"]["by_family"]["property"]["recall"], 1.0)
        self.assertEqual(
            first["extraction"]["per_type"]["relationship:FEEDS"]["precision"], 1.0
        )
        self.assertEqual(first["violations"]["authority_contamination_count"], 0)
        self.assertEqual(first["review"]["approved_rate"], 1.0)
        self.assertEqual(first["resolution"]["false_merge_rate"], 0.0)
        self.assertEqual(first["resolution"]["missed_merge_rate"], 0.0)
        self.assertEqual(
            first["coverage"]["case_class_counts"],
            {"negative": 1, "positive": 1, "security": 1},
        )
        self.assertTrue(first["drift"]["compared"])

        candidate = knowledge_baseline_candidate(first)
        self.assertEqual(candidate["schema_version"], KNOWLEDGE_BASELINE_SCHEMA_VERSION)
        self.assertFalse(candidate["locked"])

    def test_schema_evidence_authority_review_and_security_fail_closed(self) -> None:
        gold = knowledge_gold()
        good_predictions = knowledge_predictions(gold)
        policy = knowledge_policy()
        baseline = locked_baseline(gold, good_predictions, policy)
        predictions = deepcopy(good_predictions)
        positive = predictions["cases"][0]
        positive["property_facts"][0]["raw_unit"] = "psi"
        positive["property_facts"][0]["origin"] = "rule"
        positive["property_facts"][0]["authority_level"] = "authoritative"
        positive["property_facts"][0]["review_status"] = "pending"
        positive["relationships"][0]["evidence"]["document_id"] = "wrong-document"
        positive["entities"][0]["review_status"] = "rejected"
        positive["entities"][1]["review_status"] = "quarantined"

        security = predictions["cases"][2]
        security_text = gold["cases"][2]["chunk_text"]
        start = security_text.index("Restricted")
        security["entities"].append(
            {
                "id": "security-leak",
                "entity_type": "Pump",
                "canonical_name": "Restricted",
                "mention_text": "Restricted",
                "evidence": {
                    "document_id": "doc-security",
                    "chunk_id": "chunk-security",
                    "start": start,
                    "end": start + len("Restricted"),
                    "quote": "Restricted",
                },
                "origin": "llm",
                "authority_level": "secondary",
                "review_status": "approved",
            }
        )

        report = build_knowledge_quality_report(
            gold=gold, predictions=predictions, policy=policy, baseline=baseline
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["violations"]["schema_violation_count"], 1)
        self.assertEqual(report["violations"]["evidence_violation_count"], 1)
        self.assertEqual(report["violations"]["authority_contamination_count"], 1)
        self.assertEqual(report["review"]["high_risk_pending_count"], 1)
        self.assertEqual(report["review"]["rejected_count"], 1)
        self.assertEqual(report["review"]["quarantined_count"], 1)
        self.assertEqual(
            report["coverage"]["case_class_false_positive_counts"]["security"], 1
        )
        joined = "\n".join(report["failures"])
        self.assertIn("security cases produced extracted artifacts", joined)
        self.assertIn("high-risk artifacts require", joined)
        self.assertIn("authority contamination", joined)

    def test_mentions_relationships_and_temporals_must_bind_source_text(self) -> None:
        gold = knowledge_gold()
        good = knowledge_predictions(gold)
        policy = knowledge_policy()
        baseline = locked_baseline(gold, good, policy)
        predictions = deepcopy(good)
        positive = predictions["cases"][0]
        text = gold["cases"][0]["chunk_text"]
        positive["entities"][0]["evidence"] = _evidence(text, "Line L-1")
        positive["relationships"][0]["evidence"] = _evidence(text, "feeds")
        fact = positive["property_facts"][0]
        fact["raw_valid_from"] = "2025-01-01T00:00:00"
        fact["valid_to"] = fact["valid_from"]
        fact["raw_valid_to"] = "2025-01-01T00:00:00Z"

        report = build_knowledge_quality_report(
            gold=gold, predictions=predictions, policy=policy, baseline=baseline
        )
        self.assertFalse(report["passed"])
        self.assertEqual(report["violations"]["schema_violation_count"], 3)
        self.assertEqual(report["violations"]["evidence_violation_count"], 0)

    def test_resolution_false_merge_and_missed_merge_are_separate(self) -> None:
        gold = knowledge_gold()
        good_predictions = knowledge_predictions(gold)
        policy = knowledge_policy()
        baseline = locked_baseline(gold, good_predictions, policy)
        predictions = deepcopy(good_predictions)
        pairs = predictions["cases"][0]["resolution_pairs"]
        pairs[0]["predicted_merge"] = False
        pairs[1]["predicted_merge"] = True
        report = build_knowledge_quality_report(
            gold=gold, predictions=predictions, policy=policy, baseline=baseline
        )
        self.assertEqual(report["resolution"]["false_merge_rate"], 1.0)
        self.assertEqual(report["resolution"]["missed_merge_rate"], 1.0)
        self.assertFalse(report["passed"])

    def test_duplicate_prediction_is_a_false_positive_not_hidden_by_deduplication(
        self,
    ) -> None:
        gold = knowledge_gold()
        good = knowledge_predictions(gold)
        policy = knowledge_policy()
        baseline = locked_baseline(gold, good, policy)
        predictions = deepcopy(good)
        duplicate = deepcopy(predictions["cases"][0]["entities"][0])
        duplicate["id"] = "duplicate-pump-prediction"
        predictions["cases"][0]["entities"].append(duplicate)
        report = build_knowledge_quality_report(
            gold=gold, predictions=predictions, policy=policy, baseline=baseline
        )
        self.assertEqual(report["extraction"]["overall"]["false_positive"], 1)
        self.assertLess(report["extraction"]["by_family"]["entity"]["precision"], 1.0)
        self.assertFalse(report["passed"])

    def test_drift_detects_regression_even_when_absolute_thresholds_allow_it(
        self,
    ) -> None:
        gold = knowledge_gold()
        good_predictions = knowledge_predictions(gold)
        permissive = knowledge_policy()
        for name in (
            "min_overall_f1",
            "min_entity_f1",
            "min_relationship_f1",
            "min_property_f1",
        ):
            permissive["thresholds"][name] = 0.0
        permissive["thresholds"]["per_type_min_f1"] = {}
        baseline = locked_baseline(gold, good_predictions, permissive)
        predictions = deepcopy(good_predictions)
        predictions["cases"][0]["relationships"] = []

        report = build_knowledge_quality_report(
            gold=gold,
            predictions=predictions,
            policy=permissive,
            baseline=baseline,
        )
        self.assertFalse(report["passed"])
        self.assertTrue(
            any("F1 drift" in failure for failure in report["failures"]),
            report["failures"],
        )

    def test_missing_negative_security_or_prediction_case_is_rejected(self) -> None:
        gold = knowledge_gold()
        predictions = knowledge_predictions(gold)
        policy = knowledge_policy()
        incomplete = deepcopy(predictions)
        incomplete["cases"].pop()
        with self.assertRaisesRegex(ValueError, "coverage is incomplete"):
            build_knowledge_quality_report(
                gold=gold,
                predictions=incomplete,
                policy=policy,
                baseline=None,
            )

        no_security = deepcopy(gold)
        no_security["cases"].pop()
        no_security["adjudication"]["approved_case_ids"].pop()
        with self.assertRaisesRegex(ValueError, "include negative and security"):
            build_knowledge_quality_report(
                gold=no_security,
                predictions=knowledge_predictions(no_security),
                policy=policy,
                baseline=None,
            )

        partially_reviewed = deepcopy(gold)
        partially_reviewed["adjudication"]["approved_case_ids"].pop()
        with self.assertRaisesRegex(ValueError, "approve every case"):
            build_knowledge_quality_report(
                gold=partially_reviewed,
                predictions=predictions,
                policy=policy,
                baseline=None,
            )

    def test_baseline_must_be_locked_exact_and_policy_is_bounded(self) -> None:
        gold = knowledge_gold()
        predictions = knowledge_predictions(gold)
        policy = knowledge_policy()
        report = build_knowledge_quality_report(
            gold=gold, predictions=predictions, policy=policy, baseline=None
        )
        self.assertFalse(report["passed"])
        self.assertIn("locked knowledge-quality baseline", report["failures"][0])

        unlocked = knowledge_baseline_candidate(report)
        with self.assertRaisesRegex(ValueError, "explicitly locked"):
            build_knowledge_quality_report(
                gold=gold,
                predictions=predictions,
                policy=policy,
                baseline=unlocked,
            )
        stale = locked_baseline(gold, predictions, policy)
        stale["gold_digest"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "gold_digest is stale"):
            build_knowledge_quality_report(
                gold=gold,
                predictions=predictions,
                policy=policy,
                baseline=stale,
            )

        unbounded = deepcopy(policy)
        unbounded["limits"]["max_cases"] = 10_001
        with self.assertRaisesRegex(ValueError, "max_cases"):
            build_knowledge_quality_report(
                gold=gold,
                predictions=predictions,
                policy=unbounded,
                baseline=None,
            )

    def test_resolution_pair_coverage_cannot_shrink_denominator(self) -> None:
        gold = knowledge_gold()
        predictions = knowledge_predictions(gold)
        predictions["cases"][0]["resolution_pairs"].pop()
        with self.assertRaisesRegex(ValueError, "resolution pair coverage mismatch"):
            build_knowledge_quality_report(
                gold=gold,
                predictions=predictions,
                policy=knowledge_policy(),
                baseline=None,
            )


if __name__ == "__main__":
    unittest.main()
