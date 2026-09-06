# Local Playground reset

## User-visible behavior

The knowledge-construction page now has one `全部重新开始` button. Its modal
requires explicit confirmation of deletion across all personas in the local
demo: ontologies, authoritative instances, uploaded documents, extraction jobs
and audit, reviewed candidates, publications and history. It restores the ten
public fixture documents and 120 Chunks. Downloaded files are unaffected.

Cancellation sends no reset request. A running reset blocks business operations;
refreshing the browser restores its status. Failure keeps the business gate
closed and offers an explicitly confirmed retry. Success clears pending browser
construction/retirement operations and returns to step 01 with the selected
persona. Other tabs must refresh before submitting old content. This change
does not add the separately discussed workflow progress overview or change the
deferred publication-selection UI.

## Boundary and concurrency

The reset controller is installed only by the local launcher, after its existing
loopback database and `PLAYGROUND_ALLOW_DISPOSABLE_DB=1` checks. The reusable
builder defaults to reset disabled, and production `create_app` has no reset
route. This is explicitly an environment maintenance operation across the demo
database, not a way to bypass a production tenant's lifecycle controls.

The control endpoint uses an independent random token plus loopback Host and
exact same-origin Origin validation. Commands are strict JSON with an explicit
confirmation, opaque UUID generation and unique operation UUID. Repeating an
accepted command returns its result without repeating deletion. Status responses
do not expose database contents, credentials or exception text. Generations are
unique across process starts as well as resets; old `/v1` write requests fail
before entering the business backend. Production JWT and scope checks remain.

The local ASGI gate assigns unique server request IDs, registers their generation
before body/worker admission, and unregisters them when HTTP ends. The backend
wrapper separately counts actual executing workers. Under the same lock, reset
requires both registries to be idle before closing admission. Thus an HTTP
timeout does not make its still-running provider/database worker invisible, and
a cancelled request queued in the executor cannot start writing after reset:
its registration is gone. Readiness is unavailable while reset is running or
failed; liveness, the UI and reset status remain reachable.

The controller and its cached vectors are process-local. Browser refresh and
network loss are covered; restarting the server process during destructive
restoration is not a durable job-resume mechanism. A partial database remains
unready rather than being presented as a successful reset. Production deletion,
audit-retention and recovery contracts are unchanged.

## Restoration

Before enabling reset, `scripts/playground_reset_store.py` reads only the locked
fixture's 120 Chunk embeddings. It validates exact source text/checksum, complete
Chunk identities and tenant metadata, full embedding-space identity, dimensions,
finite vectors and vector checksums. The immutable cache cannot embed arbitrary
uploaded text. Before deletion, it independently compares the complete cached
Chunk tuple with a fresh locked fixture; a self-consistent but truncated cache
cannot pass this check.

The exclusively held callback deletes all nodes, then reuses the normal fixture
loader with those cached vectors and checks schema, vector generations and
readiness. It makes no provider calls, creates no synthetic model-extraction
records, and leaves physical indexes installed. An error never silently reopens
the business API against a partially restored database.

## Verification

- 58 focused tests passed, including 24 new reset tests across the controller,
  browser interaction functions and fixture cache, plus existing Playground and
  data-preserving restart checks.
- Two real-Neo4j tests passed on a separate disposable database (203.498 seconds).
  Corrupt fixture vectors refused reset without deleting existing work. Two full
  resets removed 13 categories of construction/lifecycle sentinel records,
  restored exact ten-document/120-Chunk sources and vectors, retained physical
  index identities, and passed vector queries for both tenants without provider
  calls. Sentinel records exercise deletion coverage, not full authoring flows.
- Independent Chromium checks used intercepted responses for destructive UI
  scenarios: desktop and 390px narrow layouts, cancel with zero POSTs, double
  click with one POST, running-state refresh, failed reset and explicit retry,
  successful return to 01, and stale-tab blocking. No JavaScript errors or layout
  overflow were observed.
- On real port 8002, the button and confirmation modal opened correctly, and
  cancellation/Escape sent no reset POST. Live HTTP checks passed: liveness and
  readiness 200, control status 200 with its token/403 without, invalid
  confirmation 422, and stale business write 409. No live reset was executed.
- The 8002 service was reloaded with the same database. Before/after snapshots
  matched exactly for 1,253 nodes and 2,413 relationships, including their
  properties. Existing user work was preserved for the user to choose when to
  reset.
- Independent final code review confirmed that reused backend objects hold no
  additional mutable knowledge cache requiring reset: corpus, ontology,
  construction, review and publication state are read from the restored database.
  The reset gate covers the shared backend and readiness operations.

Local evidence is under `/tmp/graphrag-reset-delivery-20260906`; independent
integration output is under `/tmp/graphrag-playground-reset-validation-20260906`.
No credentials, database dumps or generated browser artifacts are committed.

One complete Stage 8 capture passed all 948 tests, with no failures, errors or
skips:

| Suite | Passed | Seconds |
| --- | ---: | ---: |
| Unit | 760 | 148.747 |
| HTTP E2E | 16 | 1.391 |
| Security | 33 | 2.041 |
| Regression | 2 | 0.021 |
| Disposable Neo4j | 137 | 912.878 |

The capture also passed corpus rebuild, contract/schema checks, compilation,
packaging, dependency lock, the locked industrial extraction-quality gate and
diff checks. The evaluation semantic digest is
`a879f0ff312069a0e126bc0328036ed590a960ca5bd387122fbb02163befbe78`;
the extraction-quality digest remains
`5f878437f1201524aee11762dd71582ca37345b77813d7efe5a04b7d9dba147c`.

Independent baseline review confirmed exactly 24 added unit test IDs and two
added real-Neo4j test IDs, retaining all 922 previous tests. All 160 case digests,
20 contract metrics, 27 diagnostics and eight identity domains remain unchanged.
The nine reset implementation/test files matched their capture-start SHA256s
through completion. Baseline 1.8.0 records only the reviewed inventory change;
its version gate was updated after the capture.

Two report replays against baseline 1.8.0 and their deterministic comparison
passed with the same semantic digest. They reuse the one complete capture and
are not two complete test runs. The final 17 baseline/metrics/dataset/regression
checks passed in 0.031 seconds after the version-gate change.

The complete capture, `baseline-review.json` and `replay-1.json`/`replay-2.json`
are under `/tmp/graphrag-playground-reset-full-validation-20260906`.
Both disposable validation containers were removed after completion.
This is `dev-mini` development evidence; performance and cost observations do
not qualify as production-candidate validation.

Reproduce the focused checks without a provider:

```sh
uv run --locked python -m unittest \
  tests.unit.test_playground_reset tests.unit.test_playground_reset_ui \
  tests.unit.test_playground_reset_store tests.unit.test_playground_resume \
  tests.unit.test_playground
```

Reproduce the complete workflow with a new output directory:

```sh
./scripts/run_stage8_validation.sh --repeat 1 \
  --output-dir /tmp/graphrag-playground-reset-validation-recheck
```
