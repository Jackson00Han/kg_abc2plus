# Production-Candidate Validation

Stage 9 is the integrated qualification boundary for this repository. It runs
the completed ingestion, graph, retrieval, generation, API, security,
evaluation, recovery, and performance paths together under the versioned
`production-reference` profile. A passing result means that the committed
reference implementation satisfied the Stage 1 acceptance contract within the
recorded reference envelope. It does not authorize a live production rollout.

The evidence chain remains:

```text
trusted source -> versioned document -> traceable chunk -> governed graph
-> bounded retrieval -> cited answer -> measurable validation
```

Stage 8 remains the quality and regression foundation. Stage 9 does not replace
its adjudicated gold or weaken its gates; it adds production-reference corpus
scale, sustained concurrency, bounded bulk graph-write throughput, dependency
failures, deletion, interruption recovery, and a real Neo4j dump/load
exercise.

## Authoritative workflow

Run from a fully clean checkout at the exact committed implementation revision;
the recommended local form is an external detached worktree so unrelated user
work in the primary worktree is untouched. The runner rejects any tracked or
untracked checkout change. The output directory must not already exist, and
Docker and the locked `uv` environment must be available:

```bash
./scripts/run_stage9_validation.sh \
  --output-dir /tmp/sample-graphrag-stage9
```

The manual `.github/workflows/stage9-validation.yml` workflow invokes the same
entry point at the exact requested Git revision and publishes the bounded
report/diagnostic bundle. Its uploaded artifact deliberately excludes the
materialized corpus, database dump, bytecode cache, and package build output;
the local authoritative directory retains those items for dump and
deterministic-materialization verification.

The runner is the single qualification entry point. It assigns an unguessable
per-run ownership label to every disposable container and volume, binds service
ports only to loopback, and removes only resources carrying that exact label on
exit. Resource-name preflight happens before cleanup traps are installed.
Generated `load-v1` JSONL, Neo4j data,
database dumps, and report-working files stay below the external output
directory or disposable Docker volumes and must not be committed. The runner
generates an ephemeral database password in memory, passes it only to the
disposable processes, and does not write it into the evidence bundle. The API
JWT signing secret is likewise freshly generated in memory for each run,
shared only by that run's server and clients, and never recorded.

Before pulling the pinned image or starting either validation stage, the
runner requires both the host logical-CPU count and Docker daemon `.NCPU` to be
at least the configured eight CPUs. Missing, malformed, or undersized daemon
capacity fails closed. The daemon observation is emitted in the workflow log;
the formal runtime evidence separately records the created containers' actual
`NanoCpus`, memory, image, and environment and rejects any envelope drift.

The workflow is fail closed. In outline it:

1. verifies the acceptance contract, `production-reference` profile, locked
   dependencies, repository builders, and deterministic corpus manifests;
2. runs the complete Stage 8 workflow twice and requires reviewed `dev-mini`
   baseline version 1.1.0, its exact semantic projection, and the two-run
   reproducibility checks to pass;
3. resolves and enforces the configured Neo4j image digest, starts a clean
   deployment-sized disposable database, records the implementation commit and
   runtime identity, and proves the database initially contains no nodes or
   relationships. API startup must then pass both liveness and a real HTTP
   readiness call whose backend executes the configured bounded Neo4j query;
4. rebuilds `load-v1`, applies and verifies graph schema/indexes, bulk-ingests
   both Versions of every Document with per-plan ceilings of 256 Chunks, 1,024
   embeddings, and 8,192 graph rows, proves active-Version replay while the
   loader is still in its one-way offline lifecycle mode, then activates the
   tenant-specific embedding generations and requires the fixed five generation
   IDs to cover exactly
   12,000/12,000 active Chunks (10,000 for the primary tenant and 500 for each
   canary tenant). It also checks the manifest-derived primary-principal ACL
   inventory: 7,500 visible, 2,500 denied in-tenant, and 2,000 cross-tenant
   active Chunks/embeddings. The loader injects one pre-publication transaction
   interruption, proves that the full canonical graph and lifecycle counts were
   unchanged, retries the same deterministic job, verifies its complete stable
   Job/request/Snapshot projection. The committed manifest fixes the exact
   graph shape before generation activation and after query readiness; replay
   must preserve the former exactly. Bound monotonic timestamps require
   ingestion completion, replay, query readiness, and the first measured HTTP
   request to occur in that order;
