# Construction preparation failure recovery

## Observed problem

A business upload returned `dependency_unavailable` after an embedding provider
connection error. Its `PREPARE_UPSERT` ingestion job had exhausted its attempt
budget and stored `FAILED_PERMANENT`, while the parent construction job still
showed `RUNNING`, zero completed Chunks and no extraction outcomes. Refreshing
queried this stale parent state. The browser also retained the failed operation
key, so retrying could encounter the exhausted preparation budget.

## Implemented behavior

- Construction jobs support the additive `FAILED` status. Listing and detail
  reads project preparation failures through the tenant/operation composite
  index and verify document, version, snapshot, source generation and expected
  active snapshot. Existing tenant/group checks remain in place. These reads do
  not write; legacy interrupted parents become understandable immediately.
- A failed pipeline call synchronizes the durable preparation failure under the
  construction job lock. Completed parents cannot be downgraded. If the audit
  database is unavailable, the original exception survives, and later reads can
  reconcile the durable preparation state.
- An exhausted preparation produces HTTP 503 with the non-retryable code
  `construction_ingestion_failed`. Only allowlisted reason codes are returned;
  provider exception text and credentials are not exposed.
- A confirmed terminal error releases the browser operation key. The next
  explicit upload creates a new operation, preserving failed-job history.
  Unknown outcomes retain their key. Neither refresh nor failure handling issues
  an automatic upload, extraction or publication.
- Job cards and details show the phase and failure reason. The walkthrough
  distinguishes waiting, recoverable interruption, terminal failure and success.

The pipeline's own retry budgets, artifact reuse, source evidence, ontology
validation, identity resolution and publication rules remain in force. This fix
addresses failed source preparation; it does not claim to detect every possible
worker/process crash or guarantee external provider availability.

## Local service recovery during an external outage

The existing database can be reopened with `--reuse-existing-corpus
--skip-provider-warmup`. This explicit local-launcher option still verifies the
schema, both tenant vector spaces, physical indexes and readiness. It skips only
the external query warm-up; it never substitutes embeddings or disables runtime
provider calls. It cannot be combined with `--check` or initial corpus loading,
and default startup continues to run warm-up.

Use this option only with the existing loopback database configuration and the
normal disposable-database opt-in. It is useful for inspecting status while the
provider is unavailable, and is not provider-connectivity or retrieval-quality
validation. No production API startup behavior changes.

## Validation

Focused unit/API/browser-logic/launcher checks passed (91 tests). Browser QA
intercepted all construction writes and verified terminal-failure key rotation,
unknown-outcome key reuse, accurate failure cards, no automatic upload on refresh
and no JavaScript errors. The six disposable-Neo4j construction tests passed,
including failed preparation, legacy read projection without writes, tenant/group
isolation, exhausted-key rejection and successful explicit retry with a new key.

The original 8002 service was reopened against the same database with explicit
provider warm-up skipping. Only the stale job status and allowlisted failure
codes were synchronized before restart. Complete property digests for all 1099
nodes and 2111 relationships matched before/after restart. Authenticated live
job-detail and `?status=FAILED` queries returned the expected failed job. Browser
QA was repeated against the served page with construction writes intercepted.

A bounded live embedding probe failed during connection establishment, and an
unauthenticated HTTPS probe subsequently timed out during TLS connection. No
provider success or successful real business upload is claimed. No proxy, DNS,
credentials, provider endpoint or model settings were changed. The external
connection must recover before uploads can complete.

Local browser and recovery evidence is in `/tmp/graphrag-ingestion-ui-qa`;
focused Neo4j results are in `/tmp/graphrag-ingestion-focused-neo4j.json`.


One complete Stage 8 `dev-mini` capture passed 952 tests without failures, errors
or skips: 763 unit, 16 HTTP E2E, 33 security, two regression and 138 disposable
Neo4j tests. The Neo4j suite took 962.356 seconds. Corpus/gold rebuild checks,
acceptance contracts, the locked extraction-quality gate, dependency lock,
compilation, shell syntax, packaging and diff checks passed.

Baseline review retained all 948 previous test IDs and admitted exactly three
new unit tests and one new Neo4j recovery test. Case digests, contract metrics,
diagnostics and identity domains are unchanged. All captured implementation
and test source hashes stayed unchanged through the capture. Baseline 1.9.0
records this reviewed inventory change; no quality threshold was relaxed.
The evaluation semantic digest is
`8c03cd166a6fc218c4bcd10c19bf4d6ea59152e8302bea72d4be2941f53af094`.

Two report replays against baseline 1.9.0 and their deterministic comparison
passed with that digest. These reuse one complete capture, not two complete test
runs. The final 17 baseline/metrics/dataset/regression checks passed after the
version-gate update. Both disposable validation containers were removed.

The full capture and baseline review are under
`/tmp/graphrag-ingestion-failure-validation-20260906`. This is development-scale
regression evidence, not a new production-candidate performance validation.

Reproduce the full capture with:

```sh
./scripts/run_stage8_validation.sh --repeat 1 \
  --output-dir /tmp/graphrag-construction-failure-check
```
