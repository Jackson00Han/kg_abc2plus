"""Application services behind the authenticated HTTP boundary.

The HTTP controller never accepts a tenant, principal, access-group set,
query vector, or embedding-space identifier from a client.  This module
reconstructs a :class:`~graphrag_prod.domain.Principal` from the trusted
operation envelope and keeps provider-specific work behind small protocols.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import math
from numbers import Real
import time
from typing import Any, Protocol

from pydantic import ValidationError

from graphrag_prod.domain import Principal
from graphrag_prod.domain.ids import canonicalize_uri, content_checksum
from graphrag_prod.generation import (
    AnswerResult,
    GenerationRequest,
    GroundedGenerationService,
)
from graphrag_prod.ingestion import (
    IncrementalIngestionRequest,
    IngestionConflict,
    IngestionResult,
    JobView,
    Neo4jIncrementalPipeline,
)
from graphrag_prod.retrieval import (
    EvidenceSubgraph,
    Neo4jRetrievalEngine,
    RetrievalBackendError,
    RetrievalBackendTimeout,
    RetrievalBackendUnavailable,
    RetrievalRequest as DomainRetrievalRequest,
    RetrievalResult,
    RetrievalUnavailable,
    SubgraphTrustPolicy,
)

from .contracts import (
    AnswerRequest,
    AnswerResponse,
    DeleteRequest,
    DeleteResponse,
    IngestionRequest,
    IngestionResponse,
    JobResponse,
    ReadinessResponse,
    RetrievalRequest,
    RetrievalResponse,
)
from .knowledge_contracts import (
    AuthoritativeImportRequest,
    AuthoritativeImportResponse,
    ConstructionJobListRequest,
    ConstructionJobListResponse,
    ConstructionJobResponse,
    EntityResolutionApplyRequest,
    EntityResolutionApplyResponse,
    EntityResolutionRequest,
    EntityResolutionResponse,
    KnowledgeConstructionRequest,
    KnowledgeConstructionResponse,
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
    OperationEnvelope,
    OperationKind,
    RequestValidationError,
    ResourceNotFoundError,
    UsageMetadata,
    required_scope,
)


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Non-content usage returned by one provider call."""

    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0
    estimated_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "model_calls"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        cost = self.estimated_cost_usd
        if (
            isinstance(cost, bool)
            or not isinstance(cost, Real)
            or not math.isfinite(float(cost))
            or float(cost) < 0.0
        ):
            raise ValueError("estimated_cost_usd must be finite and non-negative")
        object.__setattr__(self, "estimated_cost_usd", float(cost))


@dataclass(frozen=True, slots=True)
class QueryEmbedding:
    """A server-generated query vector and its exact vector-space identity."""

    vector: tuple[float, ...]
    embedding_space_id: str
    usage: ProviderUsage = ProviderUsage(model_calls=1)

    def __post_init__(self) -> None:
        if len(self.vector) > 16_384:
            raise ValueError("query embedding exceeds the dimension bound")
        values: list[float] = []
        for value in self.vector:
            if (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not math.isfinite(float(value))
            ):
                raise ValueError("query embedding values must be finite numbers")
            values.append(float(value))
        if not values or not any(value != 0.0 for value in values):
            raise ValueError("query embedding must be non-zero")
        object.__setattr__(self, "vector", tuple(values))
        space_id = self.embedding_space_id.strip()
        if not space_id or "\x00" in space_id:
            raise ValueError("embedding_space_id must not be empty")
        object.__setattr__(self, "embedding_space_id", space_id)
        if not isinstance(self.usage, ProviderUsage):
            raise TypeError("usage must be ProviderUsage")


@dataclass(frozen=True, slots=True)
class GeneratedAnswer:
    """One answer plus usage measured for that exact provider invocation."""

    answer: AnswerResult
    usage: ProviderUsage

    def __post_init__(self) -> None:
        if not isinstance(self.answer, AnswerResult):
            raise TypeError("answer must be AnswerResult")
        if not isinstance(self.usage, ProviderUsage):
            raise TypeError("usage must be ProviderUsage")


class QueryEmbedder(Protocol):
    """Deployment-owned embedding provider; raw vectors never cross HTTP."""

    def embed(self, query_text: str, *, tenant_id: str) -> QueryEmbedding: ...


class MeteredGenerationService(Protocol):
    """Optional deployment boundary for exact per-call LLM accounting."""

    def generate_with_usage(self, request: GenerationRequest) -> GeneratedAnswer: ...


class EvidenceSubgraphProjector(Protocol):
    """Project governed graph context for an authorized Chunk selection."""

    def project(
        self,
        principal: Principal,
        selected_chunk_ids: tuple[str, ...],
        *,
        trust_policy: SubgraphTrustPolicy,
        version_filter: VersionFilter,
    ) -> EvidenceSubgraph: ...