5. runs the complete 49-case adjudicated retrieval and grounded-answer set
   against the already populated large database, retains prose-redacted,
   checksum-committed raw case evidence, and independently rebuilds every
   quality metric from the pinned gold rather than trusting copied aggregate
   values. Its active tenant
   generation IDs and canonical graph fingerprint are fixed and cross-bound to
   the measured database state;
6. preflights all 64 versioned load anchors once after index warm-up, requiring
   the exact expected source Chunk under the production five-Chunk/two-anchor
   limits, and preflights all 30 configured end-to-end answer cases under the
   complete answer-specific retrieval profile. Every case must pass the same
   HTTP, grounding, evidence, ACL, and active-version checks as a measured
   answer before the five-minute load can begin. These preflights are excluded
   from latency, cost, and measured-call baselines. The workflow then measures
   eight concurrent retrieval clients
   for at least 300 seconds using the manifest's 64 versioned retrieval anchors
   and runs the fixed 30 warmed end-to-end answer cases. Each of the eight clients
   must traverse the exact 64-case round-robin. The runner also sends eight
   high-score same-tenant-denied and cross-tenant canary requests and requires
   empty result, trace, citation, and protected-marker surfaces. The evaluator
   reconstructs
   overlap from the raw monotonic request intervals, requires the exact eight
   client identities to each span five minutes, requires an observed eight-call
   peak, and rejects a client idle gap above one second;
7. exercises success, timeout, unavailable, and failure behavior for Neo4j,
   the embedding provider, and the answer provider. Provider timeout probes
   sleep for 7 seconds without raising a synthetic timeout, so the API's real
   5-second runtime deadline must produce `504 dependency_timeout` within the
   configured 4.9-to-6.0-second observation window. The runner then performs a
   bounded drain of the deliberately late provider call before changing fault
   mode or closing the runtime;
8. deletes the manifest-selected Document and its two Versions, proves the
   exact expected source and derived record removals, verifies the tombstone,
   proves all four non-target load tenants are unchanged, and retains two
   terminal `INITIAL_LOAD` jobs plus the terminal `DELETE` job as durable audit
   evidence. Their stable job IDs are derived from and checked against the
   committed operation keys, tenant, Document, target Version/Snapshot, task
   counts, and terminal outcomes; the tombstone must name the exact DELETE job.
   The fixed target accounts for one Document, two Versions, two
   Snapshots, 100 Chunks, 100 ChunkEmbeddings, one Entity, 100 EntityMentions,
   and zero Assertions or `GraphGovernanceFinding` records;
9. performs a real `neo4j-admin database dump`, verifies its non-zero byte count
   and SHA-256 before and after copying those exact dump bytes into the report
   evidence, loads it into a fresh Neo4j data volume with `neo4j-admin database
   load`, starts the restored database, verifies schema and indexes, and
   compares canonical graph identity and business node/relationship counts;
   and
10. builds the final observation bundle and report twice from the same captured
    evidence, requires byte-identical observations, evidence projections, and
    reports, and runs package, compilation, shell-syntax, whitespace, secret,
    and unintended-artifact checks. All final checks occur before report
    construction; candidate reports stay in a CI-excluded working tree until
    byte reproduction, qualification, repository-read-only behavior, and exact
    owned-resource cleanup pass. The authoritative `report.json` is published
    last.

Any failed command terminates the workflow. A partial output directory is
diagnostic material, not a qualifying report, and must not be relabelled as a
pass. The mandatory scan of the exact Stage 9 commit file set remains a
separate pre-commit check, because its scope is defined by the proposed commit
rather than by runtime evidence.

## Fixed production-reference envelope

The authoritative settings are versioned in
`evaluation/production-reference-config.v1.json` and
`contracts/profiles/production-reference.v1.json`. The profile inherits every
Stage 1 threshold without override and makes quality and performance results
gating.

The configuration version 1.0.5 envelope fixes:

- `load-v1`, a deterministic synthetic corpus with 240 Documents, two Versions
  per Document, 24,000 total Chunks, 12,000 active Chunks, five tenants, 25
  access groups, and exactly 10,000 active Chunks in the measured primary
  tenant, plus 64 manifest-owned retrieval queries across 64 public Documents.
  Each query binds the exact source text and an anchor that is the only
  principal-visible Chunk at or above the 0.75 cosine gate; case IDs, query
  text, vector checksum, expected Chunk, expected Version, selection policy,
  and principal are versioned with the corpus;
