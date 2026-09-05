"""Contracts and adapters for governed knowledge HTTP operations."""

from __future__ import annotations

import base64
import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace
import unittest

from pydantic import ValidationError

from graphrag_prod.api.backend import GraphRAGApplicationBackend
from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.api.knowledge_contracts import (
    ActivePublicationInventoryRequest,
    ActivePublicationInventoryResponse,
    AuthoritativeImportRequest,
    ConstructionJobListRequest,
    ConstructionJobListResponse,
    DocumentLifecycleListRequest,
    DocumentLifecycleListResponse,
    DocumentRetirementRequest,
    DocumentRetirementResponse,
    KnowledgeConstructionRequest,
    OntologyImportRequest,
    OntologyListRequest,
    OntologyListResponse,
    OntologyPublishRequest,
    PublicationHistoryRequest,
    PublicationCandidatesRequest,
    PublicationRequest,
    PublishedGraphQualityResponse,
    ReviewBatchRequest,
    ReviewQueueRequest,
    RecordRevisionHistoryRequest,
    RollbackRequest,
)
from graphrag_prod.api.runtime import (
    AuthorizationError,
    BackendResult,
    ConflictError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    OperationEnvelope,
    OperationKind,
    RequestValidationError,
    ResourceNotFoundError,
    required_scope,
)
from graphrag_prod.construction import (
    ConstructionBudgetExceeded,
    ConstructionJobView,
    DocumentParseError,
)
from graphrag_prod.domain import Principal, TypedLiteralValue
from graphrag_prod.domain.ids import entity_id as make_entity_id
from graphrag_prod.graph.published_quality import (
    PublishedGraphQualityAuthorizationError,
    PublishedGraphQualityConflict,
    PublishedGraphQualityIssue,
    PublishedGraphQualityLimitExceeded,
    PublishedGraphQualityReport,
    PublishedGraphQualityUnavailable,
    PublishedGraphReviewSampleItem,
)
from graphrag_prod.graph.published_inventory import (
    ActivePublicationInventory,
    ActivePublicationInventoryAuthorizationError,
    ActivePublicationInventoryConflict,
    ActivePublicationInventoryItem,
    ActivePublicationInventoryLimitExceeded,
    ActivePublicationInventoryUnavailable,
    InventoryAssertionSummary,
    InventoryEntitySummary,
)
from graphrag_prod.graph.quality import IssueSeverity
from graphrag_prod.ingestion.retirement import (
    DocumentLifecycleView,
    DocumentRetirementBackendUnavailable,
    DocumentRetirementBlocked,
    DocumentRetirementConflict,
    DocumentRetirementResult,
    DocumentRetirementUnavailable,
)
from graphrag_prod.knowledge import (
    AssertionRecord,
    EntityIdentity,
    EvidenceReference,
    GovernanceStatus,
    KnowledgeWriteResult,
    RecordRevision,
    knowledge_record_id,
    llm_candidate_trust,
)
from graphrag_prod.knowledge.review import (
    KnowledgePublicationView,
    PublicationCandidate,
    ReviewBatchResult,
    ReviewOutcome,
    ReviewQueueItem,
    ReviewRecordKind,
)
from graphrag_prod.ontology import (
    Cardinality,
    EntityTypeDefinition,
    PropertyDataType,
    PropertyDefinition,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxValidationError,
    TBoxVersion,
)


NOW = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
ACTIVE_TBOX = TBoxVersion(
    tenant_id="tenant-alpha",
    key="industrial-assets",
    version=1,
    status=TBoxStatus.PUBLISHED,
    entity_types=(
        EntityTypeDefinition(
            "Asset",
            ("asset-id",),
            properties=(
                PropertyDefinition(
                    "PRESSURE",
                    PropertyDataType.DECIMAL,
                    False,
                    Cardinality.ZERO_OR_MORE,
                    unit="kPa",
                ),
            ),
        ),
    ),
    relationship_types=(),
)


RELATIONSHIP_TBOX = TBoxVersion(
    tenant_id="tenant-alpha",
    key="industrial-relations",
    version=1,
    status=TBoxStatus.PUBLISHED,
    entity_types=(
        EntityTypeDefinition("Asset", ("asset-id",)),
        EntityTypeDefinition("Organization", ("org-id",)),
    ),
    relationship_types=(
        RelationshipTypeDefinition(
            "SUPPLIED_BY",
            ("Asset",),
            ("Organization",),
            properties=(
                PropertyDefinition(
                    "SupplyShare",
                    PropertyDataType.DECIMAL,
                    True,
                    Cardinality.ONE,
                    unit="percent",
                ),
            ),
        ),
    ),
)


def _quality_report() -> PublishedGraphQualityReport:
    return PublishedGraphQualityReport(
        run_id="published-graph-quality:" + "1" * 64,
        ruleset_version="published-governed-graph-quality-v1",
        tenant_id="tenant-alpha",
        publication_id="publication-1",
        publication_generation=2,
        manifest_hash="2" * 64,
        ontology_version_id="tbox-1",
        tbox_checksum="3" * 64,
        corpus_revision=7,
        graph_digest="4" * 64,
        counts=(
            ("assertions", 1),
            ("canonical_entities", 2),
            ("entity_mentions", 2),
            ("literal_assertions", 0),
            ("relationship_assertions", 1),
            ("revisions", 3),
        ),
        total_issue_count=1,
        total_error_count=0,
        issues_truncated=False,
        issues=(
            PublishedGraphQualityIssue(
                issue_id="published-quality-issue:" + "5" * 64,
                code="ANOMALOUS_HUB",
                severity=IssueSeverity.REVIEW,
                object_kind="Entity",
                object_id="entity-1",
                detail="entity degree exceeds the configured review threshold",
            ),
        ),
        review_sample=(
            PublishedGraphReviewSampleItem(
                object_kind="Entity",
                object_id="entity-1",
                issue_codes=("ANOMALOUS_HUB",),
                evidence_chunk_ids=("chunk-1",),
            ),
        ),
    )


def _lifecycle_view(*, tenant_id: str = "tenant-alpha") -> DocumentLifecycleView:
    return DocumentLifecycleView(
        tenant_id=tenant_id,
        document_id="document-1",
        title="Asset report",
        source_name="controlled upload",
        canonical_uri="https://example.test/asset.txt",
        source_generation=3,
        active_snapshot_id="snapshot-3",
        active_version_id="version-3",
        chunk_count=2,
        access_policy_id="policy-engineering",
        access_policy_version=4,
        access_groups=("engineers",),
        blocker_codes=(),
    )


def _inventory(*, tenant_id: str = "tenant-alpha") -> ActivePublicationInventory:
    company = InventoryEntitySummary("entity-1", "Organization", "org:one", "Org One")
    asset = InventoryEntitySummary("entity-2", "Asset", "asset:one", "Asset One")
    return ActivePublicationInventory(
        tenant_id=tenant_id,
        publication_id="publication-1",
        publication_generation=2,
        manifest_hash="2" * 64,
        ontology_version_id="tbox-1",
        document_id=None,
        total_record_count=1,
        matching_record_count=1,
        truncated=False,
        items=(
            ActivePublicationInventoryItem(
                record_id="record-1",
                revision_id="revision-1",
                record_kind="ASSERTION",
                governance_status="PUBLISHED",
                origin="LLM_EXTRACTED",
                authority_level="SECONDARY",
                confidence=0.91,
                ontology_key="SUPPLIED_BY",
                document_id="document-1",
                version_id="version-1",
                chunk_id="chunk-1",
                evidence_chunk_ordinal=2,
                evidence_char_start=10,
                evidence_char_end=20,
                assertion=InventoryAssertionSummary(
                    subject=asset,
                    predicate="SUPPLIED_BY",
                    object_kind="entity",
                    object_entity=company,
                ),
            ),
        ),
    )


def _retirement_result(
    *, tenant_id: str = "tenant-alpha"
) -> DocumentRetirementResult:
    return DocumentRetirementResult(
        retirement_id="retirement-1",
        tenant_id=tenant_id,
        document_id="document-1",
        retired_snapshot_id="snapshot-3",
        retired_version_id="version-3",
        source_generation_before=3,
        source_generation_after=4,
        corpus_revision=8,
        retired_at=NOW,
    )


def _construct_payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "operation_key": "construction-000001",
        "canonical_uri": "https://example.test/asset.txt",
        "title": "Asset report",
        "source_name": "controlled upload",
        "mime_type": "text/plain",
        "language": "en",
        "tbox_key": "industrial-assets",
        "access_groups": ["engineers"],
        "content_base64": base64.b64encode(b"Acme owns Pump-7.").decode(),
    }
    value.update(changes)
    return value


def _authoritative_payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "ontology_version_id": ACTIVE_TBOX.tbox_id,
        "mentions": [
            {
                "source_key": "expert-pump-7",
                "entity": {
                    "entity_type": "Asset",
                    "canonical_key": "asset-id:P-7",
                    "canonical_name": "Pump-7",
                },
                "evidence": {
                    "document_id": "document-1",
                    "version_id": "version-1",
                    "chunk_id": "chunk-1",
                    "char_start": 10,
                    "char_end": 16,
                    "quoted_text": "Pump-7",
                },
            }
        ],
        "assertions": [],
    }
    value.update(changes)
    return value


