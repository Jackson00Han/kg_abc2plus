# Governance workbench completion

Validation date: 2026-09-05. This is the development-scale validation of the
governance workbench additions. It does not replace the separately documented
production-reference load and deployment prerequisites.

## Completed capabilities

- Bounded retrieval controls, version filters and trust-aware subgraph output.
- Governed entity-resolution suggestions and explicit reviewed linking.
- Evidence-backed relationship properties and publication cardinality checks.
- Independent semantic holdout questions beyond the builder-coupled fixtures.
- Construction recovery, immutable revisions and publication candidates.
- Active graph quality audit and a metadata-only active A-Box inventory.
- Record removal, document retirement and vector-generation refresh.
- Exact T-Box version loading, editing a new version and JSON export.
- Runtime document counts and fail-closed embedding/index readiness checks.
- Explicitly recorded, immutable quality observations with first-observer
  metadata, authorized history reads and an independent history view.

The inventory validates the complete active manifest before applying a document
filter or response limit. Its transaction rechecks materialized relationship
properties, typed values, evidence ranges, Chunk ordinals and ACLs. The HTTP
inventory limit is 500 records and requires complete visibility of the active
publication.

Recorded quality reports preserve a specific publication/T-Box/corpus boundary.
The HTTP report projection is checked before persistence. Replays retain the
first observer and time; recording a failing report does not approve knowledge.
The existing live quality GET remains read-only. The workbench discards stale
responses after identity, filter or publication changes.

## Committed implementation nodes

| Commit | Delivered node |
| --- | --- |
| `3cd3580` | Immutable quality-history core and schema |
| `33eeb56` | Pre-persistence transport validation and timeout propagation |
| `40b8bb6` | Transactional inventory relationship-evidence validation |
| `936e307` | Active inventory and recorded quality-history APIs |
| `fa4dd4c` | Inventory and quality-history workbench |
| `436610d` | Literal-object inventory correction and mixed-publication regression |
| `49bb96a` | One bounded read-only recovery attempt during startup warm-up |

Earlier related nodes are recorded in Git history and the completed items in
`to_do_list.md`.

## Verification

The first frozen-code suite passed 804 tests: 630 unit, 124 real-Neo4j integration,
33 security, 15 HTTP E2E and two regression tests, with no failures, errors or
skips. The disposable integration suite took 746.154 seconds and removed its
test container afterward. Provider-backed workflow results are recorded
separately below; passing deterministic tests does not imply live-provider
success.

The live smoke subsequently exposed the literal-object query defect described
below. After that correction, all 631 unit, 15 HTTP E2E, 33 security and two
regression tests passed again. The final replay, including the five startup
recovery tests, passed **811 tests**: 636 unit (140.637 seconds), 125 real-Neo4j
integration (716.039 seconds), 15 HTTP E2E, 33 security and two regression tests.
There were no failures, errors or skips. Integration code was frozen during
its full replay; the separate launcher-only recovery was covered by the final
unit replay. The final five warm-up test IDs also match the current source
methods exactly. The integration container was removed after completion.

Focused checks passed: 127 API/runtime/Playground tests; 33 security tests;
15 HTTP E2E tests; 14 real-Neo4j inventory/publication tests; six real-Neo4j
quality-history tests. The 29 Playground tests include nine executable Node VM
behavior checks for inventory and quality-history interactions.

Wheel/source builds, Python compilation, shell syntax, source/corpus rebuild,
acceptance-contract validation, lock verification, whitespace checks and tracked
credential-pattern scanning passed. The wheel contains the new history module,
HTTP contracts, migration and workbench asset.

The first full unit run started during a concurrent UI-test edit and loaded an
older Promise-wait helper. It failed one UI test. The corrected helper waits for
the complete event-loop turn; focused checks passed and the full suite was
restarted only after all implementation files were frozen.

The initial smoke service returned HTTP 200 for `/playground`; readiness reported
`neo4j`, `embedding_generations` and `vector_indexes` all `ok`.

## Reviewed regression baseline

