# Step 03 expert-draft prefill after a direct file upload

## Observed cause

After a local reset, the user published `pump-maintenance-demo` and directly
selected `authoritative_source.txt` in step 02. The browser assigned the normal
upload URI `urn:local:controlled-upload:authoritative_source.txt`. Three completed
source-only jobs referenced the same document/version and one Chunk; read-only
verification confirmed its text and SHA256 exactly matched the committed demo.

The prefill function unnecessarily required the demo kit's preset URI in
addition to the exact content hash and ontology key. It silently returned false
for the normal upload URI. The caller ignored that result, still offered the
next step, and left the empty editor's earlier identity-change message visible.
The reset and source ingestion had succeeded; the defect was in the browser's
source-to-expert-draft transition.

## Change

Exact source bytes and the demo ontology key determine eligibility for the
committed expert template. The URI, title and filename do not determine whether
the same source text supports those example facts. The template still receives
document, version, Chunk and ontology IDs from this upload's validated response;
single-Chunk source-only status, complete IDs and absence of extracted candidates
remain required. No model extraction, import or publication is triggered by
prefill.

Step 02 now distinguishes source ingestion from successful preparation of the
expert draft. A content or ontology mismatch has an explicit explanation in
step 03. An empty editor shows preparation instructions and a button returning
to step 02; import remains disabled until content is present. The initial
placeholder-ID JSON has been replaced with an honest empty state. Identity
changes still clear previous identity-bound drafts, and changes to an imported
draft still invalidate its publication receipt.

The user's existing source and ontology need no reset. After refreshing the
updated page, resubmitting the same file with the same URI and published ontology
prepares the draft through the normal idempotent ingestion path. Reviewing,
importing and publishing remain explicit user actions.

## Validation

Existing executable UI cases were extended for the normal upload URI, real
response-ID binding, seven generated records, stale identity-message replacement,
content/ontology mismatch explanations and disabled empty imports. Existing
cross-identity, partial-response, manual-draft and publication-receipt protections
remain covered. Test IDs and baseline inventory are unchanged.

The final source passed 811 automated checks, with no failures or errors:

| Suite | Passed | Seconds |
| --- | ---: | ---: |
| Unit | 760 | 144.329 |
| HTTP E2E | 16 | 1.317 |
| Security | 33 | 2.593 |
| Regression | 2 | 0.029 |

Compilation and diff checks passed. The unchanged 137 real-Neo4j tests were not
rerun for this browser-only change; the previous complete capture is recorded in
[the reset report](playground-reset.md). Logs use the prefix
`/tmp/graphrag-step03-`, including `unit-final.log`, `e2e.log`, `security.log` and
`regression.log`. Reproduce the automated checks with `uv run --locked python
-m unittest discover -s tests/unit` and the corresponding `tests/e2e`,
`tests/security` and `tests/regression` directories.

Standalone Chromium checked both the patched page and the reloaded real 8002
page: direct selection without the example-prefill button populated three
mentions and four assertions; modified source bytes left an empty, disabled
import with an explanation and return-to-02 control. Desktop and narrow layouts
were inspected with no JavaScript errors. The browser used real authenticated
read endpoints and the existing source job's response, but intercepted construction
POSTs and blocked all other business writes. This validates the browser flow
without claiming a new live ingestion/import/publication run or changing the
user's data.

The same 8002 service/database was reloaded with `--reuse-existing-corpus`.
Before/after snapshots matched for all 1,063 nodes and 2,035 relationships,
including properties. No reset, new upload, expert import or publication was
performed by this verification. Browser artifacts are in
`/tmp/graphrag-step03-qa`; database snapshot summaries are
`step03-before.json`/`step03-after.json` under
`/tmp/graphrag-reset-delivery-20260906`.

This is a browser-only correction. Production contracts, evidence validation,
entity resolution, ingestion algorithms and the evaluation baseline are
unchanged. It is development evidence, not a new production-candidate run.
