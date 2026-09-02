# Automated Evaluation and Regression Gates

Stage 8 provides one fail-closed evaluation workflow for the completed graph,
retrieval, grounded-answer, API, security, and lifecycle implementation. It
binds immutable adjudication to independently captured system results, applies
the Stage 1 acceptance contract, and produces a report whose deterministic
projection can be compared across clean runs.

This stage runs under the `dev-mini` profile. It is regression and integration
evidence, not production-candidate qualification. In particular, none of the
latency, throughput, token, or cost observations described here replaces the
`production-reference` workload required for production-candidate validation.

## Authoritative workflow

Run the complete workflow from the repository root. The output directory must
not already exist:

```bash
./scripts/run_stage8_validation.sh \
  --repeat 2 \
  --baseline evaluation/baselines/dev-mini.v1.json \
  --output-dir /tmp/sample-graphrag-stage8
```

This is the single Stage 8 validation entry point. Each repetition runs the
unit, HTTP end-to-end, adversarial security, regression, and complete
disposable-Neo4j integration suites. The Neo4j integration runner starts an
empty loopback-only database with the `dev-mini` limit of 1.5 GiB, one CPU, a
512 MiB maximum heap, and a 128 MiB page cache. Provider and ambient Neo4j
credentials are removed from the test environment.

Each repetition consumes the separately stored graph and operational
observations, writes fresh retrieval, answer, conflict, suite, and report
artifacts below its run directory, and binds all of them into the report.
After the second repetition, the workflow compares the deterministic report
projections. It then checks the evaluation gold and representative corpus
rebuilds, acceptance contract, lock file, package build, bytecode compilation,
every shell script, and `git diff --check`.

Normal validation and CI consume only a reviewed baseline. A baseline
candidate is an explicit, single-run maintenance operation and cannot be
combined with `--repeat 2`. Creating a candidate does not approve it: a
maintainer must inspect the changed cases, metrics, identities, limitations,
and rationale before replacing the versioned baseline. The ordinary workflow
never updates a baseline automatically.

## Unified, prediction-free gold

`evaluation/gold-v1/manifest.json` identifies `gold-v1` version `2.0.0`. Its
checksummed artifact bindings join the following independent annotations:

- the Stage 1 acceptance contract and `dev-corpus-v1` manifest;
- 49 questions spanning all seven question classes, with five success and two
  boundary cases per class;
- 49 answer annotations with material claims, exact values, refusal behavior,
  and complete Chunk/Document/Version provenance;
- all 120 representative corpus Chunks;
- 60 graph entity, relationship, and entity-resolution adjudications;
- two supplemental conflict cases backed by four immutable source Chunks.

The manifest declares `contains_predictions: false` and
`exhaustive_within_bounded_corpus`. Every artifact uses a repository-relative
path, item count, and SHA-256 digest. The loader rejects path traversal,
missing or extra roles, digest drift, count drift, duplicate IDs, stale case
lists, incomplete provenance, and any prediction field in gold.

The generated `evaluation/gold-v1/questions.jsonl` reuses the canonical
`dev-corpus-v1` questions and adds only `required_evidence_groups`.
`datasets/dev-corpus-v1/answers.jsonl` remains the canonical answer gold; it is
referenced by digest rather than copied. Similarly, Stage 8 splits the older
combined graph-review fixture into prediction-free
`evaluation/gold-v1/graph.json` and independent
`evaluation/observations/graph-system-v1.json`.

`evaluation/retrieval-gold-v1.json` remains a historical Stage 5 metric
fixture. Because it contains recorded rankings, the Stage 8 runner does not
use it as acceptance evidence.

## Gold and actual-result separation

The runner never calculates quality by reading predictions embedded in gold.
Actual observations are produced or loaded through separate schemas:

- graph decisions come from `graph-system-v1.json`, which contains no
  adjudicated labels;
- real-Neo4j integration writes `retrieval-results.json`, containing each
  accepted final ranking and every visible Chunk across vector recall, BM25
  recall, seed ranking, graph expansion, candidate-vector ranking, final
  ranking, and selected context;
- the same integration writes `answer-results.jsonl` from actual
  `AnswerResult` values after retrieval and generation validation;
- `capture_conflict_results.py` sends the supplemental source Chunks through
  `GroundedGenerationService` and writes `conflict-results.jsonl`;
- operational counts, latency samples, token usage, and cost samples are read
  from the explicitly non-qualifying `dev-mini-operational-v1` observation
  set.

