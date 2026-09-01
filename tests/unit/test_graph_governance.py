"""Graph schema policy, quarantine, resolution, and review metric tests."""

from __future__ import annotations

import dataclasses
from pathlib import Path
import unittest

from graphrag_prod.domain import assertion_id
from graphrag_prod.graph import (
    AuthoritativeIdentifier,
    GovernanceRejected,
    ResolutionCandidate,
    ResolutionOutcome,
    evaluate_graph_review_dataset,
    load_governance_policy,
    normalize_display_name,
    normalized_name_key,
    resolve_entity_pair,
)
from tests.fixtures.domain import make_bundle


ROOT = Path(__file__).parents[2]
POLICY_PATH = ROOT / "contracts" / "graph_governance.v1.json"


def _candidate(
    candidate_id: str,
    *,
    tenant_id: str = "tenant-a",
    entity_type: str = "Company",
    name: str = "Apple Inc.",
    aliases: tuple[str, ...] = ("Apple",),
    identifiers: tuple[AuthoritativeIdentifier, ...] = (),
) -> ResolutionCandidate:
    return ResolutionCandidate(
        candidate_id,
        tenant_id,
        entity_type,
        name,
        aliases,
        identifiers,
        (f"mention:{candidate_id}",),
    )


class GraphGovernancePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = load_governance_policy(POLICY_PATH, "company-filings:v1")

    def test_repository_policy_declares_types_properties_and_patterns(self) -> None:
        self.assertEqual(self.policy.policy_id, "company-filings:v1")
        self.assertEqual(
            set(self.policy.entity_rules_by_type),
            {"Company", "Product", "RiskFactor", "BusinessSegment"},
        )
        self.assertTrue(
            self.policy.allows_relationship("OFFERS", "Company", "entity", "Product")
        )
        self.assertFalse(
            self.policy.allows_relationship("OFFERS", "Product", "entity", "Company")
        )
        self.assertEqual(self.policy, load_governance_policy(POLICY_PATH, self.policy.policy_id))
        self.assertIn('"policy_id":"company-filings:v1"', self.policy.canonical_payload)

    def test_names_and_aliases_are_conservatively_normalized(self) -> None:
        bundle = make_bundle(activate_version=False)
        company, product = bundle.entities
        changed_company = dataclasses.replace(
            company,
            canonical_name="  Apple   Inc.  ",
            aliases=("Apple", "APPLE INC.", "Ａｐｐｌｅ"),
        )
        changed = dataclasses.replace(bundle, entities=(changed_company, product))

        governed = self.policy.govern_bundle(changed)

        result = governed.bundle.entities[0]
        self.assertEqual(result.canonical_name, "Apple Inc.")
        self.assertEqual(result.aliases, ("Apple",))
        self.assertEqual(governed.findings[0].code, "ENTITY_PROFILE_NORMALIZED")
        self.assertEqual(normalize_display_name("  Apple   Inc. "), "Apple Inc.")
        self.assertEqual(normalized_name_key("APPLE, Inc."), "apple inc")

    def test_invalid_relationship_pattern_is_rejected_before_publication(self) -> None:
        bundle = make_bundle(activate_version=False)
        assertion = bundle.assertion
        assert assertion is not None and assertion.object_entity_id is not None
        invalid = dataclasses.replace(
            assertion,
            predicate="HAS_RISK",
            assertion_id=assertion_id(
                assertion.tenant_id,
                assertion.subject_entity_id,
                "HAS_RISK",
                "entity",
                assertion.object_entity_id,
                assertion.evidence_chunk_id,
                assertion.evidence_char_start,
                assertion.evidence_char_end,
                assertion.extractor_version,
                assertion.schema_version,
            ),
        )
        changed = dataclasses.replace(bundle, assertion=invalid)

        with self.assertRaisesRegex(GovernanceRejected, "RELATIONSHIP_PATTERN_NOT_ALLOWED"):
            self.policy.govern_bundle(changed)

    def test_low_confidence_assertion_is_quarantined_not_discarded(self) -> None:
        bundle = make_bundle(activate_version=False)
        assert bundle.assertion is not None
        changed = dataclasses.replace(
            bundle,
            assertion=dataclasses.replace(bundle.assertion, confidence=0.5),
        )

        governed = self.policy.govern_bundle(changed)

        assert governed.bundle.assertion is not None
        self.assertFalse(governed.bundle.assertion.accepted)
        self.assertEqual(governed.bundle.assertion.assertion_id, bundle.assertion.assertion_id)
        self.assertEqual(governed.findings[0].action, "QUARANTINE")

    def test_low_confidence_entity_evidence_is_rejected(self) -> None:
        bundle = make_bundle(activate_version=False)
        changed = dataclasses.replace(
            bundle,
            mentions=(
                dataclasses.replace(bundle.mentions[0], confidence=0.5),
                bundle.mentions[1],
            ),
        )
        with self.assertRaisesRegex(GovernanceRejected, "ENTITY_EVIDENCE_BELOW_THRESHOLD"):
            self.policy.govern_bundle(changed)


