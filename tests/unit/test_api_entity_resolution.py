"""Governed entity-resolution adapter tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.api.knowledge_contracts import (
    EntityResolutionApplyRequest,
    EntityResolutionRequest,
)
from graphrag_prod.api.runtime import ConflictError, ResourceNotFoundError
from graphrag_prod.domain import Principal, TypedLiteralValue
from graphrag_prod.domain.ids import entity_id
from graphrag_prod.knowledge import (
    AssertionRecord,
    AuthoritativeEntityProfile,
    AuthoritativeEvidence,
    EntityIdentity,
    EntityMentionRecord,
    EvidenceReference,
    ExactAuthoritativeMatch,
    IdentityPropertyValue,
    RecordRevision,
    knowledge_record_id,
    llm_candidate_trust,
)
from graphrag_prod.knowledge.review import (
    ReviewBatchResult,
    ReviewOutcome,
    ReviewRecordKind,
)
from graphrag_prod.knowledge.trust import AuthorityLevel, GovernanceStatus
from graphrag_prod.ontology import (
    Cardinality,
    EntityTypeDefinition,
    PropertyDataType,
    PropertyDefinition,
    TBoxStatus,
    TBoxVersion,
)


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
TENANT = "tenant-alpha"
TBOX = TBoxVersion(
    tenant_id=TENANT,
    key="industrial-assets",
    version=2,
    status=TBoxStatus.PUBLISHED,
    entity_types=(
        EntityTypeDefinition(
            "Asset",
            ("asset-id",),
            properties=(
                PropertyDefinition(
                    "serial_number",
                    PropertyDataType.STRING,
                    True,
                    Cardinality.ONE,
                ),
            ),
            identity_properties=("serial_number",),
        ),
    ),
    relationship_types=(),
)


def _identity(key: str, name: str) -> EntityIdentity:
    return EntityIdentity(
        entity_id=entity_id(TENANT, "Asset", key),
        tenant_id=TENANT,
        entity_type="Asset",
        canonical_key=key,
        canonical_name=name,
    )


def _evidence(quoted_text: str) -> EvidenceReference:
    return EvidenceReference(
        tenant_id=TENANT,
        document_id="document-1",
        version_id="version-1",
        chunk_id="chunk-1",
        char_start=0,
        char_end=len(quoted_text),
        quoted_text=quoted_text,
        access_policy_id="policy-1",
        access_policy_version=1,
        access_groups=frozenset({"engineers"}),
    )


CANDIDATE_IDENTITY = _identity("llm-candidate:pump-77", "Pump from report")
CANDIDATE = EntityMentionRecord(
    revision=RecordRevision.next(
        knowledge_record_id(TENANT, "ENTITY_MENTION", "candidate-pump-77"), 0
    ),
    tenant_id=TENANT,
    entity=CANDIDATE_IDENTITY,
    evidence=_evidence("Pump SN-77"),
    confidence=0.93,
    trust=llm_candidate_trust(
        ontology_version_id=TBOX.tbox_id,
        extractor_version="extractor-v2",
        prompt_version="prompt-v2",
        extracted_at=NOW,
    ),
    created_at=NOW,
)
SERIAL_FACT = AssertionRecord(
    revision=RecordRevision.next(
        knowledge_record_id(TENANT, "ASSERTION", "candidate-pump-77-serial"), 0
    ),
    tenant_id=TENANT,
    subject=CANDIDATE_IDENTITY,
    predicate="serial_number",
    evidence=_evidence("Pump SN-77"),
    subject_mention_revision_id=CANDIDATE.revision_id,
    confidence=0.95,
    trust=CANDIDATE.trust,
    created_at=NOW,
    literal_value="SN-77",
    literal_semantics=TypedLiteralValue(
        datatype="STRING",
        typed_value="SN-77",
        raw_value="SN-77",
        canonical_value="SN-77",
    ),
)
TARGET = _identity("asset-id:SN-77", "Primary Pump 77")
TARGET_PROFILE = AuthoritativeEntityProfile(
    entity=TARGET,
    ontology_version_id=TBOX.tbox_id,
    authority=AuthorityLevel.AUTHORITATIVE,
    status=GovernanceStatus.PUBLISHED,
    evidence=(
        AuthoritativeEvidence(
            mention_revision_id="authoritative-mention-1",
            document_id="document-authority",
            version_id="version-authority",
            chunk_id="chunk-authority",
            char_start=0,
            char_end=5,
            quoted_text="SN-77",
        ),
    ),
)


class _Store:
    def __init__(self, candidate: EntityMentionRecord | None = CANDIDATE) -> None:
        self.candidate = candidate
        self.identity_call: tuple[object, ...] | None = None

    def get_entity_mention(self, principal: Principal, record_id: str, **_kwargs: object):
        if (
            self.candidate is None
            or principal.tenant_id != TENANT
            or record_id != self.candidate.record_id
            or not principal.groups.intersection(self.candidate.evidence.access_groups)
        ):
            return None
        return self.candidate

    def list_identity_property_assertions(self, principal: Principal, **kwargs: object):
        self.identity_call = (principal, kwargs)
        return (SERIAL_FACT,)


class _TBoxes:
    def get(self, tenant_id: str, tbox_id: str) -> TBoxVersion:
        if tenant_id != TENANT or tbox_id != TBOX.tbox_id:
            raise KeyError("absent")
        return TBOX

    def active(self, tenant_id: str, key: str) -> TBoxVersion | None:
        return TBOX if tenant_id == TENANT and key == TBOX.key else None


class _ResolutionSource:
    def find_exact_canonical_key(self, *_args: object, **_kwargs: object):
        return ExactAuthoritativeMatch(0)

    def find_exact_identity_properties(
        self,
        _principal: Principal,
        *,
        identity_properties: tuple[IdentityPropertyValue, ...],
        **_kwargs: object,
    ) -> ExactAuthoritativeMatch:
        self.identity_properties = identity_properties
        return ExactAuthoritativeMatch(
            1,
            TARGET_PROFILE,
            "serial_number=STRING:SN-77",
        )

    def find_exact_governed_alias(self, *_args: object, **_kwargs: object):
        raise AssertionError("identity property match should win before alias")

    def list_authoritative_entities(self, *_args: object, **_kwargs: object):
        raise AssertionError("identity property match should win before fuzzy matching")


class _Reviews:
    def __init__(self) -> None:
        self.call: tuple[Principal, dict[str, object]] | None = None

    def apply_entity_resolution(
        self, principal: Principal, **kwargs: object
    ) -> ReviewBatchResult:
        self.call = (principal, kwargs)
        return ReviewBatchResult(
            tenant_id=principal.tenant_id,
            outcomes=(
                ReviewOutcome(
                    ReviewRecordKind.ENTITY_MENTION,
                    str(kwargs["record_id"]),
                    CANDIDATE.revision_id,
                    "linked-revision-2",
                    2,
                    GovernanceStatus.APPROVED,
                ),
                ReviewOutcome(
                    ReviewRecordKind.ASSERTION,
                    SERIAL_FACT.record_id,
                    SERIAL_FACT.revision_id,
                    "rebound-assertion-revision-2",
                    2,
                    GovernanceStatus.CANDIDATE,
                ),
            ),
        )


def _principal(*, tenant_id: str = TENANT) -> Principal:
    return Principal(
        "reviewer-1",
        tenant_id,
        frozenset({"engineers"}),
        frozenset({"knowledge:review"}),
    )


class EntityResolutionAdapterTests(unittest.TestCase):
    def test_missing_identity_can_be_manually_linked_to_revision_bound_confirmed_target(self):
        from graphrag_prod.api.knowledge import _entity_payload, _evidence_payload

        class MissingStore(_Store):
            def list_identity_property_assertions(self, *_args, **_kwargs):
                return ()

        class ConfirmedReviews(_Reviews):
            selectable = True

            def resolution_targets(self, principal, candidate, query):
                return {'items':[dict(record_id='confirmed-target', revision=2,
                    entity=_entity_payload(TARGET), status='APPROVED', authority='AUTHORITATIVE',
                    evidence=_evidence_payload(CANDIDATE.evidence), identity_properties=[],
                    selectable=self.selectable, reason='Check both source contexts.')], 'truncated':False}

        reviews = ConfirmedReviews()
        adapter = self._adapter(store=MissingStore(), reviews=reviews)
        suggestions = adapter.resolution_suggestions(_principal(),
            EntityResolutionRequest(record_id=CANDIDATE.record_id, expected_revision=1)).payload
        self.assertIsNone(suggestions.suggestions[0].target)
        self.assertEqual(suggestions.review_targets[0].status, 'APPROVED')
        request = EntityResolutionApplyRequest(record_id=CANDIDATE.record_id, expected_revision=1,
            target_entity_id=TARGET.entity_id, target_record_id='confirmed-target',
            target_expected_revision=2, notes='Both paragraphs refer to this pump.')
        response = adapter.apply_resolution(_principal(), request).payload
        self.assertIsNone(response.applied_suggestion)
        self.assertEqual(response.applied_target.entity.entity_id, TARGET.entity_id)
        self.assertEqual(reviews.call[1]['target_expected_revision'], 2)
        self.assertEqual(reviews.call[1]['target'], TARGET)
        reviews.selectable = False
        with self.assertRaises(ConflictError):
            adapter.apply_resolution(_principal(), request)

    def test_manual_apply_reads_only_pinned_target_and_keeps_transaction_validation(self):
        from graphrag_prod.api.knowledge import _entity_payload, _evidence_payload
        from graphrag_prod.knowledge.store import KnowledgeConflict

        reviews = _Reviews()
        target = dict(record_id='confirmed-target', revision=2,
            entity=_entity_payload(TARGET), status='APPROVED', authority='AUTHORITATIVE',
            evidence=_evidence_payload(CANDIDATE.evidence), identity_properties=[],
            selectable=True, reason='Check both source contexts.')
        reviews.resolution_target = Mock(return_value=target)
        reviews.resolution_targets = Mock(side_effect=AssertionError('must not enumerate targets'))
        adapter = self._adapter(reviews=reviews)
        adapter.resolution_source = SimpleNamespace(
            find_exact_canonical_key=Mock(side_effect=AssertionError('manual choice must not run automatic matching')))
        request = EntityResolutionApplyRequest(record_id=CANDIDATE.record_id, expected_revision=1,
            target_entity_id=TARGET.entity_id, target_record_id='confirmed-target',
            target_expected_revision=2, notes='双方原文确认同一设备。')
        result = adapter.apply_resolution(_principal(), request).payload
        self.assertEqual(result.applied_target.entity.entity_id, TARGET.entity_id)
        self.assertIsNone(adapter.knowledge.identity_call)
        reviews.resolution_target.assert_called_once_with(_principal(), CANDIDATE, 'confirmed-target', 2)
        self.assertEqual(reviews.call[1]['target_expected_revision'], 2)
        reviews.apply_entity_resolution = Mock(side_effect=KnowledgeConflict('identity changed after selection'))
        with self.assertRaises(ConflictError):
            adapter.apply_resolution(_principal(), request)
        reviews.resolution_target.return_value = None
        with self.assertRaises(ResourceNotFoundError):
            adapter.apply_resolution(_principal(), request)
        self.assertEqual(reviews.apply_entity_resolution.call_count, 1)

    def test_suggestions_read_active_ontology_once_per_request(self):
        adapter = self._adapter()
        adapter.tboxes.get = Mock(wraps=adapter.tboxes.get)
        adapter.tboxes.active = Mock(wraps=adapter.tboxes.active)
        adapter.resolution_suggestions(_principal(), EntityResolutionRequest(
            record_id=CANDIDATE.record_id, expected_revision=1))
        adapter.tboxes.get.assert_called_once_with(TENANT, TBOX.tbox_id)
        adapter.tboxes.active.assert_called_once_with(TENANT, TBOX.key)

    def test_independent_decision_contract_reaches_transaction_with_preview_and_group(self):
        from graphrag_prod.api.knowledge_contracts import ReviewBatchRequest

        class IndependentReviews(_Reviews):
            def review_batch(self, principal, requests):
                self.requests = requests
                return self.apply_entity_resolution(principal, record_id=requests[0].record_id)

        reviews = IndependentReviews()
        adapter = self._adapter(reviews=reviews)
        response = adapter.review_batch(_principal(), ReviewBatchRequest(decisions=[dict(
            record_kind='ENTITY_MENTION', record_id=CANDIDATE.record_id, expected_revision=1,
            decision='APPROVED', notes='上下文指向独立对象', identity_action='INDEPENDENT',
            identity_group='selected', expected_identity_impact='preview-token')])).payload
        self.assertEqual(reviews.requests[0].identity_action, 'INDEPENDENT')
        self.assertEqual(reviews.requests[0].identity_group, 'selected')
        self.assertEqual(reviews.requests[0].expected_identity_impact, 'preview-token')
        self.assertEqual(response.outcomes[1].status, 'CANDIDATE')

    def _adapter(
        self,
        *,
        store: object | None = None,
        reviews: _Reviews | None = None,
    ) -> Neo4jKnowledgeOperations:
        return Neo4jKnowledgeOperations(
            driver=SimpleNamespace(),
            construction=SimpleNamespace(run=lambda *_args: None),
            tboxes=_TBoxes(),
            knowledge=store or _Store(),
            reviews=reviews or _Reviews(),
            publications=SimpleNamespace(),
            resolution_source=_ResolutionSource(),
            clock=lambda: NOW,
        )

    def test_suggestion_uses_current_candidate_identity_fact_and_audit_metadata(self) -> None:
        store = _Store()
        response = self._adapter(store=store).resolution_suggestions(
            _principal(),
            EntityResolutionRequest(
                record_id=CANDIDATE.record_id,
                expected_revision=1,
            ),
        ).payload

        self.assertEqual(response.candidate.canonical_key, "llm-candidate:pump-77")
        self.assertEqual(response.identity_properties[0].name, "serial_number")
        self.assertEqual(response.identity_properties[0].canonical_value, "SN-77")
        suggestion = response.suggestions[0]
        self.assertEqual(suggestion.outcome, "AUTO_LINK")
        self.assertEqual(suggestion.target.entity_id, TARGET.entity_id)  # type: ignore[union-attr]
        self.assertEqual(suggestion.rule_version, "authoritative-resolution-rules:v1")
        self.assertEqual(suggestion.matcher_version, "tbox-identity-properties:v1")
        self.assertEqual(
            suggestion.evidence[0].authoritative_evidence[0].chunk_id,
            "chunk-authority",
        )
        assert store.identity_call is not None
        self.assertEqual(store.identity_call[1]["subject_entity_id"], CANDIDATE_IDENTITY.entity_id)

    def test_apply_recomputes_suggestion_and_creates_cas_review_revision(self) -> None:
        reviews = _Reviews()
        response = self._adapter(reviews=reviews).apply_resolution(
            _principal(),
            EntityResolutionApplyRequest(
                record_id=CANDIDATE.record_id,
                expected_revision=1,
                target_entity_id=TARGET.entity_id,
                notes="Expert verified the serial-number match.",
            ),
        ).payload

        self.assertEqual(response.outcomes[0].status, "APPROVED")
        self.assertEqual(response.outcomes[1].status, "CANDIDATE")
        self.assertEqual(response.applied_suggestion.target.entity_id, TARGET.entity_id)  # type: ignore[union-attr]
        assert reviews.call is not None
        call = reviews.call[1]
        self.assertEqual(call["expected_revision"], 1)
        self.assertEqual(call["target"], TARGET)
        self.assertIn("authoritative-resolution-rules:v1", call["notes"])
        self.assertIn(TARGET.entity_id, call["notes"])

    def test_absent_cross_tenant_stale_and_unproposed_target_fail_closed(self) -> None:
        adapter = self._adapter()
        with self.assertRaises(ResourceNotFoundError):
            adapter.resolution_suggestions(
                _principal(tenant_id="tenant-other"),
                EntityResolutionRequest(
                    record_id=CANDIDATE.record_id,
                    expected_revision=1,
                ),
            )
        with self.assertRaises(ConflictError):
            adapter.resolution_suggestions(
                _principal(),
                EntityResolutionRequest(
                    record_id=CANDIDATE.record_id,
                    expected_revision=2,
                ),
            )
        with self.assertRaises(ResourceNotFoundError):
            adapter.apply_resolution(
                _principal(),
                EntityResolutionApplyRequest(
                    record_id=CANDIDATE.record_id,
                    expected_revision=1,
                    target_entity_id="forged-target",
                    notes="Attempted forged target.",
                ),
            )


if __name__ == "__main__":
    unittest.main()