Graph, retrieval, and answer evaluators require exact ID-set equality between
gold and actual results. Missing, extra, duplicate, malformed, skipped, or
failed cases terminate evaluation rather than shrinking a denominator.
Provider or validation failures cannot be reclassified as evidence-based
refusals.

## Per-fact evidence requirements

Positive relevance alone is insufficient for a multi-fact question: a single
easy hit could otherwise make a cross-Chunk result look complete.
`build_evaluation_gold.py` therefore derives an explicit evidence-group
projection from every material answer claim.

Members within one sourced-claim group are alternatives that support the same
fact; groups are conjunctive across the question. An inference claim instead
projects each of its evidence Chunks as a singleton group, because every
operand is required. For a top-five ranking `T` and groups `G1 ... Gn`, the
question is complete only when:

```text
for every Gi: T intersects Gi
```

For example, business and product Chunks may be equivalent support for one
offering claim, so either may satisfy that claim's group. A question asking
for revenue and margin has distinct groups, so top five must contain support
for both facts. The gold loader independently rebuilds the groups from answer
claims and rejects a stale or hand-edited projection.

The loader also proves that a hitting set for all groups fits within the
top-five bound. A future claim requiring a more complex mixture of mandatory
and alternative evidence must extend this explicit projection rather than
silently changing group semantics.

## Metric definitions

### Retrieval

Stage 8 always calls the high-level `evaluate_retrieval_results` entry point.
Its contract `recall_at_5` is the macro mean of complete-question indicators:
a question scores one only when top five intersects every required evidence
group. This implements the Stage 1 wording "queries with required evidence in
top five."

The standard macro fractional evidence recall is retained as the
`evidence_recall_at_5` diagnostic:

```text
mean(|positive relevant Chunks intersect top five| / |positive relevant Chunks|)
```

MRR uses the rank of the first positively relevant Chunk. nDCG@5 uses the
committed relevance grades and the standard exponential-gain/log-discount
definition. Unauthorized exposure count is the number of distinct,
query-forbidden Chunk IDs visible per case across any recorded retrieval or
selected-context stage.

The lower-level historical `evaluate_retrieval_items` helper exposes its
standard fractional calculation under the older `recall_at_5` field as well
as `evidence_recall_at_5`. It is retained for historical fixtures and must not
be substituted for the Stage 8 high-level entry point.

### Graph

Graph gold and actual decisions are paired by exact ID. Entity precision is
the adjudicated correctness of accepted entities; relationship precision is
the adjudicated support of accepted relationships; entity-resolution accuracy
is the fraction of exact `MERGE`, `KEEP_SEPARATE`, or `HUMAN_REVIEW` outcomes.
The additional case-outcome accuracy verifies all positive and negative graph
decisions so that perfect precision cannot be obtained by accepting too few
items.

### Answers, citations, conflicts, and refusals

The answer evaluator calculates supported-claim rate, citation precision,
citation coverage, numerical fidelity, refusal precision/recall/F1, complete
answer correctness, temporal-comparison handling, conflict handling,
generation failures, and forbidden-answer exposure. Citations must reproduce
the exact immutable source location and appear inline on the attached material
claim. Complete case coverage and `answer_correctness == 1.0` prevent empty or
partial answers from preserving a misleading rate.

The 49 representative cases are evaluated first against their own gold. The
two conflict cases are then appended, and the combined 51-case result supplies
the final answer metrics and non-empty conflict denominator.

### Lifecycle, latency, reliability, and cost

Operational metrics include ingestion success, idempotency mismatches,
deletion residue, recovery success, retrieval and answer p95, retrieval
throughput, unexpected server-error rate, model calls, input/output tokens,
total estimated cost, and mean answer cost. P95 uses the nearest-rank rule:
sort samples and select `ceil(0.95 * n) - 1`. Cost inputs are validated and
summed with decimal arithmetic before report conversion.

The committed Stage 8 operational observations are deterministic fixtures.
They exercise metric implementations, version transport, and gates; they are
not measurements of live provider or production-scale performance. Under
`dev-mini`, performance rows are reported but do not gate the acceptance
result, and cost has no Stage 1 threshold.

## Supplemental same-scope conflict design

The representative corpus contains temporal comparisons but no unresolved
same-scope conflict. Stage 8 adds a small, separately versioned fixture without
rewriting `dev-corpus-v1`:

