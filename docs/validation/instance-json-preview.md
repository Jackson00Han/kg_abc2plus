# Complete instance JSON preview

The publication preview now includes `instances_after`, a deterministic read-only
projection of the complete validated post-publication manifest, including retained
records. The collapsed **查看完整实例 JSON** section displays all entities with
nested property assertions, relationships with named endpoints, and exact evidence
references. Counts reflect the full arrays; there is no display truncation.

The snapshot carries its schema, PREVIEW status, PUBLICATION_AFTER scope,
publication ID, ontology version and manifest hash. Separate property assertions
remain separate, even for the same predicate, preserving values, literal semantics,
source grades and evidence. Source mentions of one identity are grouped, with
aliases and evidence retained. The original complete change JSON remains available
in a separate audit disclosure.

This is not a Neo4j node-property serialization or an import contract. The existing
publication transaction continues to validate governed records and write its graph
model. The browser submits only the selected revisions/removals and expected
preview hash. The snapshot is included in that hash; stale previews must be
regenerated. The preview endpoint still rolls back its transaction, and the new
view never claims the preview has already been persisted.

## Repeatable validation

- `uv run --locked python -m unittest discover -s tests/unit -q`
- `uv run --locked python -m unittest discover -s tests/e2e -q`
- `uv run --locked python -m unittest discover -s tests/security -q`
- `uv run --locked python -m unittest discover -s tests/regression -q`
- `sh scripts/run_stage8_neo4j_tests.sh /tmp/graphrag-json-neo4j.json /tmp/graphrag-json-observations`
  (Use fresh output paths for subsequent runs.)
- `uv run --locked python -m compileall -q src tests scripts`
- `git diff --check`

Focused tests cover multiple entities, repeated property predicates, named relation
endpoints, evidence linkage, deterministic ordering, empty manifests, disclosure
rendering, and the absence of display JSON from publication requests. Disposable
Neo4j coverage checks preview rollback, exact published manifest equivalence,
replay, stale hashes, missing dependencies and removal behavior.

## Results (2026-09-09)

- Unit: 1037 passed; HTTP E2E: 18 passed; security: 54 passed;
  regression: 2 passed.
- Focused projection and API checks: 12 passed. The API also rejects a publication
  request containing `instances_after` with HTTP 422.
- Compilation, lock check, package build, deterministic evaluation gold and
  development corpus checks, and acceptance-contract validation passed.
- First complete disposable-Neo4j run: 175/176 passed. The unchanged
  `test_exact_vector_queries_use_pinned_generation_and_candidate_id_seeks`
  observed two `NodeUniqueIndexSeek` operators instead of one. Functional result
  and ACL assertions before the plan assertion passed. This run is not reported
  as an all-green full suite.
- In a fresh disposable database under identical resource caps, the complete
  knowledge-review module passed 21/21 and the complete retrieval module passed
  10/10, including the previously failing assertion. No retrieval implementation
  or test assertion was changed. The plan-count failure was not reproduced;
  its root cause remains unconfirmed, and full-suite plan stability remains a
  validation limitation. Relevant suites were rerun in full before commit.
- Logs: `/tmp/graphrag-json-neo4j.log`, `/tmp/graphrag-json-neo4j.json`,
  `/tmp/graphrag-json-recheck.log`, `/tmp/graphrag-json-recheck.json`.
  Recheck used the same Stage 8 disposable-container setup, first selecting
  `test_knowledge_review_neo4j.py`, then running
  `python -m unittest tests.integration.test_retrieval_neo4j -v` with the same
  disposable database environment. Temporary runner: `/tmp/graphrag-json-recheck.sh`.
- Whitespace and staged-file secret/generated-file scans passed.

Restart the local service after upgrading the backend, reload the page, and
generate a fresh publication preview. No existing knowledge needs re-ingestion.
This change is development validation, not new production-scale evidence.
