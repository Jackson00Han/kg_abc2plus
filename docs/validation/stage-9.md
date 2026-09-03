# Stage 9 Validation Record

- Date: 2026-09-03
- Acceptance contract: `contracts/acceptance.v1.json` version 1.0.1
- Validation profile: `production-reference` profile version 1.0.0;
  configuration version 1.0.5
- Production configuration: `evaluation/production-reference-config.v1.json`
  version 1.0.5
- Load corpus: `load-v1` version 1.0.2
- Unified quality gold: `gold-v1` version 2.0.0
- Stage 8 prerequisite baseline: `dev-mini.v1.json` version 1.1.0
- Database image: `neo4j:5.26.12-community`
- Required database image digest:
  `sha256:9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37`
- Validated implementation commit: `7142fa331f74ecd868a5ba20d343c787e2f9d367`

This record was completed only from the authoritative Stage 9 workflow. The
report binds the implementation commit above; this later documentation-only
closeout does not extend qualification to subsequent code, configuration, or
data changes.

## Delivered

- A versioned, deterministic `load-v1` builder and compact manifest for 240
  Documents, 480 Versions, 24,000 total Chunks, 12,000 active Chunks, 12,000
  historical Chunks, five tenants, and exactly 10,000 active primary-tenant
  Chunks.
- Production-reference corpus ingestion with exact source/version/Chunk and
  derived provenance, tenant embedding-generation activation, interrupted
  transaction recovery, active-version replay, canonical idempotency checks,
  and exact deletion-residue accounting. The offline bulk path commits one
  Document-Version per transaction and rejects plans above 256 Chunks, 1,024
  embeddings, or 8,192 graph rows. The versioned manifest fixes both the
  73,927-node/147,360-relationship pre-generation shape used on both sides of
  replay and the 73,932-node/147,365-relationship query-ready shape.
- Large-database execution of the complete 49-case adjudicated retrieval and
  grounded-answer set. The prose-redacted, checksum-committed raw case artifact
  retains IDs and rankings while committing redacted answer/provenance text by
  SHA-256; the
  evaluator pins the gold bytes and independently reconstructs and recalculates
  all retrieval and answer metrics instead of trusting supplied aggregates.
- A warmed eight-client sustained HTTP retrieval load and at least 30 warmed
  answer samples. Retrieval traffic cycles through the 64 manifest-versioned
  query anchors. Each retrieval and answer request binds its exact embedding
  space and query-vector checksum; raw requests also bind exact expected/
  selected/visible/unauthorized/inactive Chunk IDs plus HTTP and backend trace
  timing. Each client must follow the exact 64-case round-robin, while actual
  high-score same-tenant-denied and cross-tenant canary calls must expose no
  result, trace, citation, or protected marker. Raw intervals must independently
  prove the exact eight client IDs, an eight-call overlap, five minutes of
  activity per client, and no client idle gap above one second. HTTP latency
  diagnostics, backend retrieval-stage p95, throughput, server errors,
  authorization exposure, token usage, provider calls, and reference cost are
  retained as evidence.
- Success, timeout, unavailable, and failure exercises for Neo4j, embedding,
  and answer dependencies. Embedding and answer timeout providers sleep for 7
  seconds and would succeed, while the real API runtime deadline must return
  `504 dependency_timeout` in the configured 4.9-to-6.0-second window. The
  runner boundedly drains the late calls after the timeout observation. The
  lifecycle scenarios cover idempotency, interruption recovery, deletion,
  access isolation, and backup/restore; durable job IDs and the tombstone are
  bound to the committed lifecycle operations.
- Real `neo4j-admin database dump` and `database load` operations using a
  non-empty checksummed dump and a fresh data volume. The report builder checks
  the source dump bytes, copies those exact bytes into `evidence/`, verifies the
  copy again, and cross-binds its digest to restored schema/index, canonical
  graph, and business node/relationship-count evidence.
- A strict production report builder that recomputes metrics from raw evidence,
  reconstructs the Stage 8 semantic report from its raw suite inventories,
  binds the exact raw container schema and implementation commit before
  projecting `production-container-inspection-v2` into the distinct
  `production-runtime-environment-v1` evidence schema, pins
  artifact/version/environment identities and both workload embedding spaces,
  owns qualification fields, records limitations/risks/prerequisites, and
  emits a canonical semantic digest.
