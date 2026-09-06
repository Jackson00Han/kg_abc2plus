# Guided construction and fact review

## Problem and resulting workflow

The earlier Playground mixed entity mentions and assertions in one queue. Four
facts about the same pump looked like four pumps, a failed approval did not tell
the reviewer which entity to handle first, and correct duplicate facts required
an unexplained rejection. The numbered page also required uploading at 03,
returning to 02 for expert import, then jumping to 05 for publication.

The construction page now has two consecutive workflows:

1. Establish the expert baseline: 01 publish ontology, 02 upload authoritative
   source, 03 import and explicitly publish the expert instances in the same area.
2. Extend the knowledge base: 04 upload business documents, 05 confirm entity
   identities and review facts, 06 explicitly publish the reviewed business records.

Inventory, quality, history, and document retirement are named maintenance views.
The upload form follows the selected workflow and preserves its existing service
limits. Steps 02 and 04 have a local example-prefill button, so the walkthrough
does not return to the materials panel between those steps. Upload preflight explains an unpublished ontology and links to 01.
Expert publication uses only the exact successful import receipt; editing the
source draft invalidates that receipt. A failed import cannot leave an earlier
batch looking ready to publish. Expert updates that cannot establish all required
replacements within the bounded candidate window are stopped with an explanation.

Source ingestion and graph extraction have separate outcomes. A rejected or
empty extraction reports that the source was ingested but no review records were
created, without offering the review handoff. Partial success identifies rejected
chunks alongside the available candidates. An empty review queue does not claim
completion or offer publication unless the session has approved records.

## Review behavior and boundaries

- Identity confirmation precedes fact review. Records are grouped by stable
  entity ID, never by name alone; each mention retains its own evidence and
  revision. Uncertain mentions can be deferred to a separate section.
- Facts appear as rows under their subject, with readable properties, units,
  source excerpts, checks, and actions. Unconfirmed endpoints show a direct link
  to the relevant entity. JSON and immutable history are expandable technical
  details. On narrow screens, source and actions stack vertically.
- Read-only checks compare the candidate against accessible, active authoritative
  facts under the same ontology. Literal comparison includes normalized datatype,
  value, unit, validity interval, and observation time. Relationship comparison
  includes both endpoints and normalized relationship properties.
- Exact duplicates offer “保留已有事实，不重复入图”. The explicit operation creates
  an immutable rejected revision with a `KEEP_EXISTING_AUTHORITATIVE_FACT` audit
  reason and the compared authoritative revision ID. It preserves the original
  extraction and source; it does not label that source false or publish another
  copy of the fact.
- The duplicate decision is revalidated inside the existing tenant write lock and
  revision CAS. If the authoritative publication or recommendation has changed,
  the operation fails without applying a stale decision.
- JSON corrections are saved as a new deferred revision, then checked again.
  A prior READY or identity result cannot approve edited content. Linking and
  duplicate handling are blocked while the relevant draft is unsaved. A deferred
  record can be corrected again without approving it; a repeated defer without
  an edit retains the original transition rejection.
- Differences require review and can be deferred. No authoritative value is
  overwritten automatically. Automatic suggestions never link, approve, or publish
  knowledge. Model records remain SECONDARY / LLM_EXTRACTED.
- The review queue and authority comparison each retain a 100-record bound. A
  queue full of deferred records offers a candidate-only load. This is not full
  pagination or a guarantee that the whole corpus has been reviewed.
- Cross-mention identity propagation and comparison between model candidates
  remain outside this change. A repeated name does not borrow another mention's
  identity evidence. Ordinary mention defer/edit also advances a revision without
  migrating its existing identity-property dependencies; a subsequent match may
  remain blocked. This differs from cross-mention propagation and from explicit
  resolution linking, which already rebinds dependent facts. Temporal differences are flagged conservatively, without
  deciding whether a historical or multivalue update should coexist.

The new endpoint is
`GET /v1/knowledge/review-assessments/{record_id}?expected_revision=N`.
It requires `knowledge:review`, validates the candidate and endpoint provenance,
filters source and authority access, and returns bounded dependencies and matches.
Its entire read transaction has a 25-second limit; duplicate review batches have
the same overall transaction bound. A cold Neo4j test exposed an expensive query
planning join; explicit `WITH DISTINCT` planning boundaries fixed it while keeping
all provenance and access predicates before the limit.

## Verification

Development evidence uses the existing `dev-mini` resource limits and does not
qualify as a new production-candidate load validation.

One complete Stage 8 capture passed all 910 tests, with no failures, errors or
skips:

| Suite | Passed | Seconds |
| --- | ---: | ---: |
| Unit | 724 | 171.858 |
| HTTP E2E | 16 | 2.125 |
| Security | 33 | 3.519 |
| Regression | 2 | 0.036 |
| Disposable Neo4j | 135 | 1493.917 |

