"""Application adapter for governed property-graph knowledge services."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable

from graphrag_prod.construction import (
    ConstructionMetadata,
    DocumentParseError,
    ExtractionRejected,
    LiteralNormalizationError,
    TBoxLiteralNormalizer,
)
from graphrag_prod.construction.provider_errors import MODEL_CALL_TIMEOUT
from graphrag_prod.construction.workflow import (
    ConstructionAuthorizationError,
    ConstructionBudgetExceeded,
    ConstructionConflict,
    Neo4jConstructionAuditStore,
)
from graphrag_prod.domain import Principal, RelationshipPropertyValue, TypedLiteralValue
from graphrag_prod.domain.ids import (
    entity_id as make_entity_id,
    relationship_property_value_id,
)
from graphrag_prod.graph.published_quality import (
    Neo4jPublishedGraphQualityService,
    PublishedGraphQualityAuthorizationError,
    PublishedGraphQualityConflict,
    PublishedGraphQualityLimitExceeded,
    PublishedGraphQualityReport,
    PublishedGraphQualityUnavailable,
)
from graphrag_prod.graph.published_quality_history import (
    Neo4jPublishedGraphQualityHistoryService,
    PublishedGraphQualityHistoryConflict,
    PublishedGraphQualityHistoryUnavailable,
)
from graphrag_prod.graph.published_inventory import (
    ActivePublicationInventoryAuthorizationError,
    ActivePublicationInventoryConflict,
    ActivePublicationInventoryLimitExceeded,
    ActivePublicationInventoryUnavailable,
    Neo4jActivePublicationInventoryService,
)
from graphrag_prod.ingestion import IngestionConflict
from graphrag_prod.ingestion.retirement import (
    DocumentRetirementBackendUnavailable,
    DocumentRetirementBlocked,
    DocumentRetirementConflict,
    DocumentRetirementRequest as DomainDocumentRetirementRequest,
    DocumentRetirementUnavailable,
    Neo4jDocumentRetirementService,
)
from graphrag_prod.knowledge import (
    ABoxRecordBatch,
    AssertionRecord,
    EntityIdentity,
    EntityMentionRecord,
    EvidenceReference,
    EntityResolutionService,
    IdentityPropertyValue,
    KnowledgeEvidenceError,
    KnowledgeSchemaError,
    Neo4jKnowledgeStore,
    Neo4jAuthoritativeEntitySource,
    RecordRevision,
    authoritative_import_trust,
    knowledge_record_id,
)
from graphrag_prod.knowledge.entity_resolution import (
    ResolutionBoundaryError,
    ResolutionOutcome,
    ResolutionSuggestion,
)
from graphrag_prod.knowledge.review import (
    AssertionEdit,
    KnowledgeAuthorizationError,
    KnowledgePublicationConflict,
    KnowledgeReviewUnavailable,
    MentionEdit,
    Neo4jKnowledgePublicationService,
    Neo4jKnowledgeReviewService,
    ReviewRecordKind,
    ReviewRequest,
)
from graphrag_prod.knowledge.store import KnowledgeConflict, KnowledgeStoreError
from graphrag_prod.knowledge.trust import GovernanceStatus
from graphrag_prod.ontology import (
    Neo4jTBoxStore,
    PropertyDefinition,
    TBoxStatus,
    TBoxVersion,
)
from graphrag_prod.ontology.store import TBoxConflict, TBoxValidationError

from .knowledge_contracts import (
    ActivePublicationInventoryRequest,
    ActivePublicationInventoryResponse,
    AuthoritativeImportRequest,
    AuthoritativeImportResponse,
    ConstructionJobListRequest,
    ConstructionJobListResponse,
    ConstructionJobResponse,
    DocumentLifecycleListRequest,
    DocumentLifecycleListResponse,
    DocumentRetirementRequest,
    DocumentRetirementResponse,
    EntityResolutionApplyRequest,
    EntityResolutionApplyResponse,
    EntityResolutionRequest,
    EntityResolutionResponse,
    EvidenceInput,
    KnowledgeConstructionRequest,
    KnowledgeConstructionResponse,
    KnowledgeEntityInput,
    OntologyImportRequest,
    OntologyListRequest,
    OntologyListResponse,
    OntologyPublishRequest,
    OntologyVersionResponse,
    PublicationHistoryRequest,
    PublicationHistoryResponse,
    PublicationCandidatesRequest,
    PublicationCandidatesResponse,
    PublicationRequest,
    PublicationResponse,
    PublishedGraphQualityResponse,
    RawLiteralInput,
    RelationshipPropertyInput,
    ReviewBatchRequest,
    ReviewBatchResponse,
    ReviewQueueRequest,
    ReviewQueueResponse,
    RecordRevisionHistoryRequest,
    RecordRevisionHistoryResponse,
    RollbackRequest,
)
from .runtime import (
    ApiRuntimeError,
    AuthorizationError,
    BackendResult,
    ConflictError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    RequestValidationError,
    ResourceNotFoundError,
)
from .quality_history_contracts import (
    PublishedGraphQualityRunListRequest,
    PublishedGraphQualityRunListResponse,
    PublishedGraphQualityRunResponse,
)


_EVIDENCE_QUERY = """
MATCH (document:Document {
    tenant_id: $tenant_id,
    document_id: $document_id
})-[:HAS_VERSION]->(version:DocumentVersion {
    tenant_id: $tenant_id,
    version_id: $version_id
})-[:HAS_CHUNK]->(chunk:Chunk {
    tenant_id: $tenant_id,
    chunk_id: $chunk_id
})
MATCH (document)-[:ACTIVE_VERSION]->(version)
MATCH (document)-[:ACTIVE_SNAPSHOT]->(snapshot:KnowledgeSnapshot {
    tenant_id: $tenant_id,
    build_state: 'PUBLISHED'
})-[:OF_VERSION]->(version)
MATCH (snapshot)-[:INCLUDES_CHUNK]->(chunk)
WHERE version.document_id = document.document_id
  AND chunk.document_id = document.document_id
  AND chunk.version_id = version.version_id
  AND any(group IN $groups WHERE group IN document.access_groups)
  AND any(group IN $groups WHERE group IN chunk.access_groups)
  AND $char_start >= chunk.char_start
  AND $char_end <= chunk.char_end
  AND substring(
      chunk.text,
      $char_start - chunk.char_start,
      $char_end - $char_start
  ) = $quoted_text
RETURN chunk.access_policy_id AS access_policy_id,
       chunk.access_policy_version AS access_policy_version,
       chunk.access_groups AS access_groups