- a trusted offline bulk-loader boundary of one atomic Document-Version plan
  per transaction, capped at 256 Chunks, 1,024 ChunkEmbeddings, and 8,192 total
  graph rows per plan; `load-v1` uses 50 Chunks per Version;
- eight concurrent retrieval clients, 64 retrieval warm-up requests that
  preflight the complete manifest-owned case set, 30 separately evidenced
  answer preflights covering every measured gold case, at least 300 sustained
  seconds, and 30 subsequent measured answer samples. Retrieval traffic uses
  the bounded five-Chunk/two-anchor load profile. Answer generation uses a
  separate, fully resolved Stage 6 profile: ten selected Chunks, five anchors,
  20 vector and BM25 recalls, 100 BM25 scans, five seeds, a 100-candidate cap,
  and a 12,000-character context. Both the large-database quality run and HTTP
  runtime resolve that same versioned mapping; partial overlays on the tighter
  load profile are rejected;
- Neo4j `5.26.12-community` at the committed repository digest, eight CPUs,
  3,072 MiB memory, a 512 MiB initial/1,024 MiB maximum heap, a 512 MiB page
  cache, a 32-connection driver pool, a bounded 300-second server default for
  offline validation scans, explicit five-second retrieval and readiness
  transaction timeouts, and a separately bounded 60-second atomic initial-load
  transaction timeout. The runner records the effective timeout values from
  the actual online objects and the successful HTTP readiness result; the
  report rejects static-only claims;
- exact cosine recall after tenant, active-Version, embedding-generation, ACL,
  and Version-filter matching; authorization-partitioned BM25 recall through
  `graphrag_chunk_text_v2`; standard RRF fusion, Resource Allocation expansion,
  adjacency completion, and the committed retrieval limits; and
- versioned deterministic reference embedding and recorded-answer-provider
  envelopes,
  including the separate `load-v1` and adjudicated-answer embedding spaces,
  per-request query-vector checksums, configured success latency, timeout
  latency, token accounting, and per-answer reference cost.

Only the Neo4j container has a fixed CPU and memory cgroup in this reference
profile. The local API/load-generator processes use the host's default resource
limits; the report therefore records that disclosure plus the host CPU count,
memory, and platform. The runner rejects a host exposing fewer than eight
logical CPUs or a Docker daemon exposing fewer than eight CPUs instead of
silently oversubscribing the container quota.
Throughput comparisons must retain that host context, and target-hardware
capacity testing remains a deployment prerequisite.

The manual GitHub Actions job keeps the portable `ubuntu-24.04` scheduler label
and does not treat that label as evidence of Docker capacity. The same runtime
preflight is authoritative in CI, so a hosted runner whose daemon exposes fewer
than eight CPUs stops before qualification work. Use an appropriately
provisioned runner for an authoritative run; changing to an assumed larger
hosted-runner label is not part of this validation contract.

The generated corpus is deliberately tiered above `dev-corpus-v1`; it does not
replace unit fixtures or the representative development corpus. Its source
records, checksums, stable IDs, exact Chunk ranges, vectors, tenant/access
distribution, temporal history, and lifecycle scenarios are reproducible from
the compact committed manifest and builder. Synthetic content is load and
isolation evidence, not factual company information or provider-quality gold.

Changing any fixed profile value creates a different validation envelope. Such
a change requires a version bump, reviewed metric comparison, a new complete
run, and updated validation evidence; command-line weakening is not a valid
Stage 9 result.

Configuration 1.0.5 changes only the declared Neo4j CPU envelope from the
unqualified two-CPU implementation draft to eight CPUs. Corpus size,
concurrency, duration, correctness/security invariants, and all Stage 1 gates
remain unchanged. The change follows a non-qualifying capacity probe in which
the two-CPU envelope produced a 3,872.20 ms retrieval-stage p95 and 2.3476
requests/second. That diagnostic result is not reused as qualification
evidence; the revised configuration requires a fresh clean-commit run.

The `load-v1` manifest and builder use version 1.0.2 because the committed
retrieval workload now binds each query to exact source text, selects one
cosine-isolated public anchor per Document, and records the selection contract.
The streamed graph records and embedding construction did not change, so their
separate pipeline code signature and embedding revision remain
`scripts/build_load_corpus.py:v1.0.0` and `load-v1.0`.

