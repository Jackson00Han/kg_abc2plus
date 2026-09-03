"""Trust metadata vocabulary and lifecycle tests."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
import unittest

from graphrag_prod.knowledge import (
    AuthorityLevel,
    GovernanceStatus,
    KnowledgeOrigin,
    TrustMetadata,
    allowed_governance_transitions,
    validate_governance_transition,
)


CREATED_AT = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)


def candidate(**overrides: object) -> TrustMetadata:
    values: dict[str, object] = {
        "origin": KnowledgeOrigin.LLM_EXTRACTED,
        "authority": AuthorityLevel.SECONDARY,
        "status": GovernanceStatus.CANDIDATE,
        "ontology_version_id": "industrial-pumps:v1",
        "created_at": CREATED_AT,
        "extractor_version": "qwen-extractor:v1",
        "prompt_version": "pump-extraction:v3",
    }
    values.update(overrides)
    return TrustMetadata(**values)  # type: ignore[arg-type]


class TrustMetadataTests(unittest.TestCase):
    def test_record_is_immutable_and_normalizes_text(self) -> None:
        metadata = candidate(ontology_version_id="  industrial-pumps:v1  ")
        self.assertEqual(metadata.ontology_version_id, "industrial-pumps:v1")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            metadata.status = GovernanceStatus.PUBLISHED  # type: ignore[misc]

    def test_enum_fields_reject_untyped_strings(self) -> None:
        with self.assertRaisesRegex(TypeError, "origin must be an instance of KnowledgeOrigin"):
            candidate(origin="LLM_EXTRACTED")
        with self.assertRaisesRegex(TypeError, "authority must be an instance of AuthorityLevel"):
            candidate(authority="SECONDARY")
        with self.assertRaisesRegex(TypeError, "status must be an instance of GovernanceStatus"):
            candidate(status="CANDIDATE")

    def test_origin_determines_authority_level(self) -> None:
        for origin in (KnowledgeOrigin.EXPERT_IMPORT, KnowledgeOrigin.EXPERT_CREATED):
            metadata = candidate(
                origin=origin,
                authority=AuthorityLevel.AUTHORITATIVE,
            )
            self.assertEqual(metadata.authority, AuthorityLevel.AUTHORITATIVE)
        for origin in (
            KnowledgeOrigin.LLM_EXTRACTED,
            KnowledgeOrigin.RULE_DERIVED,
            KnowledgeOrigin.FIXTURE,
        ):
            metadata = candidate(origin=origin)
            self.assertEqual(metadata.authority, AuthorityLevel.SECONDARY)
        with self.assertRaisesRegex(ValueError, "must use SECONDARY"):
            candidate(authority=AuthorityLevel.AUTHORITATIVE)

    def test_timestamps_must_be_aware_and_chronological(self) -> None:
        with self.assertRaisesRegex(ValueError, "created_at must be timezone-aware"):
            candidate(created_at=CREATED_AT.replace(tzinfo=None))
        with self.assertRaisesRegex(ValueError, "reviewed_at must not precede"):
            candidate(
                status=GovernanceStatus.APPROVED,
                reviewed_by="expert-1",
                reviewed_at=CREATED_AT - timedelta(seconds=1),
            )

    def test_review_fields_are_paired_and_required_for_decisions(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be provided together"):
            candidate(reviewed_by="expert-1")
        with self.assertRaisesRegex(ValueError, "APPROVED knowledge requires"):
            candidate(status=GovernanceStatus.APPROVED)
        with self.assertRaisesRegex(ValueError, "CANDIDATE knowledge must not carry"):
            candidate(reviewed_by="expert-1", reviewed_at=CREATED_AT)

    def test_extractor_and_prompt_versions_are_optional_but_not_blank(self) -> None:
        metadata = candidate(extractor_version=None, prompt_version=None)
        self.assertIsNone(metadata.extractor_version)
        with self.assertRaisesRegex(ValueError, "extractor_version must not be empty"):
            candidate(extractor_version="  ")

    def test_candidate_can_be_reviewed_approved_and_published(self) -> None:
        reviewed_at = CREATED_AT + timedelta(minutes=5)
        approved = candidate().transition_to(
            GovernanceStatus.APPROVED,
            reviewed_by="expert-17",
            reviewed_at=reviewed_at,
            review_notes="Evidence and identity confirmed.",
        )
        published = approved.transition_to(GovernanceStatus.PUBLISHED)

        self.assertEqual(published.status, GovernanceStatus.PUBLISHED)
        self.assertEqual(published.reviewed_by, "expert-17")
        self.assertEqual(published.reviewed_at, reviewed_at)
        self.assertTrue(published.is_retrieval_eligible)
        self.assertFalse(candidate().is_retrieval_eligible)

    def test_quarantine_can_return_to_clean_candidate_queue(self) -> None:
        quarantined = candidate().transition_to(GovernanceStatus.QUARANTINED)
        restored = quarantined.transition_to(GovernanceStatus.CANDIDATE)
        self.assertEqual(restored.status, GovernanceStatus.CANDIDATE)
        self.assertIsNone(restored.reviewed_by)

    def test_illegal_and_noop_transitions_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "CANDIDATE -> PUBLISHED"):
            candidate().transition_to(GovernanceStatus.PUBLISHED)
        rejected = candidate().transition_to(
            GovernanceStatus.REJECTED,
            reviewed_by="expert-2",
            reviewed_at=CREATED_AT,
        )
        with self.assertRaisesRegex(ValueError, "REJECTED -> CANDIDATE"):
            rejected.transition_to(GovernanceStatus.CANDIDATE)
        with self.assertRaisesRegex(ValueError, "CANDIDATE -> CANDIDATE"):
            validate_governance_transition(
                GovernanceStatus.CANDIDATE,
                GovernanceStatus.CANDIDATE,
            )

    def test_transition_catalog_is_immutable(self) -> None:
        allowed = allowed_governance_transitions(GovernanceStatus.PUBLISHED)
        self.assertEqual(
            allowed,
            frozenset(
                {GovernanceStatus.QUARANTINED, GovernanceStatus.SUPERSEDED}
            ),
        )
        with self.assertRaises(AttributeError):
            allowed.add(GovernanceStatus.REJECTED)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
