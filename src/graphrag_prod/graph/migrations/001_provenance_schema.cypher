// Stable application identifiers.
CREATE CONSTRAINT document_id_unique IF NOT EXISTS
FOR (node:Document) REQUIRE node.document_id IS UNIQUE;

CREATE CONSTRAINT document_identity_unique IF NOT EXISTS
FOR (node:Document) REQUIRE (node.tenant_id, node.canonical_uri) IS UNIQUE;

CREATE CONSTRAINT document_version_id_unique IF NOT EXISTS
FOR (node:DocumentVersion) REQUIRE node.version_id IS UNIQUE;

CREATE CONSTRAINT document_version_content_unique IF NOT EXISTS
FOR (node:DocumentVersion) REQUIRE
    (node.document_id, node.checksum, node.original_checksum) IS UNIQUE;

CREATE CONSTRAINT document_version_number_unique IF NOT EXISTS
FOR (node:DocumentVersion) REQUIRE
    (node.document_id, node.version_number) IS UNIQUE;

CREATE CONSTRAINT chunk_id_unique IF NOT EXISTS
FOR (node:Chunk) REQUIRE node.chunk_id IS UNIQUE;

CREATE CONSTRAINT chunk_ordinal_unique IF NOT EXISTS
FOR (node:Chunk) REQUIRE (node.version_id, node.ordinal) IS UNIQUE;

CREATE CONSTRAINT chunk_embedding_id_unique IF NOT EXISTS
FOR (node:ChunkEmbedding) REQUIRE node.embedding_id IS UNIQUE;

CREATE CONSTRAINT chunk_embedding_space_unique IF NOT EXISTS
FOR (node:ChunkEmbedding) REQUIRE
    (node.chunk_id, node.embedding_space_id) IS UNIQUE;

CREATE CONSTRAINT entity_id_unique IF NOT EXISTS
FOR (node:Entity) REQUIRE node.entity_id IS UNIQUE;

CREATE CONSTRAINT entity_identity_unique IF NOT EXISTS
FOR (node:Entity) REQUIRE
    (node.tenant_id, node.entity_type, node.canonical_key) IS UNIQUE;

CREATE CONSTRAINT entity_mention_id_unique IF NOT EXISTS
FOR (node:EntityMention) REQUIRE node.mention_id IS UNIQUE;

CREATE CONSTRAINT assertion_id_unique IF NOT EXISTS
FOR (node:Assertion) REQUIRE node.assertion_id IS UNIQUE;

// Access and version predicates used by every retrieval path.
CREATE INDEX document_tenant_id IF NOT EXISTS
FOR (node:Document) ON (node.tenant_id);

CREATE INDEX version_document_lookup IF NOT EXISTS
FOR (node:DocumentVersion) ON (node.tenant_id, node.document_id);

CREATE INDEX chunk_access_lookup IF NOT EXISTS
FOR (node:Chunk) ON (node.tenant_id, node.version_id);

CREATE INDEX embedding_space_lookup IF NOT EXISTS
FOR (node:ChunkEmbedding) ON (node.tenant_id, node.embedding_space_id);

CREATE INDEX entity_tenant_type IF NOT EXISTS
FOR (node:Entity) ON (node.tenant_id, node.entity_type);

CREATE INDEX assertion_access_lookup IF NOT EXISTS
FOR (node:Assertion) ON (node.tenant_id, node.accepted);
