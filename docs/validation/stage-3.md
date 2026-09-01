# Stage 3 Validation Record

- Date: 2026-09-01
- Acceptance contract: `contracts/acceptance.v1.json` version 1.0.1
- Stable ID scheme: version 1
- Python: 3.12.12
- Database fixture: `neo4j:5.26.12-community`
- Image digest: `sha256:9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37`

## Delivered

- Deterministic graph pipeline profiles, sealed knowledge snapshots, jobs,
  tasks, derivation artifacts, tenant corpus state, and deletion tombstones.
- Cache-before-compute provider orchestration with durable work registered
  before calls, stable provider idempotency keys, immediate artifact commits,
  and missing-artifact-only recovery.
- Fenced worker leases, bounded retries, exact manifest verification, invisible
  staging, and atomic active-snapshot/version publication.
- Explicit unchanged, update, reprocess, metadata/ACL update, deletion, and
  already-absent outcomes with CAS and source-generation protection.
- Tenant-scoped physical deletion of source/derived records, orphan artifact
  cleanup, shared-entity preservation, and retained operational audit jobs.
- Independent embedding-space materialization, real generation-specific Neo4j
  vector indexes, exact active-corpus coverage checks, atomic cutover, and
  automatic stale-generation invalidation after corpus mutation.
- Tenant-lock enforcement that disables the legacy direct writer after managed
  incremental ingestion begins, preventing ACL and vector-lifecycle bypasses.
- Ordered idempotent Neo4j migrations, a disposable Stage 3 runner, lifecycle
  documentation, and deterministic multi-chunk/provider fixtures.

## Reproducible Checks

Run from the repository root:

```bash
env -u OPENAI_API_KEY -u OPENAI_BASE_URL \
  -u NEO4J_URI -u NEO4J_PASSWORD \
  uv run --locked python -m unittest discover \
  -s tests/unit -p 'test_*.py' -v

./scripts/run_stage3_neo4j_tests.sh
uv lock --check
uv build --out-dir /tmp/sample-graphrag-stage3-dist
uv run --locked python -m compileall -q src tests
sh -n scripts/run_stage2_neo4j_tests.sh
sh -n scripts/run_stage3_neo4j_tests.sh
python3 scripts/validate_acceptance_contract.py
git diff --check
```

The Stage 3 runner refuses an existing fixed-name container, binds Bolt only
to `127.0.0.1:17688`, requires an initially empty database, clears application
credentials, runs the complete Neo4j integration suite, and removes the
container on exit. It never targets the database configured in `.env`. Under
the default `dev-mini` profile it caps Neo4j at 1.5 GiB, one CPU, a 512 MiB
maximum heap, and a 128 MiB page cache.

## Verified Behavior

- 49 offline unit tests pass.
- 41 real-Neo4j integration tests pass.
- Repeating the same job or losing the response after publish yields one
  canonical published graph and one terminal result.
- A changed request cannot reuse an idempotency key; stale source generations
  and active-snapshot CAS mismatches fail closed.
- A worker taking over an expired lease receives a new fencing token; the old
  worker cannot record failure or finish that job.
- A provider interruption leaves a durable `RETRY_WAIT` job and committed
  artifacts. Retry invokes only missing provider work. Across versions, only
  the changed middle chunk calls either expensive provider.
- Provider output returning after first-create/update deletion is generation
  fenced and cannot recreate source, graph, task, or artifact residue. Permanent
  failures that leave only snapshot residue are also deletion-visible and cleaned.
- Building/failed snapshots return no assertion evidence. Publication exposes
  the exact sealed chunks/entities/mentions/assertions in one pointer switch.
- Same-snapshot title/newer-ACL changes update the Document and every active
  chunk atomically; ACL rollback or changed state under one version is rejected.
- Accepted deletion leaves zero source versions, chunks, vectors, mentions,
  assertions, unsupported entities, document tasks, or orphan artifacts in the
  target tenant while preserving another tenant and retained audit jobs.
- Tombstone generations prevent a stale upsert from resurrecting deleted data.
- Vector generations require 100% active-chunk coverage, use a verified cosine
  index, reject zero/non-finite/non-float32-indexable vectors, never mix
  embedding spaces, and are made unselectable (`STALE`) in the same corpus
  transaction as a later publish/delete.
- Profile-only embedding migration preserves the graph snapshot, exact-snapshot
  conditional materialization cannot cross documents, and re-preparing an
  active generation cannot label unpublished staging vectors.
- Advancing the worker clock does not change replay fingerprints or recompute
  artifacts, and replaying a retired terminal job cannot displace a newer
  active snapshot.
- The legacy provenance writer cannot mutate a managed tenant's ACL or leave an
  active embedding generation selected without corpus revalidation.
- Applying all migrations twice is safe and all required constraints/indexes
  have the expected shape and online state.

## Decision and Limitations

Stage 3 passes its exit criteria: a repeated operation has the same canonical
published result, provider/transaction interruption is resumable, and partial
work is durably identified but never published.

This is the validated incremental-ingestion layer, not a production deployment
or complete knowledge base. The validation uses deterministic providers and a
small fixture. A deployment still needs a durable external request queue and
original-object store, provider-specific timeout/retry adapters, Stage 4 graph
quality governance, Stage 5 permission-safe retrieval, grounded generation,
API/security/observability, gold-set regression gates, and representative
load/backup/restore validation.
