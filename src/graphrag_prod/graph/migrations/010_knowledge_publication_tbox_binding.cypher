// Backfill an exact immutable T-Box binding for pre-v10 publications.
// Mixed-version or missing-version manifests intentionally remain unbound and
// therefore fail closed in publication replay and graph projection.
MATCH (publication:KnowledgePublication)
OPTIONAL MATCH (publication)-[:PUBLISHES_KNOWLEDGE_REVISION]->(revision)
WITH publication,
     count(revision) AS revision_count,
     count(
         CASE
             WHEN revision.tenant_id = publication.tenant_id
              AND revision.ontology_version_id IS NOT NULL
             THEN 1
         END
     ) AS proven_revision_count,
     collect(DISTINCT revision.ontology_version_id) AS ontology_version_ids
WHERE revision_count > 0
  AND proven_revision_count = revision_count
  AND size(ontology_version_ids) = 1
WITH publication, ontology_version_ids[0] AS ontology_version_id
WHERE publication.ontology_version_id IS NULL
   OR publication.ontology_version_id = ontology_version_id
MATCH (tbox:TBoxVersion {
    tenant_id: publication.tenant_id,
    tbox_id: ontology_version_id
})
WHERE tbox.status IN ['PUBLISHED', 'RETIRED']
SET publication.ontology_version_id = ontology_version_id
MERGE (publication)-[:USES_TBOX_VERSION]->(tbox);
