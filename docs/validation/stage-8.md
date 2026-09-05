# Stage 8 Validation Record

- Original completion date: 2026-09-02
- Current baseline maintenance date: 2026-09-06
- Acceptance contract: `contracts/acceptance.v1.json` version 1.0.1
- Validation profile: `dev-mini` version 1.0.0
- Reviewed regression baseline: `dev-mini.v1.json` version 1.5.0
- Unified gold: `evaluation/gold-v1/manifest.json` version 2.0.0
- Representative corpus: `dev-corpus-v1` version 1.0.1
- Answer gold: version 1.1.0
- Generation prompt: `grounded-answer-v1.3.0`
- Generation output schema: `grounded-answer-output-v1.0.0`
- Database fixture: `neo4j:5.26.12-community`
- Recorded image digest:
  `sha256:9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37`

The Stage 8 Neo4j runner invokes the image by tag. It records but does not
resolve or enforce the digest above at runtime.

## Delivered

- A focused `graphrag_prod.evaluation` package for fail-closed dataset loading,
  answer, graph, retrieval, operational metrics, acceptance gates, immutable
  baseline comparison, and report construction.
- `gold-v1` version 2.0.0, which binds 49 seven-class questions, 49 grounded
  answers, 120 exact Chunks, 60 graph adjudications, two supplemental conflict
  cases, four conflict source Chunks, all artifact checksums, and complete
  required-case lists.
- Strict separation of prediction-free graph/retrieval/answer gold from actual
  graph decisions, real-Neo4j rankings/traces, generated answers, and conflict
  results.
- Claim-derived `required_evidence_groups`: alternatives within each sourced
  factual group, singleton mandatory operands for inferences, mandatory
  coverage across all groups, and a proof that the complete hitting set fits
  in top five. Stage 8 Recall@5 therefore measures complete required-evidence
  retrieval rather than any-hit success.
- Standard fractional evidence Recall@5 as a separate diagnostic, plus MRR,
  nDCG@5, unauthorized exposure, graph precision/resolution, grounded-answer,
  citation, numerical, refusal, temporal, conflict, lifecycle, reliability,
  nearest-rank latency, throughput, token, and decimal cost metrics.
- A positive same-scope conflict with two distinct Document/Version sources
  and incompatible FY2024 values, paired with a different-period non-conflict
  contrast.
- Machine-readable suite results, a fixed eleven-case security manifest,
  exact negative-case coverage, per-case digests, complete version inventory,
  zero-tolerance invariants, and a reviewed deterministic baseline policy.
- A separately adjudicated industrial extraction-quality dataset, independent
  prediction artifact, zero-tolerance policy, and locked baseline covering
  ontology-constrained entities, relationships, exact evidence, typed temporal
  properties, entity resolution, review state, and restricted-content output.
- One resource-bounded workflow that runs every suite twice against independent
  disposable Neo4j databases and compares the complete deterministic report
  projections.

## Reproducible workflow

Run from the repository root. The output directory must not exist before the
command starts:

```bash
./scripts/run_stage8_validation.sh \
  --repeat 2 \
  --baseline evaluation/baselines/dev-mini.v1.json \
  --output-dir /tmp/sample-graphrag-stage8
```

Each repetition runs:

- all unit tests with no skips;
- all HTTP end-to-end tests with no skips;
- all security tests, plus exact enforcement of the eleven required security
  test IDs;
- the Stage 8 regression suite with no skips;
- every `test_*neo4j.py` integration test against a new empty, loopback-only,
  one-CPU Neo4j container;
- real retrieval and answer observation capture for all 49 representative
  questions, plus the two supplemental conflict results;
- the committed extraction-quality gate, with its report independently rebuilt
  from exact gold, prediction, policy, and locked-baseline assets;
- unified report construction, all Stage 1 metric comparisons, hard
  invariants, and exact reviewed-baseline comparison.

After both runs, the workflow compares case digests, contract metrics,
diagnostics, artifact identities (including all extraction-quality assets and
the extractor/report identities), semantic digest, and suite counts. It then
checks deterministic gold/corpus rebuilds, the acceptance profile, lock file,
package build, bytecode compilation, all shell syntax, and `git diff --check`.

The ordinary command cannot create or update a baseline. Baseline candidates
are restricted to an explicit single-run maintenance path and require manual
review before becoming the committed baseline. Commit-time secret scanning
remains a separate mandatory repository rule and must be completed before the
Stage 8 commit.

## Gate interpretation

The report applies all 20 Stage 1 metric definitions. Graph, retrieval,
answer, citation, refusal, security, ingestion, and reliability thresholds are
gating. Because the active profile is `dev-mini`, performance rows are present
but informational. Token and cost diagnostics have no Stage 1 acceptance
threshold.

