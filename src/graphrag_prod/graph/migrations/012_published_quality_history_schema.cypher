// Immutable audit evidence for active governed-graph quality runs.
CREATE CONSTRAINT published_graph_quality_run_id_unique IF NOT EXISTS
FOR (node:PublishedGraphQualityRun) REQUIRE node.run_id IS UNIQUE;

CREATE CONSTRAINT published_graph_quality_issue_id_unique IF NOT EXISTS
FOR (node:PublishedGraphQualityIssue) REQUIRE node.issue_id IS UNIQUE;

CREATE CONSTRAINT published_graph_quality_sample_id_unique IF NOT EXISTS
FOR (node:PublishedGraphQualityReviewSample) REQUIRE node.sample_id IS UNIQUE;

CREATE CONSTRAINT published_graph_quality_acl_requirement_id_unique IF NOT EXISTS
FOR (node:PublishedGraphQualityAclRequirement) REQUIRE node.requirement_id IS UNIQUE;

CREATE INDEX published_graph_quality_run_lookup IF NOT EXISTS
FOR (node:PublishedGraphQualityRun) ON
    (node.tenant_id, node.publication_generation, node.recorded_at);
