# Industrial retrieval evaluation

The I3 runner compares three predeclared retrieval configurations using the same
frozen questions, query vectors, source snapshot and active index generations.
It measures source retrieval and context completeness. It does not generate
answers or claim that source answerability annotations are runtime refusal
decisions.

The primary suite has 72 source-reviewed cases, split into 36 development and
36 holdout cases by event group. Eight denied requests have matched authorized
controls. Relevant missing-evidence sources for `INSUFFICIENT` questions remain
in the ranking metric denominator. Holdout results are reported separately and
must not be used to select or tune retrieval configurations.

## Reproducible commands

Validate committed gold without a database or provider call:

```bash
uv run python scripts/evaluate_industrial_retrieval.py
uv run python -m unittest tests.unit.test_industrial_evaluation -v
```

The existing local database must already contain the governed industrial corpus,
its two official excerpts, and the existing `tenant-alpha` fixture used by the
foreign-tenant negative control. Every test tenant must have an active embedding
generation matching the explicitly configured `EMBEDDING_MODEL` and
`EMBEDDING_DIMENSIONS`. The evaluator does not initialize, modify or reset these
sources.

Create the baseline worktree once from the preserved I2 commit. The runner
verifies its full commit identity and rejects changes to tracked files:

```bash
git worktree add --detach /tmp/graphrag-industrial-i2-baseline feade8048d2dd34344fefd015b17b8ef43464f68
```

Explicit live comparison, including the separate official-PDF suite:

```bash
uv run python scripts/evaluate_industrial_retrieval.py \
  --live \
  --baseline-root /tmp/graphrag-industrial-i2-baseline \
  --vector-cache /tmp/graphrag-industrial-i3/query-vectors \
  --include-pdf \
  --pdf-cache /tmp/graphrag-industrial-cache \
  --split dev \
  --output /tmp/graphrag-industrial-i3/comparison-v1.json
```

The command reads the existing local `.env` configuration. It requires an
explicit loopback Neo4j endpoint and the configured official DashScope HTTPS
embedding endpoint. There is no fixture-vector fallback. No credentials or
protected source text are printed to the console. Output and vector caches must
be outside the repository; the source snapshot and raw captures contain source
text and are local audit artifacts, not public application responses.

The PDF cache must contain the committed manifest's normalized-artifact filename
hints and complete originals named `{source_id}.pdf`. The runner independently
rechecks original hashes and reparses the selected pages through the bounded I2
normalizer before PDF evaluation. Missing files produce `NOT_RUN`, never a pass.
A present but altered artifact fails preflight.

Offline validation recomputes the metrics and checks source bindings from the
captured evidence. An independently retained pins file adds a separate identity
check:

```bash
uv run python scripts/evaluate_industrial_retrieval.py \
  --validate-report /tmp/graphrag-industrial-i3/comparison-v1.json \
  --expected-pins /tmp/graphrag-industrial-i3/comparison-v1.pins.json
```

Offline validation checks the captured source state, not the current contents
of a live database. Hashes detect changed artifacts; they are not signatures
proving who created an entirely replaced capture and pins file. Keep the pins
and validation record independently when preserving an evaluation run.

Assess a development configuration without displaying holdout or PDF results:

```bash
uv run python scripts/evaluate_industrial_retrieval.py \
  --validate-report /tmp/graphrag-industrial-i3/comparison-v1.json \
  --expected-pins /tmp/graphrag-industrial-i3/comparison-v1.pins.json \
  --selected-variant scoped_ranked --split dev \
  --assessment-output /tmp/graphrag-industrial-i3/selected-dev-acceptance.json
```

The comparison capture retains all cases. `--split dev` restricts the printed
summary and acceptance assessment to development results; it does not remove
holdout or negative cases from the immutable capture. After configuration
selection is locked, use `--split holdout` to assess the independent holdout and
`--split all` for the aggregate plus explicit dev and holdout assessments. The
complete primary gold is the aggregate acceptance population; a passing
aggregate never relabels a failed development assessment as passed. Keep each
assessment in a new external file.
No new provider calls are needed for an offline assessment.

