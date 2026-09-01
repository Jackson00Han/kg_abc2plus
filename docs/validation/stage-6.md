# Stage 6 Validation Record

- Date: 2026-09-02
- Acceptance contract: `contracts/acceptance.v1.json` version 1.0.1
- Validation profile: `dev-mini`
- Development corpus: `dev-corpus-v1` version 1.0.1
- Answer gold: `datasets/dev-corpus-v1/answers.jsonl` version 1.1.0
- Corpus generator: `scripts/build_dev_corpus.py` version 1.3.0
- Generation prompt: `grounded-answer-v1.3.0`
- Generation output schema: `grounded-answer-output-v1.0.0`
- Corpus schema signature: `company-filings:v1`
- Corpus annotation prompt signature: `checked-in-synthetic-annotations:v1`
- Python: 3.12.12
- Database fixture: `neo4j:5.26.12-community`
- Recorded image digest:
  `sha256:9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37`

The Stage 6 runner currently invokes the database image by tag; it does not
itself resolve or enforce the recorded digest. The digest above is the
repository's independently recorded digest for that fixture tag, not a value
invented from the Stage 6 runner output.

## Delivered

- A provider-neutral grounded-generation package with immutable request,
  result, claim, conflict, citation, limit, and model-boundary contracts.
- A versioned prompt and strict structured-output schema. Model output is
  treated as untrusted; the server owns citation labels and final rendering.
- Chunk-level inline citations for every material factual claim, with exact
  Chunk, Document, immutable Version, checksum, ordinal, character range,
  page/section, Document title, and publication-time provenance.
- Fail-closed context validation before a provider call, including exact
  Chunk-length and SHA-256 checks and required authoritative citation fields.
- Unique evidence excerpts expanded to authoritative scopes; per-citation
  sourced-wording, semantic-operator, local-literal, inference-labelling, and
  signed number/date/currency/unit gates. Graph-derived data remains navigation
  input, not factual answer evidence.
- Fail-closed preservation of fact-bearing prepositions, scope-edge
  qualifiers, unrecognized units, rate bases, scenario/pro-forma wording, and
  audit status.
- Deterministic insufficient-context refusal, provenance-safe conflict
  handling, provider-failure separation, and independent generation bounds.
- A versioned 49-case direct-evidence answer gold dataset plus an evaluator
  that requires complete, current-version runtime results, canonical rendering,
  unique provenance, and zero hidden failures or forbidden-answer exposure. It
  never treats gold annotations as predictions.
- Real-Neo4j retrieval-to-generation coverage over the representative corpus,
  including authorization, citation provenance, refusal, exact-value, and
  temporal-comparison behavior without an external model call.

## Reproducible checks

Run from the repository root:

```bash
uv run --locked python scripts/build_dev_corpus.py --check
env -u OPENAI_API_KEY -u OPENAI_BASE_URL \
  -u NEO4J_URI -u NEO4J_PASSWORD \
  uv run --locked python -m unittest discover \
  -s tests/unit -p 'test_*.py' -v
./scripts/run_stage6_neo4j_tests.sh
python3 scripts/validate_acceptance_contract.py
uv run --locked python scripts/evaluate_retrieval.py
uv lock --check
uv build --out-dir /tmp/sample-graphrag-stage6-dist
uv run --locked python -m compileall -q src tests scripts
sh -n scripts/run_stage2_neo4j_tests.sh
sh -n scripts/run_stage3_neo4j_tests.sh
sh -n scripts/run_stage4_neo4j_tests.sh
sh -n scripts/run_stage5_neo4j_tests.sh
sh -n scripts/run_stage5a_neo4j_tests.sh
sh -n scripts/run_stage6_neo4j_tests.sh
git diff --check
```

The standalone evaluator intentionally has no default result file. To score a
captured runtime JSONL with exactly one result for every gold case, run:

```bash
uv run --locked python scripts/evaluate_grounded_answers.py \
  --results /path/to/actual-answer-results.jsonl
```

The disposable runner removes provider and ambient Neo4j credentials, refuses
a pre-existing fixed-name container, binds Bolt to loopback port 17692, starts
a clean database, and removes the container on exit. It retains the `dev-mini`
cap of 1.5 GiB, one CPU, a 512 MiB maximum heap, and a 128 MiB page cache.

## Verified behavior

- All 153 offline unit tests pass in 0.631 seconds.
- All 62 disposable-Neo4j integration tests pass in 373.111 seconds.
- The deterministic corpus check reproduces 10 documents, 120 Chunks, 19
  governed entities, 49 questions, 169 fixture vectors, and 49 independent
  answer annotations at gold version 1.1.0.