- One documented workflow that runs the complete functional, quality,
  security, recovery, and production-reference performance validation from a
  fully clean committed revision, preferably an external detached worktree.

The delivered list describes the Stage 9 implementation scope. It is not a
statement that the authoritative run has passed; measured claims appear only
in the final evidence section below.

## Reproducible workflow

Run from a fully clean checkout of the implementation commit, preferably an
external detached worktree so the primary user worktree remains untouched. The
runner rejects every tracked or untracked checkout change. The output directory
must not already exist:

```bash
./scripts/run_stage9_validation.sh \
  --output-dir /tmp/sample-graphrag-stage9
```

The manually dispatched `.github/workflows/stage9-validation.yml` runs that
same command at the requested Git SHA and uploads the bounded diagnostic and
report bundle while excluding the materialized corpus and database dump. Its
portable `ubuntu-24.04` scheduler label is not accepted as proof of capacity;
the runner performs the same Docker daemon preflight in CI.

The fixed profile requires the committed Neo4j image digest, eight CPUs, 3,072
MiB memory, 512/1,024 MiB initial/maximum heap, 512 MiB page cache, eight
retrieval clients, a host exposing at least eight logical CPUs, and a Docker
daemon whose `.NCPU` is an integer of at least eight. Missing, malformed, or
undersized daemon capacity stops the run before image pull or either validation
stage. The runner then requires 64 retrieval warm-up requests that preflight every
manifest-owned load anchor exactly once, 30 independently counted answer
preflights covering every configured gold case under the complete Stage 6
retrieval profile and excluded from provider/latency/cost baselines, a minimum
300-second measured window, 30 subsequent answer samples, and at least 10,000
active Chunks in the measured
tenant. All Stage 1 quality and performance thresholds gate the result without
profile overrides.

The runner also executes Stage 8 twice against independent disposable
databases, verifies deterministic rebuilds and the reviewed regression
baseline, runs all unit/API/security/regression/Neo4j integration suites,
loads and measures the production-reference database, validates dependency and
lifecycle failures, performs the real dump/load restore, assembles the report
twice from the captured bundle, and compares the report bytes. Package build,
bytecode compilation, shell syntax, `git diff --check`, secret scanning, and
generated-artifact exclusions are repeated by the authoritative runner against
the clean committed implementation. Qualifying report files remain in a CI-
excluded working tree until both builds, all checks, and exact owned Docker
resource cleanup pass; the authoritative report is moved into place last. The
same diff, secret, and artifact checks
must first pass on the proposed file set before it is committed.

For workflow design, evidence definitions, qualification semantics, and the
deployment boundary, see `docs/production_candidate_validation.md`.

## Evidence inventory

The authoritative output directory contains machine-readable evidence for:

- Stage 8 reports and exact suite inventories;
- the rebuilt `load-v1` manifest and ingestion/idempotency observations,
  including all five fixed active generation IDs, exact per-tenant embedding
  coverage totaling 12,000/12,000, and the manifest-derived ACL inventory of
  7,500 visible, 2,500 denied same-tenant, and 2,000 cross-tenant active
  Chunks/embeddings;
- the 64-query versioned load workload, its expected Chunk/Version/principal
  identities, query-vector checksums, embedding-space identity, and the per-
  request HTTP/backend trace bindings;
- the 49-case large-database aggregate result plus prose-redacted,
  checksum-committed raw case
  evidence from which retrieval, answer, citation, refusal, and exposure
  metrics are independently recomputed;
- the pinned `deterministic-recorded-answer-fixture` digest/provider/version,
  cross-bound to the 49-case quality result, version inventory, evidence
  manifest, and 30 prose-redacted, checksum-committed HTTP answer commitments;
- sustained retrieval and answer request samples, provider usage, load-window
  identity, and runtime fault outcomes;
