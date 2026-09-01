// A document version may be materialized by more than one splitter profile.
DROP CONSTRAINT chunk_ordinal_unique IF EXISTS;

CREATE CONSTRAINT chunk_splitter_ordinal_unique IF NOT EXISTS
FOR (node:Chunk) REQUIRE
    (node.version_id, node.splitter_version, node.ordinal) IS UNIQUE;

// Immutable pipeline and snapshot identities.
CREATE CONSTRAINT graph_pipeline_profile_id_unique IF NOT EXISTS
FOR (node:GraphPipelineProfile) REQUIRE node.profile_id IS UNIQUE;

CREATE CONSTRAINT graph_pipeline_profile_identity_unique IF NOT EXISTS
FOR (node:GraphPipelineProfile) REQUIRE
    (
        node.normalizer_signature,
        node.splitter_signature,
        node.extractor_signature,
        node.prompt_signature,
        node.schema_signature,
        node.code_signature
    ) IS UNIQUE;

CREATE CONSTRAINT knowledge_snapshot_id_unique IF NOT EXISTS
FOR (node:KnowledgeSnapshot) REQUIRE node.snapshot_id IS UNIQUE;

CREATE CONSTRAINT knowledge_snapshot_identity_unique IF NOT EXISTS
FOR (node:KnowledgeSnapshot) REQUIRE
    (node.version_id, node.profile_id) IS UNIQUE;

// Durable ingestion work identities and retry-safe derived artifacts.
CREATE CONSTRAINT ingestion_job_id_unique IF NOT EXISTS
FOR (node:IngestionJob) REQUIRE node.job_id IS UNIQUE;

CREATE CONSTRAINT ingestion_job_idempotency_unique IF NOT EXISTS
FOR (node:IngestionJob) REQUIRE
    (node.tenant_id, node.operation, node.idempotency_key) IS UNIQUE;

CREATE CONSTRAINT ingestion_task_id_unique IF NOT EXISTS
FOR (node:IngestionTask) REQUIRE node.task_id IS UNIQUE;

CREATE CONSTRAINT ingestion_task_identity_unique IF NOT EXISTS
FOR (node:IngestionTask) REQUIRE (node.job_id, node.chunk_id) IS UNIQUE;

CREATE CONSTRAINT derivation_artifact_id_unique IF NOT EXISTS
FOR (node:DerivationArtifact) REQUIRE node.artifact_id IS UNIQUE;

CREATE CONSTRAINT derivation_artifact_identity_unique IF NOT EXISTS
FOR (node:DerivationArtifact) REQUIRE
    (node.tenant_id, node.kind, node.input_hash, node.profile_id) IS UNIQUE;

CREATE CONSTRAINT embedding_index_generation_id_unique IF NOT EXISTS
FOR (node:EmbeddingIndexGeneration) REQUIRE node.generation_id IS UNIQUE;

CREATE CONSTRAINT embedding_index_generation_identity_unique IF NOT EXISTS
FOR (node:EmbeddingIndexGeneration) REQUIRE
    (node.tenant_id, node.embedding_space_id, node.generation_version) IS UNIQUE;

CREATE CONSTRAINT tenant_corpus_state_tenant_unique IF NOT EXISTS
FOR (node:TenantCorpusState) REQUIRE node.tenant_id IS UNIQUE;

CREATE CONSTRAINT document_tombstone_identity_unique IF NOT EXISTS
FOR (node:DocumentTombstone) REQUIRE
    (node.tenant_id, node.document_id) IS UNIQUE;

// Operational lookup paths used by snapshot activation and resumable workers.
CREATE INDEX knowledge_snapshot_document_lookup IF NOT EXISTS
FOR (node:KnowledgeSnapshot) ON (node.tenant_id, node.document_id);

CREATE INDEX ingestion_job_status_lookup IF NOT EXISTS
FOR (node:IngestionJob) ON (node.tenant_id, node.status, node.next_retry_at);

CREATE INDEX ingestion_job_lease_lookup IF NOT EXISTS
FOR (node:IngestionJob) ON (node.status, node.lease_expires_at);

CREATE INDEX ingestion_task_status_lookup IF NOT EXISTS
FOR (node:IngestionTask) ON (node.job_id, node.status);

CREATE INDEX embedding_generation_state_lookup IF NOT EXISTS
FOR (node:EmbeddingIndexGeneration) ON
    (node.tenant_id, node.embedding_space_id, node.state);

CREATE INDEX document_tombstone_generation_lookup IF NOT EXISTS
FOR (node:DocumentTombstone) ON
    (node.tenant_id, node.document_id, node.generation);