- The answer gold contains 35 answered cases and 14 expected refusals across
  all seven Stage 1 question classes. It contains 56 material claims, 64 exact
  tokens, six explicitly labelled inferences, and five required temporal
  comparisons.
- All 49 actual retrieval-to-generation cases complete without a generation
  failure or forbidden-term exposure and meet every Stage 1 answer target.
- Citations are reconstructed only from authorized, hydrated retrieval data;
  model output cannot author or replace Document title, publication time, or
  any other provenance field.
- Empty, incomplete, over-budget, checksum-invalid, or structurally invalid
  context/output fails closed. Provider unavailability is reported separately
  from an evidence-based insufficient-context refusal.
- Sourced claims require every citation to independently support the ordered
  wording, semantic operators, and locally bound exact literals inside an
  authoritative scope. Material inferences are explicitly labelled and use a
  deterministic subject/measure/value/year comparison form.
- A claimed conflict requires supported alternatives from distinct
  Document/Version provenance and a shared subject/measure/period skeleton
  with incompatible comparable values. Same-Version, cross-period, different
  metric, or merely related alternatives cannot be promoted to a conflict.

### Actual 49-case answer metrics

| Metric | Observed |
| --- | ---: |
| `item_count` | 49 |
| `material_claim_count` | 56 |
| `citation_attachment_count` | 76 |
| `exact_token_count` | 64 |
| `expected_refusal_count` | 14 |
| `expected_conflict_count` | 0 |
| `expected_temporal_comparison_count` | 5 |
| `generation_failure_count` | 0 |
| `forbidden_answer_exposure_count` | 0 |
| `supported_claim_rate` | 1.0 |
| `citation_precision` | 1.0 |
| `citation_coverage` | 1.0 |
| `numerical_fidelity` | 1.0 |
| `refusal_precision` | 1.0 |
| `refusal_recall` | 1.0 |
| `refusal_f1` | 1.0 |
| `answer_correctness` | 1.0 |
| `conflict_handling_rate` | N/A (`None`) |
| `temporal_comparison_rate` | 1.0 |

`conflict_handling_rate` is deliberately N/A rather than 1.0 because this
synthetic gold set contains no expected same-scope conflict. Conflict behavior
is covered by hand-computable unit cases, but the 49-case corpus does not
provide a real-Neo4j conflict-rate denominator.

## Completeness issue exposed and bounded fix

The first retrieval-to-generation run reused the Stage 5 default context of
`top_k=5` and `anchor_k=3`. It stopped at the first assertion failure,
`graph_relationship-boundary-02`: its two-entity answer lacked selected
evidence for one material claim. Because that run stopped immediately, it does
not establish how many other cases would have been incomplete under the same
configuration.

The generation integration profile was changed to the still-bounded
`top_k=10` and `anchor_k=5`, matching the generation service's ten-Chunk input
ceiling. The Stage 5 default remains `top_k=5`/`anchor_k=3`; ACL filters, the
audited 0.75 Neo4j vector-score gate, RRF/RA formulas, metric thresholds, and
all character/output budgets were not weakened. The rerun completed all 49
cases with the metrics above.

## Decision and limitations

Stage 6 satisfies its functional exit criteria under `dev-mini`: answers are
bounded, cited at Chunk level, traceable through immutable Document Versions,
permission-safe, numerically faithful, and fail closed for unsupported or
insufficient evidence. The committed evaluator makes these properties
repeatable over explicit actual results.

This is not production-candidate model-quality evidence. The corpus is
synthetic, its manifest declares embedding `quality_claim: none`, and its
adjudicated evidence-cluster vectors are gold-derived. The 49-case integration
uses a deterministic static model payload assembled only after actual Neo4j
retrieval; it validates retrieval-to-generation orchestration, enforcement,
provenance, and metrics, but not a real LLM's answer quality, latency, token
usage, cost, or failure distribution.

The authoritative-scope, lexical, semantic-operator, and exact-literal runtime
gates are conservative safety checks, not a complete semantic-entailment
model. They may reject valid paraphrases. The corpus has no positive
same-scope conflict case, the `top_k=10`/`anchor_k=5` setting is
workload-calibrated, and the runner does not enforce its recorded image digest.
Provider, customer-corpus, production-scale, concurrency, sustained-load,
recovery, latency-percentile, and operating-cost evidence remains for Stages
7 through 9; none is claimed by this record.