def _candidate_literal() -> AssertionRecord:
    quote = (
        "Pump-7 pressure was 100 psi and corrected to 90 psi at "
        "2025-01-02T03:04:05Z"
    )
    subject = EntityIdentity(
        entity_id=make_entity_id("tenant-alpha", "Asset", "asset-id:P-7"),
        tenant_id="tenant-alpha",
        entity_type="Asset",
        canonical_key="asset-id:P-7",
        canonical_name="Pump-7",
    )
    semantics = TypedLiteralValue(
        datatype="DECIMAL",
        typed_value="689.4757293168361336722673443",
        raw_value="100",
        raw_unit="psi",
        canonical_value="689.4757293168361336722673443",
        canonical_unit="kPa",
        observed_at=datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
        raw_observed_at="2025-01-02T03:04:05Z",
    )
    return AssertionRecord(
        revision=RecordRevision.next(
            knowledge_record_id("tenant-alpha", "ASSERTION", "candidate-pressure"),
            0,
        ),
        tenant_id="tenant-alpha",
        subject=subject,
        predicate="PRESSURE",
        evidence=EvidenceReference(
            tenant_id="tenant-alpha",
            document_id="document-1",
            version_id="version-1",
            chunk_id="chunk-1",
            char_start=10,
            char_end=10 + len(quote),
            quoted_text=quote,
            access_policy_id="policy-engineering",
            access_policy_version=3,
            access_groups=frozenset({"engineers"}),
        ),
        subject_mention_revision_id="subject-mention-r1",
        literal_value="100",
        literal_semantics=semantics,
        confidence=0.91,
        trust=llm_candidate_trust(
            ontology_version_id=ACTIVE_TBOX.tbox_id,
            extractor_version="extractor-v1",
            prompt_version="prompt-v1",
            extracted_at=NOW,
        ),
        created_at=NOW,
    )


