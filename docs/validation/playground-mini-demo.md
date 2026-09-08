# Chinese minimal Playground maintenance

## Implemented behavior

The default CLI selects `demo-mini-zh-v1`: four immutable Chinese synthetic
documents, six exact-range Chunks, 341 source characters, ten example questions
and two tenants. Five demo identities distinguish public reading, construction,
review, administration and the other tenant. The three optional/manual pump-kit
sources total 613 characters and are never ingested or extracted at startup.

The 62-file isolation inventory is committed in
`docs/playground-demo-isolation.v1.json`. Isolation is at the loader and database
boundary: source files and pinned regression corpora remain in their original
locations. The ordinary CLI no longer loads them. Industrial mode and explicit
`--corpus-profile dev-corpus-v1` retain their existing behavior.

The local shell launcher defaults to Bolt port 17693, distinct from the retained
industrial service. The old UI at port 8000 opens construction without an
automatic query; refreshes do not incur retrieval provider calls. Its top bar
exposes the existing local reset controller. Reset descriptions use actual
corpus counts instead of the former fixed 10-document/120-Chunk text.

Reset caches now bind the exact complete seed to its dataset ID. Unknown,
partial, modified or incompatible caches fail before deletion. Restoration
reuses verified vectors without provider access. The existing request exclusion,
generation fence on writes, same-origin control token, failure recovery and
browser operation-key clearing remain in force. Industrial mode still disables
whole-database reset.

## Repeatable checks

```sh
.venv/bin/python -m unittest discover -s tests/unit
.venv/bin/python -m unittest discover -s tests/e2e
.venv/bin/python -m unittest discover -s tests/security
.venv/bin/python -m unittest discover -s tests/regression
./scripts/run_stage8_neo4j_tests.sh /tmp/mini-check/integration.json /tmp/mini-check/observations
.venv/bin/python -m compileall -q src tests scripts
sh -n scripts/run_playground.sh
git diff --check
```

The new unit checks cover deterministic reconstruction, exact ranges and
checksums, small corpus bounds, separate duties, isolation inventory and
fail-closed reset-cache binding. The real-Neo4j test performs two resets,
checks unchanged seed identities/text, verifies permissions and exact citation
ranges over all ten questions, and confirms zero new embedding calls during
restoration. Existing test corpora and expected retrieval metrics are unchanged.

## Live service evidence

The final automated run passed 1006 unit, 16 HTTP E2E, 54 security,
2 regression and 166 real-Neo4j integration tests, without skips. Python
compilation, shell syntax and whitespace checks passed. The staged-file scan
covered all 19 changed files with no secret or unintended generated-file
findings. Local execution evidence is under `/tmp/demo-validation` and
`/tmp/demo-*.log`; these generated outputs are not committed.

On 2026-09-08 the independent service at `127.0.0.1:8000/playground` passed:

- Bootstrap: four documents, six Chunks, reset enabled.
- Two rounds using the same ontology version, source URI, file and operation
  key: ontology import/publication, source-only ingestion, idempotent replay,
  evidence-bound expert import and graph publication.
- A real provider query returned the uploaded source citation and governed
  evidence subgraph.
- After each round, reset removed ontology/publication state; an old-generation
  write was rejected. The next round created the same inputs without conflict.
- The final reset left the service ready for the user's first demonstration.

The initial ad hoc verification script incorrectly assumed an ingestion
`status` field, omitted the reset confirmation literal, and expected stale
reads rather than stale writes to be rejected. These were test-harness errors;
the script was corrected to the existing API contracts without altering or
weakening the service. Both complete rounds then passed.

## Scope and limitations

This is a small demo profile, not a new production quality baseline. Startup
still uses real configured embeddings and tenant retrieval warm-up. Manual
queries and extraction use the existing bounded providers. The live check
covered source-only/expert construction; model extraction and human review
remain covered by the complete automated suites and the existing pump-kit
validation, rather than a new paid extraction campaign.

Initial seed data contains source text only; governed graph navigation becomes
visible after publication. Final answer generation remains disabled as before.
There was no connected browser for visual inspection; the executable JavaScript
UI checks ran. No original dataset, industrial database, production constraint
or locked regression baseline was replaced.
