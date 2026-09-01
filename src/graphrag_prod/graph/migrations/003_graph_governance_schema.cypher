// Versioned governance policies, reproducible reports, and review decisions.
CREATE CONSTRAINT graph_governance_policy_id_unique IF NOT EXISTS
FOR (node:GraphGovernancePolicy) REQUIRE node.policy_id IS UNIQUE;

CREATE CONSTRAINT graph_quality_run_id_unique IF NOT EXISTS
FOR (node:GraphQualityRun) REQUIRE node.run_id IS UNIQUE;

CREATE CONSTRAINT graph_quality_issue_id_unique IF NOT EXISTS
FOR (node:GraphQualityIssue) REQUIRE node.issue_id IS UNIQUE;

CREATE CONSTRAINT graph_review_decision_id_unique IF NOT EXISTS
FOR (node:GraphReviewDecision) REQUIRE node.decision_id IS UNIQUE;

CREATE CONSTRAINT graph_governance_finding_id_unique IF NOT EXISTS
FOR (node:GraphGovernanceFinding) REQUIRE node.finding_id IS UNIQUE;

CREATE CONSTRAINT entity_resolution_decision_id_unique IF NOT EXISTS
FOR (node:EntityResolutionDecision) REQUIRE node.decision_id IS UNIQUE;

CREATE INDEX entity_governance_status_lookup IF NOT EXISTS
FOR (node:Entity) ON (node.tenant_id, node.governance_status);

CREATE INDEX assertion_governance_status_lookup IF NOT EXISTS
FOR (node:Assertion) ON (node.tenant_id, node.governance_status);

CREATE INDEX graph_quality_run_lookup IF NOT EXISTS
FOR (node:GraphQualityRun) ON
    (node.tenant_id, node.policy_id, node.corpus_revision);

CREATE INDEX graph_review_target_lookup IF NOT EXISTS
FOR (node:GraphReviewDecision) ON
    (node.tenant_id, node.target_kind, node.target_id);

CREATE INDEX entity_resolution_decision_lookup IF NOT EXISTS
FOR (node:EntityResolutionDecision) ON
    (node.tenant_id, node.outcome, node.rule_id);