class DocumentOperations(Protocol):
    """Tenant-scoped document and durable-job application operations."""

    def ingest(
        self, principal: Principal, request: IngestionRequest
    ) -> BackendResult: ...

    def delete(
        self,
        principal: Principal,
        document_id: str,
        request: DeleteRequest,
    ) -> BackendResult: ...

    def get_job(self, principal: Principal, job_id: str) -> BackendResult: ...


class IncrementalIngestionPlanner(Protocol):
    """Build one Stage 3 request from authenticated, validated API input.

    Deployments own their splitter, pipeline profile, governance policy,
    embedding profile, version-number lookup, and ingestion clock.  The
    document adapter validates every security- and provenance-relevant field
    returned by this planner before any durable work begins.
    """

    def plan(
        self,
        *,
        principal: Principal,
        request: IngestionRequest,
    ) -> IncrementalIngestionRequest: ...


class ReadinessOperations(Protocol):
    """Return bounded, non-sensitive dependency readiness checks."""

    def check(self) -> BackendResult: ...


class KnowledgeOperations(Protocol):
    """Governed T-Box/A-Box construction, review, and publication boundary."""

    def ontology_list(
        self, principal: Principal, request: OntologyListRequest
    ) -> BackendResult: ...

    def ontology_import(
        self, principal: Principal, request: OntologyImportRequest
    ) -> BackendResult: ...

    def ontology_publish(
        self,
        principal: Principal,
        tbox_id: str,
        request: OntologyPublishRequest,
    ) -> BackendResult: ...

    def authoritative_import(
        self, principal: Principal, request: AuthoritativeImportRequest
    ) -> BackendResult: ...

    def construct(
        self, principal: Principal, request: KnowledgeConstructionRequest
    ) -> BackendResult: ...

    def construction_job(
        self, principal: Principal, job_id: str
    ) -> BackendResult: ...

    def construction_jobs(
        self, principal: Principal, request: ConstructionJobListRequest
    ) -> BackendResult: ...

    def review_queue(
        self, principal: Principal, request: ReviewQueueRequest
    ) -> BackendResult: ...

    def revision_history(
        self,
        principal: Principal,
        record_id: str,
        request: RecordRevisionHistoryRequest,
    ) -> BackendResult: ...

    def review_batch(
        self, principal: Principal, request: ReviewBatchRequest
    ) -> BackendResult: ...

    def resolution_suggestions(
        self, principal: Principal, request: EntityResolutionRequest
    ) -> BackendResult: ...

    def apply_resolution(
        self, principal: Principal, request: EntityResolutionApplyRequest
    ) -> BackendResult: ...

    def publish(
        self, principal: Principal, request: PublicationRequest
    ) -> BackendResult: ...

    def rollback(
        self,
        principal: Principal,
        publication_id: str,
        request: RollbackRequest,
    ) -> BackendResult: ...

    def history(
        self, principal: Principal, request: PublicationHistoryRequest
    ) -> BackendResult: ...

    def publication_candidates(
        self, principal: Principal, request: PublicationCandidatesRequest
    ) -> BackendResult: ...


def _job_response_payload(job: JobView) -> dict[str, object]:
    """Project a durable job without leases, fingerprints, or tenant data."""
    if not isinstance(job, JobView):
        raise DependencyUnavailableError()
    return {
        "job_id": job.job_id,
        "operation": job.operation,
        "status": job.status.value,
        "phase": job.phase.value,
        "document_id": job.document_id,
        "target_version_id": job.target_version_id,
        "target_snapshot_id": job.target_snapshot_id,
        "expected_active_snapshot_id": job.expected_active_snapshot_id,
        "source_generation": job.source_generation,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "completed_tasks": job.completed_tasks,
        "expected_tasks": job.expected_tasks,
        "outcome": job.outcome,
        "last_error_code": job.last_error_code,
    }


