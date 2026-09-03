// Append-only human review publications and atomic activation history.
CREATE CONSTRAINT knowledge_publication_state_tenant_unique IF NOT EXISTS
FOR (node:KnowledgePublicationState) REQUIRE node.tenant_id IS UNIQUE;

CREATE CONSTRAINT knowledge_publication_id_unique IF NOT EXISTS
FOR (node:KnowledgePublication) REQUIRE node.publication_id IS UNIQUE;

CREATE CONSTRAINT knowledge_publication_identity_unique IF NOT EXISTS
FOR (node:KnowledgePublication) REQUIRE
    (node.tenant_id, node.generation) IS UNIQUE;

CREATE CONSTRAINT knowledge_publication_activation_id_unique IF NOT EXISTS
FOR (node:KnowledgePublicationActivation) REQUIRE node.activation_id IS UNIQUE;

CREATE CONSTRAINT knowledge_publication_activation_identity_unique IF NOT EXISTS
FOR (node:KnowledgePublicationActivation) REQUIRE
    (node.tenant_id, node.activation_generation) IS UNIQUE;

CREATE INDEX knowledge_publication_status_lookup IF NOT EXISTS
FOR (node:KnowledgePublication) ON
    (node.tenant_id, node.status, node.generation);

CREATE INDEX knowledge_publication_activation_lookup IF NOT EXISTS
FOR (node:KnowledgePublicationActivation) ON
    (node.tenant_id, node.activated_at);
