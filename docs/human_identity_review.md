# Human identity review

The workbench separates matcher suggestions from human identity decisions.
A missing domain identifier, a same-name candidate or an uncertain matcher result
is not evidence that a new entity is forbidden. Source existence, exact evidence,
active ontology, allowed identity namespace, tenant/ACL and revision checks remain
mandatory. Ordinary changing properties are not generic identity constraints;
only properties explicitly declared by the ontology participate in the existing
identity-conflict guard. No equipment-specific rule is introduced.

## Backend contract

`GET /v1/knowledge/entity-resolution/{record_id}` adds `identity_actions` with a
versioned independent-action permission and a visible reason. Suggestions keep
backward-compatible matcher outcomes; they are recommendations, not approval.

`POST /v1/knowledge/reviews:batch` accepts `identity_action: "INDEPENDENT"` on an
unedited `ENTITY_MENTION` approval. A nonblank human reason (at most 2000 Unicode
characters) is mandatory. The server derives a new application identity from the
tenant, ontology, source record and expected revision, using the existing identity
namespace. It does not fabricate a business property, use the name as an ID or
change the ontology. Repeated stale requests conflict without creating duplicates.

An optional `identity_group` groups **only explicitly selected requests in this
batch** into one independent entity. Without that field, each request creates its
own identity. The first selected mention establishes the group; subsequent
mentions use the existing confirmed-target transaction checks. Different entity
types or known identity contradictions abort the whole batch. The original
extraction grouping does not authorize automatic merging.

The immutable review notes contain the human reason and a structured decision
snapshot: policy version, source revision, resulting entity ID and bounded visible
candidate IDs (or the selected group target revision). Source evidence, reviewer,
time, extraction provenance and source authority stay on the immutable revision.
Existing general approval without this action retains its established identity;
it does not request independent allocation. Existing linking remains a separate
explicit decision and does not approve dependent facts.

Independent decisions rebind only facts attached to the selected mention revision,
inside the same transaction. Inaccessible, stale or already-approved dependent
facts block a split rather than being silently left behind. Pending facts retain
their review status. No publication is performed. Total returned outcomes and
per-mention dependent work retain the existing review bounds. Historical and
published revisions are never rewritten.

## Validation

Backend targeted checks:

```sh
uv run --locked python -m unittest tests.unit.test_identity_review tests.unit.test_api_entity_resolution tests.unit.test_knowledge_review tests.unit.test_review_context -q
./scripts/run_stage8_neo4j_tests.sh /tmp/identity-integration.json /tmp/identity-observations test_knowledge_review_neo4j.py
```

The added real-Neo4j cases cover an entity type without identity properties,
selected-mention splitting, dependent relationship references, same-group human
confirmation, replay rejection, type mismatch rollback and source ACL rejection.
This is development maintenance evidence, not new production-candidate validation.

The suggestions response also includes the bounded dependent-fact list and an
`impact_token`. A client submitting `expected_identity_impact` pins that preview;
all selected previews are checked before any batch member changes a shared fact.
Missing identity information is reported as `IDENTITY_EVIDENCE_MISSING`, separately
from ambiguous targets and invalid boundaries. Known contradictions are checked
again for existing-identity approvals and suggested links, including legacy calls.

The integration runner's optional third argument selects focused maintenance
checks. The authoritative Stage 8 workflow continues to pass only two arguments
and therefore runs every Neo4j test; a focused receipt cannot replace its full
suite-coverage gate.

### Backend checkpoint (2026-09-09)

1063 unit checks and all 26 focused disposable-Neo4j review/publication checks
passed. The HTTP E2E (18) and security (57) suites also passed. Python compilation,
shell syntax, packaging, whitespace and staged-file secret checks passed.
Receipts are `/tmp/identity-backend-complete-unit.json`,
`/tmp/identity-backend-pass.json`, `/tmp/identity-backend-e2e.json` and
`/tmp/identity-backend-security.json`.

Initial runs exposed two test reads using the default published-only status and
an actual legacy batch-response bug that omitted rebound dependent records.
The test reads now explicitly select candidates. The service returns the final
version of each affected record, including dependencies. Both the complete unit
suite and the focused database suite were rerun successfully after the fix.

## Workbench behavior

The four per-mention entries are independent confirmation, existing-entity
selection, rejection and deferral. Existing-entity selection opens the matching
panel without writing. Independent availability uses the server's action policy,
not the presence of similar candidates. Loading, read errors and disallowed
identity formats have visible explanations alongside the buttons.

The original extraction card is labelled a pending group. Ordinary selected-mention
confirmation creates separate identities; an explicitly labelled second action
creates one shared independent identity from the selected mentions. A preview
lists each selected source excerpt (bounded to 180 Unicode characters), the
allocation mode and every affected property/relationship record, deduplicated by
record ID. The user provides a reason and confirms the preview before submission.
The full original evidence remains available in the source viewer and unchanged
in storage. Different declared identity values and mixed types cannot be grouped;
missing values alone do not prohibit human grouping.

Cancellation, an empty/oversized reason, outdated review context or invalid group
selection sends no write. Successful review refreshes suggestions and publication
candidates; dependent facts remain unapproved. Native confirmation dialogs follow
the existing workbench's manual-link interaction convention.

### Workbench checkpoint (2026-09-09)

All 108 Playground tests, eight related context/publication-group tests and 18
HTTP E2E tests passed. These include six new executable-page scenarios for missing
identifiers, similar candidates, action policy, separate versus shared allocation,
preview/reason submission, cancellation, contradictory groups and success refresh.
The old stale-response test keeps its same assertions and now expects the revised
uncertainty label instead of calling every matcher conflict an identity mismatch.
JavaScript/asset checks, Python compilation and whitespace checks passed.
Receipts: `/tmp/identity-ui-final.log`, `/tmp/identity-ui-final-related.log`, and
`/tmp/identity-ui-e2e.log`. No connected browser was available to the CUA tool;
these checks are not a browser visual inspection.