LIMIT 2
"""


def _require_capability(principal: Principal, capability: str) -> None:
    if capability not in principal.capabilities:
        raise AuthorizationError()


def _outbound(model: type[Any], payload: object) -> Any:
    """Validate adapter output as dependency data, never as client input."""

    try:
        return model.model_validate(payload, from_attributes=True)
    except (TypeError, ValueError) as error:
        raise DependencyUnavailableError() from error


def _entity(principal: Principal, value: KnowledgeEntityInput) -> EntityIdentity:
    entity_id = make_entity_id(
        principal.tenant_id,
        value.entity_type,
        value.canonical_key,
    )
    return EntityIdentity(
        entity_id=entity_id,
        tenant_id=principal.tenant_id,
        entity_type=value.entity_type,
        canonical_key=value.canonical_key,
        canonical_name=value.canonical_name,
        aliases=value.aliases,
    )


def _tbox_payload(value: TBoxVersion) -> dict[str, object]:
    payload = value.to_mapping()
    payload.pop("tenant_id", None)
    payload.update(tbox_id=value.tbox_id, checksum=value.checksum)
    return payload


def _entity_payload(value: EntityIdentity) -> dict[str, object]:
    return {
        "entity_id": value.entity_id,
        "entity_type": value.entity_type,
        "canonical_key": value.canonical_key,
        "canonical_name": value.canonical_name,
        "aliases": value.aliases,
    }


def _evidence_payload(value: EvidenceReference) -> dict[str, object]:
    return {
        "document_id": value.document_id,
        "version_id": value.version_id,
        "chunk_id": value.chunk_id,
        "char_start": value.char_start,
        "char_end": value.char_end,
        "quoted_text": value.quoted_text,
    }


def _trust_payload(value: Any) -> dict[str, object]:
    return {
        "origin": value.origin.value,
        "authority": value.authority.value,
        "status": value.status.value,
        "ontology_version_id": value.ontology_version_id,
        "created_at": value.created_at,
        "extractor_version": value.extractor_version,
        "prompt_version": value.prompt_version,
        "reviewed_by": value.reviewed_by,
        "reviewed_at": value.reviewed_at,
        "review_notes": value.review_notes,
    }


def _literal_semantics_payload(value: TypedLiteralValue) -> dict[str, object]:
    return {
        "datatype": value.datatype,
        "typed_value": value.typed_value,
        "raw_value": value.raw_value,
        "raw_unit": value.raw_unit,
        "canonical_value": value.canonical_value,
        "canonical_unit": value.canonical_unit,
        "valid_from": value.valid_from,
        "valid_to": value.valid_to,
        "observed_at": value.observed_at,
        "raw_valid_from": value.raw_valid_from,
        "raw_valid_to": value.raw_valid_to,
        "raw_observed_at": value.raw_observed_at,
    }


def _relationship_property_payload(
    value: RelationshipPropertyValue,
    parent: EvidenceReference,
) -> dict[str, object]:
    return {
        "property_value_id": value.property_value_id,
        "name": value.name,
        "literal_semantics": _literal_semantics_payload(value.literal_semantics),
        "evidence": {
            "document_id": parent.document_id,
            "version_id": parent.version_id,
            "chunk_id": value.evidence_chunk_id,
            "char_start": value.evidence_char_start,
            "char_end": value.evidence_char_end,
            "quoted_text": value.evidence_text,
        },
        "confidence": value.confidence,
    }


def _declared_entity_property(
    tbox: TBoxVersion,
    entity_type: str,
    predicate: str,
) -> PropertyDefinition:
    for definition in tbox.entity_types:
        if definition.name != entity_type:
            continue
        for property_definition in definition.properties:
            if property_definition.name == predicate:
                return property_definition
        break
    raise RequestValidationError()


def _declared_relationship_property(
    tbox: TBoxVersion,
    relationship_type: str,
    property_name: str,
) -> PropertyDefinition:
    for definition in tbox.relationship_types:
        if definition.name != relationship_type:
            continue
        for property_definition in definition.properties:
            if property_definition.name == property_name:
                return property_definition
        break
    raise RequestValidationError()


def _review_record_payload(item: Any) -> dict[str, object]:
    record = item.record
    common: dict[str, object] = {
        "record_kind": item.record_kind.value,
        "record_id": record.record_id,
        "revision_id": record.revision_id,
        "revision": record.revision.revision,
        "confidence": record.confidence,
        "evidence": _evidence_payload(record.evidence),
        "trust": _trust_payload(record.trust),
    }
    if isinstance(record, EntityMentionRecord):
        common["entity"] = _entity_payload(record.entity)
    elif isinstance(record, AssertionRecord):
        common.update(
            subject=_entity_payload(record.subject),
            predicate=record.predicate,
            subject_mention_revision_id=record.subject_mention_revision_id,
            object_entity=(
                None
                if record.object_entity is None
                else _entity_payload(record.object_entity)
            ),
            object_mention_revision_id=record.object_mention_revision_id,
            literal_value=record.literal_value,
            literal_semantics=(
                None
                if record.literal_semantics is None
                else _literal_semantics_payload(record.literal_semantics)
            ),
            relationship_properties=tuple(
                _relationship_property_payload(value, record.evidence)
                for value in record.relationship_properties
            ),
        )
    else:
        raise DependencyUnavailableError()
    return common


def _publication_payload(value: Any) -> dict[str, object]:
    return {
        "publication_id": value.publication_id,
        "ontology_version_id": value.ontology_version_id,
        "generation": value.generation,
        "manifest_hash": value.manifest_hash,
        "source_revision_ids": value.source_revision_ids,
        "published_revision_ids": value.published_revision_ids,
        "removed_record_ids": value.removed_record_ids,
        "replaced_record_ids": value.replaced_record_ids,
        "status": value.status,
        "created_by": value.created_by,
        "created_at": value.created_at,
        "activated_at": value.activated_at,
        "rolled_back_by": value.rolled_back_by,
        "rolled_back_at": value.rolled_back_at,
    }


def _construction_chunk_payload(value: Any) -> dict[str, object]:
    return {
        "chunk_id": value.chunk_id,
        "artifact_id": value.artifact_id,
        "status": value.status,
        "finding_codes": value.finding_codes,
        "mention_record_ids": value.mention_record_ids,
        "assertion_record_ids": value.assertion_record_ids,
        "replayed": value.replayed,
    }


def _construction_job_payload(value: Any) -> dict[str, object]:
    return {
        "job_id": value.job_id,
        "document_id": value.document_id,
        "version_id": value.version_id,
        "snapshot_id": value.snapshot_id,
        "tbox_id": value.tbox_id,
        "status": value.status,
        "expected_chunks": value.expected_chunks,
        "completed_chunks": value.completed_chunks,
        "failed_chunk_id": value.failed_chunk_id,
        "last_finding_codes": value.last_finding_codes,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
        "completed_at": value.completed_at,
        "chunks": tuple(_construction_chunk_payload(item) for item in value.chunks),
    }


def _resolution_evidence_payload(value: Any) -> dict[str, object]:
    return {
        "match_kind": value.match_kind,
        "candidate_value": value.candidate_value,
        "target_value": value.target_value,
        "matcher_version": value.matcher_version,
        "authoritative_evidence": tuple(
            {
                "mention_revision_id": item.mention_revision_id,
                "document_id": item.document_id,
                "version_id": item.version_id,
                "chunk_id": item.chunk_id,
                "char_start": item.char_start,
                "char_end": item.char_end,
                "quoted_text": item.quoted_text,
            }
            for item in value.authoritative_evidence
        ),
    }


def _resolution_suggestion_payload(value: ResolutionSuggestion) -> dict[str, object]:
    return {
        "target": None if value.target is None else _entity_payload(value.target),
        "ontology_version_id": value.ontology_version_id,
        "rule_version": value.rule_version,
        "matcher_version": value.matcher_version,
        "evidence": tuple(
            _resolution_evidence_payload(item) for item in value.evidence
        ),
        "confidence": value.confidence,
        "outcome": value.outcome.value,
        "reason": value.reason,
    }


def _published_quality_payload(
    value: PublishedGraphQualityReport,
) -> dict[str, object]:
    """Project only bounded metadata; source/evidence text is never an API field."""

    return {
        "run_id": value.run_id,
        "ruleset_version": value.ruleset_version,
        "publication_id": value.publication_id,
        "publication_generation": value.publication_generation,
        "manifest_hash": value.manifest_hash,
        "ontology_version_id": value.ontology_version_id,
        "tbox_checksum": value.tbox_checksum,
        "corpus_revision": value.corpus_revision,
        "graph_digest": value.graph_digest,
        "counts": dict(value.counts),
        "total_issue_count": value.total_issue_count,
        "total_error_count": value.total_error_count,
        "issues_truncated": value.issues_truncated,
        "issues": tuple(
            {
                "issue_id": item.issue_id,
                "code": item.code,
                "severity": item.severity.value,
                "object_kind": item.object_kind,
                "object_id": item.object_id,
                "detail": item.detail,
            }
            for item in value.issues
        ),
        "review_sample": tuple(
            {
                "object_kind": item.object_kind,
                "object_id": item.object_id,
                "issue_codes": item.issue_codes,
                "evidence_chunk_ids": item.evidence_chunk_ids,
            }
            for item in value.review_sample
        ),
        "passed": value.passed,
    }


def _inventory_entity_payload(value: Any) -> dict[str, object]:
    return {
        "entity_id": value.entity_id,
        "entity_type": value.entity_type,
        "canonical_key": value.canonical_key,
        "display_name": value.display_name,
    }


def _inventory_literal_payload(value: Any) -> dict[str, object]:
    return {
        "value": value.value,
        "datatype": value.datatype,
        "typed_value": value.typed_value,
        "canonical_value": value.canonical_value,
        "canonical_unit": value.canonical_unit,
        "valid_from": value.valid_from,
        "valid_to": value.valid_to,
        "observed_at": value.observed_at,
    }


def _inventory_item_payload(value: Any) -> dict[str, object]:
    evidence = {
        "document_id": value.document_id,
        "version_id": value.version_id,
        "chunk_id": value.chunk_id,
        "ordinal": value.evidence_chunk_ordinal,
        "char_start": value.evidence_char_start,
        "char_end": value.evidence_char_end,
    }
    assertion = None
    if value.assertion is not None:
        assertion = {
            "subject": _inventory_entity_payload(value.assertion.subject),
            "predicate": value.assertion.predicate,
            "object_kind": value.assertion.object_kind,
            "object_entity": (
                None
                if value.assertion.object_entity is None
                else _inventory_entity_payload(value.assertion.object_entity)
            ),
            "literal": (
                None
                if value.assertion.literal is None
                else _inventory_literal_payload(value.assertion.literal)
            ),
            "relationship_properties": tuple(
                {
                    "property_value_id": item.property_value_id,
                    "name": item.name,
                    "confidence": item.confidence,
                    "literal": _inventory_literal_payload(item.literal),
                    "evidence": {
                        "document_id": value.document_id,
                        "version_id": value.version_id,
                        "chunk_id": item.evidence_chunk_id,
                        "ordinal": item.evidence_chunk_ordinal,
                        "char_start": item.evidence_char_start,
                        "char_end": item.evidence_char_end,
                    },
                }
                for item in value.assertion.relationship_properties
            ),
        }
    return {
        "record_id": value.record_id,
        "revision_id": value.revision_id,
        "record_kind": value.record_kind,
        "governance_status": value.governance_status,
        "origin": value.origin,
        "authority_level": value.authority_level,
        "confidence": value.confidence,
        "ontology_key": value.ontology_key,
        "evidence": evidence,
        "entity": (
            None if value.entity is None else _inventory_entity_payload(value.entity)
        ),
        "assertion": assertion,
    }


class Neo4jKnowledgeOperations:
    """Real adapter over T-Box, A-Box, construction, review, and publication."""

    def __init__(
        self,
        *,
        driver: Any,
        construction: Any,
        database: str = "neo4j",
        tboxes: Any | None = None,
        knowledge: Any | None = None,
        reviews: Any | None = None,
        publications: Any | None = None,
        construction_audit: Any | None = None,
        resolution_source: Any | None = None,
        quality_service: Any | None = None,
        quality_history_service: Any | None = None,
        inventory_service: Any | None = None,
        retirement_service: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(construction, "run", None)):
            raise TypeError("construction must implement run")
        if quality_service is not None and not callable(
            getattr(quality_service, "audit", None)
        ):
            raise TypeError("quality_service must implement audit")
        if quality_history_service is not None and any(
            not callable(getattr(quality_history_service, method, None))
            for method in ("audit_and_record", "get_run", "list_runs")
        ):
            raise TypeError("quality_history_service must implement audit_and_record, get_run and list_runs")
        if inventory_service is not None and not callable(
            getattr(inventory_service, "list_active", None)
        ):
            raise TypeError("inventory_service must implement list_active")
        if retirement_service is not None and any(
            not callable(getattr(retirement_service, method, None))
            for method in ("list_active_documents", "retire")
        ):
            raise TypeError(
                "retirement_service must implement list_active_documents and retire"
            )
        self.driver = driver
        self.database = database
        self.construction = construction
        workflow_audit = getattr(construction, "audit_store", None)
        self.construction_audit = (
            construction_audit
            or workflow_audit
            or Neo4jConstructionAuditStore(driver, database)
        )
        self.tboxes = tboxes or Neo4jTBoxStore(driver, database)
        self.knowledge = knowledge or Neo4jKnowledgeStore(driver, database)
        self.reviews = reviews or Neo4jKnowledgeReviewService(driver, database)
        self.publications = publications or Neo4jKnowledgePublicationService(
            driver, database
        )
        self.resolution_source = resolution_source or Neo4jAuthoritativeEntitySource(
            driver, database
        )
        self.quality_service = quality_service or Neo4jPublishedGraphQualityService(
            driver, database
        )
        self.quality_history_service = quality_history_service or Neo4jPublishedGraphQualityHistoryService(
            driver,
            database,
            auditor=self.quality_service,
            clock=clock,
            report_validator=lambda report: PublishedGraphQualityResponse.model_validate(
                _published_quality_payload(report)
            ),
        )
        self.inventory_service = inventory_service or Neo4jActivePublicationInventoryService(
            driver,
            database,
            quality_service=self.quality_service,
        )
        self.retirement_service = retirement_service or Neo4jDocumentRetirementService(
            driver, database
        )
        self.literal_normalizer = TBoxLiteralNormalizer()
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        try:
            value = self.clock()
            if (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() is None
            ):
                raise ValueError("clock must return a timezone-aware datetime")
            return value
        except ApiRuntimeError:
            raise
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error

    def _active_tbox(self, principal: Principal, tbox_id: str) -> TBoxVersion:
        """Resolve one exact, tenant-owned, published active T-Box version."""

        try:
            requested = self.tboxes.get(principal.tenant_id, tbox_id)
            active = self.tboxes.active(principal.tenant_id, requested.key)
        except ApiRuntimeError:
            raise
        except KeyError as error:
            raise ResourceNotFoundError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except TBoxConflict as error:
            raise ConflictError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        if (
            requested.tbox_id != tbox_id
            or requested.tenant_id != principal.tenant_id
            or requested.status is not TBoxStatus.PUBLISHED
            or active is None
            or active.tbox_id != requested.tbox_id
            or active.tenant_id != principal.tenant_id
            or active.status is not TBoxStatus.PUBLISHED
        ):
            raise ConflictError()
        return requested

    def _normalize_literal(
        self,
        tbox: TBoxVersion,
        *,
        entity_type: str,
        predicate: str,
        source: RawLiteralInput,
    ) -> TypedLiteralValue:
        definition = _declared_entity_property(tbox, entity_type, predicate)
        try:
            return self.literal_normalizer.normalize(
                definition,
                raw_value=source.raw_literal,
                raw_unit=source.raw_unit,
                valid_from=source.raw_valid_from,
                valid_to=source.raw_valid_to,
                observed_at=source.raw_observed_at,
            )
        except LiteralNormalizationError as error:
            raise RequestValidationError() from error

    def _relationship_properties(
        self,
        principal: Principal,
        tbox: TBoxVersion,
        *,
        relationship_type: str,
        values: tuple[RelationshipPropertyInput, ...],
        parent_evidence: EvidenceReference,
        extractor_version: str,
        schema_version: str,
    ) -> tuple[RelationshipPropertyValue, ...]:
        results: list[RelationshipPropertyValue] = []
        for value in values:
            definition = _declared_relationship_property(
                tbox,
                relationship_type,
                value.name,
            )
            try:
                literal = self.literal_normalizer.normalize(
                    definition,
                    raw_value=value.literal.raw_literal,
                    raw_unit=value.literal.raw_unit,
                    valid_from=value.literal.raw_valid_from,
                    valid_to=value.literal.raw_valid_to,
                    observed_at=value.literal.raw_observed_at,
                )
            except LiteralNormalizationError as error:
                raise RequestValidationError() from error
            evidence = self._evidence(principal, value.evidence)
            same_scope = (
                evidence.tenant_id == parent_evidence.tenant_id
                and evidence.document_id == parent_evidence.document_id
                and evidence.version_id == parent_evidence.version_id
                and evidence.chunk_id == parent_evidence.chunk_id
                and evidence.access_policy_id == parent_evidence.access_policy_id
                and evidence.access_policy_version
                == parent_evidence.access_policy_version
                and evidence.access_groups == parent_evidence.access_groups
                and parent_evidence.char_start
                <= evidence.char_start
                < evidence.char_end
                <= parent_evidence.char_end
            )
            if not same_scope:
                # Keep cross-document, cross-policy, and unauthorized details
                # behind the same no-existence boundary as source evidence.
                raise ResourceNotFoundError()
            results.append(
                RelationshipPropertyValue(
                    property_value_id=relationship_property_value_id(
                        principal.tenant_id,
                        relationship_type,
                        value.name,
                        literal.identity_reference,
                        evidence.chunk_id,
                        evidence.char_start,
                        evidence.char_end,
                        extractor_version,
                        schema_version,
                    ),
                    tenant_id=principal.tenant_id,
                    relationship_type=relationship_type,
                    name=value.name,
                    literal_semantics=literal,
                    evidence_chunk_id=evidence.chunk_id,
                    evidence_char_start=evidence.char_start,
                    evidence_char_end=evidence.char_end,
                    evidence_text=evidence.quoted_text,
                    extractor_version=extractor_version,
                    schema_version=schema_version,
                    confidence=value.confidence,
                )
            )
        return tuple(results)

    def _current_review_assertion(
        self,
        principal: Principal,
        record_id: str,
        expected_revision: int,
    ) -> AssertionRecord:
        try:
            record = self.knowledge.get_assertion(
                principal,
                record_id,
                statuses=(
                    GovernanceStatus.CANDIDATE,
                    GovernanceStatus.QUARANTINED,
                ),
            )
        except ApiRuntimeError:
            raise
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except KnowledgeStoreError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        if record is None:
            raise ResourceNotFoundError()
        if record.revision.revision != expected_revision:
            raise ConflictError()
        return record

    def ontology_list(
        self, principal: Principal, request: OntologyListRequest
    ) -> BackendResult:
        _require_capability(principal, "ontology:read")
        try:
            values = self.tboxes.list(
                principal.tenant_id,
                key=request.key,
                status=request.status,
            )
        except ApiRuntimeError:
            raise
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except TBoxConflict as error:
            raise ConflictError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            items = tuple(_tbox_payload(value) for value in values[: request.limit])
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(OntologyListResponse, {"items": items}))

    def ontology_import(
        self, principal: Principal, request: OntologyImportRequest
    ) -> BackendResult:
        _require_capability(principal, "ontology:write")
        mapping = request.model_dump(
            mode="python",
            exclude={"expected_checksum"},
            exclude_none=True,
        )
        mapping.update(tenant_id=principal.tenant_id, status=TBoxStatus.DRAFT.value)
        try:
            value = TBoxVersion.from_mapping(mapping)
        except (TypeError, ValueError) as error:
            raise RequestValidationError() from error
        try:
            stored = self.tboxes.import_version(
                value,
                expected_checksum=request.expected_checksum,
            )
        except ApiRuntimeError:
            raise
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except TBoxValidationError as error:
            raise RequestValidationError() from error
        except TBoxConflict as error:
            raise ConflictError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            payload = _tbox_payload(stored)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(OntologyVersionResponse, payload))

    def ontology_publish(
        self,
        principal: Principal,
        tbox_id: str,
        request: OntologyPublishRequest,
    ) -> BackendResult:
        _require_capability(principal, "ontology:publish")
        try:
            self.tboxes.get(principal.tenant_id, tbox_id)
        except ApiRuntimeError:
            raise
        except KeyError as error:
            raise ResourceNotFoundError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except TBoxConflict as error:
            raise ConflictError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            stored = self.tboxes.publish(
                principal.tenant_id,
                tbox_id,
                expected_active_tbox_id=request.expected_active_tbox_id,
            )
        except ApiRuntimeError:
            raise
        except KeyError as error:
            raise ResourceNotFoundError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except TBoxConflict as error:
            raise ConflictError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            payload = _tbox_payload(stored)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(OntologyVersionResponse, payload))

    def _evidence(
        self, principal: Principal, value: EvidenceInput
    ) -> EvidenceReference:
        try:
            with self.driver.session(database=self.database) as session:
                rows = list(
                    session.run(
                        _EVIDENCE_QUERY,
                        tenant_id=principal.tenant_id,
                        groups=sorted(principal.groups),
                        document_id=value.document_id,
                        version_id=value.version_id,
                        chunk_id=value.chunk_id,
                        char_start=value.char_start,
                        char_end=value.char_end,
                        quoted_text=value.quoted_text,
                    )
                )
        except ApiRuntimeError:
            raise
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        if len(rows) != 1:
            raise ResourceNotFoundError()
        try:
            row = rows[0]
            groups = frozenset(row["access_groups"] or ())
            policy_id = row["access_policy_id"]
            policy_version = row["access_policy_version"]
        except (KeyError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        if not groups or not groups.intersection(principal.groups):
            raise ResourceNotFoundError()
        try:
            return EvidenceReference(
                tenant_id=principal.tenant_id,
                document_id=value.document_id,
                version_id=value.version_id,
                chunk_id=value.chunk_id,
                char_start=value.char_start,
                char_end=value.char_end,
                quoted_text=value.quoted_text,
                access_policy_id=policy_id,
                access_policy_version=policy_version,
                access_groups=groups,
            )
        except (TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error

    def authoritative_import(
        self, principal: Principal, request: AuthoritativeImportRequest
    ) -> BackendResult:
        _require_capability(principal, "knowledge:import")
        now = self._now()
        tbox = self._active_tbox(principal, request.ontology_version_id)
        try:
            trust = authoritative_import_trust(
                ontology_version_id=request.ontology_version_id,
                imported_by=principal.principal_id,
                imported_at=now,
                review_notes=request.review_notes,
            )
            mentions: list[EntityMentionRecord] = []
            by_source_key: dict[str, EntityMentionRecord] = {}
            for item in request.mentions:
                record_id = knowledge_record_id(
                    principal.tenant_id, "ENTITY_MENTION", item.source_key
                )
                mention = EntityMentionRecord(
                    revision=RecordRevision.next(
                        record_id, item.expected_previous_revision
                    ),
                    tenant_id=principal.tenant_id,
                    entity=_entity(principal, item.entity),
                    evidence=self._evidence(principal, item.evidence),
                    confidence=item.confidence,
                    trust=trust,
                    created_at=now,
                )
                mentions.append(mention)
                by_source_key[item.source_key] = mention

            assertions: list[AssertionRecord] = []
            for item in request.assertions:
                subject_mention = by_source_key[item.subject_mention_source_key]
                object_mention = (
                    None
                    if item.object_mention_source_key is None
                    else by_source_key[item.object_mention_source_key]
                )
                record_id = knowledge_record_id(
                    principal.tenant_id, "ASSERTION", item.source_key
                )
                literal_semantics = (
                    None
                    if item.literal is None
                    else self._normalize_literal(
                        tbox,
                        entity_type=subject_mention.entity.entity_type,
                        predicate=item.predicate,
                        source=item.literal,
                    )
                )
                evidence = self._evidence(principal, item.evidence)
                relationship_properties = (
                    ()
                    if object_mention is None
                    else self._relationship_properties(
                        principal,
                        tbox,
                        relationship_type=item.predicate,
                        values=item.relationship_properties,
                        parent_evidence=evidence,
                        extractor_version="EXPERT_IMPORT:reviewed",
                        schema_version=tbox.tbox_id,
                    )
                )
                assertions.append(
                    AssertionRecord(
                        revision=RecordRevision.next(
                            record_id, item.expected_previous_revision
                        ),
                        tenant_id=principal.tenant_id,
                        subject=subject_mention.entity,
                        predicate=item.predicate,
                        evidence=evidence,
                        subject_mention_revision_id=subject_mention.revision_id,
                        confidence=item.confidence,
                        trust=trust,
                        created_at=now,
                        object_entity=(
                            None if object_mention is None else object_mention.entity
                        ),
                        object_mention_revision_id=(
                            None
                            if object_mention is None
                            else object_mention.revision_id
                        ),
                        literal_value=(
                            None
                            if item.literal is None
                            else item.literal.raw_literal
                        ),
                        literal_semantics=literal_semantics,
                        relationship_properties=relationship_properties,
                    )
                )
            batch = ABoxRecordBatch(
                tenant_id=principal.tenant_id,
                mentions=tuple(mentions),
                assertions=tuple(assertions),
            )
        except ApiRuntimeError:
            raise
        except (TypeError, ValueError, KeyError) as error:
            raise RequestValidationError() from error
        try:
            written = self.knowledge.import_authoritative(batch)
        except ApiRuntimeError:
            raise
        except (KnowledgeConflict, KnowledgeSchemaError) as error:
            raise ConflictError() from error
        except KnowledgeEvidenceError as error:
            raise ResourceNotFoundError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except KnowledgeStoreError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            payload = {
                "ontology_version_id": written.ontology_version_id,
                "mention_count": written.mention_count,
                "assertion_count": written.assertion_count,
                "revision_ids": written.revision_ids,
            }
        except (AttributeError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(
            _outbound(AuthoritativeImportResponse, payload)
        )

    def construct(
        self, principal: Principal, request: KnowledgeConstructionRequest
    ) -> BackendResult:
        _require_capability(principal, "knowledge:construct")
        if not frozenset(request.access_groups) <= principal.groups:
            raise AuthorizationError()
        try:
            metadata = ConstructionMetadata(
                operation_key=request.operation_key,
                canonical_uri=request.canonical_uri,
                title=request.title,
                source_name=request.source_name,
                mime_type=request.mime_type,
                language=request.language,
                tbox_key=request.tbox_key,
                access_groups=frozenset(request.access_groups),
                published_at=request.published_at,
                max_attempts=request.max_attempts,
            )
            content = request.decoded_content()
        except (TypeError, ValueError) as error:
            raise RequestValidationError() from error
        try:
            result = self.construction.run(
                principal,
                content,
                metadata,
            )
        except ApiRuntimeError:
            raise
        except ConstructionAuthorizationError as error:
            raise ResourceNotFoundError() from error
        except (ConstructionConflict, IngestionConflict) as error:
            raise ConflictError() from error
        except (ConstructionBudgetExceeded, DocumentParseError) as error:
            raise RequestValidationError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except ExtractionRejected as error:
            if any(item.code == MODEL_CALL_TIMEOUT for item in error.findings):
                raise DependencyTimeoutError() from error
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            payload = {
                "job_id": result.job_id,
                "document_id": result.document_id,
                "version_id": result.version_id,
                "snapshot_id": result.snapshot_id,
                "tbox_id": result.tbox_id,
                "chunks": tuple(
                    {
                        "chunk_id": item.chunk_id,
                        "artifact_id": item.artifact_id,
                        "status": item.status,
                        "finding_codes": item.finding_codes,
                        "mention_record_ids": item.mention_record_ids,
                        "assertion_record_ids": item.assertion_record_ids,
                        "replayed": item.replayed,
                    }
                    for item in result.chunks
                ),
            }
        except (AttributeError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(
            _outbound(KnowledgeConstructionResponse, payload)
        )

    def review_queue(
        self, principal: Principal, request: ReviewQueueRequest
    ) -> BackendResult:
        _require_capability(principal, "knowledge:review")
        try:
            statuses = tuple(
                GovernanceStatus(value) for value in request.statuses
            )
        except (TypeError, ValueError) as error:
            raise RequestValidationError() from error
        try:
            items = self.reviews.review_queue(
                principal,
                statuses=statuses,
                limit=request.limit,
            )
        except ApiRuntimeError:
            raise
        except KnowledgeAuthorizationError as error:
            raise AuthorizationError() from error
        except KnowledgeReviewUnavailable as error:
            raise ResourceNotFoundError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except KnowledgeStoreError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            payload = {
                "items": tuple(_review_record_payload(item) for item in items)
            }
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(ReviewQueueResponse, payload))

    def construction_job(
        self,
        principal: Principal,
        job_id: str,
    ) -> BackendResult:
        _require_capability(principal, "knowledge:construct")
        try:
            value = self.construction_audit.get_job(principal, job_id)
        except ApiRuntimeError:
            raise
        except ConstructionAuthorizationError as error:
            raise ResourceNotFoundError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except ConstructionConflict as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        if value is None:
            raise ResourceNotFoundError()
        return BackendResult(
            _outbound(ConstructionJobResponse, _construction_job_payload(value))
        )

    def construction_jobs(
        self,
        principal: Principal,
        request: ConstructionJobListRequest,
    ) -> BackendResult:
        _require_capability(principal, "knowledge:construct")
        try:
            values = self.construction_audit.list_jobs(
                principal,
                statuses=request.statuses,
                limit=request.limit,
            )
            payload = {"items": tuple(_construction_job_payload(item) for item in values)}
        except ApiRuntimeError:
            raise
        except ConstructionAuthorizationError as error:
            raise AuthorizationError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except ConstructionConflict as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(ConstructionJobListResponse, payload))

    def revision_history(
        self,
        principal: Principal,
        record_id: str,
        request: RecordRevisionHistoryRequest,
    ) -> BackendResult:
        _require_capability(principal, "knowledge:review")
        try:
            items = self.reviews.revision_history(
                principal,
                record_id,
                limit=request.limit,
            )
            payload = {
                "record_id": record_id,
                "items": tuple(_review_record_payload(item) for item in items),
            }
        except ApiRuntimeError:
            raise
        except KnowledgeAuthorizationError as error:
            raise AuthorizationError() from error
        except KnowledgeReviewUnavailable as error:
            raise ResourceNotFoundError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except KnowledgeStoreError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(RecordRevisionHistoryResponse, payload))

    def publication_candidates(
        self,
        principal: Principal,
        request: PublicationCandidatesRequest,
    ) -> BackendResult:
        _require_capability(principal, "knowledge:publish")
        try:
            values = self.publications.candidates(principal, limit=request.limit)
            payload = {
                "items": tuple(
                    {
                        "record": _review_record_payload(value.item),
                        "requires_replacement": value.requires_replacement,
                    }
                    for value in values
                )
            }
        except ApiRuntimeError:
            raise
        except KnowledgeAuthorizationError as error:
            raise AuthorizationError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except KnowledgeStoreError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(PublicationCandidatesResponse, payload))

    def _resolution_context(
        self,
        principal: Principal,
        request: EntityResolutionRequest | EntityResolutionApplyRequest,
    ) -> tuple[EntityMentionRecord, tuple[IdentityPropertyValue, ...], tuple[ResolutionSuggestion, ...]]:
        _require_capability(principal, "knowledge:review")
        try:
            candidate = self.knowledge.get_entity_mention(
                principal,
                request.record_id,
                statuses=(
                    GovernanceStatus.CANDIDATE,
                    GovernanceStatus.QUARANTINED,
                ),
            )
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except KnowledgeStoreError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        if candidate is None:
            raise ResourceNotFoundError()
        if candidate.revision.revision != request.expected_revision:
            raise ConflictError()

        tbox = self._active_tbox(principal, candidate.trust.ontology_version_id)
        definition = next(
            (
                item
                for item in tbox.entity_types
                if item.name == candidate.entity.entity_type
            ),
            None,
        )
        if definition is None:
            raise ConflictError()
        properties: tuple[IdentityPropertyValue, ...] = ()
        if definition.identity_properties:
            try:
                facts = self.knowledge.list_identity_property_assertions(
                    principal,
                    subject_entity_id=candidate.entity.entity_id,
                    ontology_version_id=tbox.tbox_id,
                    predicates=definition.identity_properties,
                    statuses=(
                        GovernanceStatus.CANDIDATE,
                        GovernanceStatus.QUARANTINED,
                    ),
                )
                properties = tuple(
                    IdentityPropertyValue.from_literal(
                        fact.predicate,
                        fact.literal_semantics,
                    )
                    for fact in facts
                    if fact.object_entity is None
                    and fact.literal_semantics is not None
                    and fact.subject_mention_revision_id == candidate.revision_id
                )
            except TimeoutError as error:
                raise DependencyTimeoutError() from error
            except KnowledgeStoreError as error:
                raise DependencyUnavailableError() from error
            except (TypeError, ValueError) as error:
                raise DependencyUnavailableError() from error
            except Exception as error:
                raise DependencyUnavailableError() from error
        try:
            suggestions = EntityResolutionService(
                self.resolution_source,
                active_tbox=tbox,
            ).suggest(
                principal,
                candidate.entity,
                identity_properties=properties,
            )
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except ResolutionBoundaryError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        return candidate, properties, suggestions

    def resolution_suggestions(
        self,
        principal: Principal,
        request: EntityResolutionRequest,
    ) -> BackendResult:
        candidate, properties, suggestions = self._resolution_context(
            principal, request
        )
        payload = {
            "record_id": candidate.record_id,
            "revision_id": candidate.revision_id,
            "revision": candidate.revision.revision,
            "candidate": _entity_payload(candidate.entity),
            "identity_properties": tuple(
                {
                    "name": item.property_name,
                    "datatype": item.datatype,
                    "canonical_value": item.canonical_value,
                    "canonical_unit": item.canonical_unit,
                }
                for item in properties
            ),
            "suggestions": tuple(
                _resolution_suggestion_payload(item) for item in suggestions
            ),
        }
        return BackendResult(_outbound(EntityResolutionResponse, payload))

    def apply_resolution(
        self,
        principal: Principal,
        request: EntityResolutionApplyRequest,
    ) -> BackendResult:
        candidate, _properties, suggestions = self._resolution_context(
            principal, request
        )
        selected = next(
            (
                item
                for item in suggestions
                if item.target is not None
                and item.target.entity_id == request.target_entity_id
                and item.outcome
                in {ResolutionOutcome.AUTO_LINK, ResolutionOutcome.REVIEW}
            ),
            None,
        )
        if selected is None or selected.target is None:
            raise ResourceNotFoundError()
        reviewed_at = self._now()
        try:
            result = self.reviews.apply_entity_resolution(
                principal,
                record_id=candidate.record_id,
                expected_revision=request.expected_revision,
                target=selected.target,
                reviewed_at=reviewed_at,
                notes=(
                    f"{request.notes}\n"
                    f"Resolution rule={selected.rule_version}; "
                    f"matcher={selected.matcher_version}; "
                    f"target={selected.target.entity_id}"
                ),
            )
        except KnowledgeReviewUnavailable as error:
            raise ResourceNotFoundError() from error
        except KnowledgeAuthorizationError as error:
            raise AuthorizationError() from error
        except KnowledgeConflict as error:
            raise ConflictError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except KnowledgeStoreError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        payload = {
            "outcomes": tuple(
                {
                    "record_kind": outcome.record_kind.value,
                    "record_id": outcome.record_id,
                    "previous_revision_id": outcome.previous_revision_id,
                    "revision_id": outcome.revision_id,
                    "revision": outcome.revision,
                    "status": outcome.status.value,
                }
                for outcome in result.outcomes
            ),
            "applied_suggestion": _resolution_suggestion_payload(selected),
        }
        return BackendResult(_outbound(EntityResolutionApplyResponse, payload))

    def review_batch(
        self, principal: Principal, request: ReviewBatchRequest
    ) -> BackendResult:
        _require_capability(principal, "knowledge:review")
        now = self._now()
        try:
            work: list[ReviewRequest] = []
            for item in request.decisions:
                edit: MentionEdit | AssertionEdit | None = None
                if item.mention_edit is not None:
                    edit = MentionEdit(
                        _entity(principal, item.mention_edit.entity),
                        item.mention_edit.confidence,
                    )
                elif item.assertion_edit is not None:
                    source = item.assertion_edit
                    literal_semantics = None
                    current = self._current_review_assertion(
                        principal,
                        item.record_id,
                        item.expected_revision,
                    )
                    tbox = self._active_tbox(
                        principal,
                        current.trust.ontology_version_id,
                    )
                    if source.literal is not None:
                        literal_semantics = self._normalize_literal(
                            tbox,
                            entity_type=source.subject.entity_type,
                            predicate=source.predicate,
                            source=source.literal,
                        )
                    relationship_properties = ()
                    if source.object_entity is not None:
                        relationship_properties = (
                            current.relationship_properties
                            if source.relationship_properties is None
                            else self._relationship_properties(
                                principal,
                                tbox,
                                relationship_type=source.predicate,
                                values=source.relationship_properties,
                                parent_evidence=current.evidence,
                                extractor_version=(
                                    current.trust.extractor_version
                                    or f"{current.trust.origin.value}:reviewed"
                                ),
                                schema_version=current.trust.ontology_version_id,
                            )
                        )
                    edit = AssertionEdit(
                        subject=_entity(principal, source.subject),
                        predicate=source.predicate,
                        subject_mention_revision_id=(
                            source.subject_mention_revision_id
                        ),
                        confidence=source.confidence,
                        object_entity=(
                            None
                            if source.object_entity is None
                            else _entity(principal, source.object_entity)
                        ),
                        object_mention_revision_id=(
                            source.object_mention_revision_id
                        ),
                        literal_value=(
                            None
                            if source.literal is None
                            else source.literal.raw_literal
                        ),
                        literal_semantics=literal_semantics,
                        relationship_properties=relationship_properties,
                    )
                work.append(
                    ReviewRequest(
                        record_kind=ReviewRecordKind(item.record_kind),
                        record_id=item.record_id,
                        expected_revision=item.expected_revision,
                        decision=GovernanceStatus(item.decision),
                        reviewed_at=now,
                        notes=item.notes,
                        edit=edit,
                    )
                )
        except (TypeError, ValueError) as error:
            raise RequestValidationError() from error
        try:
            result = self.reviews.review_batch(principal, tuple(work))
        except ApiRuntimeError:
            raise
        except KnowledgeReviewUnavailable as error:
            raise ResourceNotFoundError() from error
        except KnowledgeAuthorizationError as error:
            raise AuthorizationError() from error
        except KnowledgeConflict as error:
            raise ConflictError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except KnowledgeStoreError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            payload = {
                "outcomes": tuple(
                    {
                        "record_kind": item.record_kind.value,
                        "record_id": item.record_id,
                        "previous_revision_id": item.previous_revision_id,
                        "revision_id": item.revision_id,
                        "revision": item.revision,
                        "status": item.status.value,
                    }
                    for item in result.outcomes
                )
            }
        except (AttributeError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(
            _outbound(ReviewBatchResponse, payload)
        )

    def publish(
        self, principal: Principal, request: PublicationRequest
    ) -> BackendResult:
        _require_capability(principal, "knowledge:publish")
        try:
            result = self.publications.publish(
                principal,
                request.approved_revision_ids,
                expected_active_publication_id=(
                    request.expected_active_publication_id
                ),
                published_at=self._now(),
                remove_record_ids=request.remove_record_ids,
                replace_record_ids=request.replace_record_ids,
            )
        except ApiRuntimeError:
            raise
        except KnowledgeReviewUnavailable as error:
            raise ResourceNotFoundError() from error
        except KnowledgeAuthorizationError as error:
            raise AuthorizationError() from error
        except (KnowledgePublicationConflict, KnowledgeConflict) as error:
            raise ConflictError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except KnowledgeStoreError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            payload = _publication_payload(result)
        except (AttributeError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(PublicationResponse, payload))

    def rollback(
        self,
        principal: Principal,
        publication_id: str,
        request: RollbackRequest,
    ) -> BackendResult:
        _require_capability(principal, "knowledge:publish")
        try:
            target = self.publications.get(principal, publication_id)
        except ApiRuntimeError:
            raise
        except KnowledgeAuthorizationError as error:
            raise AuthorizationError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except KnowledgeStoreError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        if target is None:
            raise ResourceNotFoundError()
        try:
            result = self.publications.rollback(
                principal,
                publication_id,
                expected_active_publication_id=(
                    request.expected_active_publication_id
                ),
                rolled_back_at=self._now(),
            )
        except ApiRuntimeError:
            raise
        except KnowledgeReviewUnavailable as error:
            raise ResourceNotFoundError() from error
        except KnowledgeAuthorizationError as error:
            raise AuthorizationError() from error
        except (KnowledgePublicationConflict, KnowledgeConflict) as error:
            raise ConflictError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except KnowledgeStoreError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            payload = _publication_payload(result)
        except (AttributeError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(PublicationResponse, payload))

    def history(
        self, principal: Principal, request: PublicationHistoryRequest
    ) -> BackendResult:
        _require_capability(principal, "knowledge:publish")
        try:
            items = self.publications.history(principal, limit=request.limit)
        except ApiRuntimeError:
            raise
        except KnowledgeAuthorizationError as error:
            raise AuthorizationError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except KnowledgeStoreError as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            payload = {
                "items": tuple(_publication_payload(item) for item in items)
            }
        except (AttributeError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(PublicationHistoryResponse, payload))

    def quality(self, principal: Principal) -> BackendResult:
        """Audit the caller's complete active publication through a dedicated scope."""

        _require_capability(principal, "knowledge:quality")
        try:
            report = self.quality_service.audit(principal)
        except ApiRuntimeError:
            raise
        except PublishedGraphQualityAuthorizationError as error:
            raise AuthorizationError() from error
        except (PublishedGraphQualityConflict, PublishedGraphQualityLimitExceeded) as error:
            raise ConflictError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except PublishedGraphQualityUnavailable as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            payload = _published_quality_payload(report)
        except (AttributeError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(PublishedGraphQualityResponse, payload))

    @staticmethod
    def _quality_history_call(operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return operation(*args, **kwargs)
        except ApiRuntimeError:
            raise
        except PublishedGraphQualityAuthorizationError as error:
            raise AuthorizationError() from error
        except (PublishedGraphQualityHistoryConflict, PublishedGraphQualityConflict, PublishedGraphQualityLimitExceeded) as error:
            raise ConflictError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except (PublishedGraphQualityHistoryUnavailable, PublishedGraphQualityUnavailable) as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error

    @staticmethod
    def _quality_run_response(principal: Principal, value: Any) -> BackendResult:
        try:
            if value.tenant_id != principal.tenant_id:
                raise DependencyUnavailableError()
            payload = {
                "report": _published_quality_payload(value.report),
                "recorded_by": value.recorded_by,
                "recorded_at": value.recorded_at,
                "record_hash": value.record_hash,
            }
        except (AttributeError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(PublishedGraphQualityRunResponse, payload))

    def record_quality(self, principal: Principal) -> BackendResult:
        _require_capability(principal, "knowledge:quality")
        value = self._quality_history_call(self.quality_history_service.audit_and_record, principal)
        return self._quality_run_response(principal, value)

    def quality_run(self, principal: Principal, run_id: str) -> BackendResult:
        _require_capability(principal, "knowledge:quality")
        value = self._quality_history_call(self.quality_history_service.get_run, principal, run_id)
        if value is None:
            raise ResourceNotFoundError()
        if getattr(value, "run_id", None) != run_id:
            raise DependencyUnavailableError()
        return self._quality_run_response(principal, value)

    def quality_runs(self, principal: Principal, request: PublishedGraphQualityRunListRequest) -> BackendResult:
        _require_capability(principal, "knowledge:quality")
        values = self._quality_history_call(
            self.quality_history_service.list_runs, principal,
            publication_id=request.publication_id, limit=request.limit,
        )
        try:
            items = []
            for value in values:
                if value.tenant_id != principal.tenant_id or (
                    request.publication_id is not None
                    and value.publication_id != request.publication_id
                ):
                    raise DependencyUnavailableError()
                items.append({
                    "run_id": value.run_id,
                    "publication_id": value.publication_id,
                    "publication_generation": value.publication_generation,
                    "ontology_version_id": value.ontology_version_id,
                    "corpus_revision": value.corpus_revision,
                    "graph_digest": value.graph_digest,
                    "ruleset_version": value.ruleset_version,
                    "passed": value.passed,
                    "total_issue_count": value.total_issue_count,
                    "total_error_count": value.total_error_count,
                    "issues_truncated": value.issues_truncated,
                    "counts": dict(value.counts),
                    "recorded_by": value.recorded_by,
                    "recorded_at": value.recorded_at,
                    "record_hash": value.record_hash,
                })
            if len(items) > request.limit:
                raise DependencyUnavailableError()
        except (AttributeError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(PublishedGraphQualityRunListResponse, {"items": items}))

    def inventory(
        self,
        principal: Principal,
        request: ActivePublicationInventoryRequest,
    ) -> BackendResult:
        """Return a safe, bounded projection of the active governed A-Box."""

        _require_capability(principal, "knowledge:quality")
        try:
            result = self.inventory_service.list_active(
                principal,
                document_id=request.document_id,
                limit=request.limit,
            )
        except ApiRuntimeError:
            raise
        except ActivePublicationInventoryAuthorizationError as error:
            raise AuthorizationError() from error
        except (
            ActivePublicationInventoryConflict,
            ActivePublicationInventoryLimitExceeded,
        ) as error:
            raise ConflictError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except ActivePublicationInventoryUnavailable as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        if result.tenant_id != principal.tenant_id:
            raise DependencyUnavailableError()
        try:
            payload = {
                "publication_id": result.publication_id,
                "publication_generation": result.publication_generation,
                "manifest_hash": result.manifest_hash,
                "ontology_version_id": result.ontology_version_id,
                "document_id": result.document_id,
                "total_record_count": result.total_record_count,
                "matching_record_count": result.matching_record_count,
                "truncated": result.truncated,
                "items": tuple(_inventory_item_payload(item) for item in result.items),
            }
        except (AttributeError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        return BackendResult(_outbound(ActivePublicationInventoryResponse, payload))

    def documents(
        self,
        principal: Principal,
        request: DocumentLifecycleListRequest,
    ) -> BackendResult:
        """List fully visible active sources as metadata-only lifecycle rows."""

        _require_capability(principal, "knowledge:lifecycle")
        try:
            items = self.retirement_service.list_active_documents(
                principal,
                limit=request.limit,
            )
        except ApiRuntimeError:
            raise
        except DocumentRetirementUnavailable as error:
            raise AuthorizationError() from error
        except DocumentRetirementConflict as error:
            raise ConflictError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except DocumentRetirementBackendUnavailable as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        try:
            payload_items = tuple(
                {
                    "document_id": item.document_id,
                    "title": item.title,
                    "source_name": item.source_name,
                    "canonical_uri": item.canonical_uri,
                    "source_generation": item.source_generation,
                    "active_snapshot_id": item.active_snapshot_id,
                    "active_version_id": item.active_version_id,
                    "chunk_count": item.chunk_count,
                    "access_policy_id": item.access_policy_id,
                    "access_policy_version": item.access_policy_version,
                    "access_groups": item.access_groups,
                    "blocked": item.blocked,
                    "blocker_codes": item.blocker_codes,
                }
                for item in items
                if item.tenant_id == principal.tenant_id
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        if len(payload_items) != len(items):
            raise DependencyUnavailableError()
        return BackendResult(
            _outbound(DocumentLifecycleListResponse, {"items": payload_items})
        )

    def retire_document(
        self,
        principal: Principal,
        document_id: str,
        request: DocumentRetirementRequest,
    ) -> BackendResult:
        """Withdraw one active source while retaining its immutable audit graph."""

        _require_capability(principal, "knowledge:lifecycle")
        try:
            result = self.retirement_service.retire(
                principal,
                DomainDocumentRetirementRequest(
                    document_id=document_id,
                    operation_key=request.operation_key,
                    expected_active_snapshot_id=request.expected_active_snapshot_id,
                    source_generation=request.source_generation,
                ),
            )
        except ApiRuntimeError:
            raise
        except DocumentRetirementUnavailable as error:
            # Missing, foreign-tenant and partially visible IDs deliberately
            # collapse to the same non-enumerating public result.
            raise AuthorizationError() from error
        except (DocumentRetirementConflict, DocumentRetirementBlocked) as error:
            raise ConflictError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except DocumentRetirementBackendUnavailable as error:
            raise DependencyUnavailableError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        if (
            result.tenant_id != principal.tenant_id
            or result.document_id != document_id
        ):
            raise DependencyUnavailableError()
        payload = {
            "retirement_id": result.retirement_id,
            "document_id": result.document_id,
            "retired_snapshot_id": result.retired_snapshot_id,
            "retired_version_id": result.retired_version_id,
            "source_generation_before": result.source_generation_before,
            "source_generation_after": result.source_generation_after,
            "corpus_revision": result.corpus_revision,
            "retired_at": result.retired_at,
            "status": result.status,
        }
        return BackendResult(_outbound(DocumentRetirementResponse, payload))


__all__ = ["Neo4jKnowledgeOperations"]
