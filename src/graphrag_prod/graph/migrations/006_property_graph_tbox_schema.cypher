// Tenant-owned property-graph T-Box catalogs, immutable versions, and definitions.
CREATE CONSTRAINT tbox_catalog_identity_unique IF NOT EXISTS
FOR (node:TBoxCatalog) REQUIRE (node.tenant_id, node.key) IS UNIQUE;

CREATE CONSTRAINT tbox_version_id_unique IF NOT EXISTS
FOR (node:TBoxVersion) REQUIRE node.tbox_id IS UNIQUE;

CREATE CONSTRAINT tbox_version_identity_unique IF NOT EXISTS
FOR (node:TBoxVersion) REQUIRE (node.tenant_id, node.key, node.version) IS UNIQUE;

CREATE CONSTRAINT tbox_entity_type_id_unique IF NOT EXISTS
FOR (node:TBoxEntityType) REQUIRE node.entity_type_id IS UNIQUE;

CREATE CONSTRAINT tbox_entity_type_identity_unique IF NOT EXISTS
FOR (node:TBoxEntityType) REQUIRE (node.tbox_id, node.name) IS UNIQUE;

CREATE CONSTRAINT tbox_relationship_type_id_unique IF NOT EXISTS
FOR (node:TBoxRelationshipType) REQUIRE node.relationship_type_id IS UNIQUE;

CREATE CONSTRAINT tbox_relationship_type_identity_unique IF NOT EXISTS
FOR (node:TBoxRelationshipType) REQUIRE (node.tbox_id, node.name) IS UNIQUE;

CREATE CONSTRAINT tbox_property_definition_id_unique IF NOT EXISTS
FOR (node:TBoxPropertyDefinition) REQUIRE node.property_definition_id IS UNIQUE;

CREATE CONSTRAINT tbox_property_definition_identity_unique IF NOT EXISTS
FOR (node:TBoxPropertyDefinition) REQUIRE
    (node.tbox_id, node.owner_kind, node.owner_name, node.name) IS UNIQUE;

CREATE INDEX tbox_version_tenant_status_lookup IF NOT EXISTS
FOR (node:TBoxVersion) ON (node.tenant_id, node.key, node.status);

CREATE INDEX tbox_entity_type_tenant_lookup IF NOT EXISTS
FOR (node:TBoxEntityType) ON (node.tenant_id, node.tbox_id);

CREATE INDEX tbox_relationship_type_tenant_lookup IF NOT EXISTS
FOR (node:TBoxRelationshipType) ON (node.tenant_id, node.tbox_id);