Contract `recall_at_5` comes only from the high-level independent-gold/result
evaluator: a query passes when top five intersects every material-claim
evidence group. The standard macro fractional score remains separately
reported as `evidence_recall_at_5`. The historical low-level retrieval helper
uses its older `recall_at_5` field for the fractional score and is not the
Stage 8 gate.

The regression policy additionally requires exact complete-answer,
same-scope-conflict, temporal-comparison, graph-case, security-suite, and
zero-exposure/failure outcomes. Missing, extra, duplicate, skipped, malformed,
or stale-version cases fail before aggregation; rates cannot improve by
silently dropping negative cases or complete answers.

## Final measured evidence

<!-- STAGE8_FINAL_RESULTS_BEGIN -->
The original Stage 8 completion at commit `ce2f729` ran the authoritative
workflow twice. Each run passed 263 unit tests, four HTTP end-to-end tests,
eleven adversarial security tests, two Stage 8 regression tests, and all 64
disposable-Neo4j integration tests with no skips. The two Neo4j suites took
346.273 and 373.078 seconds, and both reports reproduced semantic digest
`20b75cba22c3e9b65dbdd7d7006e52db1cb3a42b71387378baa89605d4d752ce`.

Stage 9 subsequently added regression coverage without rewriting that
published history. A controlled baseline-maintenance run on 2026-09-03 passed
327 unit tests, five HTTP end-to-end tests, fifteen adversarial security tests,
two Stage 8 regression tests, and all 75 disposable-Neo4j integration tests
with no skips; the Neo4j suite took 396.925 seconds. Relative to the earlier
unqualified Stage 9 draft baseline with 317 unit and 72 integration tests, the
reviewed changes are ten additional unit test IDs and three additional
integration test IDs for Stage 9 evidence, resource checks, backup timing,
optimized retrieval, and concurrent corpus/ACL cutover safety. Baseline version
1.1.0 reproduces
semantic digest
`af94664fb502498b884eada4b27af892d13d73b9fcf66790601957e672cb126d`.
The Stage 9 authoritative workflow subsequently replayed this current baseline
twice from clean implementation commit
`7142fa331f74ecd868a5ba20d343c787e2f9d367`. Each replay passed 327 unit,
five HTTP end-to-end, fifteen security, two regression, and 75
disposable-Neo4j integration tests with no skips. The two integration suites
took 385.298 and 373.932 seconds, and both reports reproduced semantic digest
`af94664fb502498b884eada4b27af892d13d73b9fcf66790601957e672cb126d`.

The post-Stage 9 industrial-knowledge maintenance run on 2026-09-04 then
captured and manually reviewed baseline version 1.2.0. It adds the governed
property-graph construction loop, typed temporal property facts, an executable
extraction-quality gate, and the corrected
`graphrag_chunk_text_v2+embedding-generation-v1` operational index identity.
Two independent full replays each passed 499 unit tests, nine HTTP end-to-end
tests, 22 adversarial security tests, two Stage 8 regression tests, and 101
disposable-Neo4j integration tests with no skips. The integration suites took
554.480 and 556.137 seconds. Both reports reproduced semantic digest
`a923e89cefbdc5a14e5980035e34c0053d4eecaa46305e3a0e6feeb08c1610b4`;
the embedded extraction-quality report reproduced digest
`5f878437f1201524aee11762dd71582ca37345b77813d7efe5a04b7d9dba147c`.
The previously established contract metrics and per-case digests did not
change; the reviewed baseline changes are the new knowledge-quality identity
and diagnostics, four new quality-asset unit test IDs, the v2 index identity,
the current regression-policy digest, and migrations 006 through 010.

The 2026-09-05 workbench maintenance reviewed baseline version 1.3.0 after
the active-inventory literal correction and bounded startup-read recovery.
Final evidence contains 636 unit, 15 HTTP end-to-end, 33 security, two
regression and 125 disposable-Neo4j integration tests, all passing without
skips. The final unit and integration suites took 140.637 and 716.039 seconds.
The exact projection changes from 1.2.0 are migrations 011/012 and expanded
test-ID sets. All ten earlier migration hashes, 160 per-case digests, contract
metrics, quality diagnostics and other identities are unchanged. The two old
test IDs absent from the new sets were reviewed as preserved/extended test
renames, not removal of assertions or weakened gates.

Two unified-report replays over this same final suite/observation set passed
the reviewed baseline and reproduced semantic digest
`5327ec4f55c280fb8e3469bb5fd603bb1703f3cbfbd02423d2956b7f6e34dcaf`.
Those report replays are not two independent full-suite cycles. The baseline
version update also passed 15 focused evaluation tests. This development
maintenance is distinct from historical Stage 8 completion and does not renew
Stage 9 qualification. The live Qwen timeout, partial temporal retrieval,
inventory defect/correction and unavailable browser are explicitly recorded in
`governance-workbench-completion.md`; deterministic baseline success does not
erase those observations.

