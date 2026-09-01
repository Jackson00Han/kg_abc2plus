# Stage 4 Validation Record

- Date: 2026-09-01
- Acceptance contract: `contracts/acceptance.v1.json` version 1.0.1
- Governance catalog: `contracts/graph_governance.v1.json` version 1.0.0
- Review dataset: `evaluation/graph-review-v1.json` version 1.0.0, 60 items
- Python: 3.12.12
- Database fixture: `neo4j:5.26.12-community`
- Image digest: `sha256:9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37`

## Delivered

- Versioned allowed Entity properties/types/key namespaces and Assertion
  properties/predicates/endpoint patterns.
- A fail-closed ingestion gate with conservative name/alias normalization,
  entity rejection, Assertion quarantine, and persisted finding evidence.
- Deterministic authoritative-identifier entity resolution with explicit
  homonym protection, rule/rationale records, and mention-evidence links.
- Tenant/active-snapshot-scoped graph audits for invalid patterns, unsupported
  claims, missing provenance, normalized duplicates, alias collisions,
  isolation, orphans, and anomalous hubs.
- Immutable quality reports, deterministic human-review samples, and audited
  Entity/Assertion accept/quarantine decisions.
- Governance-aware provenance reads that exclude quarantined Assertions and
  either quarantined endpoint.
- A 60-item adjudicated fixture and exact Stage 1 graph metric evaluator.
- Ordered idempotent Neo4j migration 003 and a resource-bounded disposable
  Stage 4 runner.

## Reproducible checks

Run from the repository root:

```bash
env -u OPENAI_API_KEY -u OPENAI_BASE_URL \
  -u NEO4J_URI -u NEO4J_PASSWORD \
  uv run --locked python -m unittest discover \
  -s tests/unit -p 'test_*.py' -v

./scripts/run_stage4_neo4j_tests.sh
uv run --locked python scripts/evaluate_graph_review.py
uv lock --check
uv build --out-dir /tmp/sample-graphrag-stage4-dist
uv run --locked python -m compileall -q src tests scripts
sh -n scripts/run_stage2_neo4j_tests.sh
sh -n scripts/run_stage3_neo4j_tests.sh
sh -n scripts/run_stage4_neo4j_tests.sh
python3 scripts/validate_acceptance_contract.py
git diff --check
```

The disposable runner refuses an existing fixed-name container, binds Bolt to
`127.0.0.1:17689`, requires an empty database, removes application provider
credentials, runs the full Neo4j integration suite, and removes the container
on exit. The `dev-mini` resource cap remains 1.5 GiB, one CPU, a 512 MiB heap,
and a 128 MiB page cache.

## Verified behavior

- 75 offline unit tests pass.
- 48 real-Neo4j integration tests pass.
- The 60-item adjudicated fixture measures entity precision 1.0, relationship
  precision 1.0, and entity-resolution accuracy 1.0; all exceed 0.95.
- Unknown entity/key schemas and invalid relationship endpoint patterns fail
  before snapshot publication.
- Low-confidence claims remain traceable but unaccepted, and their quarantine
  reasons survive as immutable snapshot findings.
- Exact authoritative identifiers merge deterministically; conflicting IDs,
  types, or tenants never merge; name-only homonyms require human review.
- Resolution records link to every declared EntityMention and reject missing
  or cross-tenant evidence.
- Clean reports and review samples reproduce for the same tenant, corpus
  revision, policy, time, and seed.
- Corrupted evidence, invalid patterns, normalized duplicates, missing
  mentions, orphans, and anomalous hubs are detected with stable issue codes.
- Reviewer quarantine immediately removes the target from evidence reads;
  acceptance fails while a structural error remains.
- Applying all three migrations twice is safe and all expected constraints and
  indexes have the exact online shape.
- All Stage 2/3 provenance, ingestion, recovery, deletion, vector-generation,
  and authorization integration tests continue to pass.

## Decision and limitations

Stage 4 passes its exit criteria: the committed adjudicated sample exceeds all
Stage 1 graph-quality targets, accepted relationships retain exact source
evidence, schema violations cannot publish, and quality exceptions are
detectable and auditable.

This is a governance reference implementation, not a live review operation.
The deterministic sample establishes the declared fixture metrics but not
quality on a future customer corpus. Candidate generation, provider-specific
semantic adjudication, reviewer UI/work queues, API authorization, production
retrieval, unified Stage 8 regression runs, and representative-scale Stage 9
validation remain outside this stage.