Without `--selected-variant`, the CLI reports `quality_acceptance: NOT_ASSESSED`;
a successful capture or integrity check is not a quality pass. With an explicit
selection, the existing industrial contract requires Recall@5 at least0.85 and
MRR/nDCG@5 at least0.80, with zero observed scope, authorization, citation or
runtime failures. A failed assessment exits1. The assessment pins the contract
and original report separately, preserving the original comparison artifact.
These are I3 retrieval checks; the contract's graph browsing bound checks remain
pending I4, and runtime answer correctness is not evaluated here.

## Compared implementations

| Variant | Source implementation | Declared scope | Context selection |
| --- | --- | --- | --- |
| `legacy_default` | Clean I2 commit `feade8048d2dd34344fefd015b17b8ef43464f68` | Existing tenant, ACL and publication-date filter; no industrial family/asset capability | Existing anchor3, adjacency1 |
| `scoped_default` | Pinned current source bytes | Current industrial family, user-supplied primary assets, references and date restrictions | Same anchor3, adjacency1 |
| `scoped_ranked` | Same current source bytes | Same industrial restrictions | Existing `anchor_k=top_k=5` knob; adjacency remains1 |

All three use top5, a12000-character context ceiling, the same recall and graph
caps, RRF constant60 and the existing ranking/graph methods. The last variant
changes an existing context-selection parameter; it introduces no new scoring
formula. The actual fulltext index name, analyzer/configuration and readiness
are captured rather than inferred from configuration defaults.

The subprocess worker imports `graphrag_prod` only from its selected checkout's
`src` directory, checks every imported package module's origin, and records the
retrieval engine file hash. It never imports industrial gold, a scope resolver or
an embedding provider. Prepared JSON requests contain only query text/vector,
principal, limits and version filters. Current scope resolution happens before
the worker; judged evidence and expected answer notes never enter retrieval.

The preserved v1 comparison always requires its original three variants and all
72 cases. A v2 report explicitly declares its capture scope and the new
`scoped_reranked` variant. During development, capture the complete36-case dev
split, including every dev negative and authorized control:

```bash
uv run python scripts/evaluate_industrial_retrieval.py \
  --live --rerank --capture-split dev --split dev \
  --rerank-profile listwise-evidence-v1 \
  --vector-cache /tmp/graphrag-industrial-i3/query-vectors \
  --rerank-cache /tmp/graphrag-industrial-i3/rerank-cache \
  --output /tmp/graphrag-industrial-i3/rerank-dev-v1.json
```

The selected profile is fixed before holdout inspection. Its quality still
requires the measured acceptance assessment. The report is labeled
`PARTIAL_DEV_ONLY`; it cannot be assessed as
holdout or all-case acceptance. Missing even one corresponding negative fails
validation. Once the selected profile is fixed, `--capture-split all` captures
all72 cases, and `--include-pdf --pdf-cache ...` adds the separate three-case PDF
suite. The historical v1 capture remains unchanged and independently readable.

Reranking uses a separate current-engine worker. The legacy worker never imports
the rerank provider. The current worker requires explicit live-provider
permission and receives only query/vector, principal, limits and authorized
version filters. It does not import gold or the scope resolver.

The external rerank cache is accessed only after the engine supplies its
authorized candidate set. Its identity includes model, endpoint, instruction,
rendering version, exact query checksum, ordered candidate IDs and immutable
source checksums, and the checksums of the complete rendered input documents.
Changing source title context or rendering profile invalidates reuse even when
the raw Chunk text is unchanged. Cache replay revalidates the complete result
permutation, actual HTTP input identity and normalized provider-output checksum.
Missing or invalid caches never silently select another provider or ranking.
Live calls, cache hits and newly reported billing tokens are counted separately;
cached historical tokens are not counted as new usage.

The selected listwise profile uses the configured `qwen3.8-max` chat model to
return a complete candidate permutation; its neutral scores remain `null`.
The versioned normalization policy only removes repeated valid indices while
preserving their first occurrence. Missing, out-of-range, boolean or excessive
indices fail; no missing candidate is filled in. Raw permutations, removed
counts and checksums remain in the trace. Exact successful HTTP response bodies
are retained only in the private local cache/capture, allowing offline validation
to reproduce both the original and normalized output. Public retrieval traces
do not include those HTTP bodies. Pointwise qwen3 profiles remain separately
identified experiments and are never a fallback.

