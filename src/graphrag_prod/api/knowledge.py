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
from graphrag_prod.construction.workflow import (
    ConstructionAuthorizationError,
    ConstructionConflict,
)
from graphrag_prod.domain import Principal, TypedLiteralValue
from graphrag_prod.domain.ids import entity_id as make_entity_id
from graphrag_prod.ingestion import IngestionConflict
from graphrag_prod.knowledge import (
    ABoxRecordBatch,
    AssertionRecord,
    EntityIdentity,
    EntityMentionRecord,
    EvidenceReference,
    KnowledgeEvidenceError,
    KnowledgeSchemaError,
    Neo4jKnowledgeStore,
    RecordRevision,
    authoritative_import_trust,
    knowledge_record_id,
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
from graphrag_prod.ontology.store import TBoxConflict

from .knowledge_contracts import (
    AuthoritativeImportRequest,
    AuthoritativeImportResponse,
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
    PublicationRequest,
    PublicationResponse,
    RawLiteralInput,
    ReviewBatchRequest,
    ReviewBatchResponse,
    ReviewQueueRequest,
    ReviewQueueResponse,
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
        )
    else:
        raise DependencyUnavailableError()
    return common


def _publication_payload(value: Any) -> dict[str, object]:
    return {
        "publication_id": value.publication_id,
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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(getattr(construction, "run", None)):
            raise TypeError("construction must implement run")
        self.driver = driver
        self.database = database
        self.construction = construction
        self.tboxes = tboxes or Neo4jTBoxStore(driver, database)
        self.knowledge = knowledge or Neo4jKnowledgeStore(driver, database)
        self.reviews = reviews or Neo4jKnowledgeReviewService(driver, database)
        self.publications = publications or Neo4jKnowledgePublicationService(
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
        try:
            metadata = ConstructionMetadata(
                operation_key=request.operation_key,
                canonical_uri=request.canonical_uri,
                title=request.title,
                source_name=request.source_name,
                mime_type=request.mime_type,
                language=request.language,
                tbox_key=request.tbox_key,
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
        except DocumentParseError as error:
            raise RequestValidationError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except ExtractionRejected as error:
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
                    if source.literal is not None:
                        current = self._current_review_assertion(
                            principal,
                            item.record_id,
                            item.expected_revision,
                        )
                        tbox = self._active_tbox(
                            principal,
                            current.trust.ontology_version_id,
                        )
                        literal_semantics = self._normalize_literal(
                            tbox,
                            entity_type=source.subject.entity_type,
                            predicate=source.predicate,
                            source=source.literal,
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


__all__ = ["Neo4jKnowledgeOperations"]