class KnowledgeContractTests(unittest.TestCase):
    def test_document_lifecycle_contracts_are_strict_metadata_only_and_bounded(
        self,
    ) -> None:
        item = {
            "document_id": "document-1",
            "title": "Asset report",
            "source_name": "controlled upload",
            "canonical_uri": "https://example.test/asset.txt",
            "source_generation": 3,
            "active_snapshot_id": "snapshot-3",
            "active_version_id": "version-3",
            "chunk_count": 2,
            "access_policy_id": "policy-engineering",
            "access_policy_version": 4,
            "access_groups": ["engineers"],
            "blocked": True,
            "blocker_codes": ["CURRENT_REVIEW"],
        }
        response = DocumentLifecycleListResponse.model_validate({"items": [item]})
        self.assertTrue(response.items[0].blocked)
        self.assertNotIn("tenant_id", response.model_dump(mode="json"))
        self.assertNotIn("source_text", str(response.model_dump(mode="json")))
        for invalid in (
            {**item, "blocked": False},
            {**item, "blocker_codes": ["UNSTABLE_INTERNAL_REASON"]},
            {**item, "access_groups": ["engineers", "engineers"]},
            {**item, "canonical_uri": "https://example.test/a?credential=secret"},
            {**item, "source_text": "protected source"},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    DocumentLifecycleListResponse.model_validate({"items": [invalid]})
        with self.assertRaises(ValidationError):
            DocumentLifecycleListRequest(limit=101)
        with self.assertRaises(ValidationError):
            DocumentLifecycleListResponse.model_validate({"items": [item, item]})

        request = DocumentRetirementRequest(
            operation_key="retirement-operation-0001",
            expected_active_snapshot_id="snapshot-3",
            source_generation=3,
        )
        self.assertEqual(request.source_generation, 3)
        result = DocumentRetirementResponse(
            retirement_id="retirement-1",
            document_id="document-1",
            retired_snapshot_id="snapshot-3",
            retired_version_id="version-3",
            source_generation_before=3,
            source_generation_after=4,
            corpus_revision=8,
            retired_at=NOW,
            status="RETIRED",
        )
        self.assertEqual(result.status, "RETIRED")
        with self.assertRaises(ValidationError):
            DocumentRetirementRequest.model_validate(
                {**request.model_dump(), "tenant_id": "tenant-victim"}
            )
        with self.assertRaises(ValidationError):
            DocumentRetirementResponse.model_validate(
                {**result.model_dump(), "source_generation_after": 5}
            )

    def test_published_quality_response_is_bounded_and_has_no_source_text_shape(
        self,
    ) -> None:
        payload = _quality_report().to_dict()
        payload.pop("tenant_id")
        response = PublishedGraphQualityResponse.model_validate(payload)
        self.assertEqual(len(response.issues), 1)
        self.assertFalse(response.issues_truncated)

        with self.assertRaises(ValidationError):
            PublishedGraphQualityResponse.model_validate(
                {**payload, "source_text": "protected source"}
            )
        with self.assertRaises(ValidationError):
            PublishedGraphQualityResponse.model_validate(
                {**payload, "total_issue_count": 2, "issues_truncated": False}
            )
        with self.assertRaises(ValidationError):
            PublishedGraphQualityResponse.model_validate(
                {**payload, "issues": payload["issues"] * 1_001}
            )

    def test_active_publication_inventory_contract_is_strict_and_text_free(
        self,
    ) -> None:
        payload = {
            "publication_id": "publication-1",
            "publication_generation": 2,
            "manifest_hash": "2" * 64,
            "ontology_version_id": "tbox-1",
            "document_id": None,
            "total_record_count": 1,
            "matching_record_count": 1,
            "truncated": False,
            "items": [
                {
                    "record_id": "record-1",
                    "revision_id": "revision-1",
                    "record_kind": "ASSERTION",
                    "governance_status": "PUBLISHED",
                    "origin": "LLM_EXTRACTED",
                    "authority_level": "SECONDARY",
                    "confidence": 0.91,
                    "ontology_key": "SUPPLIED_BY",
                    "evidence": {
                        "document_id": "document-1",
                        "version_id": "version-1",
                        "chunk_id": "chunk-1",
                        "ordinal": 2,
                        "char_start": 10,
                        "char_end": 20,
                    },
                    "entity": None,
                    "assertion": {
                        "subject": {
                            "entity_id": "entity-2",
                            "entity_type": "Asset",
                            "canonical_key": "asset:one",
                            "display_name": "Asset One",
                        },
                        "predicate": "SUPPLIED_BY",
                        "object_kind": "entity",
                        "object_entity": {
                            "entity_id": "entity-1",
                            "entity_type": "Organization",
                            "canonical_key": "org:one",
                            "display_name": "Org One",
                        },
                        "literal": None,
                        "relationship_properties": [],
                    },
                }
            ],
        }
        response = ActivePublicationInventoryResponse.model_validate(payload)
        self.assertEqual(response.items[0].evidence.ordinal, 2)
        serialized = str(response.model_dump(mode="json"))
        self.assertNotIn("tenant_id", serialized)
        self.assertNotIn("evidence_text", serialized)
        self.assertNotIn("source_text", serialized)
        for invalid in (
            {**payload, "tenant_id": "tenant-victim"},
            {**payload, "truncated": True},
            {**payload, "matching_record_count": 0},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    ActivePublicationInventoryResponse.model_validate(invalid)
        with self.assertRaises(ValidationError):
            ActivePublicationInventoryRequest(limit=501)

    def test_construct_uses_strict_canonical_base64_and_supported_mime(self) -> None:
        for mime_type in (
            "text/plain",
            "text/markdown",
            "text/csv",
            "application/json",
        ):
            with self.subTest(mime_type=mime_type):
                request = KnowledgeConstructionRequest.model_validate(
                    _construct_payload(mime_type=mime_type)
                )
                self.assertEqual(request.decoded_content(), b"Acme owns Pump-7.")
        for changes in (
            {"content_base64": "not base64"},
            {"content_base64": "eA"},
            {"content_base64": base64.urlsafe_b64encode(b"\xff").decode()},
            {"mime_type": "application/pdf"},
            {"content_base64": base64.b64encode(b"").decode()},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(ValidationError):
                    KnowledgeConstructionRequest.model_validate(
                        _construct_payload(**changes)
                    )

    def test_clients_cannot_supply_identity_acl_or_capabilities(self) -> None:
        targets = (
            (KnowledgeConstructionRequest, _construct_payload()),
            (AuthoritativeImportRequest, _authoritative_payload()),
            (
                OntologyImportRequest,
                {
                    "key": "industrial-assets",
                    "version": 1,
                    "entity_types": [
                        {
                            "name": "Asset",
                            "canonical_key_namespaces": ["asset-id"],
                        }
                    ],
                },
            ),
        )
        for model, payload in targets:
            forbidden_fields = ["tenant_id", "principal_id", "capabilities"]
            if model is not KnowledgeConstructionRequest:
                forbidden_fields.append("access_groups")
            for forbidden in forbidden_fields:
                with self.subTest(model=model.__name__, forbidden=forbidden):
                    with self.assertRaises(ValidationError):
                        model.model_validate({**payload, forbidden: "forged"})

    def test_construction_requires_a_unique_nonempty_source_acl(self) -> None:
        for access_groups in ([], ["engineers", "engineers"]):
            with self.subTest(access_groups=access_groups):
                with self.assertRaises(ValidationError):
                    KnowledgeConstructionRequest.model_validate(
                        _construct_payload(access_groups=access_groups)
                    )

    def test_literal_writes_accept_raw_source_tokens_only(self) -> None:
        quote = "Pump-7 pressure was 100 psi at 2025-01-02T03:04:05Z"
        assertion = {
            "source_key": "expert-pressure-1",
            "subject_mention_source_key": "expert-pump-7",
            "predicate": "PRESSURE",
            "evidence": {
                "document_id": "document-1",
                "version_id": "version-1",
                "chunk_id": "chunk-1",
                "char_start": 10,
                "char_end": 10 + len(quote),
                "quoted_text": quote,
            },
            "literal": {
                "raw_literal": "100",
                "raw_unit": "psi",
                "raw_observed_at": "2025-01-02T03:04:05Z",
            },
        }
        request = AuthoritativeImportRequest.model_validate(
            _authoritative_payload(assertions=[assertion])
        )
        self.assertEqual(request.assertions[0].literal.raw_literal, "100")

        invalid_assertions = (
            {**assertion, "literal_value": "100", "literal": None},
            {
                **assertion,
                "literal": {
                    **assertion["literal"],
                    "canonical_value": "689.4757293168",
                },
            },
            {
                **assertion,
                "object_mention_source_key": "expert-pump-7",
            },
            {
                **assertion,
                "literal": {
                    **assertion["literal"],
                    "ontology_version_id": ACTIVE_TBOX.tbox_id,
                },
            },
        )
        for invalid in invalid_assertions:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    AuthoritativeImportRequest.model_validate(
                        _authoritative_payload(assertions=[invalid])
                    )

    def test_operation_scopes_are_independent_and_writes_never_retry(self) -> None:
        expected = {
            OperationKind.ONTOLOGY_LIST: ("ontology:read", False),
            OperationKind.ONTOLOGY_IMPORT: ("ontology:write", True),
            OperationKind.ONTOLOGY_PUBLISH: ("ontology:publish", True),
            OperationKind.KNOWLEDGE_IMPORT: ("knowledge:import", True),
            OperationKind.KNOWLEDGE_CONSTRUCT: ("knowledge:construct", True),
            OperationKind.KNOWLEDGE_CONSTRUCTION_JOB: ("knowledge:construct", False),
            OperationKind.KNOWLEDGE_CONSTRUCTION_JOBS: ("knowledge:construct", False),
            OperationKind.KNOWLEDGE_REVIEW_QUEUE: ("knowledge:review", False),
            OperationKind.KNOWLEDGE_REVISION_HISTORY: ("knowledge:review", False),
            OperationKind.KNOWLEDGE_REVIEW_BATCH: ("knowledge:review", True),
            OperationKind.ENTITY_RESOLUTION_SUGGEST: ("knowledge:review", False),
            OperationKind.ENTITY_RESOLUTION_APPLY: ("knowledge:review", True),
            OperationKind.KNOWLEDGE_PUBLISH: ("knowledge:publish", True),
            OperationKind.KNOWLEDGE_ROLLBACK: ("knowledge:publish", True),
            OperationKind.KNOWLEDGE_HISTORY: ("knowledge:publish", False),
            OperationKind.KNOWLEDGE_PUBLICATION_CANDIDATES: ("knowledge:publish", False),
            OperationKind.KNOWLEDGE_QUALITY: ("knowledge:quality", False),
            OperationKind.KNOWLEDGE_INVENTORY: ("knowledge:quality", False),
            OperationKind.KNOWLEDGE_DOCUMENTS: ("knowledge:lifecycle", False),
            OperationKind.KNOWLEDGE_DOCUMENT_RETIRE: ("knowledge:lifecycle", True),
        }
        for operation, (scope, write) in expected.items():
            with self.subTest(operation=operation):
                self.assertEqual(required_scope(operation), scope)
                self.assertEqual(operation.is_write, write)
                self.assertEqual(operation.is_retry_safe, not write)


class _Documents:
    ingest = delete = get_job = lambda *args, **kwargs: None


class _Queries:
    retrieve = answer = lambda *args, **kwargs: None


class _Readiness:
    check = lambda *args, **kwargs: None


class _Knowledge:
    def __init__(self) -> None:
        self.principal: Principal | None = None

    def ontology_list(self, principal: Principal, _request: object) -> BackendResult:
        self.principal = principal
        return BackendResult(OntologyListResponse(items=()))

    def construction_jobs(self, principal: Principal, _request: object) -> BackendResult:
        self.principal = principal
        return BackendResult(ConstructionJobListResponse(items=()))

    ontology_import = ontology_publish = authoritative_import = lambda *args: None
    construct = construction_job = lambda *args: None
    review_queue = revision_history = review_batch = lambda *args: None
    resolution_suggestions = apply_resolution = lambda *args: None
    publish = rollback = history = publication_candidates = lambda *args: None
    record_quality = quality_runs = quality_run = lambda *args: None

    def documents(self, principal: Principal, _request: object) -> BackendResult:
        self.principal = principal
        view = _lifecycle_view()
        return BackendResult(
            DocumentLifecycleListResponse(
                items=(
                    {
                        name: getattr(view, name)
                        for name in (
                            "document_id",
                            "title",
                            "source_name",
                            "canonical_uri",
                            "source_generation",
                            "active_snapshot_id",
                            "active_version_id",
                            "chunk_count",
                            "access_policy_id",
                            "access_policy_version",
                            "access_groups",
                            "blocked",
                            "blocker_codes",
                        )
                    },
                )
            )
        )

    def retire_document(
        self, principal: Principal, document_id: str, _request: object
    ) -> BackendResult:
        self.principal = principal
        return BackendResult(
            DocumentRetirementResponse(
                retirement_id="retirement-1",
                document_id=document_id,
                retired_snapshot_id="snapshot-3",
                retired_version_id="version-3",
                source_generation_before=3,
                source_generation_after=4,
                corpus_revision=8,
                retired_at=NOW,
                status="RETIRED",
            )
        )

    def quality(self, principal: Principal) -> BackendResult:
        self.principal = principal
        payload = _quality_report().to_dict()
        payload.pop("tenant_id")
        return BackendResult(PublishedGraphQualityResponse.model_validate(payload))

    def inventory(self, principal: Principal, _request: object) -> BackendResult:
        self.principal = principal
        value = _inventory()
        payload = value.to_dict()
        payload.pop("tenant_id")
        item = payload["items"][0]
        item["evidence"] = {
            "document_id": item.pop("document_id"),
            "version_id": item.pop("version_id"),
            "chunk_id": item.pop("chunk_id"),
            "ordinal": item.pop("evidence_chunk_ordinal"),
            "char_start": item.pop("evidence_char_start"),
            "char_end": item.pop("evidence_char_end"),
        }
        return BackendResult(ActivePublicationInventoryResponse.model_validate(payload))


class KnowledgeBackendTests(unittest.TestCase):
    def test_legacy_backend_constructor_remains_valid_and_defaults_closed(self) -> None:
        backend = GraphRAGApplicationBackend(
            documents=_Documents(),
            queries=_Queries(),
            readiness=_Readiness(),
        )
        with self.assertRaises(ResourceNotFoundError):
            backend.execute(
                OperationEnvelope(
                    operation=OperationKind.ONTOLOGY_LIST,
                    request_id="request-1",
                    trace_id="trace-1",
                    principal_id="expert-1",
                    tenant_id="tenant-alpha",
                    access_groups=frozenset({"engineers"}),
                    scopes=frozenset({"ontology:read"}),
                    payload={},
                )
            )

    def test_backend_passes_verified_scopes_as_domain_capabilities(self) -> None:
        knowledge = _Knowledge()
        backend = GraphRAGApplicationBackend(
            documents=_Documents(),
            queries=_Queries(),
            readiness=_Readiness(),
            knowledge=knowledge,
        )
        result = backend.execute(
            OperationEnvelope(
                operation=OperationKind.ONTOLOGY_LIST,
                request_id="request-1",
                trace_id="trace-1",
                principal_id="expert-1",
                tenant_id="tenant-alpha",
                access_groups=frozenset({"engineers"}),
                scopes=frozenset({"ontology:read", "knowledge:review"}),
                payload={"limit": 10},
            )
        )
        self.assertEqual(result.payload.items, ())
        assert knowledge.principal is not None
        self.assertEqual(
            knowledge.principal.capabilities,
            frozenset({"ontology:read", "knowledge:review"}),
        )

    def test_wrong_scope_stops_before_knowledge_adapter(self) -> None:
        knowledge = _Knowledge()
        backend = GraphRAGApplicationBackend(
            documents=_Documents(),
            queries=_Queries(),
            readiness=_Readiness(),
            knowledge=knowledge,
        )
        with self.assertRaises(AuthorizationError):
            backend.execute(
                OperationEnvelope(
                    operation=OperationKind.ONTOLOGY_LIST,
                    request_id="request-1",
                    trace_id="trace-1",
                    principal_id="expert-1",
                    tenant_id="tenant-alpha",
                    access_groups=frozenset({"engineers"}),
                    scopes=frozenset({"ontology:write"}),
                    payload={},
                )
            )
        self.assertIsNone(knowledge.principal)

    def test_backend_routes_bounded_construction_job_list(self) -> None:
        knowledge = _Knowledge()
        backend = GraphRAGApplicationBackend(
            documents=_Documents(),
            queries=_Queries(),
            readiness=_Readiness(),
            knowledge=knowledge,
        )
        result = backend.execute(
            OperationEnvelope(
                operation=OperationKind.KNOWLEDGE_CONSTRUCTION_JOBS,
                request_id="request-1",
                trace_id="trace-1",
                principal_id="expert-1",
                tenant_id="tenant-alpha",
                access_groups=frozenset({"engineers"}),
                scopes=frozenset({"knowledge:construct"}),
                payload={"statuses": ("RETRY_WAIT",), "limit": 25},
            )
        )
        self.assertEqual(result.payload.items, ())
        assert knowledge.principal is not None
        self.assertEqual(knowledge.principal.tenant_id, "tenant-alpha")

    def test_backend_routes_quality_with_its_independent_scope(self) -> None:
        knowledge = _Knowledge()
        backend = GraphRAGApplicationBackend(
            documents=_Documents(),
            queries=_Queries(),
            readiness=_Readiness(),
            knowledge=knowledge,
        )
        result = backend.execute(
            OperationEnvelope(
                operation=OperationKind.KNOWLEDGE_QUALITY,
                request_id="request-1",
                trace_id="trace-1",
                principal_id="expert-1",
                tenant_id="tenant-alpha",
                access_groups=frozenset({"engineers"}),
                scopes=frozenset({"knowledge:quality"}),
                payload={},
            )
        )
        self.assertTrue(result.payload.passed)
        assert knowledge.principal is not None
        self.assertEqual(knowledge.principal.capabilities, frozenset({"knowledge:quality"}))

    def test_backend_routes_document_list_and_non_retrying_retirement(self) -> None:
        knowledge = _Knowledge()
        backend = GraphRAGApplicationBackend(
            documents=_Documents(),
            queries=_Queries(),
            readiness=_Readiness(),
            knowledge=knowledge,
        )
        common = {
            "request_id": "request-1",
            "trace_id": "trace-1",
            "principal_id": "expert-1",
            "tenant_id": "tenant-alpha",
            "access_groups": frozenset({"engineers"}),
            "scopes": frozenset({"knowledge:lifecycle"}),
        }
        listed = backend.execute(
            OperationEnvelope(
                operation=OperationKind.KNOWLEDGE_DOCUMENTS,
                payload={"limit": 25},
                **common,
            )
        )
        retired = backend.execute(
            OperationEnvelope(
                operation=OperationKind.KNOWLEDGE_DOCUMENT_RETIRE,
                payload={
                    "document_id": "document-1",
                    "request": {
                        "operation_key": "retirement-operation-0001",
                        "expected_active_snapshot_id": "snapshot-3",
                        "source_generation": 3,
                    },
                },
                **common,
            )
        )
        self.assertEqual(listed.payload.items[0].document_id, "document-1")
        self.assertEqual(retired.payload.status, "RETIRED")
        assert knowledge.principal is not None
        self.assertEqual(
            knowledge.principal.capabilities,
            frozenset({"knowledge:lifecycle"}),
        )


class _RowsSession:
    def __init__(self, owner: "_Driver") -> None:
        self.owner = owner

    def __enter__(self) -> "_RowsSession":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def run(self, query: str, **parameters: object) -> list[dict[str, object]]:
        self.owner.calls.append((query, parameters))
        if self.owner.failure is not None:
            raise self.owner.failure
        if self.owner.allowed and self.owner.active:
            return [
                {
                    "access_policy_id": "policy-engineering",
                    "access_policy_version": 3,
                    "access_groups": ["engineers"],
                }
            ]
        return []


class _Driver:
    def __init__(
        self,
        *,
        allowed: bool = True,
        active: bool = True,
        failure: Exception | None = None,
    ) -> None:
        self.allowed = allowed
        self.active = active
        self.failure = failure
        self.calls: list[tuple[str, dict[str, object]]] = []

    def session(self, **_kwargs: object) -> _RowsSession:
        return _RowsSession(self)


class _Construction:
    def __init__(self, *, status: str = "CANDIDATE") -> None:
        self.call = None
        self.status = status

    def run(self, *_args: object) -> object:
        self.call = _args
        return SimpleNamespace(
            job_id="job-1",
            document_id="document-1",
            version_id="version-1",
            snapshot_id="snapshot-1",
            tbox_id="tbox-1",
            chunks=(
                SimpleNamespace(
                    chunk_id="chunk-1",
                    artifact_id="artifact-1",
                    status=self.status,
                    finding_codes=(),
                    mention_record_ids=(
                        () if self.status == "EMPTY" else ("mention-1",)
                    ),
                    assertion_record_ids=(),
                    replayed=False,
                ),
            ),
        )


class _ConstructionAudit:
    def __init__(self) -> None:
        self.value = ConstructionJobView(
            job_id="job-1",
            tenant_id="tenant-alpha",
            document_id="document-1",
            version_id="version-1",
            snapshot_id="snapshot-1",
            tbox_id="tbox-1",
            status="COMPLETED",
            expected_chunks=1,
            completed_chunks=1,
            created_at=NOW,
            updated_at=NOW,
            completed_at=NOW,
        )
        self.calls: list[tuple[str, object]] = []

    def get_job(self, principal: Principal, job_id: str) -> ConstructionJobView | None:
        self.calls.append(("get", (principal, job_id)))
        if principal.tenant_id != self.value.tenant_id or job_id != self.value.job_id:
            return None
        return self.value

    def list_jobs(self, principal: Principal, **kwargs: object) -> tuple[ConstructionJobView, ...]:
        self.calls.append(("list", (principal, kwargs)))
        return (self.value,) if principal.tenant_id == self.value.tenant_id else ()


class _MalformedConstruction:
    def run(self, *_args: object) -> object:
        return SimpleNamespace(
            job_id="",
            document_id="document-1",
            version_id="version-1",
            snapshot_id="snapshot-1",
            tbox_id="tbox-1",
            chunks=(),
        )


class _FailingConstruction:
    def __init__(self, failure: Exception) -> None:
        self.failure = failure

    def run(self, *_args: object) -> object:
        raise self.failure


class _KnowledgeStore:
    def __init__(self, current_assertion: AssertionRecord | None = None) -> None:
        self.batch = None
        self.current_assertion = current_assertion
        self.get_call = None

    def import_authoritative(self, batch: object) -> KnowledgeWriteResult:
        self.batch = batch
        return KnowledgeWriteResult(
            tenant_id="tenant-alpha",
            ontology_version_id=batch.ontology_version_id,  # type: ignore[attr-defined]
            mention_count=len(batch.mentions),  # type: ignore[attr-defined]
            assertion_count=len(batch.assertions),  # type: ignore[attr-defined]
            revision_ids=tuple(  # type: ignore[attr-defined]
                record.revision_id
                for record in (*batch.mentions, *batch.assertions)
            ),
        )

    def get_assertion(
        self,
        principal: Principal,
        record_id: str,
        *,
        statuses: tuple[object, ...],
    ) -> AssertionRecord | None:
        self.get_call = (principal, record_id, statuses)
        value = self.current_assertion
        if (
            value is None
            or value.tenant_id != principal.tenant_id
            or value.record_id != record_id
        ):
            return None
        return value


class _TBoxes:
    def __init__(
        self,
        *,
        active: bool = True,
        import_failure: Exception | None = None,
    ) -> None:
        self.value = ACTIVE_TBOX
        self.is_active = active
        self.import_failure = import_failure
        self.imported = None
        self.publish_call = None

    def list(self, tenant_id: str, **_kwargs: object) -> tuple[TBoxVersion, ...]:
        return (self.value,) if tenant_id == "tenant-alpha" else ()

    def import_version(
        self, value: TBoxVersion, *, expected_checksum: str | None
    ) -> TBoxVersion:
        if self.import_failure is not None:
            raise self.import_failure
        self.imported = (value, expected_checksum)
        return value

    def get(self, tenant_id: str, tbox_id: str) -> TBoxVersion:
        if tenant_id != "tenant-alpha" or tbox_id != self.value.tbox_id:
            raise KeyError("absent")
        return self.value

    def active(self, tenant_id: str, key: str) -> TBoxVersion | None:
        if (
            self.is_active
            and tenant_id == self.value.tenant_id
            and key == self.value.key
        ):
            return self.value
        return None

    def publish(
        self,
        tenant_id: str,
        tbox_id: str,
        *,
        expected_active_tbox_id: str | None,
    ) -> TBoxVersion:
        self.publish_call = (tenant_id, tbox_id, expected_active_tbox_id)
        return self.value.with_status(TBoxStatus.PUBLISHED)


class _TBoxesWithValue(_TBoxes):
    def __init__(self, value: TBoxVersion) -> None:
        super().__init__()
        self.value = value


class _Reviews:
    def __init__(self, queue: tuple[object, ...] = ()) -> None:
        self.queue_call = None
        self.batch_call = None
        self.queue = queue

    def review_queue(self, principal: Principal, **kwargs: object) -> tuple[object, ...]:
        self.queue_call = (principal, kwargs)
        return self.queue

    def revision_history(
        self,
        principal: Principal,
        record_id: str,
        **kwargs: object,
    ) -> tuple[object, ...]:
        self.history_call = (principal, record_id, kwargs)
        return self.queue

    def review_batch(
        self, principal: Principal, requests: tuple[object, ...]
    ) -> ReviewBatchResult:
        self.batch_call = (principal, requests)
        item = requests[0]
        return ReviewBatchResult(
            tenant_id=principal.tenant_id,
            outcomes=(
                ReviewOutcome(
                    record_kind=item.record_kind,
                    record_id=item.record_id,
                    previous_revision_id="revision-1",
                    revision_id="revision-2",
                    revision=2,
                    status=item.decision,
                ),
            ),
        )


class _Publications:
    def __init__(self, candidates: tuple[object, ...] = ()) -> None:
        self.value = KnowledgePublicationView(
            publication_id="publication-1",
            tenant_id="tenant-alpha",
            ontology_version_id="tbox-1",
            generation=1,
            manifest_hash="b" * 64,
            source_revision_ids=("revision-2",),
            published_revision_ids=("published-revision-1",),
            removed_record_ids=(),
            replaced_record_ids=(),
            status="ACTIVE",
            created_by="expert-1",
            created_at=NOW,
            activated_at=NOW,
        )
        self.calls: list[tuple[str, object]] = []
        self.candidate_values = candidates

    def publish(self, principal: Principal, ids: tuple[str, ...], **kwargs: object) -> object:
        self.calls.append(("publish", (principal, ids, kwargs)))
        return self.value

    def get(self, principal: Principal, publication_id: str) -> object | None:
        self.calls.append(("get", (principal, publication_id)))
        return self.value if publication_id == self.value.publication_id else None

    def rollback(self, principal: Principal, publication_id: str, **kwargs: object) -> object:
        self.calls.append(("rollback", (principal, publication_id, kwargs)))
        return self.value

    def history(self, principal: Principal, *, limit: int) -> tuple[object, ...]:
        self.calls.append(("history", (principal, limit)))
        return (self.value,)

    def candidates(self, principal: Principal, *, limit: int) -> tuple[object, ...]:
        self.calls.append(("candidates", (principal, limit)))
        return self.candidate_values


class _Quality:
    def __init__(self, value: object = None, failure: Exception | None = None) -> None:
        self.value = _quality_report() if value is None else value
        self.failure = failure
        self.calls: list[Principal] = []

    def audit(self, principal: Principal) -> object:
        self.calls.append(principal)
        if self.failure is not None:
            raise self.failure
        return self.value


class _Inventory:
    def __init__(self, value: object = None, failure: Exception | None = None) -> None:
        self.value = _inventory() if value is None else value
        self.failure = failure
        self.calls: list[tuple[Principal, str | None, int]] = []

    def list_active(
        self,
        principal: Principal,
        *,
        document_id: str | None,
        limit: int,
    ) -> object:
        self.calls.append((principal, document_id, limit))
        if self.failure is not None:
            raise self.failure
        return self.value


class _Retirement:
    def __init__(
        self,
        *,
        documents: tuple[object, ...] | None = None,
        result: object | None = None,
        list_failure: Exception | None = None,
        retire_failure: Exception | None = None,
    ) -> None:
        self.document_values = documents or (_lifecycle_view(),)
        self.result = _retirement_result() if result is None else result
        self.list_failure = list_failure
        self.retire_failure = retire_failure
        self.calls: list[tuple[str, object]] = []

    def list_active_documents(
        self, principal: Principal, *, limit: int
    ) -> tuple[object, ...]:
        self.calls.append(("list", (principal, limit)))
        if self.list_failure is not None:
            raise self.list_failure
        return self.document_values

    def retire(self, principal: Principal, request: object) -> object:
        self.calls.append(("retire", (principal, request)))
        if self.retire_failure is not None:
            raise self.retire_failure
        return self.result


class KnowledgeAdapterTests(unittest.TestCase):
    def _adapter(
        self,
        driver: _Driver,
        store: _KnowledgeStore,
        *,
        construction: object | None = None,
        tboxes: object | None = None,
        reviews: object | None = None,
        publications: object | None = None,
        construction_audit: object | None = None,
        quality_service: object | None = None,
        inventory_service: object | None = None,
        retirement_service: object | None = None,
    ) -> Neo4jKnowledgeOperations:
        return Neo4jKnowledgeOperations(
            driver=driver,
            construction=construction or _Construction(),
            tboxes=tboxes or _TBoxes(),
            knowledge=store,
            reviews=reviews or SimpleNamespace(),
            publications=publications or SimpleNamespace(),
            construction_audit=construction_audit,
            quality_service=quality_service,
            inventory_service=inventory_service,
            retirement_service=retirement_service,
            clock=lambda: NOW,
        )

    def test_quality_service_injection_requires_an_audit_boundary(self) -> None:
        with self.assertRaisesRegex(TypeError, "quality_service must implement audit"):
            self._adapter(
                _Driver(),
                _KnowledgeStore(),
                quality_service=object(),
            )

    def test_inventory_service_injection_requires_list_boundary(self) -> None:
        with self.assertRaisesRegex(TypeError, "inventory_service must implement"):
            self._adapter(
                _Driver(),
                _KnowledgeStore(),
                inventory_service=object(),
            )

    def test_retirement_service_injection_requires_both_lifecycle_boundaries(
        self,
    ) -> None:
        for value in (
            object(),
            SimpleNamespace(list_active_documents=lambda *_args, **_kwargs: ()),
            SimpleNamespace(retire=lambda *_args, **_kwargs: None),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    TypeError,
                    "retirement_service must implement list_active_documents and retire",
                ):
                    self._adapter(
                        _Driver(),
                        _KnowledgeStore(),
                        retirement_service=value,
                    )

    def test_document_lifecycle_adapter_is_metadata_only_and_tenant_closed(
        self,
    ) -> None:
        retirement = _Retirement()
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:lifecycle"}),
        )
        adapter = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            retirement_service=retirement,
        )
        listed = adapter.documents(principal, DocumentLifecycleListRequest(limit=25))
        retired = adapter.retire_document(
            principal,
            "document-1",
            DocumentRetirementRequest(
                operation_key="retirement-operation-0001",
                expected_active_snapshot_id="snapshot-3",
                source_generation=3,
            ),
        )
        self.assertEqual(listed.payload.items[0].document_id, "document-1")
        self.assertFalse(listed.payload.items[0].blocked)
        self.assertEqual(retired.payload.corpus_revision, 8)
        self.assertNotIn("tenant_id", listed.payload.model_dump(mode="json"))
        self.assertNotIn("tenant_id", retired.payload.model_dump(mode="json"))
        self.assertNotIn("source_text", str(listed.payload.model_dump(mode="json")))
        self.assertEqual(retirement.calls[0], ("list", (principal, 25)))
        domain_request = retirement.calls[1][1][1]
        self.assertEqual(domain_request.document_id, "document-1")
        self.assertEqual(domain_request.expected_active_snapshot_id, "snapshot-3")

        foreign = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            retirement_service=_Retirement(documents=(_lifecycle_view(tenant_id="tenant-other"),)),
        )
        with self.assertRaises(DependencyUnavailableError):
            foreign.documents(principal, DocumentLifecycleListRequest())
        foreign_result = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            retirement_service=_Retirement(
                result=_retirement_result(tenant_id="tenant-other")
            ),
        )
        with self.assertRaises(DependencyUnavailableError):
            foreign_result.retire_document(
                principal,
                "document-1",
                DocumentRetirementRequest(
                    operation_key="retirement-operation-0001",
                    expected_active_snapshot_id="snapshot-3",
                    source_generation=3,
                ),
            )

    def test_document_lifecycle_errors_are_sanitized_and_non_enumerating(
        self,
    ) -> None:
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:lifecycle"}),
        )
        request = DocumentRetirementRequest(
            operation_key="retirement-operation-0001",
            expected_active_snapshot_id="snapshot-3",
            source_generation=3,
        )
        for failure, expected in (
            (DocumentRetirementUnavailable(), AuthorizationError),
            (DocumentRetirementConflict(), ConflictError),
            (DocumentRetirementBlocked(), ConflictError),
            (TimeoutError("bolt://secret/source"), DependencyTimeoutError),
            (DocumentRetirementBackendUnavailable(), DependencyUnavailableError),
            (RuntimeError("protected source"), DependencyUnavailableError),
        ):
            with self.subTest(failure=type(failure).__name__):
                retirement = _Retirement(retire_failure=failure)
                with self.assertRaises(expected) as caught:
                    self._adapter(
                        _Driver(),
                        _KnowledgeStore(),
                        retirement_service=retirement,
                    ).retire_document(principal, "unknown-or-foreign", request)
                self.assertNotIn("protected", caught.exception.public_message)
                self.assertNotIn("secret", caught.exception.public_message)

        unavailable = _Retirement(list_failure=DocumentRetirementUnavailable())
        with self.assertRaises(AuthorizationError):
            self._adapter(
                _Driver(),
                _KnowledgeStore(),
                retirement_service=unavailable,
            ).documents(principal, DocumentLifecycleListRequest())
        conflicted = _Retirement(list_failure=DocumentRetirementConflict())
        with self.assertRaises(ConflictError):
            self._adapter(
                _Driver(),
                _KnowledgeStore(),
                retirement_service=conflicted,
            ).documents(principal, DocumentLifecycleListRequest())
        no_scope = dataclasses.replace(
            principal,
            capabilities=frozenset({"knowledge:review"}),
        )
        with self.assertRaises(AuthorizationError):
            self._adapter(
                _Driver(),
                _KnowledgeStore(),
                retirement_service=_Retirement(),
            ).documents(no_scope, DocumentLifecycleListRequest())

    def test_published_quality_adapter_is_dedicated_bounded_and_metadata_only(
        self,
    ) -> None:
        quality = _Quality()
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:quality"}),
        )
        result = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            quality_service=quality,
        ).quality(principal)

        self.assertIsInstance(result.payload, PublishedGraphQualityResponse)
        self.assertTrue(result.payload.passed)
        self.assertEqual(result.payload.publication_id, "publication-1")
        self.assertEqual(result.payload.counts.relationship_assertions, 1)
        self.assertEqual(result.payload.issues[0].code, "ANOMALOUS_HUB")
        self.assertEqual(result.payload.review_sample[0].evidence_chunk_ids, ("chunk-1",))
        payload = result.payload.model_dump(mode="json")
        self.assertNotIn("tenant_id", payload)
        self.assertNotIn("source_text", str(payload))
        self.assertNotIn("quoted_text", str(payload))
        self.assertEqual(quality.calls, [principal])

        review_only = dataclasses.replace(
            principal,
            capabilities=frozenset({"knowledge:review"}),
        )
        with self.assertRaises(AuthorizationError):
            self._adapter(
                _Driver(),
                _KnowledgeStore(),
                quality_service=quality,
            ).quality(review_only)
        self.assertEqual(quality.calls, [principal])

    def test_published_quality_failures_use_public_runtime_taxonomy(self) -> None:
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:quality"}),
        )
        cases = (
            (PublishedGraphQualityAuthorizationError(), AuthorizationError),
            (PublishedGraphQualityConflict(), ConflictError),
            (PublishedGraphQualityLimitExceeded(), ConflictError),
            (TimeoutError("driver address and source text"), DependencyTimeoutError),
            (PublishedGraphQualityUnavailable(), DependencyUnavailableError),
            (RuntimeError("secret backend detail"), DependencyUnavailableError),
        )
        for failure, expected in cases:
            with self.subTest(failure=type(failure).__name__):
                adapter = self._adapter(
                    _Driver(),
                    _KnowledgeStore(),
                    quality_service=_Quality(failure=failure),
                )
                with self.assertRaises(expected):
                    adapter.quality(principal)

    def test_active_inventory_adapter_is_scoped_text_free_and_sanitized(self) -> None:
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:quality"}),
        )
        inventory = _Inventory()
        adapter = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            inventory_service=inventory,
        )
        result = adapter.inventory(
            principal,
            ActivePublicationInventoryRequest(document_id=None, limit=25),
        )
        self.assertEqual(result.payload.items[0].ontology_key, "SUPPLIED_BY")
        serialized = str(result.payload.model_dump(mode="json"))
        self.assertNotIn("tenant_id", serialized)
        self.assertNotIn("evidence_text", serialized)
        self.assertNotIn("source_text", serialized)
        self.assertEqual(inventory.calls, [(principal, None, 25)])

        cases = (
            (ActivePublicationInventoryAuthorizationError(), AuthorizationError),
            (ActivePublicationInventoryConflict(), ConflictError),
            (ActivePublicationInventoryLimitExceeded(), ConflictError),
            (ActivePublicationInventoryUnavailable(), DependencyUnavailableError),
            (TimeoutError("secret endpoint"), DependencyTimeoutError),
        )
        for failure, expected in cases:
            with self.subTest(failure=type(failure).__name__):
                failing = self._adapter(
                    _Driver(),
                    _KnowledgeStore(),
                    inventory_service=_Inventory(failure=failure),
                )
                with self.assertRaises(expected) as raised:
                    failing.inventory(
                        principal,
                        ActivePublicationInventoryRequest(),
                    )
                self.assertNotIn("secret", str(raised.exception))

        foreign = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            inventory_service=_Inventory(_inventory(tenant_id="tenant-other")),
        )
        with self.assertRaises(DependencyUnavailableError):
            foreign.inventory(principal, ActivePublicationInventoryRequest())

    def test_authoritative_import_hydrates_acl_from_authorized_chunk(self) -> None:
        driver = _Driver()
        store = _KnowledgeStore()
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:import"}),
        )
        result = self._adapter(driver, store).authoritative_import(
            principal,
            AuthoritativeImportRequest.model_validate(_authoritative_payload()),
        )
        self.assertEqual(result.payload.mention_count, 1)
        self.assertIsNotNone(store.batch)
        mention = store.batch.mentions[0]  # type: ignore[union-attr]
        self.assertEqual(mention.tenant_id, "tenant-alpha")
        self.assertEqual(mention.evidence.access_groups, frozenset({"engineers"}))
        self.assertEqual(mention.trust.reviewed_by, "expert-1")
        self.assertEqual(driver.calls[0][1]["tenant_id"], "tenant-alpha")
        self.assertEqual(driver.calls[0][1]["groups"], ["engineers"])

    def test_authoritative_literal_is_normalized_from_active_tbox(self) -> None:
        driver = _Driver()
        store = _KnowledgeStore()
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:import"}),
        )
        quote = "Pump-7 pressure was 100 psi at 2025-01-02T03:04:05Z"
        assertion = {
            "source_key": "expert-pressure-1",
            "subject_mention_source_key": "expert-pump-7",
            "predicate": "PRESSURE",
            "evidence": {
                "document_id": "document-1",
                "version_id": "version-1",
                "chunk_id": "chunk-1",
                "char_start": 10,
                "char_end": 10 + len(quote),
                "quoted_text": quote,
            },
            "literal": {
                "raw_literal": "100",
                "raw_unit": "psi",
                "raw_observed_at": "2025-01-02T03:04:05Z",
            },
        }
        result = self._adapter(driver, store).authoritative_import(
            principal,
            AuthoritativeImportRequest.model_validate(
                _authoritative_payload(assertions=[assertion])
            ),
        )
        self.assertEqual(result.payload.assertion_count, 1)
        literal = store.batch.assertions[0]  # type: ignore[union-attr]
        self.assertEqual(literal.literal_value, "100")
        self.assertIsInstance(literal.literal_semantics, TypedLiteralValue)
        assert literal.literal_semantics is not None
        self.assertEqual(literal.literal_semantics.raw_unit, "psi")
        self.assertEqual(literal.literal_semantics.canonical_unit, "kPa")
        self.assertEqual(
            literal.literal_semantics.canonical_value,
            "689.4757293168361336722673443",
        )
        self.assertEqual(
            literal.literal_semantics.raw_observed_at,
            "2025-01-02T03:04:05Z",
        )

    def test_authoritative_relationship_property_is_server_identified_and_scoped(
        self,
    ) -> None:
        text = "Pump-7 supplied by Acme at 40 percent."
        payload = {
            "ontology_version_id": RELATIONSHIP_TBOX.tbox_id,
            "mentions": [
                {
                    "source_key": "asset",
                    "entity": {
                        "entity_type": "Asset",
                        "canonical_key": "asset-id:P-7",
                        "canonical_name": "Pump-7",
                    },
                    "evidence": {
                        "document_id": "document-1",
                        "version_id": "version-1",
                        "chunk_id": "chunk-1",
                        "char_start": 0,
                        "char_end": 6,
                        "quoted_text": "Pump-7",
                    },
                },
                {
                    "source_key": "supplier",
                    "entity": {
                        "entity_type": "Organization",
                        "canonical_key": "org-id:ACME",
                        "canonical_name": "Acme",
                    },
                    "evidence": {
                        "document_id": "document-1",
                        "version_id": "version-1",
                        "chunk_id": "chunk-1",
                        "char_start": 19,
                        "char_end": 23,
                        "quoted_text": "Acme",
                    },
                },
            ],
            "assertions": [
                {
                    "source_key": "supply",
                    "subject_mention_source_key": "asset",
                    "object_mention_source_key": "supplier",
                    "predicate": "SUPPLIED_BY",
                    "evidence": {
                        "document_id": "document-1",
                        "version_id": "version-1",
                        "chunk_id": "chunk-1",
                        "char_start": 0,
                        "char_end": len(text),
                        "quoted_text": text,
                    },
                    "relationship_properties": [
                        {
                            "name": "SupplyShare",
                            "literal": {
                                "raw_literal": "40",
                                "raw_unit": "percent",
                            },
                            "evidence": {
                                "document_id": "document-1",
                                "version_id": "version-1",
                                "chunk_id": "chunk-1",
                                "char_start": 27,
                                "char_end": 37,
                                "quoted_text": "40 percent",
                            },
                            "confidence": 0.99,
                        }
                    ],
                }
            ],
        }
        driver = _Driver()
        store = _KnowledgeStore()
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:import"}),
        )
        self._adapter(
            driver,
            store,
            tboxes=_TBoxesWithValue(RELATIONSHIP_TBOX),
        ).authoritative_import(
            principal,
            AuthoritativeImportRequest.model_validate(payload),
        )
        relation = store.batch.assertions[0]  # type: ignore[union-attr]
        value = relation.relationship_properties[0]
        self.assertEqual(value.name, "SupplyShare")
        self.assertEqual(value.literal_semantics.raw_value, "40")
        self.assertEqual(value.literal_semantics.raw_unit, "percent")
        self.assertEqual(value.evidence_text, "40 percent")
        self.assertEqual(value.confidence, 0.99)

        payload["assertions"][0]["relationship_properties"][0]["evidence"][  # type: ignore[index]
            "chunk_id"
        ] = "chunk-other"
        with self.assertRaises(ResourceNotFoundError):
            self._adapter(
                _Driver(),
                _KnowledgeStore(),
                tboxes=_TBoxesWithValue(RELATIONSHIP_TBOX),
            ).authoritative_import(
                principal,
                AuthoritativeImportRequest.model_validate(payload),
            )

    def test_review_queue_and_edit_preserve_server_owned_literal_semantics(self) -> None:
        current = _candidate_literal()
        reviews = _Reviews(
            (ReviewQueueItem(ReviewRecordKind.ASSERTION, current),)
        )
        store = _KnowledgeStore(current)
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:review"}),
        )
        adapter = self._adapter(
            _Driver(),
            store,
            reviews=reviews,
        )
        queued = adapter.review_queue(principal, ReviewQueueRequest(limit=5))
        queued_semantics = queued.payload.items[0].literal_semantics
        self.assertIsNotNone(queued_semantics)
        assert queued_semantics is not None
        self.assertEqual(queued_semantics.raw_unit, "psi")
        self.assertEqual(queued_semantics.canonical_unit, "kPa")

        request = ReviewBatchRequest.model_validate(
            {
                "decisions": [
                    {
                        "record_kind": "ASSERTION",
                        "record_id": current.record_id,
                        "expected_revision": 1,
                        "decision": "APPROVED",
                        "notes": "Expert corrected the source-backed value",
                        "assertion_edit": {
                            "subject": {
                                "entity_type": "Asset",
                                "canonical_key": "asset-id:P-7",
                                "canonical_name": "Pump-7",
                            },
                            "predicate": "PRESSURE",
                            "subject_mention_revision_id": (
                                current.subject_mention_revision_id
                            ),
                            "confidence": 0.99,
                            "literal": {
                                "raw_literal": "90",
                                "raw_unit": "psi",
                                "raw_observed_at": "2025-01-02T03:04:05Z",
                            },
                        },
                    }
                ]
            }
        )
        adapter.review_batch(principal, request)
        assert reviews.batch_call is not None
        edit = reviews.batch_call[1][0].edit
        self.assertEqual(edit.literal_value, "90")
        self.assertIsNotNone(edit.literal_semantics)
        self.assertEqual(edit.literal_semantics.raw_unit, "psi")
        self.assertEqual(edit.literal_semantics.canonical_unit, "kPa")
        self.assertEqual(
            edit.literal_semantics.canonical_value,
            "620.5281563851525203050406099",
        )
        self.assertEqual(store.get_call[1], current.record_id)

    def test_review_literal_edit_rejects_bad_version_unit_and_time(self) -> None:
        current = _candidate_literal()
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:review"}),
        )

        def request(**literal_changes: object) -> ReviewBatchRequest:
            literal = {
                "raw_literal": "90",
                "raw_unit": "psi",
                **literal_changes,
            }
            return ReviewBatchRequest.model_validate(
                {
                    "decisions": [
                        {
                            "record_kind": "ASSERTION",
                            "record_id": current.record_id,
                            "expected_revision": 1,
                            "decision": "APPROVED",
                            "notes": "Validate raw edit",
                            "assertion_edit": {
                                "subject": {
                                    "entity_type": "Asset",
                                    "canonical_key": "asset-id:P-7",
                                    "canonical_name": "Pump-7",
                                },
                                "predicate": "PRESSURE",
                                "subject_mention_revision_id": (
                                    current.subject_mention_revision_id
                                ),
                                "confidence": 0.99,
                                "literal": literal,
                            },
                        }
                    ]
                }
            )

        for literal_changes in (
            {"raw_unit": "second"},
            {
                "raw_valid_from": "2026-01-02T00:00:00Z",
                "raw_valid_to": "2025-01-02T00:00:00Z",
            },
        ):
            with self.subTest(literal_changes=literal_changes):
                adapter = self._adapter(
                    _Driver(),
                    _KnowledgeStore(current),
                    reviews=_Reviews(),
                )
                with self.assertRaises(RequestValidationError):
                    adapter.review_batch(principal, request(**literal_changes))

        inactive = self._adapter(
            _Driver(),
            _KnowledgeStore(current),
            reviews=_Reviews(),
            tboxes=_TBoxes(active=False),
        )
        with self.assertRaises(ConflictError):
            inactive.review_batch(principal, request())

    def test_missing_and_cross_tenant_evidence_have_one_public_error(self) -> None:
        store = _KnowledgeStore()
        request = AuthoritativeImportRequest.model_validate(_authoritative_payload())
        errors: list[tuple[type[Exception], str]] = []
        for tenant in ("tenant-alpha", "tenant-other"):
            principal = Principal(
                "expert-1",
                tenant,
                frozenset({"engineers"}),
                frozenset({"knowledge:import"}),
            )
            with self.assertRaises(ResourceNotFoundError) as caught:
                self._adapter(_Driver(allowed=False), store).authoritative_import(
                    principal, request
                )
            errors.append((type(caught.exception), caught.exception.public_message))
        self.assertEqual(errors[0], errors[1])

    def test_authoritative_import_rejects_historical_chunk_evidence(self) -> None:
        driver = _Driver(active=False)
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:import"}),
        )
        with self.assertRaises(ResourceNotFoundError):
            self._adapter(driver, _KnowledgeStore()).authoritative_import(
                principal,
                AuthoritativeImportRequest.model_validate(_authoritative_payload()),
            )
        query = driver.calls[0][0]
        self.assertIn("ACTIVE_VERSION", query)
        self.assertIn("ACTIVE_SNAPSHOT", query)
        self.assertIn("INCLUDES_CHUNK", query)
        self.assertIn("OF_VERSION", query)
        self.assertIn("build_state: 'PUBLISHED'", query)

    def test_evidence_dependency_failures_are_redacted_and_classified(self) -> None:
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:import"}),
        )
        request = AuthoritativeImportRequest.model_validate(_authoritative_payload())
        for failure, expected in (
            (TimeoutError("driver timed out with secret detail"), DependencyTimeoutError),
            (RuntimeError("driver failed with secret detail"), DependencyUnavailableError),
        ):
            with self.subTest(expected=expected.__name__):
                with self.assertRaises(expected) as caught:
                    self._adapter(
                        _Driver(failure=failure), _KnowledgeStore()
                    ).authoritative_import(principal, request)
                self.assertNotIn("secret detail", caught.exception.public_message)

    def test_tbox_adapter_injects_tenant_and_keeps_publish_cas(self) -> None:
        tboxes = _TBoxes()
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"ontology:read", "ontology:write", "ontology:publish"}),
        )
        adapter = self._adapter(_Driver(), _KnowledgeStore(), tboxes=tboxes)
        listed = adapter.ontology_list(
            principal, OntologyListRequest(key="industrial-assets", limit=1)
        )
        imported = adapter.ontology_import(
            principal,
            OntologyImportRequest(
                key="industrial-assets",
                version=1,
                entity_types=(
                    {
                        "name": "Asset",
                        "canonical_key_namespaces": ("asset-id",),
                    },
                ),
            ),
        )
        published = adapter.ontology_publish(
            principal,
            tboxes.value.tbox_id,
            OntologyPublishRequest(expected_active_tbox_id=None),
        )
        self.assertEqual(len(listed.payload.items), 1)
        self.assertEqual(imported.payload.status, "DRAFT")
        assert tboxes.imported is not None
        self.assertEqual(tboxes.imported[0].tenant_id, "tenant-alpha")
        self.assertEqual(published.payload.status, "PUBLISHED")
        self.assertEqual(
            tboxes.publish_call,
            ("tenant-alpha", tboxes.value.tbox_id, None),
        )

    def test_tbox_semantic_validation_error_is_a_client_error(self) -> None:
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"ontology:write"}),
        )
        adapter = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            tboxes=_TBoxes(
                import_failure=TBoxValidationError("unrecognized Pint unit")
            ),
        )
        request = OntologyImportRequest(
            key="industrial-assets",
            version=1,
            entity_types=(
                {
                    "name": "Asset",
                    "canonical_key_namespaces": ("asset-id",),
                    "properties": (
                        {
                            "name": "pressure",
                            "datatype": "DECIMAL",
                            "required": False,
                            "cardinality": "ZERO_OR_ONE",
                            "unit": "not_a_real_unit_xyz",
                        },
                    ),
                },
            ),
        )

        with self.assertRaises(RequestValidationError):
            adapter.ontology_import(principal, request)

    def test_construct_review_and_publication_adapters_preserve_capabilities(self) -> None:
        construction = _Construction()
        reviews = _Reviews()
        publications = _Publications()
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset(
                {"knowledge:construct", "knowledge:review", "knowledge:publish"}
            ),
        )
        adapter = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            construction=construction,
            reviews=reviews,
            publications=publications,
        )
        constructed = adapter.construct(
            principal,
            KnowledgeConstructionRequest.model_validate(_construct_payload()),
        )
        queued = adapter.review_queue(principal, ReviewQueueRequest(limit=5))
        reviewed = adapter.review_batch(
            principal,
            ReviewBatchRequest(
                decisions=(
                    {
                        "record_kind": "ENTITY_MENTION",
                        "record_id": "mention-1",
                        "expected_revision": 1,
                        "decision": "REJECTED",
                        "notes": "Wrong identity",
                    },
                )
            ),
        )
        published = adapter.publish(
            principal,
            PublicationRequest(approved_revision_ids=("revision-2",)),
        )
        rolled_back = adapter.rollback(
            principal,
            "publication-1",
            RollbackRequest(expected_active_publication_id="publication-2"),
        )
        history = adapter.history(principal, PublicationHistoryRequest(limit=5))
        self.assertEqual(constructed.payload.job_id, "job-1")
        assert construction.call is not None
        self.assertEqual(construction.call[0], principal)
        self.assertEqual(construction.call[1], b"Acme owns Pump-7.")
        self.assertEqual(
            construction.call[2].access_groups,
            frozenset({"engineers"}),
        )
        self.assertEqual(queued.payload.items, ())
        self.assertEqual(reviewed.payload.outcomes[0].status, "REJECTED")
        assert reviews.batch_call is not None
        self.assertEqual(reviews.batch_call[1][0].reviewed_at, NOW)
        self.assertEqual(published.payload.publication_id, "publication-1")
        self.assertEqual(published.payload.ontology_version_id, "tbox-1")
        self.assertEqual(rolled_back.payload.publication_id, "publication-1")
        self.assertEqual(rolled_back.payload.ontology_version_id, "tbox-1")
        self.assertEqual(len(history.payload.items), 1)
        self.assertEqual(history.payload.items[0].ontology_version_id, "tbox-1")
        rollback = next(value for name, value in publications.calls if name == "rollback")
        self.assertEqual(rollback[0], principal)
        self.assertEqual(rollback[1], "publication-1")
        self.assertEqual(
            rollback[2]["expected_active_publication_id"],
            "publication-2",
        )

    def test_recovery_adapters_project_jobs_revisions_and_publishable_candidates(self) -> None:
        record = dataclasses.replace(
            _candidate_literal(),
            trust=dataclasses.replace(
                _candidate_literal().trust,
                status=GovernanceStatus.APPROVED,
                reviewed_by="expert-1",
                reviewed_at=NOW,
                review_notes="Verified exact source evidence.",
            ),
        )
        item = ReviewQueueItem(ReviewRecordKind.ASSERTION, record)
        audit = _ConstructionAudit()
        reviews = _Reviews((item,))
        publications = _Publications((PublicationCandidate(item, True),))
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset(
                {"knowledge:construct", "knowledge:review", "knowledge:publish"}
            ),
        )
        adapter = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            reviews=reviews,
            publications=publications,
            construction_audit=audit,
        )

        detail = adapter.construction_job(principal, "job-1")
        jobs = adapter.construction_jobs(
            principal,
            ConstructionJobListRequest(statuses=("COMPLETED",), limit=5),
        )
        revisions = adapter.revision_history(
            principal,
            record.record_id,
            RecordRevisionHistoryRequest(limit=5),
        )
        candidates = adapter.publication_candidates(
            principal,
            PublicationCandidatesRequest(limit=5),
        )

        self.assertEqual(detail.payload.status, "COMPLETED")
        self.assertEqual(jobs.payload.items[0].job_id, "job-1")
        self.assertEqual(revisions.payload.record_id, record.record_id)
        self.assertEqual(revisions.payload.items[0].revision_id, record.revision_id)
        self.assertTrue(candidates.payload.items[0].requires_replacement)
        self.assertEqual(
            candidates.payload.items[0].record.revision_id,
            record.revision_id,
        )
        self.assertEqual(audit.calls[1][1][1]["statuses"], ("COMPLETED",))
        self.assertEqual(reviews.history_call[1], record.record_id)

        with self.assertRaises(ResourceNotFoundError):
            adapter.construction_job(principal, "missing-job")

    def test_construct_returns_empty_for_a_valid_no_fact_extraction(self) -> None:
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:construct"}),
        )
        result = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            construction=_Construction(status="EMPTY"),
        ).construct(
            principal,
            KnowledgeConstructionRequest.model_validate(_construct_payload()),
        )
        self.assertEqual(result.payload.chunks[0].status, "EMPTY")
        self.assertEqual(result.payload.chunks[0].mention_record_ids, ())
        self.assertEqual(result.payload.chunks[0].assertion_record_ids, ())

    def test_malformed_dependency_output_is_not_reported_as_client_input(self) -> None:
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:construct"}),
        )
        adapter = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            construction=_MalformedConstruction(),
        )
        with self.assertRaises(DependencyUnavailableError):
            adapter.construct(
                principal,
                KnowledgeConstructionRequest.model_validate(_construct_payload()),
            )

    def test_construction_failures_use_runtime_error_taxonomy(self) -> None:
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"engineers"}),
            frozenset({"knowledge:construct"}),
        )
        request = KnowledgeConstructionRequest.model_validate(_construct_payload())
        cases = (
            (TimeoutError("provider timeout detail"), DependencyTimeoutError),
            (RuntimeError("provider failure detail"), DependencyUnavailableError),
            (ValueError("malformed provider output"), DependencyUnavailableError),
            (DocumentParseError("invalid uploaded JSON"), RequestValidationError),
            (ConstructionBudgetExceeded("too many calls"), RequestValidationError),
            (ConflictError(), ConflictError),
        )
        for failure, expected in cases:
            with self.subTest(failure=type(failure).__name__):
                adapter = self._adapter(
                    _Driver(),
                    _KnowledgeStore(),
                    construction=_FailingConstruction(failure),
                )
                with self.assertRaises(expected):
                    adapter.construct(principal, request)

    def test_construct_acl_must_be_a_nonempty_principal_group_subset(self) -> None:
        construction = _Construction()
        adapter = self._adapter(
            _Driver(),
            _KnowledgeStore(),
            construction=construction,
        )
        principal = Principal(
            "expert-1",
            "tenant-alpha",
            frozenset({"board", "public"}),
            frozenset({"knowledge:construct"}),
        )
        request = KnowledgeConstructionRequest.model_validate(
            _construct_payload(access_groups=["board"])
        )
        adapter.construct(principal, request)
        assert construction.call is not None
        self.assertEqual(
            construction.call[2].access_groups,
            frozenset({"board"}),
        )

        construction.call = None
        unauthorized = KnowledgeConstructionRequest.model_validate(
            _construct_payload(access_groups=["operators"])
        )
        with self.assertRaises(AuthorizationError):
            adapter.construct(principal, unauthorized)
        self.assertIsNone(construction.call)

    def test_rollback_unknown_and_cross_tenant_targets_are_indistinguishable(self) -> None:
        publications = _Publications()
        adapter = self._adapter(
            _Driver(), _KnowledgeStore(), publications=publications
        )
        errors: list[tuple[type[Exception], str]] = []
        for tenant in ("tenant-alpha", "tenant-other"):
            principal = Principal(
                "expert-1",
                tenant,
                frozenset({"engineers"}),
                frozenset({"knowledge:publish"}),
            )
            with self.assertRaises(ResourceNotFoundError) as caught:
                adapter.rollback(
                    principal,
                    "unknown-publication",
                    RollbackRequest(
                        expected_active_publication_id="active-publication"
                    ),
                )
            errors.append((type(caught.exception), caught.exception.public_message))
        self.assertEqual(errors[0], errors[1])


if __name__ == "__main__":
    unittest.main()
