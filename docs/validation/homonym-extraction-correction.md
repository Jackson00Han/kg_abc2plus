# Homonym extraction evidence correction

## Observed failure

The original 151-character `homonym_report.txt` clearly describes a second pump,
code `BC-P-202`, with rated power `22.0 kW`. Both model responses failed before
entity resolution. This was an extraction-coordinate failure, not a reason to
merge or reject the equipment because its name matches the existing `BC-P-101`.

The immutable regression fixture is
`tests/fixtures/homonym-span-failure.v1.json`. It contains the exact source and
both raw provider responses, their original SHA256 digests, and a separately
labelled adjudicated response for deterministic offline testing.

| Attempt | Entity mention | Property evidence | Result |
| --- | --- | --- | --- |
| 1 | `循环水泵` at `[47,51)`; another occurrence at `[115,134)` | Code `[52,81)`, power `[82,107)` | Neither fact enclosed a declared mention |
| 2 | Entire name/code/power sentence `[47,107)` | Code `[47,81)`, power `[47,107)` | Code evidence did not enclose the oversized mention |

The strict validator correctly reported `ENDPOINT_OUTSIDE_EVIDENCE`. Both
historical responses still fail the unchanged validator after this fix.

## Change and boundaries

The initial extraction instructions distinguish a compact identifying name/code
mention from the larger supporting property statement. Correction feedback now
identifies the failing fact, its entity reference, exact evidence coordinates
and exact declared mention coordinates. Correct compact mentions should be
retained; the model must repair insufficient fact evidence or an oversized
mention, without inventing source text.

`construction/validation_feedback.py` optionally supplies a mechanical enclosure
hint for an entity property. It uses only an already declared, exact mention and
already exact fact evidence containing complete value/unit/time tokens. There
must be exactly one eligible mention in the same sentence, no other declared
entity inside the envelope, no crossed newline/sentence boundary, and no span
longer than 512 characters. Ambiguity, malformed spans, missing references,
duplicate references and a mention enclosing an overly short fact suppress the
hint. Relationship errors receive coordinate diagnostics without independent
single-endpoint repair proposals.

Hints are explicitly labelled coordinate-only, not evidence of ownership or
entailment. The model may still omit an unsupported claim. The server never
expands evidence in stored output, narrows mentions automatically, transfers an
identity between mentions, or accepts a response because a hint exists. Every
returned response passes the original full schema, ontology, exact-source,
enclosure, token, temporal and trust checks.

Feedback remains capped at 8,192 characters and 32 findings. The provider policy
still allows at most two attempts, with a 30-second timeout and 2,048 output-token
limit per call in the Playground. Provider failures and oversized responses do
not trigger a corrective call. Each original response and checksum is retained
before correction or candidate persistence.

The Playground prompt is now
`industrial-property-graph-extraction:v5-endpoint-context`; the request-policy
signature also binds the compact-mention policy and
`strict-validation-feedback-v2-endpoint-context`. Old failures remain historical
failures; new work cannot silently reuse an artifact from the old call policy.

## Verification

The historical failure and correction can be reproduced without a provider call:

```sh
uv run --locked python -m unittest \
  tests.unit.test_construction_validation_feedback \
  tests.unit.test_construction_endpoint_feedback \
  tests.unit.test_industrial_demo
```

- Final extraction, workflow, demo, endpoint-feedback and Playground focused run:
  115 tests passed. This includes the two original rejected responses, successful
  adjudicated correction, exact typed values, immutable attempts, negative
  enclosure cases and actual resolution of the extracted code against a 101
  authoritative profile: the 202 equipment remains `NO_MATCH`.
- A fresh real-provider extraction of the unchanged original file passed in one
  call and produced `BC-P-202` plus normalized `22 kW` (raw `22.0 kW`).
- A separate correction probe supplied the recorded first failure as the first
  response, then made one real corrective provider call. The model returned a
  valid response with the same correct code and power. This is a hybrid regression
  probe, not a claim that both calls were new provider calls.
- Two final conservative hint guards were checked after these provider probes:
  no envelope encouraging an oversized mention, and the validator's complete
  token matching instead of substring matching. The exercised original-first
  correction coordinates are unchanged; the guards add negative-case protection.

Provider results are retained locally in
`/tmp/graphrag-homonym-fix-20260906/provider-results.json`; the original rejected
job remains in the live database. No fixed number of model calls or successful
extractions is promised for arbitrary documents. These checks establish this
regression and preserve failure handling; they do not replace the locked
extraction-quality gate or production-scale evaluation.

### Real HTTP verification on the reloaded service

The unchanged original file was uploaded through the normal construction API on
port 8002, using a separate Tenant Beta test context. That context had the demo
T-Box and seven published expert baseline records, including `BC-P-101`. No Alpha
document, review or publication was modified by this test.

