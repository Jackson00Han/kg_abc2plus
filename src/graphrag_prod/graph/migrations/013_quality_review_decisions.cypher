// Human conclusions are separate immutable events, never automatic report edits.
CREATE CONSTRAINT quality_review_decision_id_unique IF NOT EXISTS
FOR (node:QualityReviewDecision) REQUIRE node.review_id IS UNIQUE;
CREATE INDEX quality_review_decision_run_lookup IF NOT EXISTS
FOR (node:QualityReviewDecision) ON (node.tenant_id, node.run_id, node.recorded_at);
