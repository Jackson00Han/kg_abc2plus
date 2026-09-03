"""Security and contract tests for the authenticated GraphRAG backend."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
import hashlib
import math
from numbers import Real
import unittest

from graphrag_prod.api.backend import (
    GeneratedAnswer,
    GraphRAGApplicationBackend,
    GraphRAGQueryOperations,
    ProviderUsage,
    QueryEmbedding,
)
from graphrag_prod.api.contracts import (
    AnswerRequest as APIAnswerRequest,
    AnswerResponse,
    RetrievalRequest as APIRetrievalRequest,
    RetrievalResponse,
)
from graphrag_prod.api.runtime import (
    AuthorizationError,
    BackendResult,
    DependencyTimeoutError,
    DependencyUnavailableError,
    OperationEnvelope,
    OperationKind,
    RequestValidationError,
    UsageMetadata,
    required_scope,
)
from graphrag_prod.domain import Principal, TypedLiteralValue
from graphrag_prod.domain.ids import entity_id as make_entity_id
from graphrag_prod.generation import (
    AnswerCitation,
    AnswerResult,
    AnswerStatus,
    Claim,
    GroundedGenerationService,
)
from graphrag_prod.retrieval import (
    Citation,
    EvidenceSubgraph,
    RetrievalLimits,
    RetrievalResult,
    RetrievalTrace,
    RetrievedChunk,
    SubgraphAssertion,
    SubgraphCitation,
    SubgraphEntityNode,
    SubgraphEvidence,
    SubgraphPath,
    SubgraphProvenance,
    SubgraphTrustPolicy,
    VersionFilter,
)
from graphrag_prod.knowledge import EntityIdentity
from graphrag_prod.knowledge.trust import (
    AuthorityLevel,
    GovernanceStatus,
    KnowledgeOrigin,
)
from graphrag_prod.retrieval.models import RetrievalRequest as DomainRetrievalRequest


def _citation(*, checksum: str | None = None) -> Citation:
    text = "Acme reported revenue of USD 42 million in 2025."
    return Citation(
        chunk_id="chunk-001",
        chunk_checksum=checksum or hashlib.sha256(text.encode()).hexdigest(),
        document_id="document-001",
        canonical_uri="https://example.test/filing",
        source_name="Acme filing",
        version_id="version-001",
        version_checksum="b" * 64,
        version_number=1,
        ordinal=0,
        char_start=0,
        char_end=len(text),
        page_number=1,
        section="Results",
        document_title="Acme annual filing",
        published_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


def _chunk(*, checksum: str | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        text="Acme reported revenue of USD 42 million in 2025.",
        citation=_citation(checksum=checksum),
        role="ranked",
        score=0.91,
        reasons=("vector_recall", "bm25_recall"),
    )


def _retrieval_result(
    *,
    chunks: tuple[RetrievedChunk, ...] | None = None,
    tenant_id: str = "tenant-alpha",
) -> RetrievalResult:
    selected = (_chunk(),) if chunks is None else chunks
    limits = RetrievalLimits()
    trace = RetrievalTrace(
        trace_id="retrieval-trace-001",
        method="vector+bm25+rrf+ra+adjacency",
        tenant_id=tenant_id,
        corpus_revision=7,
        embedding_generation_id="embedding-generation-001",
        embedding_space_id="space-v1",
        vector_recall=(),
        bm25_recall=(),
        seed_ranking=(),
        graph_expansion=(),
        candidate_vector_ranking=(),
        final_ranking=(),
        decisions=(),
        selected_chunk_ids=tuple(chunk.citation.chunk_id for chunk in selected),
        context_chars=sum(len(chunk.text) for chunk in selected),
        limits=limits,
        version_filter=VersionFilter(),
    )
    return RetrievalResult(chunks=selected, trace=trace)


def _evidence_subgraph(
    *,
    tenant_id: str = "tenant-alpha",
    trust_policy: SubgraphTrustPolicy = (
        SubgraphTrustPolicy.PUBLISHED_SECONDARY_INCLUSIVE
    ),
) -> EvidenceSubgraph:
    text = "Acme reported revenue of USD 42 million in 2025."
    citation = SubgraphCitation(
        tenant_id=tenant_id,
        chunk_id="chunk-001",
        chunk_checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        chunk_text=text,
        document_id="document-001",
        document_title="Acme annual filing",
        canonical_uri="https://example.test/filing",
        source_name="Acme filing",
        version_id="version-001",
        version_checksum="b" * 64,
        version_number=1,
        ordinal=0,
        char_start=0,
        char_end=len(text),
        page_number=1,
        section="Results",
        published_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    authority = (
        AuthorityLevel.AUTHORITATIVE
        if trust_policy is SubgraphTrustPolicy.AUTHORITATIVE_ONLY
        else AuthorityLevel.SECONDARY
    )
    origin = (
        KnowledgeOrigin.EXPERT_IMPORT
        if authority is AuthorityLevel.AUTHORITATIVE
        else KnowledgeOrigin.LLM_EXTRACTED
    )

    def evidence(record_id: str, revision_id: str) -> SubgraphEvidence:
        return SubgraphEvidence(
            citation=citation,
            char_start=0,
            char_end=len(text),
            quoted_text=text,
            provenance=SubgraphProvenance(
                publication_id="publication-001",
                record_id=record_id,
                revision_id=revision_id,
                ontology_version_id="ontology-001",
                origin=origin,
                authority=authority,
                status=GovernanceStatus.PUBLISHED,
                confidence=0.97,
                extractor_version=(
                    None if authority is AuthorityLevel.AUTHORITATIVE else "extractor-v1"
                ),
                prompt_version=(
                    None if authority is AuthorityLevel.AUTHORITATIVE else "prompt-v1"
                ),
            ),
        )

    company = EntityIdentity(
        entity_id=make_entity_id(tenant_id, "Company", "acme"),
        tenant_id=tenant_id,
        entity_type="Company",
        canonical_key="acme",
        canonical_name="Acme",
    )
    metric = EntityIdentity(
        entity_id=make_entity_id(tenant_id, "Metric", "revenue"),
        tenant_id=tenant_id,
        entity_type="Metric",
        canonical_key="revenue",
        canonical_name="Revenue",
    )
    company_mention = evidence("mention-acme", "mention-acme-r1")
    metric_mention = evidence("mention-revenue", "mention-revenue-r1")
    relationship_evidence = evidence("assertion-reports", "assertion-reports-r1")
    literal_evidence = evidence("assertion-revenue", "assertion-revenue-r1")
    literal_semantics = TypedLiteralValue(
        datatype="STRING",
        typed_value="USD 42 million",
        raw_value="USD 42 million",
        canonical_value="USD 42 million",
    )
    relationship = SubgraphAssertion(
        record_id="assertion-reports",
        revision_id="assertion-reports-r1",
        predicate="REPORTS",
        subject_entity_id=company.entity_id,
        subject_mention_revision_id="mention-acme-r1",
        object_kind="entity",
        object_entity_id=metric.entity_id,
        object_mention_revision_id="mention-revenue-r1",
        literal_value=None,
        evidence=relationship_evidence,
    )
    literal = SubgraphAssertion(
        record_id="assertion-revenue",
        revision_id="assertion-revenue-r1",
        predicate="REVENUE",
        subject_entity_id=company.entity_id,
        subject_mention_revision_id="mention-acme-r1",
        object_kind="literal",
        object_entity_id=None,
        object_mention_revision_id=None,
        literal_value="USD 42 million",
        evidence=literal_evidence,
        literal_semantics=literal_semantics,
    )
    return EvidenceSubgraph(
        trust_policy=trust_policy,
        entities=(
            SubgraphEntityNode(company, (company_mention,)),
            SubgraphEntityNode(metric, (metric_mention,)),
        ),
        relationship_assertions=(relationship,),
        literal_assertions=(literal,),
        paths=(
            SubgraphPath(
                subject_entity_id=company.entity_id,
                assertion_revision_id=relationship.revision_id,
                predicate=relationship.predicate,
                object_entity_id=metric.entity_id,
                literal_value=None,
                evidence=relationship_evidence,
            ),
            SubgraphPath(
                subject_entity_id=company.entity_id,
                assertion_revision_id=literal.revision_id,
                predicate=literal.predicate,
                object_entity_id=None,
                literal_value=literal.literal_value,
                evidence=literal_evidence,
                literal_semantics=literal_semantics,
            ),
        ),
        matched_chunk_ids=("chunk-001",),
        publication_ids=("publication-001",),
    )


def _answered_result() -> AnswerResult:
    citation = AnswerCitation.from_retrieval("S1", _citation())
    claim_text = "Acme reported revenue of USD 42 million in 2025."
    return AnswerResult(
        status=AnswerStatus.ANSWERED,
        answer=f"{claim_text} [S1]",
        claims=(
            Claim(
                text=claim_text,
                material=True,
                citation_ids=("S1",),
                inference=False,
            ),
        ),
        citations=(citation,),
    )


def _job_payload() -> dict[str, object]:
    return {
        "job_id": "job-001",
        "operation": "INGEST",
        "status": "SUCCEEDED",
        "phase": "COMPLETE",
        "document_id": "document-001",
        "target_version_id": "version-001",
        "target_snapshot_id": "snapshot-001",
        "expected_active_snapshot_id": None,
        "source_generation": 1,
        "attempts": 1,
        "max_attempts": 3,
        "completed_tasks": 1,
        "expected_tasks": 1,
        "outcome": "created",
        "last_error_code": None,
    }


def _ingestion_payload(*, access_groups: tuple[str, ...] = ("finance",)) -> dict[str, object]:
    return {
        "operation_key": "operation-key-0001",
        "canonical_uri": "https://example.test/filing",
        "title": "Acme annual filing",
        "source_name": "Acme filing",
        "content": "Acme reported revenue of USD 42 million in 2025.",
        "access_policy_id": "policy-001",
        "access_policy_version": 1,
        "access_groups": access_groups,
        "source_generation": 1,
    }


def _envelope(
    operation: OperationKind,
    payload: dict[str, object],
    *,
    tenant_id: str = "tenant-alpha",
    groups: frozenset[str] = frozenset({"finance", "shared"}),
) -> OperationEnvelope:
    return OperationEnvelope(
        operation=operation,
        request_id="request-001",
        trace_id="trace-001",
        principal_id="principal-001",
        tenant_id=tenant_id,
        access_groups=groups,
        scopes=frozenset({required_scope(operation)}),
        payload=payload,
    )


class RecordingEmbedder:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def embed(self, query_text: str, *, tenant_id: str) -> object:
        self.calls.append((query_text, tenant_id))
        return self.result


class RecordingRetrievalEngine:
    def __init__(self, result: object) -> None:
        self.result = result
        self.requests: list[DomainRetrievalRequest] = []

    def retrieve(self, request: DomainRetrievalRequest) -> object:
        self.requests.append(request)
        return self.result


class RecordingSubgraphProjector:
    def __init__(self, result: object | None = None) -> None:
        self.result = result
        self.calls: list[tuple[Principal, tuple[str, ...], SubgraphTrustPolicy]] = []
        self.error: BaseException | None = None

    def project(
        self,
        principal: Principal,
        selected_chunk_ids: tuple[str, ...],
        *,
        trust_policy: SubgraphTrustPolicy,
    ) -> object:
        self.calls.append((principal, selected_chunk_ids, trust_policy))
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        return _evidence_subgraph(trust_policy=trust_policy)


class RecordingGenerationService:
    def __init__(
        self,
        result: object,
        usage: ProviderUsage = ProviderUsage(
            input_tokens=20,
            output_tokens=8,
            model_calls=1,
            estimated_cost_usd=0.03,
        ),
    ) -> None:
        self.result = result
        self.usage = usage
        self.requests: list[object] = []

    def generate(self, request: object) -> object:
        self.requests.append(request)
        return self.result

    def generate_with_usage(self, request: object) -> object:
        self.requests.append(request)
        if not isinstance(self.result, AnswerResult):
            return self.result
        return GeneratedAnswer(self.result, self.usage)


class SequenceClock:
    def __init__(self, *values: Real) -> None:
        self._values = iter(float(value) for value in values)

    def __call__(self) -> float:
        return next(self._values)


class RecordingAnswerModel:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def generate(self, request: object) -> object:
        self.requests.append(request)
        raise AssertionError("the answer provider must not receive empty context")


class ProviderBoundaryTests(unittest.TestCase):
    def test_provider_usage_normalizes_cost_and_accepts_zero(self) -> None:
        usage = ProviderUsage(
            input_tokens=12,
            output_tokens=3,
            model_calls=2,
            estimated_cost_usd=1,
        )
        self.assertEqual(usage.estimated_cost_usd, 1.0)
        self.assertEqual(ProviderUsage(), ProviderUsage(0, 0, 0, 0.0))

    def test_provider_usage_rejects_invalid_counts_and_costs(self) -> None:
        for field, value in (
            ("input_tokens", True),
            ("input_tokens", -1),
            ("output_tokens", 1.5),
            ("model_calls", False),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    ProviderUsage(**{field: value})
        for value in (True, -0.01, math.nan, math.inf, -math.inf):
            with self.subTest(cost=value):
                with self.assertRaises(ValueError):
                    ProviderUsage(estimated_cost_usd=value)

    def test_query_embedding_normalizes_values_space_and_usage(self) -> None:
        usage = ProviderUsage(input_tokens=4, model_calls=1)
        embedding = QueryEmbedding((1, -2.5), "  space-v1  ", usage)
        self.assertEqual(embedding.vector, (1.0, -2.5))
        self.assertEqual(embedding.embedding_space_id, "space-v1")
        self.assertIs(embedding.usage, usage)

    def test_query_embedding_rejects_empty_zero_nonfinite_and_boolean_vectors(self) -> None:
        for vector in ((), (0.0, 0), (math.nan,), (math.inf,), (True, 1.0)):
            with self.subTest(vector=vector):
                with self.assertRaises(ValueError):
                    QueryEmbedding(vector, "space-v1")

    def test_query_embedding_rejects_invalid_space_or_usage(self) -> None:
        for space_id in ("", "   ", "space\x00v1"):
            with self.subTest(space_id=space_id):
                with self.assertRaises(ValueError):
                    QueryEmbedding((1.0,), space_id)
        with self.assertRaises(TypeError):
            QueryEmbedding((1.0,), "space-v1", usage=object())  # type: ignore[arg-type]


class QueryOperationsTests(unittest.TestCase):
    def _operations(
        self,
        *,
        retrieval_result: object | None = None,
        embedding_result: object | None = None,
        answer_result: object | None = None,
        clock: SequenceClock | None = None,
        subgraph_projector: object | None = None,
        answer_usage: ProviderUsage = ProviderUsage(
            input_tokens=20,
            output_tokens=8,
            model_calls=1,
            estimated_cost_usd=0.03,
        ),
    ) -> tuple[
        GraphRAGQueryOperations,
        RecordingEmbedder,
        RecordingRetrievalEngine,
        RecordingGenerationService,
    ]:
        embedder = RecordingEmbedder(
            QueryEmbedding(
                (0.5, -0.25),
                "space-v1",
                ProviderUsage(
                    input_tokens=5,
                    model_calls=1,
                    estimated_cost_usd=0.01,
                ),
            )
            if embedding_result is None
            else embedding_result
        )
        engine = RecordingRetrievalEngine(
            _retrieval_result()
            if retrieval_result is None
            else retrieval_result
        )
        generation = RecordingGenerationService(
            _answered_result() if answer_result is None else answer_result,
            answer_usage,
        )
        operations = GraphRAGQueryOperations(
            engine,  # type: ignore[arg-type]
            embedder,  # type: ignore[arg-type]
            generation,  # type: ignore[arg-type]
            subgraph_projector=subgraph_projector,  # type: ignore[arg-type]
            monotonic=clock or SequenceClock(1, 1, 2, 2, 3, 3),
        )
        return operations, embedder, engine, generation

    def test_retrieve_embeds_for_jwt_tenant_and_builds_domain_principal(self) -> None:
        operations, embedder, engine, _ = self._operations(
            clock=SequenceClock(1.0, 1.004, 2.0, 2.012)
        )
        principal = Principal(
            principal_id="principal-001",
            tenant_id="tenant-alpha",
            groups=frozenset({"finance"}),
        )
        result = operations.retrieve(
            principal,
            APIRetrievalRequest(query_text="What was revenue?"),
        )

        self.assertIsInstance(result.payload, RetrievalResponse)
        self.assertEqual(embedder.calls, [("What was revenue?", "tenant-alpha")])
        self.assertEqual(len(engine.requests), 1)
        domain_request = engine.requests[0]
        self.assertIs(domain_request.principal, principal)
        self.assertEqual(domain_request.principal.tenant_id, "tenant-alpha")
        self.assertEqual(domain_request.principal.groups, frozenset({"finance"}))
        self.assertEqual(domain_request.query_vector, (0.5, -0.25))
        self.assertEqual(domain_request.query_embedding_space_id, "space-v1")
        self.assertEqual(result.usage.input_tokens, 5)
        self.assertEqual(result.usage.model_calls, 1)
        self.assertAlmostEqual(result.usage.estimated_cost_usd, 0.01)
        self.assertAlmostEqual(result.usage.retrieval_ms, 12.0)
        self.assertEqual(
            tuple(name for name, _ in result.usage.stages),
            ("query_embedding", "retrieval"),
        )
        self.assertAlmostEqual(result.usage.stages[0][1], 4.0)
        self.assertIsNone(result.payload.graph)

    def test_retrieve_projects_governed_graph_from_exact_selected_chunks(self) -> None:
        projector = RecordingSubgraphProjector()
        operations, _, _, _ = self._operations(
            subgraph_projector=projector,
            clock=SequenceClock(1.0, 1.004, 2.0, 2.012, 3.0, 3.006),
        )
        principal = Principal(
            "principal-001", "tenant-alpha", frozenset({"finance"})
        )
        result = operations.retrieve(
            principal,
            APIRetrievalRequest(query_text="What connects Acme and revenue?"),
        )

        self.assertEqual(
            projector.calls,
            [
                (
                    principal,
                    ("chunk-001",),
                    SubgraphTrustPolicy.PUBLISHED_SECONDARY_INCLUSIVE,
                )
            ],
        )
        graph = result.payload.graph
        self.assertIsNotNone(graph)
        assert graph is not None
        self.assertEqual(len(graph.entities), 2)
        self.assertEqual(len(graph.relationship_assertions), 1)
        self.assertEqual(len(graph.literal_assertions), 1)
        semantics = graph.literal_assertions[0].literal_semantics
        self.assertIsNotNone(semantics)
        assert semantics is not None
        self.assertEqual(semantics.datatype, "STRING")
        self.assertEqual(semantics.raw_value, "USD 42 million")
        self.assertEqual(semantics.canonical_value, "USD 42 million")
        self.assertEqual(graph.paths[1].literal_semantics, semantics)
        self.assertEqual(len(graph.paths), 2)
        self.assertEqual(graph.matched_chunk_ids, ("chunk-001",))
        graph_json = graph.model_dump_json()
        self.assertNotIn("tenant-alpha", graph_json)
        self.assertNotIn("tenant_id", graph_json)
        self.assertEqual(result.usage.input_tokens, 5)
        self.assertEqual(result.usage.model_calls, 1)
        self.assertAlmostEqual(result.usage.retrieval_ms, 18.0)
        self.assertEqual(
            tuple(name for name, _ in result.usage.stages),
            ("query_embedding", "retrieval", "graph_projection"),
        )
        self.assertAlmostEqual(result.usage.stages[-1][1], 6.0)

    def test_graph_projection_can_be_disabled_or_authoritative_only(self) -> None:
        disabled_projector = RecordingSubgraphProjector()
        operations, *_ = self._operations(
            subgraph_projector=disabled_projector,
        )
        principal = Principal(
            "principal-001", "tenant-alpha", frozenset({"finance"})
        )
        disabled = operations.retrieve(
            principal,
            APIRetrievalRequest(query_text="Revenue?", include_graph=False),
        )
        self.assertIsNone(disabled.payload.graph)
        self.assertEqual(disabled_projector.calls, [])
        self.assertEqual(
            tuple(name for name, _ in disabled.usage.stages),
            ("query_embedding", "retrieval"),
        )

        authoritative_projector = RecordingSubgraphProjector()
        operations, *_ = self._operations(
            subgraph_projector=authoritative_projector,
        )
        authoritative = operations.retrieve(
            principal,
            APIRetrievalRequest(
                query_text="Revenue?",
                graph_trust_policy="AUTHORITATIVE_ONLY",
            ),
        )
        self.assertEqual(
            authoritative_projector.calls[0][2],
            SubgraphTrustPolicy.AUTHORITATIVE_ONLY,
        )
        self.assertEqual(
            authoritative.payload.graph.trust_policy,
            "AUTHORITATIVE_ONLY",
        )

    def test_configured_projection_distinguishes_empty_graph_from_unavailable(self) -> None:
        projector = RecordingSubgraphProjector()
        operations, *_ = self._operations(
            retrieval_result=_retrieval_result(chunks=()),
            subgraph_projector=projector,
        )
        principal = Principal(
            "principal-001", "tenant-alpha", frozenset({"finance"})
        )
        result = operations.retrieve(
            principal,
            APIRetrievalRequest(query_text="Unknown relationship?"),
        )
        self.assertIsNotNone(result.payload.graph)
        self.assertEqual(result.payload.graph.entities, ())
        self.assertEqual(result.payload.graph.paths, ())
        self.assertEqual(projector.calls, [])
        self.assertEqual(result.usage.stages[-1][0], "graph_projection")

    def test_graph_projector_failures_and_cross_tenant_output_fail_closed(self) -> None:
        principal = Principal(
            "principal-001", "tenant-alpha", frozenset({"finance"})
        )
        request = APIRetrievalRequest(query_text="Revenue?")
        for error, expected in (
            (TimeoutError("provider details"), DependencyTimeoutError),
            (RuntimeError("driver details"), DependencyUnavailableError),
        ):
            with self.subTest(error=type(error).__name__):
                projector = RecordingSubgraphProjector()
                projector.error = error
                operations, *_ = self._operations(
                    subgraph_projector=projector,
                )
                with self.assertRaises(expected):
                    operations.retrieve(principal, request)
        for invalid in (
            {"entities": []},
            _evidence_subgraph(tenant_id="tenant-victim"),
        ):
            with self.subTest(output=type(invalid).__name__):
                operations, *_ = self._operations(
                    subgraph_projector=RecordingSubgraphProjector(invalid),
                )
                with self.assertRaises(DependencyUnavailableError):
                    operations.retrieve(principal, request)

    def test_answer_never_projects_graph_as_factual_evidence(self) -> None:
        projector = RecordingSubgraphProjector()
        operations, _, _, generation = self._operations(
            subgraph_projector=projector,
        )
        principal = Principal(
            "principal-001", "tenant-alpha", frozenset({"finance"})
        )
        result = operations.answer(
            principal,
            APIAnswerRequest(query_text="What was revenue?"),
        )
        self.assertEqual(result.payload.status, "answered")
        self.assertEqual(projector.calls, [])
        self.assertEqual(generation.requests[0].chunks, (_chunk(),))

    def test_retrieve_accepts_bounded_resource_allocation_explanations(self) -> None:
        baseline = _retrieval_result()
        reason = (
            "RA from seed 00000000-0000-0000-0000-000000000001 rank 1; "
            "shared entities "
            + ",".join(
                f"00000000-0000-0000-0000-{index:012d}" for index in range(20)
            )
        )
        self.assertGreater(len(reason), 512)
        self.assertLessEqual(len(reason), 2_048)
        chunk = replace(baseline.chunks[0], reasons=(reason,))
        operations, *_ = self._operations(
            retrieval_result=RetrievalResult(
                chunks=(chunk,),
                trace=baseline.trace,
            )
        )
        principal = Principal("principal-001", "tenant-alpha", frozenset({"finance"}))

        result = operations.retrieve(
            principal,
            APIRetrievalRequest(query_text="What was revenue?"),
        )

        self.assertEqual(result.payload.chunks[0].reasons, (reason,))

    def test_retrieve_preserves_exact_chunk_boundary_whitespace(self) -> None:
        baseline = _retrieval_result()
        text = "\n  Exact source text with boundary whitespace.  \n"
        citation = replace(
            baseline.chunks[0].citation,
            chunk_checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            char_start=41,
            char_end=41 + len(text),
        )
        chunk = replace(baseline.chunks[0], text=text, citation=citation)
        trace = replace(
            baseline.trace,
            context_chars=len(text),
        )
        operations, *_ = self._operations(
            retrieval_result=RetrievalResult(chunks=(chunk,), trace=trace)
        )
        principal = Principal("principal-001", "tenant-alpha", frozenset({"finance"}))

        result = operations.retrieve(
            principal,
            APIRetrievalRequest(query_text="What was the exact source text?"),
        )

        returned = result.payload.chunks[0]
        self.assertEqual(returned.text, text)
        self.assertEqual(
            returned.citation.char_end - returned.citation.char_start,
            len(text),
        )
        self.assertEqual(
            returned.citation.chunk_checksum,
            hashlib.sha256(returned.text.encode("utf-8")).hexdigest(),
        )

    def test_answer_returns_contract_and_combined_usage_with_all_stages(self) -> None:
        operations, _, _, generation = self._operations(
            clock=SequenceClock(1.0, 1.002, 2.0, 2.007, 3.0, 3.011)
        )
        principal = Principal("principal-001", "tenant-alpha", frozenset({"finance"}))
        request = APIAnswerRequest(query_text="What was revenue?")
        result = operations.answer(principal, request)

        self.assertIsInstance(result.payload, AnswerResponse)
        self.assertEqual(result.payload.status, "answered")
        self.assertEqual(len(generation.requests), 1)
        generation_request = generation.requests[0]
        self.assertEqual(generation_request.question, "What was revenue?")
        self.assertEqual(generation_request.chunks, (_chunk(),))
        self.assertEqual(result.usage.input_tokens, 25)
        self.assertEqual(result.usage.output_tokens, 8)
        self.assertEqual(result.usage.model_calls, 2)
        self.assertAlmostEqual(result.usage.estimated_cost_usd, 0.04)
        self.assertAlmostEqual(result.usage.retrieval_ms, 7.0)
        self.assertAlmostEqual(result.usage.generation_ms, 11.0)
        self.assertEqual(
            tuple(name for name, _ in result.usage.stages),
            ("query_embedding", "retrieval", "generation"),
        )

    def test_empty_authorized_context_does_not_count_answer_provider(self) -> None:
        answer_model = RecordingAnswerModel()
        embedder = RecordingEmbedder(
            QueryEmbedding(
                (0.5, -0.25),
                "space-v1",
                ProviderUsage(
                    input_tokens=5,
                    model_calls=1,
                    estimated_cost_usd=0.01,
                ),
            )
        )
        engine = RecordingRetrievalEngine(_retrieval_result(chunks=()))
        operations = GraphRAGQueryOperations(
            engine,  # type: ignore[arg-type]
            embedder,  # type: ignore[arg-type]
            GroundedGenerationService(answer_model),  # type: ignore[arg-type]
            monotonic=SequenceClock(1, 1, 2, 2, 3, 3),
        )
        principal = Principal("principal-001", "tenant-alpha", frozenset({"finance"}))
        request = APIAnswerRequest(query_text="Unknown fact?")
        result = operations.answer(principal, request)

        self.assertEqual(result.payload.status, "insufficient_context")
        self.assertEqual(answer_model.requests, [])
        self.assertEqual(result.usage.input_tokens, 5)
        self.assertEqual(result.usage.output_tokens, 0)
        self.assertEqual(result.usage.model_calls, 1)
        self.assertAlmostEqual(result.usage.estimated_cost_usd, 0.01)

    def test_invalid_provider_and_downstream_types_fail_closed(self) -> None:
        principal = Principal("principal-001", "tenant-alpha", frozenset({"finance"}))
        retrieval_request = APIRetrievalRequest(query_text="Revenue?")
        answer_request = APIAnswerRequest(query_text="Revenue?")
        operations, *_ = self._operations(embedding_result={"vector": [1.0]})
        with self.assertRaises(DependencyUnavailableError):
            operations.retrieve(principal, retrieval_request)
        operations, *_ = self._operations(retrieval_result={"chunks": []})
        with self.assertRaises(DependencyUnavailableError):
            operations.retrieve(principal, retrieval_request)
        operations, *_ = self._operations(answer_result={"status": "answered"})
        with self.assertRaises(DependencyUnavailableError):
            operations.answer(principal, answer_request)

    def test_malformed_retrieval_payload_fails_as_dependency_error(self) -> None:
        malformed = _retrieval_result(chunks=(_chunk(checksum="not-a-checksum"),))
        operations, *_ = self._operations(retrieval_result=malformed)
        principal = Principal("principal-001", "tenant-alpha", frozenset({"finance"}))
        request = APIRetrievalRequest(query_text="Revenue?")
        with self.assertRaises(DependencyUnavailableError):
            operations.retrieve(principal, request)

    def test_retrieval_trace_must_match_server_owned_scope_and_request(self) -> None:
        baseline = _retrieval_result()
        mismatched_traces = {
            "tenant": replace(baseline.trace, tenant_id="tenant-other"),
            "embedding_space": replace(
                baseline.trace,
                embedding_space_id="attacker-space",
            ),
            "limits": replace(
                baseline.trace,
                limits=replace(baseline.trace.limits, adjacent_window=2),
            ),
            "version_filter": replace(
                baseline.trace,
                version_filter=VersionFilter(document_ids=frozenset({"other-doc"})),
            ),
            "selected_chunks": replace(baseline.trace, selected_chunk_ids=()),
        }
        principal = Principal("principal-001", "tenant-alpha", frozenset({"finance"}))
        request = APIRetrievalRequest(query_text="Revenue?")
        for invariant, trace in mismatched_traces.items():
            with self.subTest(invariant=invariant):
                operations, *_ = self._operations(
                    retrieval_result=RetrievalResult(
                        chunks=baseline.chunks,
                        trace=trace,
                    )
                )
                with self.assertRaises(DependencyUnavailableError):
                    operations.retrieve(principal, request)


class RecordingDocuments:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.invalid_result: object | None = None

    def ingest(self, principal: Principal, request: object) -> object:
        self.calls.append(("ingest", principal, request))
        if self.invalid_result is not None:
            return self.invalid_result
        return BackendResult(
            {
                "job": _job_payload(),
                "snapshot_id": "snapshot-001",
                "active_snapshot_id": "snapshot-001",
            },
            UsageMetadata(total_ms=2),
        )

    def delete(self, principal: Principal, document_id: str, request: object) -> object:
        self.calls.append(("delete", principal, document_id, request))
        if self.invalid_result is not None:
            return self.invalid_result
        return BackendResult({"job": _job_payload()})

    def get_job(self, principal: Principal, job_id: str) -> object:
        self.calls.append(("job", principal, job_id))
        if self.invalid_result is not None:
            return self.invalid_result
        return BackendResult(_job_payload())


class RecordingQueries:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        retrieval = _retrieval_result()
        self.retrieval_result: object = BackendResult(
            RetrievalResponse.model_validate(asdict(retrieval), strict=False)
        )
        self.answer_result: object = BackendResult(
            AnswerResponse.model_validate(_answered_result().as_dict(), strict=False)
        )

    def retrieve(self, principal: Principal, request: object) -> object:
        self.calls.append(("retrieve", principal, request))
        return self.retrieval_result

    def answer(self, principal: Principal, request: object) -> object:
        self.calls.append(("answer", principal, request))
        return self.answer_result


class RecordingReadiness:
    def __init__(self) -> None:
        self.calls = 0
        self.result: object = BackendResult(
            {"status": "ready", "checks": {"neo4j": "ok"}}
        )

    def check(self) -> object:
        self.calls += 1
        return self.result


class ApplicationBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = RecordingDocuments()
        self.queries = RecordingQueries()
        self.readiness = RecordingReadiness()
        self.backend = GraphRAGApplicationBackend(
            documents=self.documents,  # type: ignore[arg-type]
            queries=self.queries,  # type: ignore[arg-type]
            readiness=self.readiness,  # type: ignore[arg-type]
        )

    def test_dispatches_all_supported_operations_with_trusted_principal(self) -> None:
        cases = (
            (OperationKind.INGESTION, _ingestion_payload(), "ingest"),
            (
                OperationKind.DELETION,
                {
                    "document_id": "document-001",
                    "request": {"operation_key": "operation-key-0002"},
                },
                "delete",
            ),
            (OperationKind.JOB_STATUS, {"job_id": "job-001"}, "job"),
            (OperationKind.RETRIEVAL, {"query_text": "Revenue?"}, "retrieve"),
            (OperationKind.ANSWER, {"query_text": "Revenue?"}, "answer"),
        )
        for operation, payload, expected in cases:
            with self.subTest(operation=operation):
                before_documents = len(self.documents.calls)
                before_queries = len(self.queries.calls)
                result = self.backend.execute(_envelope(operation, payload))
                self.assertIsInstance(result, BackendResult)
                calls = (
                    self.documents.calls[before_documents:]
                    + self.queries.calls[before_queries:]
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], expected)
                principal = calls[0][1]
                self.assertIsInstance(principal, Principal)
                self.assertEqual(principal.principal_id, "principal-001")
                self.assertEqual(principal.tenant_id, "tenant-alpha")
                self.assertEqual(principal.groups, frozenset({"finance", "shared"}))

        ready = self.backend.execute(
            _envelope(OperationKind.READINESS, {}, groups=frozenset())
        )
        self.assertEqual(ready.payload.status, "ready")
        self.assertEqual(self.readiness.calls, 1)

    def test_client_cannot_inject_tenant_principal_groups_or_query_vector(self) -> None:
        forbidden = (
            {"query_text": "Revenue?", "tenant_id": "tenant-other"},
            {"query_text": "Revenue?", "principal_id": "admin"},
            {"query_text": "Revenue?", "access_groups": ["admin"]},
            {"query_text": "Revenue?", "query_vector": [1.0, 2.0]},
            {"query_text": "Revenue?", "embedding_space_id": "attacker-space"},
        )
        for payload in forbidden:
            for operation in (OperationKind.RETRIEVAL, OperationKind.ANSWER):
                with self.subTest(operation=operation, field=set(payload) - {"query_text"}):
                    with self.assertRaises(RequestValidationError):
                        self.backend.execute(_envelope(operation, payload))
        self.assertEqual(self.queries.calls, [])

    def test_backend_rechecks_ingestion_acl_before_document_operation(self) -> None:
        with self.assertRaises(AuthorizationError):
            self.backend.execute(
                _envelope(
                    OperationKind.INGESTION,
                    _ingestion_payload(access_groups=("finance", "executive")),
                )
            )
        self.assertEqual(self.documents.calls, [])

    def test_invalid_envelopes_payloads_and_unsupported_health_fail_closed(self) -> None:
        with self.assertRaises(RequestValidationError):
            self.backend.execute(object())  # type: ignore[arg-type]
        invalid_cases = (
            _envelope(OperationKind.DELETION, {"document_id": "document-001"}),
            _envelope(OperationKind.DELETION, {"document_id": 7, "request": {}}),
            _envelope(
                OperationKind.DELETION,
                {
                    "document_id": "document 001",
                    "request": {"operation_key": "operation-key-0002"},
                },
            ),
            _envelope(OperationKind.JOB_STATUS, {"job_id": 7}),
            _envelope(OperationKind.JOB_STATUS, {"job_id": "\tjob-001"}),
            _envelope(OperationKind.RETRIEVAL, {"query_text": ""}),
            _envelope(OperationKind.HEALTH, {}),
        )
        for envelope in invalid_cases:
            with self.subTest(operation=envelope.operation, payload=dict(envelope.payload)):
                with self.assertRaises(RequestValidationError):
                    self.backend.execute(envelope)

    def test_invalid_document_and_readiness_responses_fail_as_dependency_errors(self) -> None:
        self.documents.invalid_result = {"job": _job_payload()}
        with self.assertRaises(DependencyUnavailableError):
            self.backend.execute(
                _envelope(OperationKind.INGESTION, _ingestion_payload())
            )
        self.documents.invalid_result = BackendResult(
            {
                "job": {
                    **_job_payload(),
                    "status": "RUNNING",
                    "phase": "STAGE",
                },
                "snapshot_id": "snapshot-001",
                "active_snapshot_id": "snapshot-001",
            }
        )
        with self.assertRaises(DependencyUnavailableError):
            self.backend.execute(
                _envelope(OperationKind.INGESTION, _ingestion_payload())
            )
        self.documents.invalid_result = BackendResult({"unexpected": "payload"})
        with self.assertRaises(DependencyUnavailableError):
            self.backend.execute(
                _envelope(OperationKind.JOB_STATUS, {"job_id": "job-001"})
            )
        self.readiness.result = BackendResult({"status": "ready", "checks": {}})
        with self.assertRaises(DependencyUnavailableError):
            self.backend.execute(
                _envelope(OperationKind.READINESS, {}, groups=frozenset())
            )

    def test_invalid_query_adapter_responses_fail_as_dependency_errors(self) -> None:
        for operation, attribute in (
            (OperationKind.RETRIEVAL, "retrieval_result"),
            (OperationKind.ANSWER, "answer_result"),
        ):
            for invalid in (
                {"unexpected": "object"},
                BackendResult({"unexpected": "payload"}),
            ):
                with self.subTest(operation=operation, invalid=type(invalid).__name__):
                    setattr(self.queries, attribute, invalid)
                    with self.assertRaises(DependencyUnavailableError):
                        self.backend.execute(
                            _envelope(operation, {"query_text": "Revenue?"})
                        )

    def test_backend_enforces_requested_graph_mode_and_trust_policy(self) -> None:
        query_operations = GraphRAGQueryOperations(
            RecordingRetrievalEngine(_retrieval_result()),  # type: ignore[arg-type]
            RecordingEmbedder(QueryEmbedding((0.5, -0.25), "space-v1")),
            RecordingGenerationService(_answered_result()),  # type: ignore[arg-type]
            subgraph_projector=RecordingSubgraphProjector(),
            monotonic=SequenceClock(1, 1, 2, 2, 3, 3),
        )
        projected = query_operations.retrieve(
            Principal("principal-001", "tenant-alpha", frozenset({"finance"})),
            APIRetrievalRequest(query_text="Revenue?"),
        )
        self.queries.retrieval_result = projected
        for payload in (
            {"query_text": "Revenue?", "include_graph": False},
            {
                "query_text": "Revenue?",
                "graph_trust_policy": "AUTHORITATIVE_ONLY",
            },
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(DependencyUnavailableError):
                    self.backend.execute(
                        _envelope(OperationKind.RETRIEVAL, payload)
                    )


if __name__ == "__main__":
    unittest.main()
