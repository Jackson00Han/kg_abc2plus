"""Closed-loop upload construction tests with all external systems injected."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import json
from types import SimpleNamespace
import unittest

from graphrag_prod.construction import (
    AuditedExtraction,
    BoundedDocumentParser,
    ChunkingConfig,
    ConstructionAuthorizationError,
    ConstructionBudgetExceeded,
    ConstructionConfig,
    ConstructionConflict,
    ConstructionDeadlineExceeded,
    ConstructionChunkResult,
    ConstructionMetadata,
    MAX_CONSTRUCTION_CHUNKS,
    MAX_CONSTRUCTION_DEADLINE_SECONDS,
    MAX_CONSTRUCTION_EXTRACTION_CHARS,
    MAX_CONSTRUCTION_MODEL_CALLS,
    Neo4jConstructionAuditStore,
    Neo4jKnowledgeConstructionWorkflow,
    ObservedDocumentState,
)
from graphrag_prod.construction.extraction import ExtractionFinding, ExtractionRejected
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import assertion_id, entity_id, mention_id
from graphrag_prod.domain.models import Assertion, Chunk, Entity, EntityMention
from graphrag_prod.ingestion.pipeline import EmbeddingProfile, ExtractionOutput
from graphrag_prod.knowledge import AuthorityLevel, GovernanceStatus, KnowledgeOrigin
from graphrag_prod.ontology import (
    EntityTypeDefinition,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxVersion,
)


NOW = datetime(2026, 9, 4, 8, 30, tzinfo=UTC)
SOURCE = b"Acme owns Pump-7."


def _tbox(tenant_id: str = "tenant-industrial") -> TBoxVersion:
    return TBoxVersion(
        tenant_id=tenant_id,
        key="industrial-assets",
        version=1,
        status=TBoxStatus.PUBLISHED,
        entity_types=(
            EntityTypeDefinition("Company", ("company-id",)),
            EntityTypeDefinition("Asset", ("asset-id",)),
        ),
        relationship_types=(
            RelationshipTypeDefinition("OWNS", ("Company",), ("Asset",)),
        ),
    )


class _TBoxStore:
    def __init__(self, tbox: TBoxVersion | None) -> None:
        self.tbox = tbox
        self.calls: list[tuple[str, str]] = []

    def active(self, tenant_id: str, key: str) -> TBoxVersion | None:
        self.calls.append((tenant_id, key))
        return self.tbox


class _AuditStore:
    def __init__(self) -> None:
        self.jobs: dict[str, object] = {}
        self.artifacts: dict[str, dict[str, object]] = {}
        self.outcomes: dict[tuple[str, str], object] = {}
        self.failed: list[tuple[str, tuple[str, ...]]] = []
        self.observed_principals: list[Principal] = []
        self.completed_jobs: list[str] = []

    def observe_document(
        self,
        principal: Principal,
        *,
        document_id_value: str,
        version_id_value: str,
        canonical_uri: str,
        source_name: str,
        access_groups: frozenset[str],
    ) -> ObservedDocumentState:
        del document_id_value, version_id_value, canonical_uri, source_name
        self.observed_principals.append(principal)
        return ObservedDocumentState(
            expected_active_snapshot_id=None,
            source_generation=0,
            version_number=1,
            access_policy_id="server-derived-policy",
            access_policy_version=1,
            access_groups=access_groups,
        )

    def ensure_job(self, state, *, expected_chunks: int):  # type: ignore[no-untyped-def]
        self.expected_chunks = expected_chunks
        existing = self.jobs.get(state.job_id)
        if existing is None:
            self.jobs[state.job_id] = state
            return state
        if existing.request_fingerprint != state.request_fingerprint:  # type: ignore[attr-defined]
            raise ConstructionConflict("construction idempotency key conflicts")
        return existing

    def read_artifact(
        self,
        *,
        tenant_id: str,
        artifact_id: str,
        input_hash: str,
        profile_id: str,
    ):
        del tenant_id, input_hash, profile_id
        return self.artifacts.get(artifact_id)

    def persist_artifact(
        self,
        *,
        tenant_id: str,
        artifact_id: str,
        input_hash: str,
        profile_id: str,
        payload: dict[str, object],
        created_at: datetime,
    ) -> None:
        del tenant_id, input_hash, profile_id, created_at
        existing = self.artifacts.setdefault(artifact_id, payload)
        if existing != payload:
            raise ConstructionConflict("artifact conflict")

    def read_outcome(
        self,
        principal: Principal,
        *,
        job_id: str,
        chunk_id: str,
    ):
        del principal
        value = self.outcomes.get((job_id, chunk_id))
        return None if value is None else replace(value, replayed=True)

    def persist_outcome(
        self,
        principal: Principal,
        *,
        job_id: str,
        result,
        access_groups: frozenset[str],
        artifact_input_hash: str,
        artifact_profile_id: str,
        completed_at: datetime,
    ) -> None:
        del (
            principal,
            access_groups,
            artifact_input_hash,
            artifact_profile_id,
            completed_at,
        )
        existing = self.outcomes.setdefault((job_id, result.chunk_id), result)
        if existing != result:
            raise ConstructionConflict("outcome conflict")

    def complete_job(
        self,
        *,
        tenant_id: str,
        job_id: str,
        completed_at: datetime,
    ) -> None:
        del tenant_id, completed_at
        if sum(key[0] == job_id for key in self.outcomes) != self.expected_chunks:
            raise ConstructionConflict("incomplete job")
        self.completed_jobs.append(job_id)

    def record_retryable_failure(
        self,
        *,
        tenant_id: str,
        job_id: str,
        chunk_id: str,
        findings: tuple[ExtractionFinding, ...],
        failed_at: datetime,
    ) -> None:
        del tenant_id, job_id, failed_at
        self.failed.append((chunk_id, tuple(item.code for item in findings)))


class _KnowledgeStore:
    def __init__(self) -> None:
        self.mentions: dict[str, object] = {}
        self.assertions: dict[str, object] = {}
        self.candidate_writes = 0
        self.quarantine_writes = 0
        self.last_batch = None

    def get_entity_mention(  # type: ignore[no-untyped-def]
        self, principal, record_id, *, statuses
    ):
        del principal, statuses
        return self.mentions.get(record_id)

    def get_assertion(self, principal, record_id, *, statuses):  # type: ignore[no-untyped-def]
        del principal, statuses
        return self.assertions.get(record_id)

    def _persist(self, batch) -> None:  # type: ignore[no-untyped-def]
        self.last_batch = batch
        for item in batch.mentions:
            if item.record_id in self.mentions:
                raise AssertionError("duplicate mention write")
            self.mentions[item.record_id] = item
        for item in batch.assertions:
            if item.record_id in self.assertions:
                raise AssertionError("duplicate assertion write")
            self.assertions[item.record_id] = item

    def persist_llm_candidates(self, batch):  # type: ignore[no-untyped-def]
        self.candidate_writes += 1
        self._persist(batch)

    def persist_llm_quarantined(self, batch):  # type: ignore[no-untyped-def]
        self.quarantine_writes += 1
        self._persist(batch)


class _Pipeline:
    def __init__(self, *, after_run=None) -> None:  # type: ignore[no-untyped-def]
        self.requests = []
        self.canonical_outputs: list[ExtractionOutput] = []
        self.after_run = after_run

    def run(  # type: ignore[no-untyped-def]
        self, request, extraction_provider, embedding_provider
    ):
        self.requests.append(request)
        _document, _version, chunks = request.domain_inputs()
        for chunk in chunks:
            self.canonical_outputs.append(
                extraction_provider(
                    artifact_id="canonical-artifact",
                    input_hash=chunk.checksum,
                    chunk=chunk,
                    profile=request.profile,
                )
            )
        del embedding_provider
        if self.after_run is not None:
            self.after_run()
        return SimpleNamespace(
            snapshot_id=request.snapshot_id,
            active_snapshot_id=request.snapshot_id,
        )


class _ConflictingOutcomeResult:
    def single(self) -> dict[str, bool]:
        return {"compatible": False}


class _ConflictingOutcomeTx:
    def __init__(self) -> None:
        self.query = ""
        self.parameters: dict[str, object] = {}

    def run(self, _query: str, **_parameters: object) -> _ConflictingOutcomeResult:
        self.query = _query
        self.parameters = _parameters
        return _ConflictingOutcomeResult()


class _RollbackAwareSession:
    def __init__(self) -> None:
        self.rolled_back = False
        self.tx = _ConflictingOutcomeTx()

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_write(self, work, *args):  # type: ignore[no-untyped-def]
        try:
            return work(self.tx, *args)
        except Exception:
            self.rolled_back = True
            raise


class _RollbackAwareDriver:
    def __init__(self) -> None:
        self.session_value = _RollbackAwareSession()

    def session(self, *, database: str) -> _RollbackAwareSession:
        if database != "neo4j":
            raise AssertionError("unexpected database")
        return self.session_value


class _Extractor:
    def __init__(
        self,
        tbox: TBoxVersion,
        *,
        status: GovernanceStatus = GovernanceStatus.CANDIDATE,
        reject: tuple[ExtractionFinding, ...] | None = None,
        on_call=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.active_tbox = tbox
        self.model = "qwen-plus"
        self.prompt_version = "industrial-prompt:v1"
        self.limits = SimpleNamespace(timeout_seconds=1.0)
        self.status = status
        self.reject = reject
        self.on_call = on_call
        self.calls = 0

    def extract_audited(  # type: ignore[no-untyped-def]
        self, *, artifact_id, input_hash, chunk, profile
    ):
        del artifact_id, input_hash
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        if self.reject is not None:
            raise ExtractionRejected(self.reject)
        company_key = "llm-candidate:company"
        asset_key = "llm-candidate:asset"
        company_id = entity_id(chunk.tenant_id, "Company", company_key)
        asset_id = entity_id(chunk.tenant_id, "Asset", asset_key)
        company = Entity(
            company_id,
            chunk.tenant_id,
            "Company",
            company_key,
            "Acme",
        )
        asset = Entity(
            asset_id,
            chunk.tenant_id,
            "Asset",
            asset_key,
            "Pump-7",
        )
        company_mention = EntityMention(
            mention_id(
                chunk.chunk_id,
                "Company",
                chunk.char_start,
                chunk.char_start + 4,
                "Acme",
                profile.extractor_signature,
            ),
            chunk.tenant_id,
            chunk.chunk_id,
            company_id,
            "Company",
            "Acme",
            chunk.char_start,
            chunk.char_start + 4,
            profile.extractor_signature,
            0.97,
        )
        asset_mention = EntityMention(
            mention_id(
                chunk.chunk_id,
                "Asset",
                chunk.char_start + 10,
                chunk.char_start + 16,
                "Pump-7",
                profile.extractor_signature,
            ),
            chunk.tenant_id,
            chunk.chunk_id,
            asset_id,
            "Asset",
            "Pump-7",
            chunk.char_start + 10,
            chunk.char_start + 16,
            profile.extractor_signature,
            0.96,
        )
        assertion = Assertion(
            assertion_id(
                chunk.tenant_id,
                company_id,
                "OWNS",
                "entity",
                asset_id,
                chunk.chunk_id,
                chunk.char_start,
                chunk.char_end,
                profile.extractor_signature,
                profile.schema_signature,
            ),
            chunk.tenant_id,
            company_id,
            "OWNS",
            chunk.chunk_id,
            chunk.char_start,
            chunk.char_end,
            profile.extractor_signature,
            profile.schema_signature,
            0.95,
            False,
            object_entity_id=asset_id,
        )
        findings = (
            (
                ExtractionFinding(
                    "LOW_RELATIONSHIP_CONFIDENCE",
                    "QUARANTINE",
                    "$.relationships[0].confidence",
                    "below threshold",
                ),
            )
            if self.status is GovernanceStatus.QUARANTINED
            else ()
        )
        return AuditedExtraction(
            output=ExtractionOutput(
                entities=(company, asset),
                mentions=(company_mention, asset_mention),
                assertions=(assertion,),
            ),
            origin=KnowledgeOrigin.LLM_EXTRACTED,
            authority=AuthorityLevel.SECONDARY,
            status=self.status,
            ontology_version_id=self.active_tbox.tbox_id,
            ontology_checksum=self.active_tbox.checksum,
            extractor_version=profile.extractor_signature,
            prompt_version=profile.prompt_signature,
            model=self.model,
            findings=findings,
        )


def _metadata(
    *,
    operation_key: str = "upload-1",
    access_groups: frozenset[str] = frozenset({"engineers"}),
) -> ConstructionMetadata:
    return ConstructionMetadata(
        operation_key=operation_key,
        canonical_uri="urn:industrial:asset-report-1",
        title="Asset report",
        source_name="controlled-upload",
        mime_type="text/plain",
        language="en",
        tbox_key="industrial-assets",
        access_groups=access_groups,
        published_at=NOW,
    )


def _workflow(
    *,
    extractor: _Extractor,
    audit: _AuditStore | None = None,
    knowledge: _KnowledgeStore | None = None,
    pipeline: _Pipeline | None = None,
    tbox_store: _TBoxStore | None = None,
    config: ConstructionConfig | None = None,
    parser: BoundedDocumentParser | None = None,
    monotonic=None,  # type: ignore[no-untyped-def]
):
    selected_audit = audit or _AuditStore()
    selected_knowledge = knowledge or _KnowledgeStore()
    selected_pipeline = pipeline or _Pipeline()
    workflow = Neo4jKnowledgeConstructionWorkflow(
        driver=object(),
        pipeline=selected_pipeline,
        embedding_provider=lambda **_kwargs: (0.1, 0.2),
        embedding_profile=EmbeddingProfile(
            "dashscope",
            "text-embedding-v4",
            "2026-08",
            2,
            "l2",
        ),
        extractor_factory=lambda _tbox_value: extractor,
        config=config
        or ConstructionConfig(
            extractor_signature="qwen-ontology:v1",
            prompt_signature="industrial-prompt:v1",
        ),
        parser=parser,
        tbox_store=tbox_store or _TBoxStore(extractor.active_tbox),
        knowledge_store=selected_knowledge,
        audit_store=selected_audit,
        clock=lambda: NOW,
        monotonic=monotonic,
    )
    return workflow, selected_audit, selected_knowledge, selected_pipeline


class KnowledgeConstructionWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.principal = Principal(
            "engineer:alice",
            "tenant-industrial",
            frozenset({"engineers"}),
            frozenset({"knowledge:construct"}),
        )

    def test_upload_publishes_only_empty_canonical_graph_then_candidate_abox(self) -> None:
        extractor = _Extractor(_tbox())
        workflow, audit, knowledge, pipeline = _workflow(extractor=extractor)
        result = workflow.run(self.principal, SOURCE, _metadata())

        self.assertEqual(result.tenant_id, self.principal.tenant_id)
        self.assertEqual(extractor.calls, 1)
        self.assertEqual(knowledge.candidate_writes, 1)
        self.assertEqual(knowledge.quarantine_writes, 0)
        self.assertEqual(result.chunks[0].status, "CANDIDATE")
        self.assertTrue(
            all(
                not output.entities and not output.mentions and not output.assertions
                for output in pipeline.canonical_outputs
            )
        )
        request = pipeline.requests[0]
        self.assertEqual(request.tenant_id, self.principal.tenant_id)
        self.assertEqual(request.access_groups, frozenset({"engineers"}))
        self.assertEqual(request.access_policy_id, "server-derived-policy")
        self.assertEqual(knowledge.last_batch.mentions[0].evidence.quoted_text, "Acme")
        self.assertEqual(
            knowledge.last_batch.assertions[0].evidence.quoted_text,
            SOURCE.decode(),
        )
        self.assertEqual(len(audit.completed_jobs), 1)

    def test_outcome_identity_conflict_raises_inside_transaction_for_rollback(self) -> None:
        driver = _RollbackAwareDriver()
        store = Neo4jConstructionAuditStore(driver)
        result = ConstructionChunkResult(
            chunk_id="chunk-1",
            artifact_id="artifact-1",
            status="CANDIDATE",
            finding_codes=(),
            mention_record_ids=("mention-1",),
            assertion_record_ids=(),
        )
        with self.assertRaisesRegex(ConstructionConflict, "outcome conflicts"):
            store.persist_outcome(
                self.principal,
                job_id="job-1",
                result=result,
                access_groups=self.principal.groups,
                artifact_input_hash="a" * 64,
                artifact_profile_id="profile-1",
                completed_at=NOW,
            )
        self.assertTrue(driver.session_value.rolled_back)
        self.assertIn("MATCH (artifact:DerivationArtifact", driver.session_value.tx.query)
        self.assertNotIn(
            "OPTIONAL MATCH (artifact:DerivationArtifact",
            driver.session_value.tx.query,
        )
        self.assertEqual(
            driver.session_value.tx.parameters["artifact_input_hash"],
            "a" * 64,
        )

    def test_construction_job_reads_are_bounded_by_tenant_acl_and_outcome_integrity(self) -> None:
        payload = json.dumps(
            {
                "format_version": 1,
                "chunk_id": "chunk-1",
                "artifact_id": "artifact-1",
                "status": "CANDIDATE",
                "finding_codes": [],
                "mention_record_ids": ["mention-1"],
                "assertion_record_ids": ["assertion-1"],
            }
        )
        properties = {
            "job_id": "job-1",
            "tenant_id": self.principal.tenant_id,
            "document_id": "document-1",
            "version_id": "version-1",
            "snapshot_id": "snapshot-1",
            "tbox_id": "tbox-1",
            "status": "COMPLETED",
            "expected_chunks": 1,
            "completed_chunks": 1,
            "created_at": NOW,
            "updated_at": NOW,
            "completed_at": NOW,
        }

        class _Result(list):
            def single(self):  # type: ignore[no-untyped-def]
                return self[0] if self else None

        class _Session:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args):  # type: ignore[no-untyped-def]
                return None

            def run(self, query: str, **parameters: object) -> _Result:
                self.calls.append((query, parameters))
                if "job_id: $job_id" in query:
                    return _Result(({"job": properties, "result_jsons": [payload]},))
                return _Result(({"job": properties, "result_jsons": []},))

        class _Driver:
            def __init__(self) -> None:
                self.value = _Session()

            def session(self, **_kwargs: object) -> _Session:
                return self.value

        driver = _Driver()
        store = Neo4jConstructionAuditStore(driver)
        detail = store.get_job(self.principal, "job-1")
        listed = store.list_jobs(
            self.principal,
            statuses=("COMPLETED",),
            limit=10,
        )
        assert detail is not None
        self.assertEqual(detail.chunks[0].chunk_id, "chunk-1")
        self.assertTrue(detail.chunks[0].replayed)
        self.assertEqual(len(listed), 1)
        detail_query, detail_parameters = driver.value.calls[0]
        for boundary in (
            "tenant_id: $tenant_id",
            "any(group IN $groups WHERE group IN job.access_groups)",
            "AND NOT EXISTS",
            "invalid.access_groups <> job.access_groups",
            "USED_ARTIFACT",
            "HAS_VERSION",
            "coalesce(job.completed_chunks, 0) = size(result_jsons)",
            "job.expected_chunks = size(result_jsons)",
        ):
            self.assertIn(boundary, detail_query)
        self.assertEqual(detail_parameters["tenant_id"], self.principal.tenant_id)
        self.assertEqual(detail_parameters["groups"], ["engineers"])
        list_query, list_parameters = driver.value.calls[1]
        self.assertLess(list_query.index("job.status IN $statuses"), list_query.index("LIMIT $limit"))
        self.assertEqual(list_parameters["limit"], 10)

        with self.assertRaisesRegex(ValueError, "between"):
            store.list_jobs(self.principal, limit=101)

    def test_construction_requires_dedicated_capability_before_any_work(self) -> None:
        extractor = _Extractor(_tbox())
        workflow, audit, knowledge, pipeline = _workflow(extractor=extractor)
        reader = Principal(
            "reader:bob",
            self.principal.tenant_id,
            self.principal.groups,
        )
        with self.assertRaisesRegex(PermissionError, "capability"):
            workflow.run(reader, SOURCE, _metadata())
        self.assertEqual(audit.observed_principals, [])
        self.assertEqual(knowledge.candidate_writes, 0)
        self.assertEqual(pipeline.requests, [])

    def test_selected_acl_is_not_widened_to_all_principal_groups(self) -> None:
        principal = Principal(
            "engineer:alice",
            "tenant-industrial",
            frozenset({"engineers", "public"}),
            frozenset({"knowledge:construct"}),
        )
        extractor = _Extractor(_tbox())
        workflow, _audit, knowledge, pipeline = _workflow(extractor=extractor)
        workflow.run(
            principal,
            SOURCE,
            _metadata(access_groups=frozenset({"engineers"})),
        )
        self.assertEqual(
            pipeline.requests[0].access_groups,
            frozenset({"engineers"}),
        )
        self.assertEqual(
            knowledge.last_batch.mentions[0].evidence.access_groups,
            frozenset({"engineers"}),
        )

        with self.assertRaises(ConstructionAuthorizationError):
            workflow.run(
                principal,
                SOURCE,
                _metadata(
                    operation_key="upload-unauthorized",
                    access_groups=frozenset({"operators"}),
                ),
            )
        self.assertEqual(extractor.calls, 1)

    def test_preflight_budgets_stop_before_ingestion_or_provider_calls(self) -> None:
        cases = (
            (
                b"x" * 18,
                BoundedDocumentParser(
                    chunking=ChunkingConfig(max_chars=5, minimum_boundary_ratio=1.0)
                ),
                ConstructionConfig(
                    "qwen-ontology:v1",
                    "industrial-prompt:v1",
                    max_chunks=1,
                    max_model_calls=100,
                    max_total_extraction_chars=100,
                ),
            ),
            (
                SOURCE * 2,
                BoundedDocumentParser(
                    chunking=ChunkingConfig(
                        max_chars=len(SOURCE), minimum_boundary_ratio=1.0
                    )
                ),
                ConstructionConfig(
                    "qwen-ontology:v1",
                    "industrial-prompt:v1",
                    max_chunks=2,
                    max_model_calls=1,
                    max_total_extraction_chars=100,
                ),
            ),
            (
                SOURCE,
                BoundedDocumentParser(),
                ConstructionConfig(
                    "qwen-ontology:v1",
                    "industrial-prompt:v1",
                    max_chunks=100,
                    max_model_calls=100,
                    max_total_extraction_chars=10,
                ),
            ),
        )
        for payload, parser, config in cases:
            with self.subTest(config=config):
                extractor = _Extractor(_tbox())
                workflow, audit, knowledge, pipeline = _workflow(
                    extractor=extractor,
                    config=config,
                    parser=parser,
                )
                with self.assertRaises(ConstructionBudgetExceeded):
                    workflow.run(self.principal, payload, _metadata())
                self.assertEqual(extractor.calls, 0)
                self.assertEqual(audit.observed_principals, [])
                self.assertEqual(knowledge.candidate_writes, 0)
                self.assertEqual(pipeline.requests, [])

    def test_construction_budget_configuration_rejects_unbounded_values(self) -> None:
        for field, invalid in (
            ("max_chunks", 0),
            ("max_chunks", True),
            ("max_model_calls", -1),
            ("max_total_extraction_chars", 1.5),
            ("deadline_seconds", 0.0),
            ("deadline_seconds", float("inf")),
            ("deadline_seconds", float("nan")),
            ("deadline_seconds", True),
            ("max_chunks", MAX_CONSTRUCTION_CHUNKS + 1),
            ("max_model_calls", MAX_CONSTRUCTION_MODEL_CALLS + 1),
            (
                "max_total_extraction_chars",
                MAX_CONSTRUCTION_EXTRACTION_CHARS + 1,
            ),
            (
                "deadline_seconds",
                MAX_CONSTRUCTION_DEADLINE_SECONDS + 0.1,
            ),
        ):
            with self.subTest(field=field, invalid=invalid):
                with self.assertRaises(ValueError):
                    ConstructionConfig(
                        "qwen-ontology:v1",
                        "industrial-prompt:v1",
                        **{field: invalid},  # type: ignore[arg-type]
                    )
        bounded = ConstructionConfig(
            "qwen-ontology:v1",
            "industrial-prompt:v1",
            max_chunks=MAX_CONSTRUCTION_CHUNKS,
            max_model_calls=MAX_CONSTRUCTION_MODEL_CALLS,
            max_total_extraction_chars=MAX_CONSTRUCTION_EXTRACTION_CHARS,
            deadline_seconds=MAX_CONSTRUCTION_DEADLINE_SECONDS,
        )
        self.assertEqual(bounded.max_chunks, MAX_CONSTRUCTION_CHUNKS)
        self.assertEqual(
            bounded.deadline_seconds,
            MAX_CONSTRUCTION_DEADLINE_SECONDS,
        )

    def test_deadline_stops_before_the_next_model_call(self) -> None:
        timer = SimpleNamespace(value=0.0)
        extractor = _Extractor(
            _tbox(),
            on_call=lambda _calls: setattr(timer, "value", 10.0),
        )
        parser = BoundedDocumentParser(
            chunking=ChunkingConfig(
                max_chars=len(SOURCE), minimum_boundary_ratio=1.0
            )
        )
        workflow, audit, knowledge, pipeline = _workflow(
            extractor=extractor,
            parser=parser,
            monotonic=lambda: timer.value,
            config=ConstructionConfig(
                "qwen-ontology:v1",
                "industrial-prompt:v1",
                max_chunks=2,
                max_model_calls=2,
                max_total_extraction_chars=100,
                deadline_seconds=10.0,
            ),
        )
        with self.assertRaises(ConstructionDeadlineExceeded):
            workflow.run(self.principal, SOURCE * 2, _metadata())
        self.assertEqual(extractor.calls, 1)
        self.assertEqual(len(pipeline.requests), 1)
        self.assertEqual(knowledge.candidate_writes, 1)
        self.assertEqual(len(audit.outcomes), 1)
        self.assertEqual(audit.completed_jobs, [])

    def test_deadline_after_parse_stops_before_ingestion_and_providers(self) -> None:
        times = iter((0.0, 0.0, 10.0))
        extractor = _Extractor(_tbox())
        workflow, audit, knowledge, pipeline = _workflow(
            extractor=extractor,
            monotonic=lambda: next(times),
            config=ConstructionConfig(
                "qwen-ontology:v1",
                "industrial-prompt:v1",
                deadline_seconds=10.0,
            ),
        )
        with self.assertRaises(ConstructionDeadlineExceeded):
            workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(extractor.calls, 0)
        self.assertEqual(audit.observed_principals, [])
        self.assertEqual(knowledge.candidate_writes, 0)
        self.assertEqual(pipeline.requests, [])

    def test_deadline_after_ingestion_starts_no_model_call(self) -> None:
        timer = SimpleNamespace(value=0.0)
        pipeline = _Pipeline(after_run=lambda: setattr(timer, "value", 10.0))
        extractor = _Extractor(_tbox())
        workflow, _audit, knowledge, _pipeline = _workflow(
            extractor=extractor,
            pipeline=pipeline,
            monotonic=lambda: timer.value,
            config=ConstructionConfig(
                "qwen-ontology:v1",
                "industrial-prompt:v1",
                deadline_seconds=10.0,
            ),
        )
        with self.assertRaises(ConstructionDeadlineExceeded):
            workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(extractor.calls, 0)
        self.assertEqual(knowledge.candidate_writes, 0)

    def test_provider_timeout_must_fit_inside_workflow_deadline(self) -> None:
        extractor = _Extractor(_tbox())
        extractor.limits = SimpleNamespace(timeout_seconds=10.0)
        workflow, audit, _knowledge, pipeline = _workflow(
            extractor=extractor,
            config=ConstructionConfig(
                "qwen-ontology:v1",
                "industrial-prompt:v1",
                deadline_seconds=10.0,
            ),
        )
        with self.assertRaisesRegex(ConstructionConflict, "provider timeout"):
            workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(audit.observed_principals, [])
        self.assertEqual(pipeline.requests, [])

        del extractor.limits
        with self.assertRaisesRegex(ConstructionConflict, "bounded provider timeout"):
            workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(audit.observed_principals, [])
        self.assertEqual(pipeline.requests, [])

    def test_low_confidence_output_uses_quarantine_not_candidate_lane(self) -> None:
        extractor = _Extractor(_tbox(), status=GovernanceStatus.QUARANTINED)
        workflow, _audit, knowledge, _pipeline = _workflow(extractor=extractor)
        result = workflow.run(self.principal, SOURCE, _metadata())

        self.assertEqual(result.chunks[0].status, "QUARANTINED")
        self.assertEqual(knowledge.candidate_writes, 0)
        self.assertEqual(knowledge.quarantine_writes, 1)
        self.assertEqual(
            knowledge.last_batch.mentions[0].trust.status,
            GovernanceStatus.QUARANTINED,
        )

    def test_exact_replay_skips_model_and_abox_writes(self) -> None:
        extractor = _Extractor(_tbox())
        workflow, _audit, knowledge, pipeline = _workflow(extractor=extractor)
        first = workflow.run(self.principal, SOURCE, _metadata())
        second = workflow.run(self.principal, SOURCE, _metadata())

        self.assertEqual(extractor.calls, 1)
        self.assertEqual(knowledge.candidate_writes, 1)
        self.assertEqual(len(pipeline.requests), 2)
        self.assertFalse(first.chunks[0].replayed)
        self.assertTrue(second.chunks[0].replayed)

    def test_artifact_resume_repairs_missing_outcome_without_repeat_model_write(self) -> None:
        extractor = _Extractor(_tbox())
        workflow, audit, knowledge, _pipeline = _workflow(extractor=extractor)
        first = workflow.run(self.principal, SOURCE, _metadata())
        audit.outcomes.pop((first.job_id, first.chunks[0].chunk_id))

        resumed = workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(extractor.calls, 1)
        self.assertEqual(knowledge.candidate_writes, 1)
        self.assertFalse(resumed.chunks[0].replayed)

    def test_semantically_rejected_response_is_audited_without_abox(self) -> None:
        finding = ExtractionFinding(
            "ENTITY_TYPE_NOT_ALLOWED",
            "REJECT",
            "$.entities[0].type",
            "outside T-Box",
        )
        extractor = _Extractor(_tbox(), reject=(finding,))
        workflow, audit, knowledge, _pipeline = _workflow(extractor=extractor)

        first = workflow.run(self.principal, SOURCE, _metadata())
        second = workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(first.chunks[0].status, "REJECTED")
        self.assertEqual(first.chunks[0].finding_codes, (finding.code,))
        self.assertTrue(second.chunks[0].replayed)
        self.assertEqual(extractor.calls, 1)
        self.assertEqual(knowledge.candidate_writes, 0)
        self.assertEqual(len(audit.artifacts), 1)

    def test_provider_failure_remains_retryable_and_is_not_cached_as_rejection(self) -> None:
        finding = ExtractionFinding(
            "MODEL_CALL_FAILED",
            "REJECT",
            "$",
            "timeout",
        )
        extractor = _Extractor(_tbox(), reject=(finding,))
        workflow, audit, knowledge, _pipeline = _workflow(extractor=extractor)

        with self.assertRaises(ExtractionRejected):
            workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(len(audit.failed), 1)
        self.assertEqual(audit.artifacts, {})
        self.assertEqual(audit.outcomes, {})
        self.assertEqual(knowledge.candidate_writes, 0)

    def test_same_operation_key_with_changed_payload_is_rejected(self) -> None:
        extractor = _Extractor(_tbox())
        workflow, _audit, knowledge, _pipeline = _workflow(extractor=extractor)
        workflow.run(self.principal, SOURCE, _metadata())

        with self.assertRaisesRegex(ConstructionConflict, "idempotency"):
            workflow.run(self.principal, b"Acme owns Pump-8.", _metadata())
        self.assertEqual(knowledge.candidate_writes, 1)

    def test_missing_or_cross_tenant_tbox_stops_before_ingestion(self) -> None:
        extractor = _Extractor(_tbox())
        missing_store = _TBoxStore(None)
        pipeline = _Pipeline()
        workflow, *_ = _workflow(
            extractor=extractor,
            pipeline=pipeline,
            tbox_store=missing_store,
        )
        with self.assertRaisesRegex(ConstructionConflict, "active PUBLISHED"):
            workflow.run(self.principal, SOURCE, _metadata())
        self.assertEqual(pipeline.requests, [])
        self.assertEqual(missing_store.calls, [(self.principal.tenant_id, "industrial-assets")])

        wrong_tenant = _Extractor(_tbox("tenant-other"))
        workflow, *_ = _workflow(
            extractor=wrong_tenant,
            tbox_store=_TBoxStore(_tbox()),
        )
        with self.assertRaisesRegex(ConstructionConflict, "active tenant T-Box"):
            workflow.run(self.principal, SOURCE, _metadata(operation_key="upload-2"))


if __name__ == "__main__":
    unittest.main()
