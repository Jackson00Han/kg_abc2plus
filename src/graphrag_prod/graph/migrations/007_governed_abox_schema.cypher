// Append-only governed A-Box record heads and immutable revisions.
CREATE CONSTRAINT knowledge_record_head_id_unique IF NOT EXISTS
FOR (node:KnowledgeRecordHead) REQUIRE node.record_id IS UNIQUE;

CREATE CONSTRAINT governed_entity_mention_revision_id_unique IF NOT EXISTS
FOR (node:GovernedEntityMentionRevision) REQUIRE node.revision_id IS UNIQUE;

CREATE CONSTRAINT governed_entity_mention_revision_identity_unique IF NOT EXISTS
FOR (node:GovernedEntityMentionRevision) REQUIRE
    (node.tenant_id, node.record_id, node.revision) IS UNIQUE;

CREATE CONSTRAINT governed_assertion_revision_id_unique IF NOT EXISTS
FOR (node:GovernedAssertionRevision) REQUIRE node.revision_id IS UNIQUE;

CREATE CONSTRAINT governed_assertion_revision_identity_unique IF NOT EXISTS
FOR (node:GovernedAssertionRevision) REQUIRE
    (node.tenant_id, node.record_id, node.revision) IS UNIQUE;

CREATE INDEX knowledge_record_head_lookup IF NOT EXISTS
FOR (node:KnowledgeRecordHead) ON
    (node.tenant_id, node.record_kind, node.current_revision);

CREATE INDEX governed_entity_mention_review_lookup IF NOT EXISTS
FOR (node:GovernedEntityMentionRevision) ON
    (node.tenant_id, node.governance_status, node.authority_level);

CREATE INDEX governed_entity_mention_evidence_lookup IF NOT EXISTS
FOR (node:GovernedEntityMentionRevision) ON
    (node.tenant_id, node.chunk_id);

CREATE INDEX governed_assertion_review_lookup IF NOT EXISTS
FOR (node:GovernedAssertionRevision) ON
    (node.tenant_id, node.governance_status, node.authority_level);

CREATE INDEX governed_assertion_evidence_lookup IF NOT EXISTS
FOR (node:GovernedAssertionRevision) ON
    (node.tenant_id, node.chunk_id);
