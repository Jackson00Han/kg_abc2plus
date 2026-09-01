# Stage 2 Validation Record

- Date: 2026-09-01
- Acceptance contract: `contracts/acceptance.v1.json` version 1.0.1
- Data model: stable ID scheme version 1
- Python: 3.12.12
- Database fixture: `neo4j:5.26.12-community`
- Image digest: `sha256:9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37`

## Delivered

- Installable `graphrag_prod` package with credential-free domain imports.
- Frozen Document, DocumentVersion, Chunk, ChunkEmbedding, Entity,
  EntityMention, and reified Assertion records.
- Deterministic application IDs and exact source/evidence validation.
- Default-deny tenant/group authorization and policy-version rollback defense.
- Idempotent Neo4j constraints/index migration plus structural verification.
- Transactional provenance persistence and authorized Assertion-to-source read.
- Locked dependency graph in `uv.lock` and a disposable Neo4j test runner.
- Design decisions and Community Edition limitations in
  `docs/provenance_model.md`.

## Reproducible Checks

Run from the repository root:

```bash
env -u OPENAI_API_KEY -u OPENAI_BASE_URL \
  -u NEO4J_URI -u NEO4J_PASSWORD \
  uv run --locked python -m unittest discover \
  -s tests/unit -p 'test_*.py' -v

sh scripts/run_stage2_neo4j_tests.sh
uv lock --check
uv build --out-dir /tmp/sample-graphrag-stage2-dist
uv run --locked python -m compileall -q src/graphrag_prod tests
sh -n scripts/run_stage2_neo4j_tests.sh
python3 scripts/validate_acceptance_contract.py
git diff --check
```

The disposable runner refuses an existing fixed-name container, binds Bolt to
`127.0.0.1:17687`, requires an initially empty database, clears application
credentials, runs only the integration suite, and removes the container on
exit. It never targets the database configured in `.env`. The current local
runner also caps Neo4j at 1.5 GiB, one CPU, a 512 MiB maximum heap, and a 128 MiB
page cache under the `dev-mini` profile.

## Verified Behavior

- 28 offline unit tests pass.
- 13 real-Neo4j integration tests pass.
- Applying the migration twice is safe; all expected schema objects have the
  exact shape and all indexes are online.
- Every stable and natural identity uniqueness constraint rejects duplicates.
- The exact Chunk text, offsets, checksums, Version, Document, URI, and source
  round-trip from an Assertion.
- Entity mentions preserve their exact surface and source offsets.
- Wrong-tenant, wrong-group, Document-denied, Chunk-denied, unpublished, and
  unaccepted reads return no evidence.
- Access policy v2 cannot be rolled back by replaying v1 or by changing state
  under the same version.
- Immutable-ID conflicts and second-active-version attempts roll back without
  partial writes.
- Rebuilding an empty graph preserves all application IDs and evidence paths.
- Multiple embedding spaces remain separate and cannot alias one another.
- The production package contains no `elementId` dependency.

## Decision and Limitations

Stage 2 passes its exit criteria: every accepted derived fact represented by
the production model can be traced to one immutable DocumentVersion, exact
Chunk, and exact character evidence under query-time authorization.

This is a data/provenance foundation, not a complete knowledge base. Ingestion
lifecycle, graph governance, production retrieval, grounded generation, API
hardening, evaluation, and load/recovery validation remain gated by Stages
3-9. Required property existence is enforced at the application boundary
because the validated Community Edition schema cannot express all Enterprise
existence/type constraints.

Accordingly, this stage does not claim the contract's retrieval/answer quality,
10,000-Chunk scale, latency, throughput, cost, or complete security targets.
Those metrics require the later integrated evaluation corpus and workflows.
