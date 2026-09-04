CREATE CONSTRAINT relationship_property_value_id_unique IF NOT EXISTS
FOR (node:RelationshipPropertyValue) REQUIRE node.property_value_id IS UNIQUE;

CREATE INDEX relationship_property_value_access_lookup IF NOT EXISTS
FOR (node:RelationshipPropertyValue) ON (node.tenant_id, node.evidence_chunk_id);