The subsequent extraction-timeout correction reviewed baseline version 1.4.0.
All 824 tests passed: 649 unit, 15 HTTP E2E, 33 security, two regression and 125
real-Neo4j integration tests, without skips. The complete unit suite was rerun
after the final Unicode token-hint correction (165.170 seconds); integration
took 765.834 seconds. Exact comparison against 1.3.0 found only 13 additional
unit test IDs. No prior test IDs were removed or renamed, and all 160 case
digests, 20 contract metrics, diagnostics and schema/quality identities stayed
unchanged. See `extraction-timeout-correction.md` for the independently passing
real-provider workflow and the limits of that small-source evidence.

The 2026-09-06 industrial-demo workbench maintenance reviewed baseline version
1.5.0. All 890 tests passed in one complete final-code development cycle: 707
unit, 15 HTTP E2E, 33 security, two regression and 133 real-Neo4j tests, without
skips. The revision adds source-only initialization, the versioned industrial
kit, bounded model validation feedback, automatic resolution suggestions and
an identity-query planning correction. Independent baseline comparison found
exactly 58 additional unit and eight additional integration tests. No prior
IDs were removed or renamed; all 160 case digests, 20 contract metrics,
diagnostics and schema/quality identities stayed unchanged. The final
semantic digest is
`692c8b935cacd0e3370d172651c76d88f569b9a2023fc65301dfc9d1c6860fae`.
Two report replays verify this same final evidence; they are not two full
suite executions. See `industrial-demo-workbench.md` for browser checks, the
real-provider construction/retrieval/retirement workflow, conservative
multi-mention behavior and other remaining limits. This maintenance does not
renew the historical Stage 9 qualification.

Across the historical completion and later maintenance captures,
gold/corpus rebuilds, acceptance-profile validation, lock verification,
package build, bytecode compilation, shell syntax, and `git diff --check`
passed.

Measured gating quality results are graph entity precision 1.0, relationship
precision 1.0, entity-resolution accuracy 1.0, complete-evidence Recall@5
0.9714285714285714, MRR 1.0, nDCG@5 0.9879560689802693, supported-claim rate
1.0, citation precision 1.0, citation coverage 1.0, numerical fidelity 1.0,
refusal F1 1.0, ingestion success 1.0, recovery success 1.0, and zero
unauthorized exposure, idempotency mismatch, deletion residue, unexpected
server error, generation failure, or forbidden-answer exposure. Standard
fractional evidence Recall@5 is 0.9738095238095239; complete-answer,
same-scope-conflict, temporal-comparison, graph-case, and security-completeness
diagnostics are all 1.0/true.

The industrial extraction-quality reference reports entity, relationship,
property, per-type, and overall F1 of 1.0, with zero evidence/schema
violations, authority contamination, restricted-content false positives,
high-risk pending reviews, false merges, or missed merges. These are bounded
offline reference observations, not a claim about unrestricted customer
documents or every future model version.

The committed deterministic operational fixture reports retrieval p95 190 ms,
answer p95 1200 ms, 8.333333333333334 retrieval requests/second, and estimated
cost USD 0.0006. These values passed their inherited thresholds but remain
explicitly informational and non-qualifying under `dev-mini`. The exact staged
Stage 8 file set passed the pre-commit secret scan; no credential-like value or
forbidden generated/runtime artifact was included.
<!-- STAGE8_FINAL_RESULTS_END -->

## Decision and limitations

<!-- STAGE8_FINAL_DECISION_BEGIN -->
Stage 8 is complete under its declared `dev-mini` scope. One documented
workflow produced two repeatable complete-system quality and regression
reports, every inherited gating threshold and hard invariant passed, negative
and security cases remained mandatory, and the reviewed exact baseline matched.
This decision advances the plan to Stage 9; it is not production-candidate
qualification.
<!-- STAGE8_FINAL_DECISION_END -->

The 2026-09-05 maintenance validates the current industrial-knowledge changes
under `dev-mini`; it does not retroactively extend the historical Stage 9
production-reference qualification at commit `7142fa3`. A fresh Stage 9 run is
required before describing the current head as a production candidate.

Regardless of the final `dev-mini` result, the report always records
`production_candidate_eligible: false`. The current corpus, embeddings,
structured answer model, conflict fixture, graph observations, and operational
observations are deterministic regression instruments. They do not establish
real embedding/LLM quality, provider latency or failure distributions,
customer-corpus behavior, production-scale throughput, or operating cost.

The supplemental conflict cases validate a narrow numeric same-scope
disagreement and a different-period contrast through the generation service;
they do not traverse real retrieval. The current evidence projection supports
alternatives for sourced claims and mandatory singleton operands for
inferences. More complex nested evidence logic would require a versioned
schema extension.

The Neo4j image digest is recorded but not enforced by the runner. The
performance and cost samples are committed, non-qualifying fixtures, and the
graph actual observations are a split projection of the Stage 4 reviewed
sample rather than a new production-scale extraction run.