The capture also passed corpus rebuild, contract/schema checks, compilation,
packaging, dependency lock, industrial extraction-quality gate and diff checks.
The baseline is now 1.6.0: 20 new passed test IDs were added; all previous 890
remain. Independent projection review confirmed that the 160 case digests,
20 contract metrics, 27 diagnostics and eight identity domains were unchanged.
The evaluation semantic digest is
`cc21d03b89fa228fd2d17b49e10ed6469b9e10a2a4e7d1088da25d562aa2a7f4`;
the extraction-quality digest is
`5f878437f1201524aee11762dd71582ca37345b77813d7efe5a04b7d9dba147c`.
All 21 baseline-related focused checks also passed.

The full capture preceded the final deferred-edit correction and final UI
changes. Those changes were checked separately below; the original suite
artifacts were not rewritten or stitched together. Two identical successful
evaluation replays used that same capture, rather than two complete test runs.

Reproduce the complete workflow with:

```sh
./scripts/run_stage8_validation.sh --repeat 1 \
  --output-dir /tmp/graphrag-guided-review-validation
```

Final scoped checks:

- Final backend focused tests: 100 unit/API and all 33 security tests passed.
- The complete knowledge-review Neo4j module passed all 13 cases on a separate
  cold disposable database after the final correction changes (422.375 seconds).
  One earlier container readiness attempt failed before any test ran; a new
  container started with a 180-second readiness wait. Business transaction limits
  stayed at 25 seconds. The earlier two-case query-planning regression also
  passed on a cold database (179.730 seconds).
- Final frontend/review HTTP focused run: 68 passed (4.346 seconds), including
  receipt invalidation, busy-state, mobile layout, zero-result upload, and empty
  review queue transitions. Existing test IDs were extended without changing the
  baseline inventory.
- Headless Chromium mock UI: 65 interaction checks passed at 1440px and 390px;
  screenshots inspected. Nine write requests were intercepted; no knowledge writes
  occurred in this UI isolation check. The final run used the public corpus
  fixture bootstrap and intercepted all network calls. Coverage includes grouping, dependent
  approvals, duplicate/conflict comparison, editing during automatic refresh,
  stale/busy controls, visible mobile actions, rejected or empty extraction,
  recovery of the next-step controls and empty-queue publication guidance.

The final focused frontend command is:

```sh
uv run --locked python -m unittest \
  tests.unit.test_playground tests.unit.test_playground_review_flow \
  tests.unit.test_playground_resolution tests.unit.test_playground_flow \
  tests.unit.test_playground_demo_ui tests.e2e.test_knowledge_api
```

## Real-provider browser check and delivery

The existing HTTP port 8002 was used; no temporary HTTP entry was created.
Browser QA used Tenant Beta / Board, leaving the user's Alpha governance data
untouched during testing. The initial real-provider pass completed 01 through 04
with three mentions and four assertions. After restarting with the final backend,
01 through 03 completed again: one authoritative source Chunk, seven imported
expert records and an active expert publication.

That final-backend run of the original maintenance report returned an ingested
source but rejected extraction with zero candidates. Attempt 1 reported
`INVALID_JSON`; attempt 2 reported `ENDPOINT_OUTSIDE_EVIDENCE`,
`FACT_TOKEN_OUTSIDE_EVIDENCE` and `INVALID_TEMPORAL_QUALIFIER`. The UI had still
offered review and called an empty queue complete. This observed failure prompted
the final empty-result UI fix and regression checks above. The failure was not
removed or replaced by a successful observation.

A separate small control document with no time qualifiers was then uploaded once
under its own URI. It passed live extraction and the actual browser/API flow
through 05 and 06: two existing-entity links, one new risk approval, three explicit
duplicate dismissals, one new relationship approval and a successful publication.
The original failed job remained readable afterward. The final HTML was served
through a browser route override for this check; all session and knowledge API
calls used the real running backend. Model records retained SECONDARY /
LLM_EXTRACTED, including after publication.

This control is workflow evidence, not a precision evaluation: the model also
inferred a mechanical-seal mention and a CONTAINS relationship from a risk phrase.
Exact duplicate classification proves equivalence to an existing authoritative
fact, not that the new source independently entails it. Human source review is
still required. The original demo failure and this additional inference remain
limitations; neither is counted as an extraction-quality pass or hidden by fixture
insertion. The separate locked extraction-quality gate is reported above.

Local evidence is retained under
`/tmp/graphrag-guided-construction-qa-20260906/{mock,live,live-final,live-control}`;
the complete suite capture and replay outputs are under
`/tmp/graphrag-review-flow-validation-final-20260906`. These temporary artifacts
are excluded from the commit.

Final restart completed on 2026-09-06 using the existing launcher and port 8002.
Readiness returned 200 with Neo4j, embedding generations and vector indexes all
`ok`; the 10-document / 120-Chunk baseline was reloaded. The served HTML digest
matched both the working file and the final browser-tested HTML:
`beacf2ac4219b5a78a374db3832f76b020ef51bfa6cf528cef5288fb7d132bbf`.
Alpha Finance + Legal returned zero ontologies, review records and publications,
ready for the walkthrough from 01. Port 8003 remained closed. Only this task's
disposable service database was replaced; unrelated older database containers
were left intact. The final read-only verification is in the local
`delivery.json` artifact.

Manual instructions are in [the revised walkthrough](../industrial_demo_walkthrough.md).
The deferred redesign item was removed from `to_do_list.md`; the independent
cross-mention identity propagation item remains.