class EntityResolutionTests(unittest.TestCase):
    def test_exact_authoritative_identifier_merges_with_audit_evidence(self) -> None:
        identifier = AuthoritativeIdentifier("ticker", "AAPL")
        decision = resolve_entity_pair(
            _candidate("raw-a", identifiers=(identifier,)),
            _candidate("raw-b", name="Apple", identifiers=(identifier,)),
        )
        replay = resolve_entity_pair(
            _candidate("raw-b", name="Apple", identifiers=(identifier,)),
            _candidate("raw-a", identifiers=(identifier,)),
        )
        self.assertEqual(decision.outcome, ResolutionOutcome.MERGE)
        self.assertEqual(decision.rule_id, "exact-authoritative-identifier:v1")
        self.assertEqual(decision.decision_id, replay.decision_id)
        self.assertEqual(len(decision.evidence_mention_ids), 2)

    def test_conflicting_identifiers_types_and_tenants_never_merge(self) -> None:
        cases = (
            (
                _candidate("a", identifiers=(AuthoritativeIdentifier("ticker", "AAPL"),)),
                _candidate("b", identifiers=(AuthoritativeIdentifier("ticker", "MSFT"),)),
                "conflicting-authoritative-identifier:v1",
            ),
            (_candidate("c"), _candidate("d", entity_type="Product"), "entity-type-boundary:v1"),
            (_candidate("e"), _candidate("f", tenant_id="tenant-b"), "tenant-boundary:v1"),
        )
        for left, right, rule_id in cases:
            with self.subTest(rule_id=rule_id):
                decision = resolve_entity_pair(left, right)
                self.assertEqual(decision.outcome, ResolutionOutcome.KEEP_SEPARATE)
                self.assertEqual(decision.rule_id, rule_id)

    def test_name_only_match_requires_human_review_to_protect_homonyms(self) -> None:
        decision = resolve_entity_pair(
            _candidate("company-apple", name="Apple", aliases=()),
            _candidate("other-apple", name="Apple", aliases=()),
        )
        self.assertEqual(decision.outcome, ResolutionOutcome.HUMAN_REVIEW)
        self.assertEqual(decision.rule_id, "name-only-homonym-guard:v1")

    def test_candidate_rejects_conflicting_values_in_one_identifier_namespace(self) -> None:
        with self.assertRaisesRegex(ValueError, "one namespace"):
            _candidate(
                "invalid",
                identifiers=(
                    AuthoritativeIdentifier("ticker", "AAPL"),
                    AuthoritativeIdentifier("ticker", "MSFT"),
                ),
            )


class GraphReviewDatasetTests(unittest.TestCase):
    def test_adjudicated_dataset_meets_stage_1_graph_targets(self) -> None:
        metrics = evaluate_graph_review_dataset(
            ROOT / "evaluation" / "graph-review-v1.json"
        )
        self.assertGreaterEqual(metrics.item_count, 50)
        self.assertEqual(metrics.entity_precision, 1.0)
        self.assertEqual(metrics.relationship_precision, 1.0)
        self.assertEqual(metrics.entity_resolution_accuracy, 1.0)
        self.assertTrue(metrics.meets(0.95))


if __name__ == "__main__":
    unittest.main()