- exact deletion targets, removed counts, residue counts, tombstone, all four
  non-target load-tenant fingerprints, and the two terminal `INITIAL_LOAD`
  plus one terminal `DELETE` durable audit jobs, including exact stable job,
  operation, tenant, Document, Version/Snapshot, task-count, and tombstone
  bindings. The fixed target contains one
  Document, two Versions, two Snapshots, 100 Chunks, 100 ChunkEmbeddings, one
  Entity, 100 EntityMentions, and zero Assertions or
  `GraphGovernanceFinding` records;
- clean container inspection, exact runtime/version identities, and observed
  source/restored container resource envelopes, including the actual five-
  second retrieval and readiness transaction deadlines, plus a successful
  HTTP readiness probe that executed the bounded Neo4j readiness query,
  alongside the separate 60-second initial-load and 300-second server
  envelopes;
- the quality run's final graph fingerprint, matching canonical pre-validation
  and post-validation states around the subsequent read-only load/fault window,
  plus the post-deletion backup-source and restored graph states. Every state
  retains and validates the schema/index verification flag, complete sorted
  label counts, business node/relationship counts, and SHA-256; both state
  pairs must be exactly equal, not merely digest-equal;
- the exact database dump artifact, digest, byte count, dump/load command
  identity, and restored schema/index, business-count, and graph-identity
  result;
- the bound production observation object, evidence manifest, final report,
  and independently rebuilt observation/evidence/report tree.

The machine-readable evidence lives below the caller-selected external output
directory. `report.json` is the qualification result, `observations.json` is
its validated input, `evidence/` contains normalized checksum-bound
projections, and `reproduction/` contains the independent second build. The
original lifecycle, load, provider, request, container, graph, and backup inputs
remain below `raw/`. Console output may be retained by CI for diagnosis, but it
is not accepted in place of any required JSON or JSONL artifact.

The bulk initial-load recovery case reflects the implementation's actual
atomic semantics. A failed `BEFORE_PUBLISH` transaction must leave zero Job and
Task nodes. Retrying the same plan must create the fixed deterministic
`InitialLoadJob`, whose request fingerprint, Document, Version/Snapshot,
Snapshot manifest, built Chunk/embedding counts, and terminal aggregate
`expected_tasks == completed_tasks == 50` projection are queried back and
validated. This bulk path does not persist `IngestionTask` or `HAS_TASK`
records, so the evidence explicitly requires their count to remain zero rather
than claiming task-node recovery coverage.

The final report is qualifying only if every static and runtime artifact is
checksum-bound, the recorded code identity is the exact implementation commit,
the initial database is empty, all required scenario and suite IDs are present,
no required test is skipped, raw metrics reproduce the declared values, the
pre/post validation states are equal, and the backup-source/restored states are
equal. The monotonic lifecycle evidence must also prove ingestion completion
before exact replay, replay completion before query readiness, and query
readiness before every measured HTTP request; the replay event in the fault
timeline must bind the same timestamps.

`load-v1` manifest/builder version 1.0.2 records exact source-bound query text,
one cosine-isolated public anchor per Document, and the 0.75 selection contract.
Its streamed graph records did not change, so the independently recorded
pipeline code signature and embedding revision intentionally remain
`scripts/build_load_corpus.py:v1.0.0` and `load-v1.0`.

## Gate interpretation

All 20 Stage 1 metrics gate under `production-reference`. The retrieval p95
contract value is nearest-rank p95 over warmed server-side retrieval-stage
samples; HTTP retrieval percentiles remain diagnostics, while throughput and
server-error rate remain derived from the HTTP five-minute window. Answer p95
continues to use the end-to-end HTTP answer samples.
Unlike `dev-mini`, Stage 9 has no informational-only performance row.

`passed` and `production_candidate_eligible` are derived by the report builder;
input observations cannot set or override them. Structurally invalid or
incomplete evidence aborts report construction. Structurally valid evidence
that misses any metric, suite, scenario, clean-environment, or graph-identity
gate produces a report with both fields false and a complete failure list. A
documented exception does not convert a failing machine result into a pass.