class Neo4jDocumentOperations:
    """Tenant-safe API adapter for the durable Stage 3 document lifecycle.

    The planner and derivation providers are deployment-owned because their
    versions are part of the immutable provenance contract.  This adapter is
    the trust boundary: it rejects a plan that changes authenticated scope,
    submitted source bytes, ACL metadata, lifecycle CAS values, or any other
    client-authoritative source metadata before the Neo4j pipeline is called.
    """

    def __init__(
        self,
        pipeline: Neo4jIncrementalPipeline,
        planner: IncrementalIngestionPlanner,
        extraction_provider: Any,
        embedding_provider: Any,
    ) -> None:
        service = getattr(pipeline, "service", None)
        if not callable(getattr(pipeline, "run", None)) or any(
            not callable(getattr(service, method, None))
            for method in ("delete_document", "get_job_for_tenant")
        ):
            raise TypeError("pipeline must provide the Stage 3 lifecycle")
        if not callable(getattr(planner, "plan", None)):
            raise TypeError("planner must provide plan()")
        if not callable(extraction_provider):
            raise TypeError("extraction_provider must be callable")
        if not callable(embedding_provider):
            raise TypeError("embedding_provider must be callable")
        self._pipeline = pipeline
        self._planner = planner
        self._extraction_provider = extraction_provider
        self._embedding_provider = embedding_provider

    @staticmethod
    def _validate_plan(
        principal: Principal,
        request: IngestionRequest,
        planned: object,
    ) -> IncrementalIngestionRequest:
        if not isinstance(planned, IncrementalIngestionRequest):
            raise DependencyUnavailableError()
        try:
            canonical_uri = canonicalize_uri(request.canonical_uri)
            original_checksum = content_checksum(request.content)
            normalized_text = planned.normalized_text
        except (TypeError, ValueError) as error:
            raise DependencyUnavailableError() from error
        expected = {
            "tenant_id": principal.tenant_id,
            "operation_key": request.operation_key,
            "canonical_uri": canonical_uri,
            "title": request.title,
            "source_name": request.source_name,
            "mime_type": request.mime_type,
            "language": request.language,
            "published_at": request.published_at,
            "access_policy_id": request.access_policy_id,
            "access_policy_version": request.access_policy_version,
            "access_groups": frozenset(request.access_groups),
            "source_generation": request.source_generation,
            "expected_active_snapshot_id": request.expected_active_snapshot_id,
            "max_attempts": request.max_attempts,
        }
        if any(getattr(planned, name) != value for name, value in expected.items()):
            raise DependencyUnavailableError()
        if normalized_text != request.content:
            raise DependencyUnavailableError()
        if planned.original_checksum not in (None, original_checksum):
            raise DependencyUnavailableError()
        return planned

    @staticmethod
    def _validate_result(
        result: object,
        *,
        tenant_id: str,
        document_id: str,
    ) -> IngestionResult:
        if (
            not isinstance(result, IngestionResult)
            or result.job.tenant_id != tenant_id
            or result.job.document_id != document_id
        ):
            raise DependencyUnavailableError()
        return result

    def ingest(
        self,
        principal: Principal,
        request: IngestionRequest,
    ) -> BackendResult:
        if not frozenset(request.access_groups).issubset(principal.groups):
            raise AuthorizationError()
        try:
            planned = self._validate_plan(
                principal,
                request,
                self._planner.plan(principal=principal, request=request),
            )
            result = self._validate_result(
                self._pipeline.run(
                    planned,
                    extraction_provider=self._extraction_provider,
                    embedding_provider=self._embedding_provider,
                ),
                tenant_id=principal.tenant_id,
                document_id=planned.document_id,
            )
        except ApiRuntimeError:
            raise
        except IngestionConflict as error:
            raise ConflictError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        return BackendResult(
            {
                "job": _job_response_payload(result.job),
                "snapshot_id": result.snapshot_id,
                "active_snapshot_id": result.active_snapshot_id,
            }
        )

    def delete(
        self,
        principal: Principal,
        document_id: str,
        request: DeleteRequest,
    ) -> BackendResult:
        try:
            result = self._validate_result(
                self._pipeline.service.delete_document(
                    tenant_id=principal.tenant_id,
                    document_id=document_id,
                    operation_key=request.operation_key,
                    expected_active_snapshot_id=request.expected_active_snapshot_id,
                    source_generation=request.source_generation,
                    max_attempts=request.max_attempts,
                ),
                tenant_id=principal.tenant_id,
                document_id=document_id,
            )
        except ApiRuntimeError:
            raise
        except IngestionConflict as error:
            raise ConflictError() from error
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        return BackendResult({"job": _job_response_payload(result.job)})

    def get_job(self, principal: Principal, job_id: str) -> BackendResult:
        try:
            job = self._pipeline.service.get_job_for_tenant(
                principal.tenant_id,
                job_id,
            )
        except KeyError as error:
            # A job owned by another tenant and a nonexistent job deliberately
            # have the same externally observable result.
            raise ResourceNotFoundError() from error
        except ApiRuntimeError:
            raise
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        if job.tenant_id != principal.tenant_id:
            raise DependencyUnavailableError()
        return BackendResult(_job_response_payload(job))


def _elapsed_ms(started: float, monotonic: Any) -> float:
    return max(0.0, (float(monotonic()) - started) * 1_000.0)


def _provider_usage(usage: ProviderUsage) -> dict[str, int | float]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "model_calls": usage.model_calls,
        "estimated_cost_usd": usage.estimated_cost_usd,
    }


def _merge_provider_usage(*values: ProviderUsage) -> ProviderUsage:
    return ProviderUsage(
        input_tokens=sum(value.input_tokens for value in values),
        output_tokens=sum(value.output_tokens for value in values),
        model_calls=sum(value.model_calls for value in values),
        estimated_cost_usd=sum(value.estimated_cost_usd for value in values),
    )