## Measured quality and operating evidence

The final report applies all 20 Stage 1 thresholds. In particular, Stage 9
gates graph precision/resolution, complete-evidence Recall@5, MRR, nDCG@5,
unauthorized exposure, grounded claims, citation precision and coverage,
numerical fidelity, refusal F1, ingestion success, idempotency, deletion,
recovery, retrieval and answer p95 latency, sustained retrieval throughput, and
unexpected server-error rate.

Quality results come from actual large-database rankings, traces, selected
contexts, and `AnswerResult` observations for the complete 49-case gold set.
The raw case artifact retains resource IDs and rankings but replaces generated
claim text, answer text, conflict topics, and citation locations with SHA-256
commitments. The evaluator pins the committed question and answer gold bytes,
validates those commitments, reconstructs the authorized inputs, and invokes
the Stage 8 retrieval and answer metric implementations itself. It rejects any
disagreement with the aggregate quality object. Case IDs and their canonical
digest are bound into evidence, so missing, additional, duplicate, malformed,
skipped, or failed cases cannot improve a denominator. Forbidden Chunk IDs
include inaccessible same-tenant material, cross-tenant material, and inactive
historical versions. Authorization is checked across initial recall, graph
expansion, adjacency, final context, citations, and answer text.
The large-database runner computes its first-pass metrics from those same
pinned gold questions, including the claim-level alternative evidence groups.
It fails closed unless removing that derived grouping field yields the exact
executable development-corpus question projection and the answer projections
also match exactly.

Both the 49-case quality run and the 30-case HTTP answer sample use the pinned
`deterministic-recorded-answer-fixture` artifact. The runtime provider reads
that prediction artifact, not answer gold. HTTP evidence retains citation and
Chunk/Version identity while replacing answer text, claim text, and citation
locations with SHA-256 commitments; the evaluator reconstructs the fixed
30-case answered subset from separately pinned gold and runs the existing
answer metric implementation. The prediction digest, provider, and version are
cross-bound through quality evidence, version inventory, and evidence manifest.
For a sourced claim, multiple recorded evidence Chunk IDs are interchangeable
adjudicated sources and at least one must be present; an inference remains
fail-closed unless every recorded operand is present.

Sustained retrieval requests use only the 64 `load-v1` anchors. Every raw
request carries its dataset/case identity, exact embedding-space ID and query-
vector checksum, plus exact expected, selected, visible, unauthorized, and
inactive Chunk IDs. Answer latency samples independently bind the adjudicated
`dev-corpus-v1` embedding identity and case-vector checksum. A matching
`X-Request-ID` and trace ID bind each HTTP observation one-to-one to
independently captured backend retrieval-stage timing. This makes the load
reproducible without mistaking arbitrary traffic or a dimension-compatible
vector from another embedding space for the declared workload.

The production retrieval design intentionally has no global approximate-vector
top-N window: exact cosine is evaluated only after the authorized active set is
matched. BM25 places opaque SHA-256-derived tenant and group scope tokens plus
an active-publication marker in the full-text query before its scan limit, and
then repeats the full relational authorization checks. Paired security cases
require the authorized result and trace to remain identical when higher-scoring
same-tenant denied, historical, or cross-tenant candidates are added.

The contract's `retrieval_p95_ms` gate uses nearest-rank p95 over the independently
captured server-side `retrieval_stage_ms` values for warmed retrieval requests,
excluding answer generation and HTTP envelope time. HTTP retrieval p50/p95/p99
remain separately named diagnostics. Throughput and error rate are independently
recomputed from the raw HTTP retrieval window. The same timelines must show the exact eight expected client
identities, real simultaneous overlap of all eight calls, at least five minutes
of activity per client, and no per-client idle gap over one second; labels and
global min/max timestamps alone do not satisfy the concurrency gate. Ingestion
throughput is recomputed from 24,000 submitted
Chunks and the monotonic interval that surrounds the bounded bulk graph-write
transactions. Schema creation occurs before this interval; embedding-generation
activation, full-text refresh, and the recorded `query_ready_ns` occur after
it. The ingestion, replay, query-ready, and first measured-request timestamps
form a strict report gate, while the replay snapshots must match the committed
pre-generation graph shape and the query-ready snapshot must match the
committed post-generation shape. The reported number is therefore graph-write throughput, not end-to-end
time-to-query-ready, and the latter remains a target-environment capacity
measurement. Provider latency samples, model-call counts, input and output
token counts, and estimated cost are limited to the declared measured window;
preflight and injected-fault calls are diagnostic and cannot inflate or dilute
the operating metrics.

