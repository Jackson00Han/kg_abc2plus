"""Disposable-Neo4j validation of upload-to-review knowledge construction."""

from __future__ import annotations

from datetime import UTC, datetime
import ipaddress
import json
import os
from types import SimpleNamespace
import unittest
from urllib.parse import urlparse

import neo4j

from graphrag_prod.construction import (
    ConstructionConfig,
    ConstructionMetadata,
    Neo4jConstructionAuditStore,
    Neo4jKnowledgeConstructionWorkflow,
    OpenAICompatibleOntologyExtractor,
)
from graphrag_prod.construction.extraction import ExtractionFinding
from graphrag_prod.domain.access import Principal
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
        }
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(payload)},
                }
            ]
        }


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
                EntityTypeDefinition("Company", ("company-id",)),
                EntityTypeDefinition("Asset", ("asset-id",)),
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
            frozenset({"engineers"}),
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
            published_at=NOW,
        )

    def tearDown(self) -> None:
        self.driver.execute_query(
            "MATCH (node) DETACH DELETE node",
            database_=self.database,
        )

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
            frozenset({"legal"}),
            frozenset({"knowledge:review"}),
        )
        wrong_tenant = Principal(
            "reviewer:mallory",
            "tenant-other",
            frozenset({"engineers"}),
            frozenset({"knowledge:review"}),
        )
        self.assertEqual(review.review_queue(wrong_group), ())
        self.assertEqual(review.review_queue(wrong_tenant), ())

        second = self.workflow.run(self.principal, SOURCE, self.metadata)
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


if __name__ == "__main__":
    unittest.main()