def _graph_citation_payload(value: Any) -> dict[str, object]:
    return {
        "chunk_id": value.chunk_id,
        "chunk_checksum": value.chunk_checksum,
        "chunk_text": value.chunk_text,
        "document_id": value.document_id,
        "document_title": value.document_title,
        "canonical_uri": value.canonical_uri,
        "source_name": value.source_name,
        "version_id": value.version_id,
        "version_checksum": value.version_checksum,
        "version_number": value.version_number,
        "ordinal": value.ordinal,
        "char_start": value.char_start,
        "char_end": value.char_end,
        "page_number": value.page_number,
        "section": value.section,
        "published_at": value.published_at,
    }


def _graph_provenance_payload(value: Any) -> dict[str, object]:
    return {
        "publication_id": value.publication_id,
        "record_id": value.record_id,
        "revision_id": value.revision_id,
        "ontology_version_id": value.ontology_version_id,
        "origin": value.origin.value,
        "authority": value.authority.value,
        "status": value.status.value,
        "confidence": value.confidence,
        "extractor_version": value.extractor_version,
        "prompt_version": value.prompt_version,
    }


def _graph_evidence_payload(value: Any) -> dict[str, object]:
    return {
        "citation": _graph_citation_payload(value.citation),
        "char_start": value.char_start,
        "char_end": value.char_end,
        "quoted_text": value.quoted_text,
        "provenance": _graph_provenance_payload(value.provenance),
    }


def _typed_literal_payload(value: Any) -> dict[str, object] | None:
    if value is None:
        return None
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


def _graph_relationship_property_payload(
    value: Any,
    parent_evidence: Any,
) -> dict[str, object]:
    return {
        "property_value_id": value.property_value_id,
        "name": value.name,
        "literal_semantics": _typed_literal_payload(value.literal_semantics),
        "evidence": {
            "citation": _graph_citation_payload(parent_evidence.citation),
            "char_start": value.evidence_char_start,
            "char_end": value.evidence_char_end,
            "quoted_text": value.evidence_text,
            "provenance": _graph_provenance_payload(parent_evidence.provenance),
        },
        "confidence": value.confidence,
    }


def _graph_assertion_payload(value: Any) -> dict[str, object]:
    return {
        "record_id": value.record_id,
        "revision_id": value.revision_id,
        "predicate": value.predicate,
        "subject_entity_id": value.subject_entity_id,
        "subject_mention_revision_id": value.subject_mention_revision_id,
        "object_kind": value.object_kind,
        "object_entity_id": value.object_entity_id,
        "object_mention_revision_id": value.object_mention_revision_id,
        "literal_value": value.literal_value,
        "literal_semantics": _typed_literal_payload(value.literal_semantics),
        "relationship_properties": tuple(
            _graph_relationship_property_payload(item, value.evidence)
            for item in value.relationship_properties
        ),
        "evidence": _graph_evidence_payload(value.evidence),
    }


def _subgraph_payload(value: EvidenceSubgraph) -> dict[str, object]:
    return {
        "trust_policy": value.trust_policy.value,
        "entities": tuple(
            {
                "entity_id": item.entity.entity_id,
                "entity_type": item.entity.entity_type,
                "canonical_key": item.entity.canonical_key,
                "canonical_name": item.entity.canonical_name,
                "aliases": item.entity.aliases,
                "evidence": tuple(
                    _graph_evidence_payload(evidence)
                    for evidence in item.evidence
                ),
            }
            for item in value.entities
        ),
        "relationship_assertions": tuple(
            _graph_assertion_payload(item)
            for item in value.relationship_assertions
        ),
        "literal_assertions": tuple(
            _graph_assertion_payload(item) for item in value.literal_assertions
        ),
        "paths": tuple(
            {
                "subject_entity_id": item.subject_entity_id,
                "assertion_revision_id": item.assertion_revision_id,
                "predicate": item.predicate,
                "object_entity_id": item.object_entity_id,
                "literal_value": item.literal_value,
                "literal_semantics": _typed_literal_payload(
                    item.literal_semantics
                ),
                "relationship_properties": tuple(
                    _graph_relationship_property_payload(value, item.evidence)
                    for value in item.relationship_properties
                ),
                "evidence": _graph_evidence_payload(item.evidence),
            }
            for item in value.paths
        ),
        "matched_chunk_ids": value.matched_chunk_ids,
        "publication_ids": value.publication_ids,
    }