The default provider mode is `deterministic_reference`; its answer source is
the pinned `deterministic-recorded-answer-fixture`. Those recorded outputs are
structurally isomorphic to the reference oracle and, although runtime does not
read answer gold, they validate only the local pipeline, grounding, commitment,
and evaluator boundary—not independent external LLM quality. They also do not
validate real external availability, latency, quotas, tokenizer behavior,
privacy, or price. The final report must retain that limitation and require target-
environment validation of the selected embedding and LLM providers before
release. Even a passing report establishes a validated reference candidate,
not permission for live production deployment.

## Final measured evidence

<!-- STAGE9_FINAL_RESULTS_BEGIN -->
The authoritative `production-reference` run completed successfully from
implementation commit `7142fa331f74ecd868a5ba20d343c787e2f9d367`.
The generated report has no failures, `passed: true`, and
`production_candidate_eligible: true`.

- Identity and reproducibility: the Stage 9 semantic digest is
  `71d67bee2c155656cb663f602e92fe30aa2e5f58a35d8848847a7bdc24b4d575`.
  The canonical `report.json` file SHA-256 is
  `92b2865fca874ccca2350c53fb0397a5cb7ea11bcad8c19d3e7ad52663ea9722`,
  and the canonical `observations.json` file SHA-256 is
  `f722a617423100b87a0a853021696b46fd8698f3003211333af0e6ec5d9a832d`.
  Both Stage 8 runs passed and produced byte-identical reports with semantic
  digest `af94664fb502498b884eada4b27af892d13d73b9fcf66790601957e672cb126d`
  and file SHA-256
  `206c0007bd606c9649508af0389e1037d891597d1d0e9b92311d23f8b34468cb`.
  Each Stage 8 run executed 327 unit, 75 integration, five API end-to-end, 15
  security, and two regression tests with no failures or skips. The independent
  Stage 9 build reproduced `report.json` and `observations.json` byte for byte,
  and its complete normalized evidence tree matched the primary tree.
- Runtime envelope: the observed image, image ID, and repository digest were
  `neo4j:5.26.12-community` and
  `sha256:9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37`.
  Both source and restored containers had 8,000,000,000 `NanoCpus`, 3,221,225,472
  bytes of memory and equal swap limit, a 512 MiB initial/1,024 MiB maximum
  heap, and 512 MiB page cache. The server default transaction timeout was 300
  seconds, initial load used 60 seconds, and retrieval/readiness used five
  seconds. The database began with zero nodes and zero relationships; readiness
  passed. The host exposed eight logical CPUs, 8,589,934,592 bytes of memory,
  and `macOS-15.7.9-x86_64-i386-64bit`; the API process limit was explicitly
  `host-default-unbounded`.
- Suite inventory: functional 407/407, quality 4/4, security 15/15, recovery
  5/5, and performance 10/10 passed. Every suite recorded zero failures, zero
  errors, and zero skipped tests.
- Corpus and ingestion: the database contained 240 Documents, 480 Versions,
  12,000 active Chunks, and 12,000 historical Chunks; the measured tenant had
  10,000 active Chunks. All 24,000 submitted Chunks and 480 Versions completed,
  with a 1.0 success rate and zero failed Versions. The atomic bulk graph-write
  interval was 346.987016644001 seconds, or 69.16685307745486 Chunks/second.
  Replaying all 240 active Versions produced zero idempotency mismatches. The
  `BEFORE_PUBLISH` interruption left zero partial Job or Task nodes; retry
  produced one successful recovered job with all 50 expected tasks represented
  by its aggregate counters. This interval excludes later embedding-generation
  activation and index refresh and is not time-to-query-ready.
- Graph, retrieval, and answer quality: entity precision, relationship
  precision, and entity-resolution accuracy were all 1.0. Across 49 adjudicated
  cases, Recall@5 was 0.9714285714285714, MRR 0.9857142857142858, and nDCG@5
  0.9796001260591746. Supported-claim rate, citation precision, citation
  coverage, numerical fidelity, and refusal F1 were all 1.0; all 14 expected
  refusals passed, and unauthorized/forbidden exposure counts were zero.
