"""Disposable-Neo4j validation of upload-to-review knowledge construction."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import ipaddress
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

import neo4j

from graphrag_prod.api.knowledge import Neo4jKnowledgeOperations
from graphrag_prod.api.knowledge_contracts import (
    AuthoritativeImportRequest,
    PublicationRequest,
)
from graphrag_prod.construction import (
    ConstructionConfig,
    ConstructionMetadata,
    ConstructionConflict,
    ConstructionIngestionFailed,
    ConstructionBudgetExceeded,
    Neo4jConstructionAuditStore,
    Neo4jKnowledgeConstructionWorkflow,
    OpenAICompatibleOntologyExtractor,
)
from graphrag_prod.construction.extraction import (
    ExtractionFinding,
    ExtractionLimits,
    ExtractionRejected,
)
from graphrag_prod.construction.workflow import ConstructionAuthorizationError
from graphrag_prod.domain.access import Principal
from graphrag_prod.domain.ids import derivation_artifact_id
from graphrag_prod.graph.schema import apply_schema, verify_schema
from graphrag_prod.ingestion.pipeline import (
    EmbeddingProfile,
    Neo4jIncrementalPipeline,
)
from graphrag_prod.knowledge.review import Neo4jKnowledgeReviewService
from graphrag_prod.ontology import (
    EntityTypeDefinition,
    Neo4jTBoxStore,
    RelationshipTypeDefinition,
    TBoxStatus,
    TBoxVersion,
)
from graphrag_prod.ontology.models import Cardinality, PropertyDataType, PropertyDefinition


NOW = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)
SOURCE = b"Acme owns Pump-7."


class _Completions:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        payload = {
            "entities": [
                {
                    "ref": "company",
                    "type": "Company",
                    "mentions": [
                        {"text": "Acme", "start": 0, "end": 4, "confidence": 0.98}
                    ],
                },
                {
                    "ref": "asset",
                    "type": "Asset",
                    "mentions": [
                        {
                            "text": "Pump-7",
                            "start": 10,
                            "end": 16,
                            "confidence": 0.97,
                        }
                    ],
                },
            ],
            "relationships": [
                {
                    "type": "OWNS",
                    "source_ref": "company",
                    "target_ref": "asset",
                    "evidence": {
                        "text": SOURCE.decode(),
                        "start": 0,
                        "end": len(SOURCE),
                    },
                    "confidence": 0.96,
                }
            ],
            "property_facts": [],
        }
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(payload)},
                }
            ]
        }


class _FeedbackCompletions(_Completions):
    """Script model responses while all validation and persistence stay real."""

    def __init__(self, *steps: str) -> None:
        super().__init__()
        self.steps = steps
        self.before_response: Callable[[int], None] | None = None
        self.payload: dict[str, object] | None = None

    def create(self, **kwargs: object) -> dict[str, object]:
        response = super().create(**kwargs)
        if self.payload is not None:
            response["choices"][0]["message"]["content"] = json.dumps(self.payload)
        number = len(self.calls)
        if self.before_response is not None:
            self.before_response(number)
        step = self.steps[number - 1]
        if step == "timeout":
            raise TimeoutError("deterministic provider interruption")
        if step == "invalid":
            message = response["choices"][0]["message"]
            payload = json.loads(message["content"])
            # An exact substring still cannot support a relation whose Company
            # endpoint lies outside it. The model must correct the evidence.
            relation = payload["relationships"][0]
            endpoint = next(item for item in payload["entities"]
                            if item["ref"] == relation["target_ref"])["mentions"][0]
            relation["evidence"] = {key: endpoint[key] for key in ("text", "start", "end")}
            message["content"] = json.dumps(payload)
        elif step != "valid":
            raise AssertionError(f"unknown scripted response: {step}")
        return response


class Neo4jConstructionWorkflowIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = (
            "TEST_NEO4J_URI",
            "TEST_NEO4J_USER",
            "TEST_NEO4J_PASSWORD",
            "TEST_NEO4J_DATABASE",
        )
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise RuntimeError(f"missing disposable Neo4j settings: {missing}")
        if os.getenv("GRAPHRAG_ALLOW_DISPOSABLE_DB") != "1":
            raise RuntimeError("GRAPHRAG_ALLOW_DISPOSABLE_DB=1 is required")
        uri = os.environ["TEST_NEO4J_URI"]
        host = urlparse(uri).hostname
        if host is None or not ipaddress.ip_address(host).is_loopback:
            raise RuntimeError("integration tests only accept a loopback Neo4j URI")
        cls.database = os.environ["TEST_NEO4J_DATABASE"]
        cls.driver = neo4j.GraphDatabase.driver(
            uri,
            auth=(
                os.environ["TEST_NEO4J_USER"],
                os.environ["TEST_NEO4J_PASSWORD"],
            ),
        )
        cls.driver.verify_connectivity()
        records, _, _ = cls.driver.execute_query(
            "MATCH (node) RETURN count(node) AS count",
            database_=cls.database,
        )
        if records[0]["count"] != 0:
            cls.driver.close()
            raise RuntimeError("disposable Neo4j database must start empty")
        apply_schema(cls.driver, cls.database)
        apply_schema(cls.driver, cls.database)
        errors = verify_schema(cls.driver, cls.database)
        if errors:
            cls.driver.close()
            raise RuntimeError(f"schema verification failed: {errors}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.driver.close()

    def setUp(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )
        self.tenant_id = "tenant-industrial"
        draft = TBoxVersion(
            tenant_id=self.tenant_id,
            key="industrial-assets",
            version=1,
            status=TBoxStatus.DRAFT,
            entity_types=(
                EntityTypeDefinition("Company", ("company-id", "llm-candidate")),
                EntityTypeDefinition("Asset", ("asset-id", "llm-candidate")),
            ),
            relationship_types=(
                RelationshipTypeDefinition("OWNS", ("Company",), ("Asset",)),
            ),
        )
        tbox_store = Neo4jTBoxStore(self.driver, self.database)
        tbox_store.import_version(draft)
        self.tbox = tbox_store.publish(
            self.tenant_id,
            draft.tbox_id,
            expected_active_tbox_id=None,
        )
        self.principal = Principal(
            "reviewer:alice",
            self.tenant_id,
            frozenset({"board", "public"}),
            frozenset({"knowledge:construct", "knowledge:review"}),
        )
        self.completions = _Completions()
        self.embedding_calls = 0

        def embedding_provider(**_kwargs: object) -> tuple[float, float]:
            self.embedding_calls += 1
            return (0.6, 0.8)

        def extractor_factory(tbox: TBoxVersion) -> OpenAICompatibleOntologyExtractor:
            client = SimpleNamespace(
                chat=SimpleNamespace(completions=self.completions)
            )
            return OpenAICompatibleOntologyExtractor(
                client=client,
                model="deterministic-test-model",
                active_tbox=tbox,
                prompt_version="industrial-prompt:v1",
            )

        self.workflow = Neo4jKnowledgeConstructionWorkflow(
            driver=self.driver,
            database=self.database,
            pipeline=Neo4jIncrementalPipeline(
                self.driver,
                self.database,
                worker_id="construction-integration-worker",
            ),
            embedding_provider=embedding_provider,
            embedding_profile=EmbeddingProfile(
                "deterministic-test-provider",
                "two-dimensional-test-embedding",
                "v1",
                2,
                "l2-unit",
            ),
            extractor_factory=extractor_factory,
            config=ConstructionConfig(
                extractor_signature="ontology-extractor:test:v1",
                prompt_signature="industrial-prompt:v1",
            ),
            clock=lambda: NOW,
        )
        self.metadata = ConstructionMetadata(
            operation_key="industrial-upload-1",
            canonical_uri="urn:industrial:asset-report-1",
            title="Industrial asset report",
            source_name="controlled-upload",
            mime_type="text/plain",
            language="en",
            tbox_key="industrial-assets",
            access_groups=frozenset({"board"}),
            published_at=NOW,
        )

    def tearDown(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )

    def test_authoritative_document_candidates_keep_source_authority_and_acl(self) -> None:
        principal = replace(self.principal, capabilities=self.principal.capabilities | {"knowledge:import"})
        metadata = replace(self.metadata, knowledge_scope="AUTHORITATIVE")
        first = self.workflow.run(principal, SOURCE, metadata)
        review = Neo4jKnowledgeReviewService(self.driver, self.database)
        queue = review.review_queue(principal)
        self.assertEqual(len(queue), 3)
        for item in queue:
            self.assertEqual(item.record.trust.origin.value, "AUTHORITATIVE_EXTRACTED")
            self.assertEqual(item.record.trust.authority.value, "AUTHORITATIVE")
            self.assertEqual(item.record.trust.status.value, "CANDIDATE")
        self.assertEqual(review.review_queue(replace(principal, groups=frozenset({"public"}))), ())
        replay = self.workflow.run(principal, SOURCE, metadata)
        self.assertEqual(replay.job_id, first.job_id)
        self.assertEqual(len(self.completions.calls), 1)
        with self.assertRaises(ConstructionConflict):
            self.workflow.run(principal, SOURCE, self.metadata)

    def _enable_validation_feedback(self, *steps: str) -> _FeedbackCompletions:
        completions = _FeedbackCompletions(*steps)
        self.completions = completions

        def extractor_factory(tbox: TBoxVersion) -> OpenAICompatibleOntologyExtractor:
            return OpenAICompatibleOntologyExtractor(
                client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
                model="deterministic-test-model",
                active_tbox=tbox,
                prompt_version="industrial-prompt:v1",
                max_validation_attempts=2,
                limits=ExtractionLimits(timeout_seconds=1.0),
            )

        self.workflow.extractor_factory = extractor_factory
        self.workflow.config = replace(
            self.workflow.config, max_model_calls=4, deadline_seconds=30.0,
        )
        return completions

    def _extraction_audits(self) -> dict[str, str]:
        rows, _, _ = self.driver.execute_query(
            """
            MATCH (artifact:DerivationArtifact {
                tenant_id: $tenant_id, kind: 'ONTOLOGY_EXTRACTION_AUDIT'
            })
            RETURN artifact.artifact_id AS id, artifact.payload_json AS payload
            """,
            tenant_id=self.tenant_id,
            database_=self.database,
        )
        return {row["id"]: row["payload"] for row in rows}

    def _proposal_and_outcome_counts(self) -> dict[str, int]:
        rows, _, _ = self.driver.execute_query(
            """
            MATCH (node)
            RETURN count(CASE WHEN node:GovernedEntityMentionRevision THEN 1 END) AS mentions,
                   count(CASE WHEN node:GovernedAssertionRevision THEN 1 END) AS assertions,
                   count(CASE WHEN node:KnowledgeConstructionChunkOutcome THEN 1 END) AS outcomes
            """,
            database_=self.database,
        )
        return dict(rows[0])

    def _interrupt_parent_assembly(self, source: bytes = SOURCE):
        """Fail after both immutable responses, before any parent or proposal."""
        # These recovery fixtures include cold source preparation. Match the
        # industrial online 90-second envelope; retain the 15-second DB and
        # 1-second model limits and existing 30-second feedback/deadline tests.
        self.workflow.config = replace(self.workflow.config, deadline_seconds=90.0)
        captured = {}

        def fail_assembly(_result, **kwargs):
            captured["chunk"] = kwargs["chunk"]
            raise RuntimeError("deliberate final audit assembly interruption")

        with patch("graphrag_prod.construction.workflow._audited_payload", side_effect=fail_assembly) as assembly:
            with self.assertRaisesRegex(RuntimeError, "deliberate final audit"):
                self.workflow.run(self.principal, source, self.metadata)
        self.assertEqual(assembly.call_count, 1)
        self.assertEqual(len(self.completions.calls), 2)
        self.assertEqual(self.embedding_calls, 1)
        audits = self._extraction_audits()
        attempts = sorted(((key, json.loads(raw)) for key, raw in audits.items()),
                          key=lambda item: item[1]["attempt"])
        self.assertEqual([item[1]["status"] for item in attempts], ["REJECTED", "CANDIDATE"])
        self.assertTrue(all(item[1]["audit_type"] == "VALIDATION_ATTEMPT" for item in attempts))
        self.assertNotIn(attempts[0][1]["parent_artifact_id"], audits)
        self.assertEqual(self._proposal_and_outcome_counts(), {"mentions": 0, "assertions": 0, "outcomes": 0})
        self.assertEqual(self.workflow.audit_store.get_job(self.principal, attempts[0][1]["job_id"]).completed_chunks, 0)
        rows, _, _ = self.driver.execute_query(
            "MATCH (d:Document {document_id:$document})-[:ACTIVE_VERSION]->(v:DocumentVersion {version_id:$version}) "
            "MATCH (d)-[:ACTIVE_SNAPSHOT]->(s:KnowledgeSnapshot {build_state:'PUBLISHED'})-[:OF_VERSION]->(v) "
            "MATCH (s)-[:INCLUDES_CHUNK]->(c:Chunk {chunk_id:$chunk})-[:HAS_EMBEDDING]->(e:ChunkEmbedding) "
            "RETURN count(DISTINCT d) AS documents,count(DISTINCT c) AS chunks,count(DISTINCT e) AS embeddings",
            document=captured["chunk"].document_id, version=captured["chunk"].version_id,
            chunk=captured["chunk"].chunk_id, database_=self.database,
        )
        self.assertEqual(dict(rows[0]), {"documents": 1, "chunks": 1, "embeddings": 1})
        return captured["chunk"], audits, attempts

    def _rewrite_saved_attempt(self, artifact_id: str, payload: dict, *, keep_original: bool = False) -> str:
        """Simulate a storage attacker who recomputes every public digest/ID."""
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        new_id = derivation_artifact_id(self.tenant_id, "ONTOLOGY_EXTRACTION_AUDIT", digest, payload["profile_id"])
        if keep_original:
            self.workflow.audit_store.persist_artifact(
                tenant_id=self.tenant_id, artifact_id=new_id, input_hash=digest,
                profile_id=payload["profile_id"], payload=payload, created_at=NOW,
            )
        else:
            self.driver.execute_query(
                "MATCH (a:DerivationArtifact {artifact_id:$old}) "
                "SET a.artifact_id=$new,a.input_hash=$digest,a.output_checksum=$digest,a.payload_json=$payload",
                old=artifact_id, new=new_id, digest=digest, payload=encoded, database_=self.database,
            )
        return new_id

    def test_interrupted_parent_assembly_recovers_saved_chinese_literal_without_provider_recall(self) -> None:
        original = self.tbox
        draft = replace(original, version=2, status=TBoxStatus.DRAFT,
                        entity_types=(original.entity_types[0], replace(original.entity_types[1], properties=(
                            PropertyDefinition("kind", PropertyDataType.STRING, False, Cardinality.ZERO_OR_ONE),
                        ))))
        store = Neo4jTBoxStore(self.driver, self.database)
        store.import_version(draft)
        self.tbox = store.publish(self.tenant_id, draft.tbox_id, expected_active_tbox_id=original.tbox_id)
        source = "Acme owns JB-7. 记录写明JB-7的类别为连接块。"
        payload = {
            "entities": [
                {"ref": "company", "type": "Company", "mentions": [
                    {"text": "Acme", "start": 0, "end": 4, "confidence": 0.98}]},
                {"ref": "asset", "type": "Asset", "mentions": [
                    {"text": "JB-7", "start": 10, "end": 14, "confidence": 0.98}]},
            ],
            "relationships": [{"type": "OWNS", "source_ref": "company", "target_ref": "asset",
                               "evidence": {"text": source[:15], "start": 0, "end": 15}, "confidence": 0.97}],
            "property_facts": [{"entity_ref": "asset", "property": "kind", "raw_literal": "连接块", "unit": None,
                                "valid_from": None, "valid_to": None, "observed_at": None,
                                "evidence": {"text": source, "start": 0, "end": len(source)}, "confidence": 0.97}],
        }
        completions = self._enable_validation_feedback("invalid", "valid")
        completions.payload = payload
        _chunk, retained, attempts = self._interrupt_parent_assembly(source.encode("utf-8"))
        recovered = self.workflow.run(self.principal, source.encode("utf-8"), self.metadata)
        self.assertEqual(recovered.job_id, attempts[0][1]["job_id"])
        self.assertEqual(recovered.chunks[0].status, "CANDIDATE")
        self.assertEqual([(attempt.attempt, attempt.status) for attempt in recovered.chunks[0].validation_attempts],
                         [(1, "REJECTED"), (2, "CANDIDATE")])
        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(self.embedding_calls, 1)
        self.assertEqual(self._proposal_and_outcome_counts(), {"mentions": 2, "assertions": 2, "outcomes": 1})
        audits = self._extraction_audits()
        self.assertTrue(retained.items() <= audits.items())
        self.assertEqual(len(audits), 3)
        parent = json.loads(audits[recovered.chunks[0].artifact_id])
        self.assertEqual([ref["artifact_id"] for ref in parent["validation_attempt_artifacts"]],
                         [item[0] for item in attempts])
        records = Neo4jKnowledgeReviewService(self.driver, self.database).review_queue(self.principal)
        literal = next(item.record for item in records if getattr(item.record, "literal_value", None) == "连接块")
        self.assertEqual(literal.literal_semantics.datatype, "STRING")
        self.assertEqual(literal.literal_semantics.raw_value, "连接块")
        self.assertEqual(literal.evidence.quoted_text, source)
        self.assertEqual(literal.trust.status.value, "CANDIDATE")
        self.assertEqual(literal.trust.authority.value, "SECONDARY")
        repeated = self.workflow.run(self.principal, source.encode("utf-8"), self.metadata)
        self.assertTrue(repeated.chunks[0].replayed)
        self.assertEqual(repeated.chunks[0].validation_attempts, recovered.chunks[0].validation_attempts)
        self.assertEqual(self._extraction_audits(), audits)
        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(self.embedding_calls, 1)

    def test_interrupted_parent_recovery_rejects_corrupt_resigned_and_competing_attempts(self) -> None:
        completions = self._enable_validation_feedback("invalid", "valid")
        _chunk, retained, attempts = self._interrupt_parent_assembly()
        terminal_id, original = attempts[-1]
        for mode in ("checksum", "predecessor", "unsupported_response", "competing_terminal"):
            with self.subTest(mode=mode):
                changed = deepcopy(original)
                current_id = terminal_id
                if mode == "checksum":
                    changed["response"] = "corrupt saved response"
                    self.driver.execute_query(
                        "MATCH (a:DerivationArtifact {artifact_id:$id}) SET a.payload_json=$payload",
                        id=terminal_id, payload=json.dumps(changed), database_=self.database)
                else:
                    if mode == "predecessor":
                        changed["previous_attempt_artifact_id"] = "missing-predecessor"
                    elif mode == "unsupported_response":
                        response = json.loads(changed["response"])
                        response["relationships"][0]["evidence"] = {"text": "Pump-7", "start": 10, "end": 16}
                        changed["response"] = json.dumps(response)
                        changed["response_checksum"] = hashlib.sha256(changed["response"].encode()).hexdigest()
                        changed["response_chars"] = len(changed["response"])
                    else:
                        changed["provider_seconds"] += 0.001
                    current_id = self._rewrite_saved_attempt(
                        terminal_id, changed, keep_original=mode == "competing_terminal")
                try:
                    with self.assertRaises(ConstructionConflict):
                        self.workflow.run(self.principal, SOURCE, self.metadata)
                    self.assertEqual(len(completions.calls), 2)
                    self.assertEqual(self.embedding_calls, 1)
                    self.assertEqual(self._proposal_and_outcome_counts(), {"mentions": 0, "assertions": 0, "outcomes": 0})
                    self.assertNotIn(original["parent_artifact_id"], self._extraction_audits())
                finally:
                    if mode == "competing_terminal":
                        self.driver.execute_query("MATCH (a:DerivationArtifact {artifact_id:$id}) DETACH DELETE a",
                                                  id=current_id, database_=self.database)
                    elif mode == "checksum":
                        self.driver.execute_query("MATCH (a:DerivationArtifact {artifact_id:$id}) SET a.payload_json=$payload",
                                                  id=terminal_id, payload=retained[terminal_id], database_=self.database)
                    else:
                        self.assertEqual(self._rewrite_saved_attempt(current_id, original), terminal_id)
                self.assertEqual(self._extraction_audits(), retained)

    def test_interrupted_attempt_recovery_requires_current_job_document_and_chunk_acl(self) -> None:
        completions = self._enable_validation_feedback("invalid", "valid")
        chunk, retained, attempts = self._interrupt_parent_assembly()
        scope = attempts[0][1]
        kwargs = {"job_id": scope["job_id"], "chunk": chunk,
                  "parent_artifact_id": scope["parent_artifact_id"], "profile_id": scope["profile_id"]}
        audit = self.workflow.audit_store
        self.assertEqual(len(audit.read_validation_attempts(self.principal, **kwargs)), 2)
        for outsider in (replace(self.principal, tenant_id="other-tenant"),
                         replace(self.principal, groups=frozenset({"public"})),
                         replace(self.principal, capabilities=frozenset())):
            with self.assertRaises(ConstructionAuthorizationError):
                audit.read_validation_attempts(outsider, **kwargs)
        for label, key, value in (("Document", "document_id", chunk.document_id),
                                  ("Chunk", "chunk_id", chunk.chunk_id),
                                  ("KnowledgeConstructionJob", "job_id", scope["job_id"])):
            with self.subTest(revoked=label):
                self.driver.execute_query(f"MATCH (n:{label} {{{key}:$id}}) SET n.access_groups=['restricted']",
                                          id=value, database_=self.database)
                try:
                    with self.assertRaises(ConstructionAuthorizationError):
                        audit.read_validation_attempts(self.principal, **kwargs)
                    with self.assertRaises((ConstructionAuthorizationError, ConstructionConflict, ConstructionIngestionFailed)):
                        self.workflow.run(self.principal, SOURCE, self.metadata)
                    self.assertEqual(len(completions.calls), 2)
                    self.assertEqual(self._proposal_and_outcome_counts(), {"mentions": 0, "assertions": 0, "outcomes": 0})
                finally:
                    self.driver.execute_query(f"MATCH (n:{label} {{{key}:$id}}) SET n.access_groups=['board']",
                                              id=value, database_=self.database)
                self.assertEqual(self._extraction_audits(), retained)
        recovered = self.workflow.run(self.principal, SOURCE, self.metadata)
        self.assertEqual(recovered.job_id, scope["job_id"])
        self.assertEqual(recovered.chunks[0].status, "CANDIDATE")
        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(self.embedding_calls, 1)

    def test_preparation_failure_projection_acl_and_explicit_new_operation_recovery(self) -> None:
        class APIConnectionError(ConnectionError):
            pass

        healthy_provider = self.workflow.embedding_provider

        def unavailable(**kwargs):
            raise APIConnectionError("private provider detail")

        self.workflow.embedding_provider = unavailable
        metadata = replace(self.metadata, max_attempts=1)
        with self.assertRaises(ConstructionIngestionFailed):
            self.workflow.run(self.principal, SOURCE, metadata)
        audit = self.workflow.audit_store
        jobs = audit.list_jobs(self.principal, statuses=("FAILED",))
        self.assertEqual(len(jobs), 1)
        failed = jobs[0]
        self.assertEqual(failed.completed_chunks, 0)
        self.assertEqual(failed.last_finding_codes, (
            "EMBEDDING_CONNECTION_ERROR", "INGESTION_ATTEMPTS_EXHAUSTED",
        ))
        self.assertEqual(audit.list_jobs(self.principal, statuses=("RUNNING",)), ())
        self.assertEqual(self.completions.calls, [])
        for forbidden in (
            replace(self.principal, tenant_id="other-tenant"),
            replace(self.principal, groups=frozenset({"public"})),
        ):
            self.assertIsNone(audit.get_job(forbidden, failed.job_id))
            self.assertEqual(audit.list_jobs(forbidden), ())
            self.assertIsNone(audit.sync_preparation_failure(forbidden, failed.job_id))

        # Old interrupted parent rows must read correctly without a GET mutation.
        self.driver.execute_query(
            "MATCH (j:KnowledgeConstructionJob {job_id:$id}) "
            "SET j.status='RUNNING', j.last_finding_codes=[]",
            id=failed.job_id, database_=self.database,
        )
        self.assertEqual(audit.get_job(self.principal, failed.job_id).status, "FAILED")
        self.assertEqual(len(audit.list_jobs(self.principal, statuses=("FAILED",))), 1)
        rows, _, _ = self.driver.execute_query(
            "MATCH (j:KnowledgeConstructionJob {job_id:$id}) RETURN j.status AS status",
            id=failed.job_id, database_=self.database,
        )
        self.assertEqual(rows[0]["status"], "RUNNING")
        self.assertEqual(audit.sync_preparation_failure(self.principal, failed.job_id), "FAILED")

        self.workflow.embedding_provider = healthy_provider
        # An exhausted operation cannot silently reset its attempt budget.
        with self.assertRaises(ConstructionIngestionFailed):
            self.workflow.run(self.principal, SOURCE, metadata)
        recovered = self.workflow.run(
            self.principal, SOURCE, replace(metadata, operation_key="explicit-retry"),
        )
        self.assertEqual(audit.get_job(self.principal, recovered.job_id).status, "COMPLETED")
        self.assertIsNone(audit.sync_preparation_failure(self.principal, recovered.job_id))
        self.assertEqual(audit.get_job(self.principal, failed.job_id).status, "FAILED")
        self.assertEqual(len(audit.list_jobs(self.principal)), 2)

    def test_validation_feedback_retains_rejection_before_candidate_and_replays_with_acl(self) -> None:
        completions = self._enable_validation_feedback("invalid", "valid")
        during_correction: list[tuple[dict[str, str], dict[str, int]]] = []

        def observe(number: int) -> None:
            if number == 2:
                during_correction.append((
                    self._extraction_audits(), self._proposal_and_outcome_counts(),
                ))

        completions.before_response = observe
        result = self.workflow.run(self.principal, SOURCE, self.metadata)
        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(len(during_correction), 1)
        early_audits, early_counts = during_correction[0]
        self.assertEqual(len(early_audits), 1)
        self.assertEqual(early_counts, {"mentions": 0, "assertions": 0, "outcomes": 0})
        early = json.loads(next(iter(early_audits.values())))
        self.assertEqual(early["audit_type"], "VALIDATION_ATTEMPT")
        self.assertEqual(early["status"], "REJECTED")
        self.assertIn("ENDPOINT_OUTSIDE_EVIDENCE", early["finding_codes"])

        chunk = result.chunks[0]
        self.assertEqual(chunk.status, "CANDIDATE")
        self.assertEqual(
            [(item.attempt, item.status) for item in chunk.validation_attempts],
            [(1, "REJECTED"), (2, "CANDIDATE")],
        )
        self.assertEqual(self._proposal_and_outcome_counts(), {
            "mentions": 2, "assertions": 1, "outcomes": 1,
        })
        audits = self._extraction_audits()
        self.assertEqual(len(audits), 3)
        self.assertTrue(early_audits.items() <= audits.items())
        aggregate = json.loads(audits[chunk.artifact_id])
        refs = aggregate["validation_attempt_artifacts"]
        self.assertEqual(len(refs), 2)
        attempts = [json.loads(audits[item["artifact_id"]]) for item in refs]
        self.assertIsNone(attempts[0]["previous_attempt_artifact_id"])
        self.assertEqual(attempts[1]["previous_attempt_artifact_id"], refs[0]["artifact_id"])
        self.assertEqual(attempts[0]["validation_run_id"], attempts[1]["validation_run_id"])
        for attempt in attempts:
            self.assertEqual(attempt["job_id"], result.job_id)
            self.assertEqual(attempt["tenant_id"], self.tenant_id)
            self.assertEqual(attempt["chunk_id"], chunk.chunk_id)
            self.assertEqual(attempt["ontology_version_id"], self.tbox.tbox_id)
            self.assertEqual(attempt["access_groups"], ["board"])
            self.assertEqual(attempt["response_checksum"], hashlib.sha256(
                attempt["response"].encode("utf-8")
            ).hexdigest())
        self.assertEqual(completions.calls[1]["messages"][-2]["content"], attempts[0]["response"])
        self.assertIn("ENDPOINT_OUTSIDE_EVIDENCE", completions.calls[1]["messages"][-1]["content"])

        audit = Neo4jConstructionAuditStore(self.driver, self.database)
        review = Neo4jKnowledgeReviewService(self.driver, self.database)
        visible = audit.get_job(self.principal, result.job_id)
        self.assertEqual(visible.chunks[0].validation_attempts, chunk.validation_attempts)
        self.assertEqual(len(review.review_queue(self.principal)), 3)
        for outsider in (
            replace(self.principal, tenant_id="tenant-other"),
            replace(self.principal, groups=frozenset({"public"})),
        ):
            with self.subTest(outsider=outsider):
                self.assertIsNone(audit.get_job(outsider, result.job_id))
                self.assertEqual(audit.list_jobs(outsider), ())
                self.assertIsNone(audit.read_outcome(
                    outsider, job_id=result.job_id, chunk_id=chunk.chunk_id,
                ))
                self.assertEqual(review.review_queue(outsider), ())

        replay = self.workflow.run(self.principal, SOURCE, self.metadata)
        self.assertTrue(replay.chunks[0].replayed)
        self.assertEqual(replay.chunks[0].validation_attempts, chunk.validation_attempts)
        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(self.embedding_calls, 1)
        self.assertEqual(self._extraction_audits(), audits)

        # A completed outcome cannot bypass the immutable response chain on
        # replay. Simulate storage corruption without changing its checksum.
        corrupted = dict(attempts[0], response="corrupted model response")
        self.driver.execute_query(
            """
            MATCH (artifact:DerivationArtifact {artifact_id: $artifact_id})
            SET artifact.payload_json = $payload_json
            """,
            artifact_id=refs[0]["artifact_id"],
            payload_json=json.dumps(corrupted),
            database_=self.database,
        )
        with self.assertRaises(ConstructionConflict):
            self.workflow.run(self.principal, SOURCE, self.metadata)
        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(self._proposal_and_outcome_counts(), {
            "mentions": 2, "assertions": 1, "outcomes": 1,
        })

    def test_validation_feedback_interruption_keeps_audit_and_recovery_uses_fresh_attempt(self) -> None:
        completions = self._enable_validation_feedback("invalid", "timeout", "valid")
        # Reserve correction capacity before ingestion or any model call.
        self.workflow.config = replace(self.workflow.config, max_model_calls=1)
        with self.assertRaises(ConstructionBudgetExceeded):
            self.workflow.run(self.principal, SOURCE, self.metadata)
        self.assertEqual(len(completions.calls), 0)
        self.assertEqual(self.embedding_calls, 0)
        self.assertEqual(self._extraction_audits(), {})
        self.workflow.config = replace(self.workflow.config, max_model_calls=4)

        with self.assertRaises(ExtractionRejected) as failure:
            self.workflow.run(self.principal, SOURCE, self.metadata)
        self.assertEqual([item.code for item in failure.exception.findings], ["MODEL_CALL_TIMEOUT"])
        self.assertEqual(len(completions.calls), 2, "provider errors must not auto-retry")
        interrupted_audits = self._extraction_audits()
        self.assertEqual(len(interrupted_audits), 2)
        interrupted = sorted(
            (json.loads(payload) for payload in interrupted_audits.values()),
            key=lambda item: item["attempt"],
        )
        self.assertEqual([item["status"] for item in interrupted], ["REJECTED", "PROVIDER_ERROR"])
        self.assertTrue(all(item["audit_type"] == "VALIDATION_ATTEMPT" for item in interrupted))
        self.assertIsNone(interrupted[1]["response"])
        self.assertIsNone(interrupted[1]["response_checksum"])
        self.assertEqual(self._proposal_and_outcome_counts(), {
            "mentions": 0, "assertions": 0, "outcomes": 0,
        })
        audit = Neo4jConstructionAuditStore(self.driver, self.database)
        waiting = audit.get_job(self.principal, interrupted[0]["job_id"])
        self.assertEqual(waiting.status, "RETRY_WAIT")
        self.assertEqual(waiting.completed_chunks, 0)

        recovered = self.workflow.run(self.principal, SOURCE, self.metadata)
        self.assertEqual(recovered.job_id, waiting.job_id)
        self.assertEqual(recovered.chunks[0].status, "CANDIDATE")
        self.assertEqual(len(completions.calls), 3)
        self.assertEqual(
            [(item.attempt, item.status) for item in recovered.chunks[0].validation_attempts],
            [(1, "CANDIDATE")],
        )
        audits = self._extraction_audits()
        self.assertEqual(len(audits), 4)
        self.assertTrue(interrupted_audits.items() <= audits.items())
        final = json.loads(audits[recovered.chunks[0].artifact_id])
        self.assertEqual(len(final["validation_attempt_artifacts"]), 1)
        reference = final["validation_attempt_artifacts"][0]
        corrected = json.loads(audits[reference["artifact_id"]])
        self.assertNotIn(reference["artifact_id"], interrupted_audits)
        self.assertNotEqual(corrected["validation_run_id"], interrupted[0]["validation_run_id"])
        self.assertIsNone(corrected["previous_attempt_artifact_id"])
        self.assertEqual(audit.get_job(self.principal, recovered.job_id).status, "COMPLETED")
        self.assertEqual(self._proposal_and_outcome_counts(), {
            "mentions": 2, "assertions": 1, "outcomes": 1,
        })
        replay = self.workflow.run(self.principal, SOURCE, self.metadata)
        self.assertTrue(replay.chunks[0].replayed)
        self.assertEqual(len(completions.calls), 3)
        self.assertEqual(self.embedding_calls, 1)
        self.assertEqual(self._extraction_audits(), audits)

    def test_real_upload_candidate_review_queue_replay_and_acl(self) -> None:
        schema_records, _, _ = self.driver.execute_query(
            """
            SHOW CONSTRAINTS YIELD name
            WHERE name STARTS WITH 'knowledge_construction_'
            RETURN name ORDER BY name
            """,
            database_=self.database,
        )
        self.assertEqual(
            {row["name"] for row in schema_records},
            {
                "knowledge_construction_job_id_unique",
                "knowledge_construction_job_operation_unique",
                "knowledge_construction_outcome_id_unique",
                "knowledge_construction_outcome_identity_unique",
            },
        )

        first = self.workflow.run(self.principal, SOURCE, self.metadata)
        self.assertEqual(len(first.chunks), 1)
        self.assertEqual(first.chunks[0].status, "CANDIDATE")
        self.assertFalse(first.chunks[0].replayed)
        self.assertEqual(len(self.completions.calls), 1)
        self.assertEqual(self.embedding_calls, 1)
        document_acl, _, _ = self.driver.execute_query(
            """
            MATCH (document:Document {tenant_id: $tenant_id})
            RETURN document.access_groups AS access_groups
            """,
            tenant_id=self.tenant_id,
            database_=self.database,
        )
        self.assertEqual(document_acl[0]["access_groups"], ["board"])

        counts, _, _ = self.driver.execute_query(
            """
            MATCH (document:Document {tenant_id: $tenant_id})
                  -[:ACTIVE_VERSION]->(version:DocumentVersion {
                      tenant_id: $tenant_id
                  })-[:HAS_CHUNK]->(chunk:Chunk {tenant_id: $tenant_id})
            MATCH (chunk)-[:HAS_EMBEDDING]->(embedding:ChunkEmbedding {
                tenant_id: $tenant_id
            })
            OPTIONAL MATCH (mention:GovernedEntityMentionRevision {
                tenant_id: $tenant_id,
                governance_status: 'CANDIDATE'
            })
            OPTIONAL MATCH (assertion:GovernedAssertionRevision {
                tenant_id: $tenant_id,
                governance_status: 'CANDIDATE'
            })
            OPTIONAL MATCH (job:KnowledgeConstructionJob {
                tenant_id: $tenant_id,
                status: 'COMPLETED'
            })-[:HAS_CHUNK_OUTCOME]->(outcome:KnowledgeConstructionChunkOutcome)
            RETURN count(DISTINCT document) AS documents,
                   count(DISTINCT version) AS versions,
                   count(DISTINCT chunk) AS chunks,
                   count(DISTINCT embedding) AS embeddings,
                   count(DISTINCT mention) AS mentions,
                   count(DISTINCT assertion) AS assertions,
                   count(DISTINCT job) AS jobs,
                   count(DISTINCT outcome) AS outcomes
            """,
            tenant_id=self.tenant_id,
            database_=self.database,
        )
        self.assertEqual(
            dict(counts[0]),
            {
                "documents": 1,
                "versions": 1,
                "chunks": 1,
                "embeddings": 1,
                "mentions": 2,
                "assertions": 1,
                "jobs": 1,
                "outcomes": 1,
            },
        )
        canonical, _, _ = self.driver.execute_query(
            "MATCH (entity:Entity) RETURN count(entity) AS count",
            database_=self.database,
        )
        self.assertEqual(canonical[0]["count"], 0)

        review = Neo4jKnowledgeReviewService(self.driver, self.database)
        queue = review.review_queue(self.principal)
        self.assertEqual(len(queue), 3)
        self.assertTrue(
            all(item.record.trust.status.value == "CANDIDATE" for item in queue)
        )
        wrong_group = Principal(
            "reviewer:mallory",
            self.tenant_id,
            frozenset({"public"}),
            frozenset({"knowledge:review"}),
        )
        wrong_tenant = Principal(
            "reviewer:mallory",
            "tenant-other",
            frozenset({"board"}),
            frozenset({"knowledge:review"}),
        )
        self.assertEqual(review.review_queue(wrong_group), ())
        self.assertEqual(review.review_queue(wrong_tenant), ())

        # Published jobs from before the optional mode field remain replayable.
        self.driver.execute_query(
            "MATCH (job:KnowledgeConstructionJob {job_id: $job_id}) REMOVE job.extraction_mode",
            job_id=first.job_id,
            database_=self.database,
        )
        second = self.workflow.run(self.principal, SOURCE, self.metadata)
        self.assertEqual(second.extraction_mode, "LLM")
        self.assertTrue(second.chunks[0].replayed)
        self.assertEqual(len(self.completions.calls), 1)
        self.assertEqual(self.embedding_calls, 1)
        revisions, _, _ = self.driver.execute_query(
            """
            MATCH (revision)
            WHERE revision:GovernedEntityMentionRevision
               OR revision:GovernedAssertionRevision
            RETURN count(revision) AS count
            """,
            database_=self.database,
        )
        self.assertEqual(revisions[0]["count"], 3)

        Neo4jConstructionAuditStore(
            self.driver,
            self.database,
        ).record_retryable_failure(
            tenant_id=self.tenant_id,
            job_id=first.job_id,
            chunk_id=first.chunks[0].chunk_id,
            findings=(
                ExtractionFinding(
                    "MODEL_CALL_FAILED",
                    "REJECT",
                    "$",
                    "simulated late retry",
                ),
            ),
            failed_at=NOW,
        )
        terminal, _, _ = self.driver.execute_query(
            """
            MATCH (job:KnowledgeConstructionJob {
                tenant_id: $tenant_id,
                job_id: $job_id
            })
            OPTIONAL MATCH (job)-[:HAS_CHUNK_OUTCOME]->(outcome)
            OPTIONAL MATCH (outcome)-[:USED_ARTIFACT]->(artifact:DerivationArtifact)
            RETURN job.status AS status,
                   count(DISTINCT outcome) AS outcomes,
                   count(DISTINCT artifact) AS artifacts
            """,
            tenant_id=self.tenant_id,
            job_id=first.job_id,
            database_=self.database,
        )
        self.assertEqual(terminal[0]["status"], "COMPLETED")
        self.assertEqual(terminal[0]["outcomes"], 1)
        self.assertEqual(terminal[0]["artifacts"], 1)

    def test_source_only_evidence_supports_explicit_authoritative_import_and_publish(self) -> None:
        def forbidden_factory(_tbox: TBoxVersion) -> None:
            self.fail("SOURCE_ONLY must not initialize the extractor")

        self.workflow.extractor_factory = forbidden_factory
        metadata = replace(self.metadata, extraction_mode="SOURCE_ONLY")
        source = self.workflow.run(self.principal, SOURCE, metadata)
        self.assertEqual(source.chunks[0].status, "SOURCE_ONLY")
        self.assertEqual(source.extraction_mode, "SOURCE_ONLY")
        self.assertEqual(self.embedding_calls, 1)
        self.assertEqual(self.completions.calls, [])
        review = Neo4jKnowledgeReviewService(self.driver, self.database)
        self.assertEqual(review.review_queue(self.principal), ())
        counts, _, _ = self.driver.execute_query(
            """
            MATCH (node)
            RETURN count(CASE WHEN node:GovernedEntityMentionRevision THEN 1 END) AS mentions,
                   count(CASE WHEN node:GovernedAssertionRevision THEN 1 END) AS assertions,
                   count(CASE WHEN node:KnowledgePublication THEN 1 END) AS publications,
                   count(CASE WHEN node:ChunkEmbedding THEN 1 END) AS embeddings
            """,
            database_=self.database,
        )
        self.assertEqual(dict(counts[0]), {
            "mentions": 0, "assertions": 0, "publications": 0, "embeddings": 1
        })

        audit = Neo4jConstructionAuditStore(self.driver, self.database)
        job = audit.get_job(self.principal, source.job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job.status, "COMPLETED")
        self.assertEqual(job.extraction_mode, "SOURCE_ONLY")
        self.assertEqual(job.chunks[0].status, "SOURCE_ONLY")
        for outsider in (
            replace(self.principal, tenant_id="tenant-other"),
            replace(self.principal, groups=frozenset({"public"})),
        ):
            with self.subTest(outsider=outsider):
                self.assertIsNone(audit.get_job(outsider, source.job_id))
                self.assertEqual(audit.list_jobs(outsider), ())

        principal = replace(
            self.principal,
            capabilities=self.principal.capabilities | {"knowledge:import", "knowledge:publish"},
        )
        adapter = Neo4jKnowledgeOperations(
            driver=self.driver, database=self.database, construction=self.workflow,
            clock=lambda: NOW,
        )

        def evidence(text: str, start: int) -> dict[str, object]:
            return {
                "document_id": source.document_id,
                "version_id": source.version_id,
                "chunk_id": source.chunks[0].chunk_id,
                "quoted_text": text,
                "char_start": start,
                "char_end": start + len(text),
            }

        request = AuthoritativeImportRequest.model_validate({
            "ontology_version_id": self.tbox.tbox_id,
            "mentions": [
                {
                    "source_key": "expert-acme",
                    "entity": {"entity_type": "Company", "canonical_key": "company-id:acme",
                               "canonical_name": "Acme", "aliases": []},
                    "evidence": evidence("Acme", 0),
                },
                {
                    "source_key": "expert-pump-7",
                    "entity": {"entity_type": "Asset", "canonical_key": "asset-id:pump-7",
                               "canonical_name": "Pump-7", "aliases": []},
                    "evidence": evidence("Pump-7", 10),
                },
            ],
            "assertions": [{
                "source_key": "expert-owns",
                "subject_mention_source_key": "expert-acme",
                "predicate": "OWNS",
                "object_mention_source_key": "expert-pump-7",
                "evidence": evidence(SOURCE.decode(), 0),
            }],
            "review_notes": "Expert examples anchored to the initial source document.",
        })
        imported = adapter.authoritative_import(principal, request).payload
        self.assertEqual(imported.mention_count, 2)
        self.assertEqual(imported.assertion_count, 1)
        before_publish, _, _ = self.driver.execute_query(
            "MATCH (node:KnowledgePublication) RETURN count(node) AS count",
            database_=self.database,
        )
        self.assertEqual(before_publish[0]["count"], 0)
        publication = adapter.publish(
            principal,
            PublicationRequest(approved_revision_ids=imported.revision_ids),
        ).payload
        self.assertEqual(len(publication.published_revision_ids), 3)
        replay = self.workflow.run(self.principal, SOURCE, metadata)
        self.assertTrue(replay.chunks[0].replayed)
        self.assertEqual(self.embedding_calls, 1)
        self.assertEqual(self.completions.calls, [])

    def test_source_only_resume_and_mode_conflict_preserve_completed_source(self) -> None:
        metadata = replace(self.metadata, extraction_mode="SOURCE_ONLY")
        first = self.workflow.run(self.principal, SOURCE, metadata)
        with self.assertRaisesRegex(ConstructionConflict, "idempotency"):
            self.workflow.run(self.principal, SOURCE, self.metadata)
        self.assertEqual(self.completions.calls, [])
        self.assertEqual(self.embedding_calls, 1)
        self.driver.execute_query(
            """
            MATCH (job:KnowledgeConstructionJob {job_id: $job_id})
            MATCH (job)-[:HAS_CHUNK_OUTCOME]->(outcome)
            SET job.status = 'RUNNING', job.completed_chunks = 0
            REMOVE job.completed_at
            DETACH DELETE outcome
            """,
            job_id=first.job_id,
            database_=self.database,
        )
        recovered = self.workflow.run(self.principal, SOURCE, metadata)
        self.assertFalse(recovered.chunks[0].replayed)
        self.assertEqual(recovered.chunks[0].artifact_id, first.chunks[0].artifact_id)
        self.assertEqual(self.embedding_calls, 1)
        self.assertEqual(self.completions.calls, [])
        audit = Neo4jConstructionAuditStore(self.driver, self.database)
        job = audit.get_job(self.principal, first.job_id)
        self.assertEqual(job.status, "COMPLETED")
        self.assertEqual(job.completed_chunks, 1)
        extracted = self.workflow.run(
            self.principal,
            SOURCE,
            replace(self.metadata, operation_key="explicit-model-extraction"),
        )
        self.assertEqual(extracted.chunks[0].status, "CANDIDATE")
        self.assertEqual(extracted.snapshot_id, first.snapshot_id)
        self.assertEqual(extracted.version_id, first.version_id)
        self.assertEqual(extracted.chunks[0].chunk_id, first.chunks[0].chunk_id)
        self.assertNotEqual(extracted.chunks[0].artifact_id, first.chunks[0].artifact_id)
        self.assertEqual(self.embedding_calls, 1)
        self.assertEqual(len(self.completions.calls), 1)


if __name__ == "__main__":
    unittest.main()