class GraphRAGQueryOperations:
    """Real retrieval/generation adapter with server-side query embedding."""

    def __init__(
        self,
        retrieval_engine: Neo4jRetrievalEngine,
        query_embedder: QueryEmbedder,
        generation_service: GroundedGenerationService,
        *,
        subgraph_projector: EvidenceSubgraphProjector | None = None,
        monotonic: Any = time.monotonic,
    ) -> None:
        if not callable(getattr(retrieval_engine, "retrieve", None)):
            raise TypeError("retrieval_engine must provide retrieve()")
        if not callable(getattr(query_embedder, "embed", None)):
            raise TypeError("query_embedder must provide embed()")
        if not callable(getattr(generation_service, "generate", None)):
            raise TypeError("generation_service must provide generate()")
        if subgraph_projector is not None and not callable(
            getattr(subgraph_projector, "project", None)
        ):
            raise TypeError("subgraph_projector must provide project()")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._retrieval_engine = retrieval_engine
        self._query_embedder = query_embedder
        self._generation_service = generation_service
        self._subgraph_projector = subgraph_projector
        self._monotonic = monotonic

    def _generate(self, request: GenerationRequest) -> GeneratedAnswer:
        metered = getattr(self._generation_service, "generate_with_usage", None)
        if callable(metered):
            try:
                result = metered(request)
            except (ApiRuntimeError, KeyboardInterrupt, SystemExit):
                raise
            except TimeoutError as error:
                raise DependencyTimeoutError() from error
            except Exception as error:
                raise DependencyUnavailableError() from error
            if not isinstance(result, GeneratedAnswer):
                raise DependencyUnavailableError()
            return result
        try:
            answer = self._generation_service.generate(request)
        except (ApiRuntimeError, KeyboardInterrupt, SystemExit):
            raise
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        if not isinstance(answer, AnswerResult):
            raise DependencyUnavailableError()
        # The Stage 6 deterministic interface predates provider usage.  We can
        # count the invocation truthfully, but zeros mean the deployment has
        # not supplied token/cost measurements.  Production assembly should
        # implement ``generate_with_usage``.
        return GeneratedAnswer(answer, ProviderUsage(model_calls=1))

    def _retrieve(
        self,
        principal: Principal,
        request: RetrievalRequest | AnswerRequest,
        *,
        limits: Any,
    ) -> tuple[RetrievalResult, UsageMetadata]:
        embedding_started = float(self._monotonic())
        try:
            embedding = self._query_embedder.embed(
                request.query_text,
                tenant_id=principal.tenant_id,
            )
        except (ApiRuntimeError, KeyboardInterrupt, SystemExit):
            raise
        except TimeoutError as error:
            raise DependencyTimeoutError() from error
        except Exception as error:
            raise DependencyUnavailableError() from error
        if not isinstance(embedding, QueryEmbedding):
            raise DependencyUnavailableError()
        embedding_ms = _elapsed_ms(embedding_started, self._monotonic)

        retrieval_started = float(self._monotonic())
        version_filter = request.version_filter.to_domain()
        try:
            result = self._retrieval_engine.retrieve(
                DomainRetrievalRequest(
                    query_text=request.query_text,
                    query_vector=embedding.vector,
                    principal=principal,
                    query_embedding_space_id=embedding.embedding_space_id,
                    limits=limits,
                    version_filter=version_filter,
                )
            )
        except RetrievalBackendTimeout as error:
            raise DependencyTimeoutError() from error
        except (RetrievalBackendUnavailable, RetrievalUnavailable) as error:
            raise DependencyUnavailableError() from error
        except RetrievalBackendError as error:
            raise ApiRuntimeError() from error
        if not isinstance(result, RetrievalResult):
            raise DependencyUnavailableError()
        if (
            result.trace.tenant_id != principal.tenant_id
            or result.trace.embedding_space_id != embedding.embedding_space_id
            or result.trace.limits != limits
            or result.trace.version_filter != version_filter
            or result.trace.selected_chunk_ids
            != tuple(chunk.citation.chunk_id for chunk in result.chunks)
        ):
            raise DependencyUnavailableError()
        retrieval_ms = _elapsed_ms(retrieval_started, self._monotonic)
        usage_values = _provider_usage(embedding.usage)
        return result, UsageMetadata(
            retrieval_ms=retrieval_ms,
            stages=(("query_embedding", embedding_ms), ("retrieval", retrieval_ms)),
            **usage_values,
        )

    def retrieve(
        self, principal: Principal, request: RetrievalRequest
    ) -> BackendResult:
        result, usage = self._retrieve(
            principal,
            request,
            limits=request.limits.to_domain(),
        )
        payload = asdict(result)
        payload["trace"]["version_filter"] = {
            "document_ids": tuple(sorted(result.trace.version_filter.document_ids)),
            "version_ids": tuple(sorted(result.trace.version_filter.version_ids)),
            "published_at_or_before": result.trace.version_filter.published_at_or_before,
        }
        payload["graph"] = None
        if request.include_graph and self._subgraph_projector is not None:
            try:
                policy = SubgraphTrustPolicy(request.graph_trust_policy)
            except (TypeError, ValueError) as error:
                raise DependencyUnavailableError() from error
            graph_started = float(self._monotonic())
            try:
                if result.trace.selected_chunk_ids:
                    graph = self._subgraph_projector.project(
                        principal,
                        result.trace.selected_chunk_ids,
                        trust_policy=policy,
                        version_filter=result.trace.version_filter,
                    )
                else:
                    graph = EvidenceSubgraph(
                        trust_policy=policy,
                        entities=(),
                        relationship_assertions=(),
                        literal_assertions=(),
                        paths=(),
                        matched_chunk_ids=(),
                        publication_ids=(),
                    )
            except (ApiRuntimeError, KeyboardInterrupt, SystemExit):
                raise
            except TimeoutError as error:
                raise DependencyTimeoutError() from error
            except Exception as error:
                raise DependencyUnavailableError() from error
            if not isinstance(graph, EvidenceSubgraph) or graph.trust_policy is not policy:
                raise DependencyUnavailableError()
            evidence = tuple(
                item
                for entity in graph.entities
                for item in entity.evidence
            ) + tuple(
                item.evidence
                for item in (
                    *graph.relationship_assertions,
                    *graph.literal_assertions,
                    *graph.paths,
                )
            )
            if any(
                entity.entity.tenant_id != principal.tenant_id
                for entity in graph.entities
            ) or any(
                item.citation.tenant_id != principal.tenant_id
                for item in evidence
            ):
                raise DependencyUnavailableError()
            graph_version_filter = result.trace.version_filter
            if any(
                (
                    graph_version_filter.document_ids
                    and item.citation.document_id
                    not in graph_version_filter.document_ids
                )
                or (
                    graph_version_filter.version_ids
                    and item.citation.version_id
                    not in graph_version_filter.version_ids
                )
                or (
                    graph_version_filter.published_at_or_before is not None
                    and (
                        item.citation.published_at is None
                        or item.citation.published_at
                        > graph_version_filter.published_at_or_before
                    )
                )
                for item in evidence
            ):
                raise DependencyUnavailableError()
            try:
                payload["graph"] = _subgraph_payload(graph)
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                raise DependencyUnavailableError() from error
            graph_ms = _elapsed_ms(graph_started, self._monotonic)
            usage = UsageMetadata(
                total_ms=usage.total_ms,
                retrieval_ms=usage.retrieval_ms + graph_ms,
                generation_ms=usage.generation_ms,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                model_calls=usage.model_calls,
                estimated_cost_usd=usage.estimated_cost_usd,
                stages=usage.stages + (("graph_projection", graph_ms),),
            )
        return _response(BackendResult(payload, usage), RetrievalResponse)

    def answer(self, principal: Principal, request: AnswerRequest) -> BackendResult:
        result, retrieval_usage = self._retrieve(
            principal,
            request,
            limits=request.retrieval_limits.to_domain(),
        )
        generation_started = float(self._monotonic())
        generated = self._generate(
            GenerationRequest(
                question=request.query_text,
                chunks=result.chunks,
                limits=request.generation_limits.to_domain(),
            )
        )
        generation_ms = _elapsed_ms(generation_started, self._monotonic)
        # Empty authorized context takes the deterministic refusal path and
        # does not invoke the answer provider.
        answer_usage = generated.usage if result.chunks else ProviderUsage()
        combined = _merge_provider_usage(
            ProviderUsage(
                input_tokens=retrieval_usage.input_tokens,
                output_tokens=retrieval_usage.output_tokens,
                model_calls=retrieval_usage.model_calls,
                estimated_cost_usd=retrieval_usage.estimated_cost_usd,
            ),
            answer_usage,
        )
        return _response(
            BackendResult(
                generated.answer,
                UsageMetadata(
                    retrieval_ms=retrieval_usage.retrieval_ms,
                    generation_ms=generation_ms,
                    stages=retrieval_usage.stages
                    + (("generation", generation_ms),),
                    **_provider_usage(combined),
                ),
            ),
            AnswerResponse,
        )