Job `91c99793-d858-5f03-a6f8-2ffdeba2868e` passed on the first provider attempt,
with no validation findings. Its response SHA256 is
`90f077cf0e41c7af937dc4721c06d71889ab6728382359fb7547b70bc596a9bc`.
The normal review API returned three mentions of one equipment and two property
facts, all `SECONDARY / LLM_EXTRACTED / CANDIDATE`. Both properties cite the exact
source range `[52,108)`: code `BC-P-202` and raw power `22.0 kW`, normalized to
`22 kW`. The new candidates were neither approved nor published.

| Source mention | Bound identity property | Resolution against the 101 baseline |
| --- | --- | --- |
| `BC-P-202` at `[73,81)` | `EquipmentCode = BC-P-202` | `NO_MATCH`; eligible for review as a new entity |
| `BC-P-202` at `[120,128)` | None | `CONFLICT`; defer pending identity evidence |
| `循环水泵` at `[47,51)` | None | `CONFLICT`; defer pending identity evidence |

This also demonstrates an existing limit: identity properties belong to a
particular mention revision. Additional mentions do not inherit them merely
because they share a model entity reference or name. This fix resolves the
extraction-validation failure; it does not implement cross-mention identity
propagation or promise that every generated mention can be approved. No mention
was automatically linked to the different equipment `BC-P-101`.

The local `http-calls.json` and `http-summary.json` in the same verification
directory retain the API results without session tokens.

### Complete automated capture

One complete Stage 8 capture passed all 922 tests, with no failures, errors or
skips:

| Suite | Passed | Seconds |
| --- | ---: | ---: |
| Unit | 736 | 133.298 |
| HTTP E2E | 16 | 1.277 |
| Security | 33 | 2.058 |
| Regression | 2 | 0.024 |
| Disposable Neo4j | 135 | 809.184 |

The capture also passed corpus rebuild, contract/schema checks, compilation,
packaging, dependency lock, the locked industrial extraction-quality gate and
diff checks. The evaluation semantic digest is
`54489dbdc9821bc934a1671493bd0e048f5622eecabc34d80a4c5ee0c8944470`;
the extraction-quality digest remains
`5f878437f1201524aee11762dd71582ca37345b77813d7efe5a04b7d9dba147c`.

Independent baseline projection review confirmed exactly 12 new unit test IDs
and preservation of all previous 910 test IDs. All 160 case digests, 20 contract
metrics, 27 diagnostics and eight identity domains remain unchanged. This is
`dev-mini` development evidence; its timing and cost observations do not qualify
as production-candidate validation or establish improved extraction precision.

Baseline 1.7.0 records that reviewed test-inventory change. Two evaluation replays
against the new baseline passed with the same semantic digest, and 17 final
baseline/metrics/dataset/regression checks passed. These replays reuse the one
complete capture; they are not two complete test runs.

The extraction changes were frozen before the complete capture began. The
read-only resume check's physical-index `COSINE` adjustment happened after unit
discovery and was checked by the separate final 34-test suite and real-database
preflight/reload described below. The complete capture's artifacts were not
rewritten or combined with that scoped check into a purported final-source full
rerun.

Reproduce the complete workflow with a new output directory:

```sh
./scripts/run_stage8_validation.sh --repeat 1 \
  --output-dir /tmp/graphrag-homonym-validation-recheck
```

## Reloading without deleting the user's work

The local Python launcher now accepts `--reuse-existing-corpus`. It verifies the
existing schema, both tenants' active index readiness, complete embedding-space
identity, and the physical vector index label, property, dimensions and similarity
before serving. It does not initialize schema, ingest the seed corpus, materialize
embeddings or change stored data. Default empty-database initialization is
unchanged. Five resume tests cover positive and incompatible states.

A read-only check on real Neo4j found that physical index similarity is returned
as uppercase `COSINE`; the check now validates its type and compares it without
case sensitivity. The stored generation contract remains strict. The final
34-test resume/Playground suite and real-database preflight passed after that
adjustment.

For this delivery, the application was reloaded on the existing port 8002 and
existing database. The old shell launcher's automatic container cleanup was
avoided. Before/after snapshots confirmed the same container and identical data
for all 1,154 nodes and 2,244 relationships. No user document, approval or
publication was reset. Verification artifacts are
`database-before.json` and `database-after-restart.json` in the local directory
above. Port 8003 is not used.

To resume an existing configured local database, provide the same
`PLAYGROUND_NEO4J_*` connection settings and run:

```sh
uv run --locked python -m scripts.run_playground \
  --port 8002 --no-open --reuse-existing-corpus
```

The shell `run_playground.sh` is the disposable fresh-database launcher; it is not
the restart command for preserving an existing corpus.