Observed prompt, completion and total tokens are reported separately for each
new listwise call. Providers that return only total usage retain an explicit
missing-breakdown count; the compatibility `input_tokens` field is not silently
interpreted as an observed prompt count. No tariff or cost is inferred.

Each worker request has a60-second deadline covering both nonblocking request
transmission, including partial writes, and response reading. Existing database
transactions have a30-second timeout. A dead or timed-out worker leaves explicit
failure rows for remaining cases. Cases, including negatives, cannot silently
disappear. Raw per-variant captures are saved before final report validation so
a failed audit remains inspectable.

## Metrics and evidence checks

Ranking metrics reuse the existing standard fractional `Recall@5`, reciprocal
rank and graded `nDCG@5` implementation. Every positively graded source is
relevant, including complete alternatives and explicitly partial supporting
sources. This can make maximum Recall@5 less than1 for questions with more than
five relevant sources; labels are not removed to improve that ceiling.

`complete_evidence_set_rate` separately measures whether every member of at least
one source-authored complete alternative set is present. It does not replace
standard recall. `SUPPORTED`, `INSUFFICIENT`, `AMBIGUOUS` and `DENIED` are recorded
as gold annotations only. No answer correctness, diagnosis, or runtime refusal
rate is claimed.

Every selected citation is checked against exact current source text, normalized
and Chunk checksums, document/version identities, source URI/title, ordinal,
character offsets and physical page. Industrial provenance JSON and its scalar
family/asset facets must agree with the same immutable DocumentVersion. PDF
Chunks must remain within their mapped physical page.

Every Chunk ID exposed in recall, graph expansion, ranking, decisions and final
context is checked against the captured current tenant and document/Chunk ACLs.
Family, primary asset, reference/source-kind and time exclusions are audited
independently of relevance. A denied request may still return a permitted
engineering summary; that is not an access-control failure. Handpicked forbidden
sentinels supplement the complete source-ACL check.

The v2 audit additionally reconstructs each rerank candidate from captured
source text and authorized citation metadata, then verifies the rendered input,
instruction/profile, full ordering and cache/live accounting. Both provider
input and output IDs are included in the same independent current-ACL and
product/asset-scope audit.

The source inventory is bounded to128 documents,2048 current Chunks and16Mi
characters of normalized text plus maps for the test tenants. Captured source,
ACL, publication/index state, fulltext configuration, code or gold changes during
a run invalidate the combined comparison.

The report pins corpus/gold versions and checksums, actual source bytes and Git
commits, worker implementation, model/revision/dimensions/normalization, query
vector checksums, active embedding generations and publication identity plus
activation generation (including rollback/re-activation),
fulltext configuration, dependency versions, lockfile and source-catalog hashes.
Query-vector caches include provider/model/revision/dimensions/normalization and
the exact query checksum, and reject changed vectors or dimensions.

Query embedding usage reports actual SDK calls, cache hits and provider-reported
tokens. Costs are `null` because no billing tariff is assumed. I2 source
embedding billing usage was not captured; its counts/tokens/cost remain
`NOT_CAPTURED_BY_I2_LOADER`/`null`. A Chunk count is not relabeled as a provider
call or token measurement.

The first comparison capture predates activation-generation pinning. Its source,
index and publication-ID checks remain reproducible; offline validation does
not claim that the newer rollback/re-activation check ran on that historical
capture. New captures require matching active publication status and tenant and
compare scoped retrieval traces against the pinned activation generation.

## Separate PDF context suite and limits

The three optional PDF cases test Canalis cross-page conditions/footnotes and
HVX table header/row continuation. Their original PDF, normalized artifact,
parser/splitter, physical-page, exact-range and text checksums are independently
pinned. Recall receives only a user-declared product family and
`OFFICIAL_PUBLICATION` source kind; target pages/ordinals never become recall
filters. Their nine comparison captures and completeness results are separated
from the primary72-case metrics.

The CLI fails for runtime failures, current-source ACL exposure, citation
inconsistency, or scope violations in the current scoped variants. Legacy scope
misses are measured as a baseline limitation. Quality improvements and remaining
incomplete evidence sets are reported independently of the existing contract's
ranking thresholds. The PDF suite is not merged into the primary quality gate.

This is a bounded `dev-mini` evaluation. Graph UI/browser validation remains I4,
and this report cannot qualify a deployment as a production candidate or confirm
the safety of an industrial operation.