def _validated(model: type[Any], payload: Mapping[str, Any]) -> Any:
    try:
        return model.model_validate(dict(payload))
    except (TypeError, ValueError, ValidationError) as error:
        raise RequestValidationError() from error


def _trusted_principal(envelope: OperationEnvelope) -> Principal:
    try:
        return Principal(
            principal_id=envelope.principal_id,
            tenant_id=envelope.tenant_id,
            groups=envelope.access_groups,
            capabilities=envelope.scopes,
        )
    except (TypeError, ValueError) as error:
        raise RequestValidationError() from error


def _internal_identifier(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise RequestValidationError()
    return value


def _response(result: BackendResult, model: type[Any]) -> BackendResult:
    if not isinstance(result, BackendResult):
        raise DependencyUnavailableError()
    try:
        payload = model.model_validate(result.payload, from_attributes=True)
    except (TypeError, ValueError, ValidationError) as error:
        raise DependencyUnavailableError() from error
    return BackendResult(payload, result.usage)


class GraphRAGApplicationBackend:
    """Dispatch typed operations while preserving the authenticated scope."""

    def __init__(
        self,
        *,
        documents: DocumentOperations,
        queries: GraphRAGQueryOperations,
        readiness: ReadinessOperations,
        knowledge: KnowledgeOperations | None = None,
    ) -> None:
        for value, methods, name in (
            (documents, ("ingest", "delete", "get_job"), "documents"),
            (queries, ("retrieve", "answer"), "queries"),
            (readiness, ("check",), "readiness"),
        ):
            if any(not callable(getattr(value, method, None)) for method in methods):
                raise TypeError(f"{name} does not implement its required operations")
        self._documents = documents
        self._queries = queries
        self._readiness = readiness
        if knowledge is not None:
            methods = (
                "ontology_list",
                "ontology_import",
                "ontology_publish",
                "authoritative_import",
                "construct",
                "construction_job",
                "construction_jobs",
                "review_queue",
                "revision_history",
                "review_batch",
                "publish",
                "rollback",
                "history",
                "publication_candidates",
            )
            if any(not callable(getattr(knowledge, method, None)) for method in methods):
                raise TypeError("knowledge does not implement its required operations")
        self._knowledge = knowledge

    def execute(self, envelope: OperationEnvelope, /) -> BackendResult:
        if not isinstance(envelope, OperationEnvelope):
            raise RequestValidationError()
        if required_scope(envelope.operation) not in envelope.scopes:
            raise AuthorizationError()
        if envelope.operation is OperationKind.READINESS:
            return _response(self._readiness.check(), ReadinessResponse)

        principal = _trusted_principal(envelope)
        knowledge_operations = {
            OperationKind.ONTOLOGY_LIST,
            OperationKind.ONTOLOGY_IMPORT,
            OperationKind.ONTOLOGY_PUBLISH,
            OperationKind.KNOWLEDGE_IMPORT,
            OperationKind.KNOWLEDGE_CONSTRUCT,
            OperationKind.KNOWLEDGE_CONSTRUCTION_JOB,
            OperationKind.KNOWLEDGE_CONSTRUCTION_JOBS,
            OperationKind.KNOWLEDGE_REVIEW_QUEUE,
            OperationKind.KNOWLEDGE_REVISION_HISTORY,
            OperationKind.KNOWLEDGE_REVIEW_BATCH,
            OperationKind.ENTITY_RESOLUTION_SUGGEST,
            OperationKind.ENTITY_RESOLUTION_APPLY,
            OperationKind.KNOWLEDGE_PUBLISH,
            OperationKind.KNOWLEDGE_ROLLBACK,
            OperationKind.KNOWLEDGE_HISTORY,
            OperationKind.KNOWLEDGE_PUBLICATION_CANDIDATES,
        }
        if envelope.operation in knowledge_operations:
            if self._knowledge is None:
                raise ResourceNotFoundError()
            if envelope.operation is OperationKind.ONTOLOGY_LIST:
                request = _validated(OntologyListRequest, envelope.payload)
                return _response(
                    self._knowledge.ontology_list(principal, request),
                    OntologyListResponse,
                )
            if envelope.operation is OperationKind.ONTOLOGY_IMPORT:
                request = _validated(OntologyImportRequest, envelope.payload)
                return _response(
                    self._knowledge.ontology_import(principal, request),
                    OntologyVersionResponse,
                )
            if envelope.operation is OperationKind.ONTOLOGY_PUBLISH:
                tbox_id = _internal_identifier(envelope.payload.get("tbox_id"))
                request_payload = envelope.payload.get("request")
                if not isinstance(request_payload, Mapping):
                    raise RequestValidationError()
                request = _validated(OntologyPublishRequest, request_payload)
                return _response(
                    self._knowledge.ontology_publish(principal, tbox_id, request),
                    OntologyVersionResponse,
                )
            if envelope.operation is OperationKind.KNOWLEDGE_IMPORT:
                request = _validated(AuthoritativeImportRequest, envelope.payload)
                return _response(
                    self._knowledge.authoritative_import(principal, request),
                    AuthoritativeImportResponse,
                )
            if envelope.operation is OperationKind.KNOWLEDGE_CONSTRUCT:
                request = _validated(KnowledgeConstructionRequest, envelope.payload)
                if not frozenset(request.access_groups).issubset(principal.groups):
                    raise AuthorizationError()
                return _response(
                    self._knowledge.construct(principal, request),
                    KnowledgeConstructionResponse,
                )
            if envelope.operation is OperationKind.KNOWLEDGE_CONSTRUCTION_JOB:
                job_id = _internal_identifier(envelope.payload.get("job_id"))
                return _response(
                    self._knowledge.construction_job(principal, job_id),
                    ConstructionJobResponse,
                )
            if envelope.operation is OperationKind.KNOWLEDGE_CONSTRUCTION_JOBS:
                request = _validated(ConstructionJobListRequest, envelope.payload)
                return _response(
                    self._knowledge.construction_jobs(principal, request),
                    ConstructionJobListResponse,
                )
            if envelope.operation is OperationKind.KNOWLEDGE_REVIEW_QUEUE:
                request = _validated(ReviewQueueRequest, envelope.payload)
                return _response(
                    self._knowledge.review_queue(principal, request),
                    ReviewQueueResponse,
                )
            if envelope.operation is OperationKind.KNOWLEDGE_REVISION_HISTORY:
                record_id = _internal_identifier(envelope.payload.get("record_id"))
                request_payload = envelope.payload.get("request")
                if not isinstance(request_payload, Mapping):
                    raise RequestValidationError()
                request = _validated(RecordRevisionHistoryRequest, request_payload)
                return _response(
                    self._knowledge.revision_history(
                        principal,
                        record_id,
                        request,
                    ),
                    RecordRevisionHistoryResponse,
                )
            if envelope.operation is OperationKind.KNOWLEDGE_REVIEW_BATCH:
                request = _validated(ReviewBatchRequest, envelope.payload)
                return _response(
                    self._knowledge.review_batch(principal, request),
                    ReviewBatchResponse,
                )
            if envelope.operation is OperationKind.ENTITY_RESOLUTION_SUGGEST:
                request = _validated(EntityResolutionRequest, envelope.payload)
                return _response(
                    self._knowledge.resolution_suggestions(principal, request),
                    EntityResolutionResponse,
                )
            if envelope.operation is OperationKind.ENTITY_RESOLUTION_APPLY:
                request = _validated(EntityResolutionApplyRequest, envelope.payload)
                return _response(
                    self._knowledge.apply_resolution(principal, request),
                    EntityResolutionApplyResponse,
                )
            if envelope.operation is OperationKind.KNOWLEDGE_PUBLISH:
                request = _validated(PublicationRequest, envelope.payload)
                return _response(
                    self._knowledge.publish(principal, request),
                    PublicationResponse,
                )
            if envelope.operation is OperationKind.KNOWLEDGE_ROLLBACK:
                publication_id = _internal_identifier(
                    envelope.payload.get("publication_id")
                )
                request_payload = envelope.payload.get("request")
                if not isinstance(request_payload, Mapping):
                    raise RequestValidationError()
                request = _validated(RollbackRequest, request_payload)
                return _response(
                    self._knowledge.rollback(principal, publication_id, request),
                    PublicationResponse,
                )
            if envelope.operation is OperationKind.KNOWLEDGE_PUBLICATION_CANDIDATES:
                request = _validated(PublicationCandidatesRequest, envelope.payload)
                return _response(
                    self._knowledge.publication_candidates(principal, request),
                    PublicationCandidatesResponse,
                )
            request = _validated(PublicationHistoryRequest, envelope.payload)
            return _response(
                self._knowledge.history(principal, request),
                PublicationHistoryResponse,
            )
        if envelope.operation is OperationKind.INGESTION:
            request = _validated(IngestionRequest, envelope.payload)
            # Defense in depth: a caller may grant only groups already held by
            # the authenticated principal.  A controller performs the same
            # check before any worker is scheduled.
            if not frozenset(request.access_groups).issubset(principal.groups):
                raise AuthorizationError()
            return _response(
                self._documents.ingest(principal, request),
                IngestionResponse,
            )
        if envelope.operation is OperationKind.DELETION:
            document_id = _internal_identifier(envelope.payload.get("document_id"))
            request_payload = envelope.payload.get("request")
            if not isinstance(request_payload, Mapping):
                raise RequestValidationError()
            request = _validated(DeleteRequest, request_payload)
            return _response(
                self._documents.delete(principal, document_id, request),
                DeleteResponse,
            )
        if envelope.operation is OperationKind.JOB_STATUS:
            job_id = _internal_identifier(envelope.payload.get("job_id"))
            return _response(self._documents.get_job(principal, job_id), JobResponse)
        if envelope.operation is OperationKind.RETRIEVAL:
            request = _validated(RetrievalRequest, envelope.payload)
            result = _response(
                self._queries.retrieve(principal, request),
                RetrievalResponse,
            )
            if result.payload.trace.tenant_id != principal.tenant_id:
                raise DependencyUnavailableError()
            if not request.include_graph and result.payload.graph is not None:
                raise DependencyUnavailableError()
            if (
                result.payload.graph is not None
                and result.payload.graph.trust_policy
                != request.graph_trust_policy
            ):
                raise DependencyUnavailableError()
            return result
        if envelope.operation is OperationKind.ANSWER:
            request = _validated(AnswerRequest, envelope.payload)
            return _response(
                self._queries.answer(principal, request),
                AnswerResponse,
            )
        raise RequestValidationError("operation is not available through this backend")


__all__ = [
    "DocumentOperations",
    "EvidenceSubgraphProjector",
    "GeneratedAnswer",
    "GraphRAGApplicationBackend",
    "GraphRAGQueryOperations",
    "MeteredGenerationService",
    "IncrementalIngestionPlanner",
    "KnowledgeOperations",
    "Neo4jDocumentOperations",
    "ProviderUsage",
    "QueryEmbedder",
    "QueryEmbedding",
    "ReadinessOperations",
]
