// Source-pinned additive context manifests and immutable mapper attempts.
CREATE CONSTRAINT context_projection_run_id_unique IF NOT EXISTS
FOR (run:KnowledgeContextProjectionRun) REQUIRE run.run_id IS UNIQUE;

CREATE CONSTRAINT context_mapping_attempt_id_unique IF NOT EXISTS
FOR (attempt:KnowledgeContextMappingAttempt) REQUIRE attempt.attempt_id IS UNIQUE;