The required lifecycle and dependency scenarios are:

| Area | Required outcomes |
| --- | --- |
| Lifecycle | idempotent replay, interrupted-ingestion recovery, exact deletion, access isolation, backup/restore |
| Neo4j | success; bounded timeout; unavailable; query/driver failure |
| Embedding provider | success; bounded timeout; unavailable; failure |
| Answer provider | success; bounded timeout; unavailable; malformed-output failure |

Timeouts must surface as the documented `504 dependency_timeout` boundary and
unavailable dependencies as `503 dependency_unavailable`. For both provider
timeout cases, the provider deliberately outlives the API deadline and would
otherwise succeed; it does not raise `TimeoutError` itself. This proves the
runtime deadline rather than merely mapping a provider-supplied exception. A
malformed answer-provider result is handled as an HTTP 200 safe refusal with
domain reason `invalid_model_output`, no unsupported answer, and no citations.
Scenario reports are projections of the raw fault timeline; the report builder
cannot replace observed failures with expected values.

## Evidence and report identity

The observation bundle records and checksum-binds:

- the acceptance contract, validation profile, production configuration,
  `load-v1` manifest, answer-embedding corpus manifest, pinned recorded-answer
  prediction artifact, Stage 8 gold/report,
  and large-database quality result. The Stage 8 report is reconstructed from
  its five raw suite files and checked against the reviewed baseline rather
  than accepted by its supplied summary fields. Its environment must have the
  exact Stage 9 implementation commit and `git_dirty: false`;
- every measured retrieval and answer request used for percentiles,
  throughput, and server-error calculation;
- exact functional, quality, security, recovery, and performance suite
  inventories, including failures, errors, and skips;
- every lifecycle/dependency fault outcome and monotonic interval;
- the clean database inspection, exact Neo4j tag and repository digest,
  resource envelope, Python/package/schema/index/provider versions, and full
  Git implementation commit. The six-field raw inspection uses
  `production-container-inspection-v2`; its expanded normalized evidence uses
  the distinct `production-runtime-environment-v1` schema;
- the large-database quality run's final fingerprint and the matching canonical
  graph fingerprints immediately before and after the subsequent read-only
  load/fault window, plus the post-deletion backup source and restored graph
  fingerprints. Each report/evidence projection retains the verified-schema
  flag, complete sorted label inventory, business node and relationship counts,
  and SHA-256 instead of reducing the state to a digest. The validation-window
  pair must be exactly equal and the backup pair must be exactly equal; the
  states include exact lifecycle counts and durable deletion-job audit records;
  and
- the real dump/load artifact bytes, identity, size, command identity, restored
  schema verification, source/restored business counts, limitations, residual
  risks, and deployment prerequisites.

The external output directory has these stable evidence locations (additional
diagnostic files may also be present):

```text
stage8/run-1/report.json
stage8/run-1/suites/*.json
raw/materialized-load-v1/{manifest.json,documents.jsonl,chunks.jsonl,entities.jsonl,mentions.jsonl}
raw/load/{ingestion.json,load-manifest.json,graph-state.json,faults.jsonl}
raw/load/{deletion.json,post-delete-graph-state.json}
raw/runtime/{requests.jsonl,retrieval-stage.jsonl,runtime-faults.jsonl,provider-usage.json,load-window.json}
raw/container-inspection.json
raw/source-container-resources.json
raw/restored-container-resources.json
raw/large-database-quality.json
raw/{pre-validation-graph-state.json,post-validation-graph-state.json}
raw/{backup-source-graph-state.json,restored-graph-state.json}
raw/backup/neo4j.dump
raw/backup-observation.json
evidence/*.json
evidence/backup_dump.dump
observations.json
report.json
reproduction/{observations.json,report.json,evidence/*.json}
reproduction/evidence/backup_dump.dump
```

`report.json` is the qualification result. Files below `raw/` are the captured
inputs used to derive it; `evidence/` contains their normalized, checksum-bound
report projections. The independently built `reproduction/` tree must compare
byte-for-byte equal to those projections and the report. Console output is
diagnostic and is not substituted for a machine-readable evidence file.