- HTTP and provider load: eight clients completed 3,265 measured retrieval
  requests during 300.4210446150005 seconds, while 30 measured answers brought
  the bound backend retrieval-stage sample count to 3,295. Peak active requests
  and deterministic-provider concurrency were both eight, with zero semantic
  failures. The gating nearest-rank backend retrieval-stage p95 was
  935.1435330027016 ms. Diagnostic HTTP retrieval p50/p95/p99 values were
  701.5782390087843/959.9147139936686/1,322.000333994627 ms; answer
  p50/p95/p99 values were
  117.7265449911356/176.21599599719048/288.98366199433804 ms. Sustained
  retrieval throughput was 10.868080177885693 requests/second, and server-error
  rate was 0.0. Embedding-provider and LLM p95 latency were 8.047316 ms and
  24.864833 ms respectively.
- Dependency and lifecycle scenarios: all 17 scenario records had no assertion
  failures. Neo4j produced the expected retrieved success, 504 timeout, 503
  unavailable, and bounded 500 failure outcomes; embedding produced 200
  success, 504 timeout, and 503 unavailable/failure outcomes; answer generation
  produced an answered success, 504 timeout, 503 unavailable, and a safe
  `invalid_model_output` refusal for malformed output. Idempotency, interrupted
  ingestion, deletion, access isolation, and backup/restore all passed.
  Deletion removed exactly one Document, two Versions, two Snapshots, 100
  Chunks, 100 embeddings, one Entity, and 100 mentions, left zero residue, and
  preserved every non-target tenant. The 32,855,922-byte database dump had
  SHA-256 `79c77033f7f41228ace0884da24491fdd0d62783dbd3fc38e5d5e07c6828607e`;
  restored schema/index verification passed, container envelopes matched, and
  source/restored canonical states were equal at 74,532 business nodes and
  148,612 business relationships with digest
  `a9c88c8aa9838468dc3aa2c7a13f512685097378347b972f9fd904d410421a3f`.
- Usage and final checks: the measured window made 3,295 embedding and 30 answer
  model calls. The 3,325 total calls included 42,420 input and 1,440 output
  tokens; 30 metered answers cost a deterministic reference total of USD
  0.0036, or USD 0.00012 each. Package build, bytecode compilation, shell
  syntax, `git diff --check`, credential scanning, unintended-artifact scanning,
  final clean-tree verification, owned Docker-resource cleanup, and final
  report/evidence reproduction gates all passed before publication.
<!-- STAGE9_FINAL_RESULTS_END -->

## Decision and limitations

<!-- STAGE9_FINAL_DECISION_BEGIN -->
Stage 9 is complete. All 20 inherited acceptance gates, every required suite
and scenario, evidence binding, access-isolation and lifecycle check,
backup/restore comparison, clean-environment check, and both reproducibility
workflows passed. The authoritative report therefore records `passed: true`
and `production_candidate_eligible: true` for commit
`7142fa331f74ecd868a5ba20d343c787e2f9d367` under configuration 1.0.5.

This result validates only the committed, deterministic reference envelope as
a production candidate. It is not a live production deployment approval; the
limitations and deployment prerequisites below remain mandatory.
<!-- STAGE9_FINAL_DECISION_END -->

The passing report preserves these known scope limitations:

- `load-v1` is deterministic synthetic load/isolation data, not customer data
  or factual provider-quality gold;
- the default embedding and answer providers are deterministic local reference
  envelopes, so external-provider availability, quality, latency, quota,
  tokenizer, privacy, and pricing remain unvalidated;
- the ingestion throughput gate measures bulk graph writes and excludes later
  embedding-generation activation/index refresh; end-to-end time-to-query-ready
  remains a target-environment capacity measurement;
- the reference run covers one bounded five-minute loopback load window and
  one Neo4j Community container, not a clustered or regional topology;
- the Neo4j container has fixed resources, but the API and load-generator
  processes use disclosed host-default-unbounded resource limits;
- reference cost is configured deterministic accounting, not a vendor quote or
  deployment forecast.

Deployment prerequisites must therefore include target-provider validation,
managed secrets and identity, customer-corpus evaluation, production hardware
and topology capacity testing, monitored storage-class backup/restore,
cluster/region recovery, distributed controls, alerting/on-call ownership, and
applicable retention, privacy, and compliance review.

The prioritized end-to-end assessment and follow-up work are recorded in
[`process-improvements.md`](process-improvements.md).