Baseline `evaluation/baselines/dev-mini.v1.json` is now version 1.3.0. Manual
and independent comparison against 1.2.0 found only the additive migration
011/012 identities and expanded test-ID sets. The ten earlier migration hashes,
51 answer, 60 graph and 49 retrieval case digests, every contract metric and
diagnostic, and the locked knowledge-quality identity remain unchanged. The
two renamed tests preserve their earlier assertions; one also adds v2 codec
compatibility coverage. No quality threshold or negative/security requirement
was relaxed.

The version update passed 15 focused evaluation tests. Two unified-report
replays over the same final suite/observation inputs passed and reproduced
semantic digest
`5327ec4f55c280fb8e3469bb5fd603bb1703f3cbfbd02423d2956b7f6e34dcaf`.
These are two report replays, not two independent full-suite cycles. Both
reports retain `production_candidate_eligible: false`; the maintenance does
not renew the historical Stage 9 qualification.

## Ad-hoc live retrieval observations

Five newly phrased questions were sent through the authenticated local HTTP
service using `text-embedding-v4`; these were not verbatim gold questions.
All five requests returned HTTP 200 with exact source locations/checksums,
authorized Trace IDs and bounded context. That is a transport, provenance and
authorization result, not five successful relevance results.

| Question | Identity | Observed outcome |
| --- | --- | --- |
| Northstar FY2024 cash amount and explicitly excluded balances | Alpha Public | Required cash evidence found |
| Shared Meridian/Harbor FY2024 risk involving delayed components and transport interruptions | Beta Board + Public | Partial: Meridian FY2024 found, Harbor FY2024 absent from final five; Harbor FY2023 present |
| Distinguish Atlas Cloud Services and Atlas Logistics by their FY2024 platform/network | Alpha Finance + Legal | Both companies' product evidence found |
| Atlas Cloud Services FY2024 cash amount from a public identity | Alpha Public | Restricted evidence absent; authorized unrelated context returned |
| Northstar FY2024 Orbit components from the other tenant | Beta Public | Cross-tenant evidence absent; authorized unrelated context returned |

The two negative cases pass the intended ACL checks, but unrelated authorized
context is not relevant evidence for their questions. The cross-period partial
result is retained in `to_do_list.md`; no ranking thresholds or expected
results were relaxed. HTTP durations in this one sequential smoke run ranged
from approximately 1.1 to 21.9 seconds while the full validation workload was
also running. They are not a latency benchmark.

A separate diagnostic repeated the same cross-company question with unchanged
retrieval thresholds and `version_filter.document_ids` restricted to the
Meridian/Harbor FY2024 documents. Both FY2024 operational-risk Chunks became the
first two results; source locations, checksums, tenant/ACL and the Document
filter all held. This verifies the existing explicit filter, not automatic
temporal interpretation or a fix to the original unfiltered query.

## Live construction and governance observations

Historical attempt below: the timeout was subsequently diagnosed and corrected
without changing the configured model/key or extending the 30-second budget.
A separate fresh real-provider workflow passed; see
[`extraction-timeout-correction.md`](extraction-timeout-correction.md).
The original failure is retained rather than retroactively relabelled.

The configured extraction model was `qwen3.8-max`, not the launcher's fallback
model name. A newly imported and published T-Box constrained this synthetic
single-Chunk source:

> Validation Pump ZX-47 is installed at Validation Site Delta.
> Validation Plant Services operates Validation Pump ZX-47.
> The rated power of Validation Pump ZX-47 is 11 kW.

The source was ingested, but the first automatic extraction returned HTTP 503.
The job remained in `RETRY_WAIT` with one expected Chunk, zero completed Chunks,
`MODEL_CALL_FAILED`, and no extracted candidates. One recovery request with
the same operation key, input and model also returned 503. No unreviewed
knowledge was published. A separate read-only diagnostic using the identical
Chunk/T-Box/model and the same 2,048-output-token/30-second call bounds raised
`APITimeoutError` after 31.289 seconds. No provider response, finish reason or
usage was available. This establishes a provider timeout under the configured
budget, not a token-truncation diagnosis or a successful automatic-extraction
acceptance. Neither the user's model setting nor `.env` was changed.

