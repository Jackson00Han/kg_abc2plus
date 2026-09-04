# Independent semantic retrieval holdout

`semantic-holdout-v1` is a small validation set authored independently of the
corpus builder and manually reviewed before release. It exercises the live
semantic retrieval path. It exists because the original 49 development
questions and the deterministic corpus are produced by the same builder; a
score on those questions alone is therefore vulnerable to fixture coupling.

The holdout is independent of the corpus builder and contains no generated
answers, expected prose, claims, query vectors, or embeddings. Its only gold
labels are:

- the authenticated principal and ACL groups;
- the question class and answerability flag;
- required source Chunk IDs for positive cases; and
- forbidden Chunk IDs for authorization-negative cases.

The 14 cases cover single-Chunk, cross-Chunk, graph-relationship, exact-value,
temporal, unanswerable/refusal-oriented, and unauthorized behavior, with two
cases in every class. The negative cases do not turn this retrieval-only check
into an answer-generation evaluation: unanswerable cases carry no evidence
target, while unauthorized cases assert zero exposure of specifically
forbidden Chunk IDs. Grounded refusal wording remains the responsibility of
the separate answer-quality suite.

## Bound and guarded assets

The manifest at
`evaluation/semantic-holdout-v1/manifest.json` pins the source corpus manifest,
Chunk inventory, legacy question inventory, and holdout question file by
SHA-256. Validation fails closed when:

- a required or forbidden Chunk does not exist;
- required evidence is outside the principal's tenant or access groups;
- forbidden evidence is actually authorized;
- IDs, query text, evidence lists, coverage declarations, or ACL groups are
  duplicated or malformed;
- a query duplicates or closely paraphrases a legacy question;
- a query copies a five-word legacy phrase or six-word source passage;
- a query contains a formatted answer value, or the artifact gains an answer,
  prediction, vector, or embedding field; or
- novelty and quality thresholds are weakened beyond the enforced floor.

The checked-in questions have no currency values, percentages, expected
answers, or source prose. Years are permitted because fiscal-period selection
is part of retrieval intent rather than an answer value.

## Live embedding boundary

Validation has two modes:

```bash
uv run python scripts/evaluate_semantic_holdout.py --validate-only
uv run python scripts/evaluate_semantic_holdout.py \
  --base-url http://127.0.0.1:8000 \
  --output artifacts/semantic-holdout-report.json
```

The live command requires an already running local Playground configured with
an allowed real embedding provider. It authenticates the matching local
persona for each case and calls `/v1/retrieval`. Every retrieval request is
exactly:

```json
{"limits": {"...": "server bootstrap defaults"}, "query_text": "..."}
```

There is no query-vector field in the asset or request. The server must create
the query embedding through its configured provider, declare a non-fixture
provider, and return an embedding-space identity. The runner accepts only an
explicit loopback IP origin so bearer tokens and holdout traffic cannot be
redirected to another host.

The output is answer-free and source-text-free. It records only dataset and
embedding identities, selected/visible Chunk IDs, evidence recall at five,
MRR, forbidden exposure count, thresholds, and pass/fail reasons. A provider
failure is an execution error, never a successful refusal.

## Interpretation

This holdout is a regression gate against memorized fixture wording, not a
replacement for the 49-case adjudicated suite. Its metrics test whether newly
worded questions recover independently labelled evidence and whether any
forbidden evidence appears anywhere in the returned trace. Because it has only
14 cases, changes to queries or evidence labels require human review, a new
artifact checksum, and a dataset-version update rather than automatic
regeneration from the corpus builder.

## Recorded development run

On 2026-09-04, the local `dev-mini` Playground ran all 14 cases through the
authenticated HTTP endpoint with the configured
`dashscope-openai-compatible` provider. The run passed with complete-evidence
Recall@5 `0.90`, evidence-ID Recall@5 `0.8667`, MRR `0.7583`, and zero forbidden
Chunk exposures. This is development regression evidence only; under the
project acceptance contract it is not a production-candidate validation run.
