// Durable upload-to-review construction jobs and per-Chunk audit outcomes.
CREATE CONSTRAINT knowledge_construction_job_id_unique IF NOT EXISTS
FOR (node:KnowledgeConstructionJob) REQUIRE node.job_id IS UNIQUE;

CREATE CONSTRAINT knowledge_construction_job_operation_unique IF NOT EXISTS
FOR (node:KnowledgeConstructionJob) REQUIRE
    (node.tenant_id, node.operation_key) IS UNIQUE;

CREATE CONSTRAINT knowledge_construction_outcome_id_unique IF NOT EXISTS
FOR (node:KnowledgeConstructionChunkOutcome) REQUIRE node.outcome_id IS UNIQUE;

CREATE CONSTRAINT knowledge_construction_outcome_identity_unique IF NOT EXISTS
FOR (node:KnowledgeConstructionChunkOutcome) REQUIRE
    (node.tenant_id, node.job_id, node.chunk_id) IS UNIQUE;

CREATE INDEX knowledge_construction_job_status_lookup IF NOT EXISTS
FOR (node:KnowledgeConstructionJob) ON
    (node.tenant_id, node.status, node.updated_at);

CREATE INDEX knowledge_construction_outcome_status_lookup IF NOT EXISTS
FOR (node:KnowledgeConstructionChunkOutcome) ON
    (node.tenant_id, node.status, node.completed_at);

CREATE INDEX knowledge_construction_outcome_chunk_lookup IF NOT EXISTS
FOR (node:KnowledgeConstructionChunkOutcome) ON
    (node.tenant_id, node.chunk_id);