To exercise the downstream APIs independently, an explicitly manual QA import
used exact source evidence for two entities, their `INSTALLED_AT` relationship
and the 11 kW property. These four authoritative records were published; they
are not substituted for or counted as successful LLM-derived candidates.

The live quality report passed with zero issues. Explicit history recording,
listing, detail retrieval and repeated recording all returned HTTP 200, with
identical immutable report/observer metadata on replay. Retrieval returned the
exact uploaded Chunk plus two entities, one relationship and one literal fact.
Both graph trust-policy requests returned only the available `AUTHORITATIVE`
records. Beta could not read the Alpha history/construction-job IDs (404) and
its retrieval output did not contain the uploaded Alpha Chunk.

This run did **not** validate real-model candidate approval or coexistence of
`SECONDARY` and `AUTHORITATIVE` results: no model candidates were created.
Those paths retain automated coverage, but the live-provider acceptance remains
open under this model and timeout budget.

The first inventory read of the otherwise valid publication returned HTTP 409,
while its quality report passed. A real-data investigation found Cypher's
three-valued null equality rejecting literal assertions: both absent
`object_entity_id` values were compared with `=`. The correction uses exact
entity-ID equality for entity objects and requires both IDs to be absent for
literal objects. Existing evidence, ACL, materialization and OBJECT-edge checks
remain mandatory. The original failed HTTP observation is retained separately
from the corrected-code verification.

Corrected-code verification on the same data returned HTTP 200, all four
records and the exact DECIMAL 11 kW fact, without source text in the inventory.
The focused real-Neo4j inventory suite passed four tests, including the new
mixed-publication test and post-audit tampering rejection. See
`inventory-literal-correction.md`.

## Startup recovery

A subsequent cold restart, running alongside the integration workload,
exceeded the unchanged 30-second Neo4j transaction bound during retrieval
warm-up and failed closed. The correction permits one retry of that tenant's
same read-only request after `RetrievalBackendTimeout`. It preserves the
request, vector, ACL, limits and per-transaction deadline, does not repeat the
Embedding call, and propagates any second timeout or other error. All 34
focused startup/Playground tests passed, including five new recovery checks.
This does not retry or extend the independent LLM-extraction timeout. See
`playground-warmup-recovery.md`.

The previous disposable database contained only the ten committed fixture
documents and the synthetic QA source above. It was stopped and removed during
the restart; the source and observations are retained here and in local
sanitized reports, not as a restorable database backup.

The final standalone restart loaded implementation `49bb96a` and returned
HTTP 200 for `/playground`. `/health/ready` returned `status: ready` with
`neo4j`, `embedding_generations` and `vector_indexes` all `ok`. The fresh
database contains ten Documents and 120 Chunks. Bootstrap metadata confirms
`text-embedding-v4` at 1,024 dimensions, extraction model `qwen3.8-max`, the
unchanged 30-second model-call budget and four-Chunk construction cap. The
service was left running at `http://127.0.0.1:8000/playground`; a later stop
will remove this disposable database in the normal way.

## Repeatable checks

Run a complete development-scale cycle from a new output directory:

```bash
./scripts/run_stage8_validation.sh \
  --output-dir /tmp/graphrag-workbench-validation-new-run \
  --repeat 1
```

The standard workflow defaults to two cycles when `--repeat` is omitted.
For the focused corrections:

```bash
uv run --locked python -m unittest \
  tests.unit.test_published_inventory \
  tests.unit.test_playground_warmup \
  tests.unit.test_playground -v
```

The real-Neo4j inventory tests run within the complete workflow; the HTTP smoke
observations above additionally used the configured external embedding and
extraction providers. They are not substituted into the deterministic baseline.

## Limits and deferred work

- Independent reranking remains explicitly deferred in `to_do_list.md`.
- The local Playground remains a disposable development service. Its default
  upload cap is four Chunks per construction operation; supported uploads are
  text, Markdown, CSV and JSON.
- Provider-backed smoke runs do not establish extraction precision on a real
  industrial corpus or production-scale retrieval quality.
- Browser runtime discovery returned no available browser. No visual screenshot
  acceptance is claimed; the page is covered by executable interaction tests
  and authenticated HTTP checks.