Canonical graph identity uses stable application IDs and deterministic
ordering. It includes durable business and lifecycle records, graph
relationships, source/version/Chunk provenance, embedding metadata, and a
digest recomputed from each stored vector. Only explicitly declared volatile
job lease/retry timestamps and counters are normalized. A stored vector that
does not match its recorded checksum is an error, not a stable fingerprint.
The quality run first loads its representative `dev-corpus-v1` fixtures and
binds its resulting graph fingerprint to the pre-validation state. The
pre/post pair then proves that the subsequent load and dependency validation
window was read-only. The independent backup-source/restored pair proves
deletion-state restore identity. Restore evidence must also preserve the source
business-node and business-relationship counts; a matching digest alone is
insufficient.

The atomic bulk loader intentionally persists one `InitialLoadJob` with
`expected_tasks == completed_tasks` rather than per-Chunk `IngestionTask` or
`HAS_TASK` records. The recovery observation therefore proves that the failed
`BEFORE_PUBLISH` transaction left zero Job and Task nodes, then queries and
validates the retried Job's fixed ID, request fingerprint, Document,
Version/Snapshot, manifest hash, built Chunk/embedding counts, terminal status,
and aggregate task counters. It explicitly records zero persisted/linked task
nodes and does not claim task-node coverage that this path does not implement.

The report builder rejects unknown or missing fields, invalid digests, stale
versions, forged eligibility fields, incomplete evidence inventories,
non-finite values, mismatched recomputations, insufficient workload, skipped
suites, scenario/timeline disagreements, a non-clean initial database, or
canonical graph drift. Qualification fields are output-only.

The workflow never exposes a qualifying report before the final package,
compile, shell, whitespace, complete tracked-file secret scan (including
`uv.lock`), artifact, two-build byte comparison, clean-tree, and owned Docker
cleanup checks have passed. Failed CI runs may upload raw diagnostics, but the
working report tree is excluded and no authoritative `report.json` exists.

For structurally valid evidence, the report has these semantics:

- `passed: true` and `production_candidate_eligible: true` only when all 20
  inherited gates, required suites, scenario outcomes, environment checks, and
  graph/restore comparisons pass;
- `passed: false` and `production_candidate_eligible: false` when valid
  evidence contains any failed threshold or required check, with every failure
  listed;
- no qualifying report when evidence is malformed, incomplete, internally
  inconsistent, or not bound to the recorded commit/configuration; and
- identical report bytes and semantic digest when the same evidence bundle is
  assembled again.

There is no observation-side override for an "accepted exception." An
exception may be documented for human review, but until the corresponding gate
passes (or the versioned Stage 1 contract is separately changed and reviewed),
the machine-readable result remains ineligible.

## Deterministic-provider scope

The default Stage 9 envelope intentionally uses a local deterministic embedding
provider and the pinned `deterministic-recorded-answer-fixture`. The recorded
answer content is structurally isomorphic to the reference oracle; it is kept
separate from runtime gold reads and is useful for testing transport,
grounding, commitment, and evaluator behavior, not for establishing independent
model quality. This makes the sustained reference run
repeatable, keeps protected content local, and tests the application/provider
boundary, latency accounting, timeouts, unavailable/failure mapping, grounded
output validation, token transport, and reference cost arithmetic.

It does **not** establish external embedding or LLM availability, latency,
quality, quota behavior, tokenizer accuracy, pricing, regional routing, data
retention, or vendor incident behavior. A report produced in
`deterministic_reference` mode automatically retains this limitation and the
deployment prerequisite to validate the selected external embedding and LLM
providers in the target environment. Reference cost is a deterministic model
of the configured envelope, not a vendor quote or forecast.

`production_candidate_eligible: true` therefore means eligible within the
committed reference envelope. It means neither "live production ready" nor
"external providers validated."

## Deployment boundary

Even a completely passing Stage 9 report leaves deployment-specific work. At a
minimum, a release owner must validate managed secrets and identity, the
selected external providers, customer-corpus quality and skew, production
hardware/topology and capacity, distributed rate limiting, monitored backup
and restore on the selected storage class, cluster failover and regional
recovery, alerting/on-call ownership, retention, privacy, and compliance.

The final Stage 9 report must list concrete limitations, residual risks, and
deployment prerequisites. Omitting them is invalid evidence; passing the
reference gates must never silently erase them.
