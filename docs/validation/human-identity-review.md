# Generic human identity review validation

## Scope and decisions

The reported same-name equipment case exposed an asymmetric workflow: missing
identity properties disabled independent confirmation while manual linking was
still offered. The fix applies to any entity type; it does not require an
equipment code or replace the ontology's declared identity rules.

The implementation and reproducible focused commands are described in
[`human_identity_review.md`](../human_identity_review.md). The backend checkpoint
is `6a18fbc`; the page checkpoint is `cd1d5ff`, followed by the explicit E2E fixture
and evidence correction in `ffd5ef8`. Published history was retained.

Human decisions are independent allocation, existing-entity linking, deferral and
rejection. Matching uncertainty is not a prohibition on independent allocation.
A source/revision-derived application ID keeps new allocations separate from the
extraction group. Explicit batch groups apply only to selected source mentions.
Known declared-identity contradictions prevent linking/grouping; ordinary mutable
properties do not become implicit identity constraints. Source evidence, source
authority and immutable revision history remain unchanged.

Review notes retain the human reason and a server-computed decision snapshot.
Previews enumerate selected mentions and affected facts; a dependency token detects
changes before a batch mutates shared facts. Rebound facts remain pending review.
The API rejects injected IDs/audit fields and incompatible combinations of action,
group, preview, record kind and decision. Existing approval/link routes also check
known contradictions. Neither approval nor linking publishes knowledge.

## Regression procedure

A full capture and comparison with the old baseline precede any baseline update:

```sh
./scripts/run_stage8_validation.sh --repeat 1 \
  --baseline-candidate /tmp/identity-evaluation-candidate.json \
  --output-dir /tmp/identity-final-capture
```

The comparison must preserve all previous test IDs, exact case digests, contract
metrics and quality diagnostics. Identity/version pins must stay identical except
for the explicitly expanded security manifest. The manifest must retain its old
required tests and add the new identity-security cases. Only after checking these
conditions may the reviewed baseline and its version constant advance together.

The final verification runs the complete suite against that reviewed baseline:

```sh
./scripts/run_stage8_validation.sh --repeat 1 \
  --output-dir /tmp/identity-final-verification
uv run --locked python scripts/compare_evaluation_reports.py \
  /tmp/identity-final-capture/run-1/report.json \
  /tmp/identity-final-verification/run-1/report.json
```

From the final checkout, the standard `--repeat 2` workflow reproduces the same
validation using the committed baseline. Both complete runs use separately owned,
loopback-only disposable Neo4j containers with 1 CPU and 1536 MiB memory. The
retained Playground database is never used for destructive test fixtures.

## Baseline comparison

The complete candidate capture passed with 1069 unit, 18 HTTP E2E, 59 security,
2 regression and 186 disposable-Neo4j integration tests (1334 total, no skips).
The locked knowledge-quality gate passed. The reviewed baseline advances from
1.15.0 to 1.16.0; its exact comparison is retained in
[`human-identity-baseline-comparison.json`](human-identity-baseline-comparison.json).
All old test IDs remain present. Evaluation case digests, contract metrics and
quality diagnostics are exactly unchanged. The only identity pin change is the
security manifest hash, which adds two required cases and retains all 57 previous
cases. The unit-count change also includes the previously committed publication
rollback test; no quality threshold or fixture prediction was changed.

The candidate report and baseline share semantic digest
`c672c7b9a76e36e190531db5bb2552cdaacfa97757e3b732c106bf8c50815404`.
The original baseline is available from commit `ffd5ef8`; comparison uses the
canonical deterministic projection rather than elapsed-time observations or
unrelated report metadata.

## Final verification results

The final verification against baseline 1.16.0 passed the same 1334 tests with no
skips. Both complete reports passed their evaluation gates and reproduced the
semantic digest above; `compare_evaluation_reports.py` exited successfully.
The locked knowledge-quality gate also reproduced digest
`5f878437f1201524aee11762dd71582ca37345b77813d7efe5a04b7d9dba147c`.

The standard workflow additionally passed deterministic gold/corpus rebuild
checks (120 chunks), acceptance/profile validation, lockfile validation, source
and wheel packaging, Python compilation, shell syntax and `git diff --check`.
The separate static-assets check passed all 13 inline scripts and first-party
JavaScript modules. Owned test containers were removed after each run. The final
staged-file secret and generated-file scan passed for all eight evidence-checkpoint
files, and the staged whitespace check passed.

Machine-readable local receipts are retained under
`/tmp/identity-final-capture/run-1/` and
`/tmp/identity-final-verification/run-1/`; the committed baseline comparison above
retains the exact changed test IDs, counts and unchanged-quality assertions.
The temporary paths are run artifacts, not prerequisites for reproducing the
standard validation workflow from the final checkout.

## Retained-runtime verification

A temporary local service using the new implementation reused the existing corpus
without fixture loading or provider warm-up. Read-only matching of the user's three
pending mentions allowed independent allocation for all three, including the two
whose matcher reason was `IDENTITY_EVIDENCE_MISSING`. The remaining mention had
`NO_MATCH` and two dependent property facts in its preview.

Full bounded graph fingerprints before and after the temporary-service checks
matched: 165 nodes and 310 relationships. Fingerprints include node and
relationship properties and topology; ephemeral Neo4j IDs are used only inside
this local comparison, never as business IDs or citations. Only digests/counts are
retained in validation evidence. The five pending review records and their
revisions were not approved, rejected, regrouped or published. Both existing
construction jobs were complete before the service update. The original 8000
process was gracefully restarted with the existing database, reuse-corpus and
skip-warm-up options. The updated 8000 page and matching API passed the same
three-mention checks; all five pending record/revision tuples and the full graph
fingerprint still matched. The temporary 8001 probe was then stopped.

## Limitations

This is `dev-mini` maintenance validation and does not constitute a new Stage 9
production-candidate load run. Routine regression uses the existing deterministic
fixtures and locked extraction-quality gate; no new provider-quality claim is
made. Human identity decisions remain auditable judgments, not an automatic proof
of real-world identity.

CUA discovery returned no connected browsers or apps, so browser visual inspection
was unavailable. The actual page JavaScript, HTTP contracts, static assets and
live matching API were checked instead. Native confirmation dialogs follow the
existing workbench convention; the original evidence viewer retains full text
while the confirmation preview bounds each excerpt to 180 Unicode characters.