- `same_scope_conflict-success-01` has two distinct canonical URIs,
  Documents, Versions, publication times, checksums, and Chunks. Both source
  statements refer to Apple Inc. revenue for fiscal year 2024, but report
  `$10 million` and `$12 million`. The expected status is `conflict`; both
  alternatives must remain sourced and cited.
- `different_periods_not_conflict-boundary-01` reports `$10 million` for
  fiscal year 2023 and `$12 million` for fiscal year 2024. Because the periods
  differ, the expected status is `answered`, with both sourced observations
  and an explicitly labelled `increased` inference. It must not be promoted
  to an unresolved conflict.

Stable UUIDv5 identifiers, exact text ranges, and SHA-256 checksums are rebuilt
by `build_evaluation_gold.py`. The loader verifies the source text, range,
complete citation provenance, two distinct Document/Version provenances for
the positive conflict, and the presence of both a conflict and a non-conflict
contrast. Runtime capture passes these Chunks through the production
generation validator, so conflict rendering and provenance rules are tested.
It does not exercise retrieval of those four supplemental Chunks.

## Negative and security completeness

The gold manifest fixes all 49 representative case IDs and separately fixes
all seven unauthorized, seven unanswerable, and two supplemental conflict
IDs. Unauthorized questions require both forbidden Chunk IDs and forbidden
answer terms. A missing negative case is a dataset error before metrics run.

`evaluation/security-suite.v1.json` additionally fixes the required
adversarial HTTP/security test IDs. The workflow records started and passed
test IDs and rejects skips, expected failures, unexpected successes, missing
tests, or tests that started without passing. These tests cover authentication
attacks, identity/vector injection, ACL escalation, cross-tenant existence
probing, protected-input echo, request-ID injection, body limits, log
redaction, metrics authorization, and bounded route labels.

Real-Neo4j integration records visible Chunks from every retrieval stage and
selected context for the query-specific exposure metric. Independent Stage 5
and Stage 8 integration checks exercise tenant, Document ACL, Chunk ACL, graph
expansion, adjacency, hydration, and selected-context boundaries in addition
to the listed canary Chunks.

## Report identity, gates, and reproducibility

Every report records the acceptance contract, profile, gold manifest,
regression policy, security manifest, and graph-migration digests. The
operational observation set must provide exact versions for the corpus, gold,
prompt, output schema, embedding model/revision/space, answer model/revision,
index, configuration, Neo4j image, and recorded image digest. The report also
records Python, Git commit, and dirty-worktree state for audit.

Stage 1 contract thresholds apply to all 20 contract metrics. Under
`dev-mini`, only performance-area threshold failures are non-gating. The
regression policy separately requires zero-tolerance answer correctness,
conflict and temporal handling, graph case outcomes, generation failures,
forbidden answer exposure, and security-suite completeness. The reviewed
baseline compares the entire deterministic projection exactly, including
per-case digests, contract observations, diagnostics, artifact identities,
and executed test identities.

Two-run comparison checks case digests, contract rows, diagnostics, identities,
semantic digest, and suite counts. Wall-clock timestamps and incidental
environment fields are outside this comparison. The operational latency/cost
values in Stage 8 are committed deterministic samples, so their derived values
are stable but remain non-qualifying.

## Known limitations

- `dev-mini` is explicitly ineligible for production-candidate validation.
- The corpus and conflict fixture are synthetic; fixture embeddings are
  derived from adjudicated evidence clusters.
- Representative answer results use a deterministic structured model after
  real retrieval; conflict capture uses direct fixture Chunks. Neither proves
  open-ended LLM quality or provider behavior.
- Graph observations are a split, deterministic projection of the Stage 4
  reviewed decisions, not a fresh production-scale extraction sample.
- Lifecycle, latency, throughput, token, and cost observations are committed
  non-qualifying fixtures rather than sustained live measurements.
- The conflict sample covers a narrow numeric same-scope disagreement and a
  different-period contrast; broader authority, logical, textual, and
  multi-source conflict policies need additional adjudicated cases.
- The retrieval exposure metric counts the query-specific forbidden Chunk
  canaries; broader identity, HTTP, log, and existence-signal behavior is
  enforced by the separately fixed security and integration suites.
- The standalone Neo4j runner invokes `neo4j:5.26.12-community` by tag and
  records, but does not enforce, an independently observed digest. Stage 9
  resolves and verifies its pinned digest first, then supplies that exact image
  reference through `STAGE8_NEO4J_IMAGE`.
