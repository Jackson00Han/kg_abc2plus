# Knowledge browsing and maintenance redesign

Owner-approved execution plan, 2026-09-09. This additive maintenance sequence
preserves completed production Stages 1–9 and does not renew production validation.
Development uses the unchanged dev-mini workload and deterministic providers.

## Product contract

Four flows: 建立专家基准, 扩充业务知识, 知识浏览, 质量与维护.
Browsing contains 实体档案 / 关系图谱 / 来源资料; maintenance contains
质量检查 / 知识与资料维护 / 发布与版本. Publication actions retain the existing
review and immutable publication boundary. IDs and hashes belong in technical
details. Authority, origin and review state are separate concepts.

Default knowledge scope is the current authorized published view, never the
candidate queue or a retrieval result misrepresented as the entire graph.
Sources may be ingested without contributing published facts. Historical
observations never represent current state. No global counts are inferred from
bounded partial pages. Entity identity, not display name, controls grouping.
Original values, units, temporal context and exact source positions are retained.

## Existing capabilities and gaps

- `graph:query` and `graph:evidence`: authorized, version-pinned bounded browsing
  and exact evidence; reuse these contracts and source ACL checks.
- Graph reads cap records at 500, nodes at 150 and assertions per page at 200.
  Exceeding a budget fails explicitly; do not claim unbounded corpus support.
- `publication-inventory`: complete-publication-access, quality-gated metadata;
  retain for governed removals, not the sole reader for broken graphs.
- `review-evidence`: existing reviewed-record source context; reuse for sources
  and edits where the caller has the required review permission.
- Active document lifecycle and immutable publication history already exist.
- Industrial graph renderer/model modules exist and can be reused independently
  of the industrial upload workflow.
- Published quality degree currently counts SUBJECT endpoints of literal facts.
  Therefore ISOLATED_ENTITY means no assertion participation, despite its English
  text claiming no relationship. Stage B5 must make rule and explanation agree
  with regression evidence; historical reports must retain their old semantics.
- Human quality dispositions require durable, version-bound records; local UI
  state is not a completed audit trail.

## Ordered implementation and exit checks

| Step | Deliverable | Exit checks | Status |
| --- | --- | --- | --- |
| B1 | Scope, terms, capability mapping and execution record | 1,037 unit + 18 HTTP E2E + 54 security; compile/diff/secret checks | Complete |
| B2 | Four flows, entity dossiers and exact evidence | Executed JS, API/security contracts, existing suites | Complete |
| B3 | Linked graph with separate model/loading/layout/rendering | 36 UI/module + 18 HTTP E2E + 54 security; syntax checks | Complete |
| B4 | Source catalogue and bidirectional knowledge navigation | 1,046 unit + 18 HTTP + 55 security + 2 Neo4j; exact text checks | Complete |
| B5 | Readable quality checks and durable human dispositions | 1,050 unit + 18 HTTP + 56 security + real-Neo4j decision lifecycle | Complete |
| B6 | Contextual corrections, removal impact, publication comparison | 1,054 unit + 18 HTTP + 57 security + 2 Neo4j lifecycle checks | Complete |
| B7 | Unified states, visual QA and complete regression | Four business journeys, complete dev-mini checks | Pending |

Each completed step has its own checked commit pushed to origin/main. Validation
results are appended here, with failures and limitations recorded honestly.

## Graph extension boundary

Keep canonical entity and immutable revision IDs, provenance references and read
pins in the model. Separate source fetching, selection/expansion state, layout,
visual theme and renderer. Reuse the current renderer first. Future hierarchical,
plant/process, fault-path and customer-presentation layouts must not invent
business edges or causal/temporal semantics. Node and edge selection opens the
same dossier/evidence components as the table. Graph browsing remains useful
when no relationships have been published.

## Business acceptance journeys

1. Publish -> locate changes -> open entity -> verify exact original evidence.
2. Search equipment code -> locate graph node -> expand -> inspect relation evidence.
3. Check quality -> inspect object/evidence -> record disposition or correct ->
   review/publish -> recheck the new version.
4. Inspect source -> preview withdrawal dependencies; inspect history -> compare
   target publication -> guarded rollback -> verify the resulting scope.

No unmeasured health score, invented relationship, automatic trust promotion,
client-side-only authorization or silent truncation is acceptable.

## B1 validation

2026-09-09: baseline passed 1,037 unit, 18 HTTP E2E and 54 security tests.
Source compilation and whitespace checks passed. The runtime inspection also
found that general graph APIs were wired only in industrial mode; B2 will wire
the existing browser into the ordinary workbench, with explicit read scopes.
Browser discovery returned no enabled surfaces; visual verification remains
required at B7 and will not be claimed from HTTP tests alone.

## B2 implementation and validation

The ordinary Playground now wires the existing graph browser. Local read personas
receive `knowledge:graph:read` alongside retrieval read; construction, review,
publication and lifecycle duties remain separate and every graph query retains
its source ACL boundary. The read-role expectation was updated explicitly, not
removed. Browser modules are served through an exact static-asset allowlist.

Entity dossiers collect all version-pinned pages within the existing server read
budget before grouping/paging canonical entities. They preserve parallel facts,
multiple mentions, aliases, original units and temporal qualifiers. Evidence is
fetched on demand, Unicode character offsets are checked before highlighting,
and rendered text is escaped. Identity changes cancel and clear prior views.
The existing 500-record server bound remains; this is not unbounded browsing.

A complete unit capture passed 1,041 tests; the subsequently added asset allowlist
check and final unit-display correction passed the five-test browser module.
All 18 HTTP E2E and 54 security checks passed. JavaScript syntax, source compilation
and whitespace checks passed. All three real-Neo4j graph browsing tests passed in 249.483 seconds under the
unchanged dev-mini cap; the owned container was removed. The final 102-test
Playground subset also passed. Source and graph tabs are staged integration
slots until B3/B4, not claimed completed features.

## B3 implementation and validation

The published graph tab reuses the vendored Cytoscape/Dagre renderer. It supports
entity/code lookup from the complete authorized directory, direction/predicate
filters, one/two-hop server expansion, bounded next pages, zoom/fit/reset, node
selection into the shared dossier and relation evidence. A keyboard-operable
content list provides the same node/evidence actions. Empty graphs and partial
pages are explicitly labelled. The retrieval-result subgraph remains separate.

`GraphSession` owns pinned loading/cancellation without a rendering dependency.
The existing renderer accepts optional layout options and element transformation,
retaining its defaults for the industrial workbench. This is the extension seam
for future industrial layouts and visual themes, not a second graph store.

Validation passed 36 executed industrial/browser/flow checks, the 29-test focused
browser/graph regression, 18 HTTP E2E and 54 security checks. JavaScript and inline
script syntax and whitespace passed. No graph query authorization or expansion
limits changed; B2's three Neo4j checks cover the reused backend. Live visual QA
remains unavailable and is not inferred from these checks.

## B4 implementation and validation

`POST /v1/knowledge/sources:query` provides keyset-paged active source metadata;
`POST /v1/knowledge/sources:read` reads one exact Chunk by document, expected
version and ordinal. Both require retrieval read, not lifecycle write privileges.
They reuse the active-source ownership/complete ACL predicates, retain the human
publication gate, validate text location/checksum and recheck authorization before
return. Lists expose only whether visible published records exist, not hidden
record counts. The source view links both ways to dossiers and exact evidence.

Passed: 1,046 unit tests, 48 final focused checks, 18 HTTP E2E, 55 security and two
owned Neo4j checks (82.207 seconds). New HTTP success coverage caught whitespace
stripping inherited from the generic DTO; the source field now explicitly
preserves whitespace, and the complete relevant focused/security/E2E set was
rerun successfully. The new security case's discovery placement was corrected
and its exact test ID was executed before the complete 55-test run. JavaScript,
compilation and diff checks passed. No original source data was modified.

## B5 implementation and validation

Quality cards now explain the checks in Chinese, show business object names when
available from the same authorized publication, and put technical identifiers
behind details. Historical names resolve through the recorded publication's
immutable revisions after the existing complete audit authorization check.
ISOLATED_ENTITY retains its established property-or-relationship degree rule;
the current English detail and Chinese explanation now match that rule. No score
or graph-quality threshold changed, and historical stored reports are unchanged.

Human decisions are independent immutable `QualityReviewDecision` events with
unique tenant/actor/operation identity and a checked payload checksum. Writes
require both review and quality scopes, preserve original observer/time on exact
replay, reject changed replay bodies, and lock/revalidate publication and corpus
state. They cannot overwrite automatic reports or propagate to another version.
The bounded history displays the most recent 100 decisions and explicitly warns
when earlier entries are not displayed. Migration 013 adds uniqueness and lookup
indexes without modifying existing knowledge.

Passed: 1,050 unit tests, 44 final focused checks, 18 HTTP E2E, 56 security and the
real-Neo4j decision lifecycle (109.129 seconds). The database check covers durable
readback, exact replay, unchanged report hash, denied ACL, stale corpus rejection
and corrupted payload rejection. Schema expectations include migration 013.

`scripts/check_playground_assets.py` parses and checks all 12 inline scripts and
first-party modules. It corrects the earlier ad-hoc inline extraction, which
spanned multiple script tags and was not a valid complete inline-script check;
B2/B3 modules and the final full inline scripts now pass the parser-based check.
Source compilation and diff/secret checks passed. Browser visual QA remains open.

## B6 implementation and validation

Maintenance uses Chinese fact summaries, contextual record correction, revision
history, staged removal confirmation and the existing complete publication
preview. Source maintenance displays withdrawal dependencies in business terms.
Publication history offers a read-only, fully authorized comparison before the
existing expected-active-publication guarded rollback. Comparison retains stable
record identity, original units/time and revision changes, bounds each manifest
to 500 records and rechecks the complete source scope before returning metadata.
Identity changes and changed selections invalidate pending confirmation dialogs.

The existing review lifecycle advances a published record head when correction
starts. Consequently current quality checks can report HEAD_CURRENT_REVISION_INVALID
until the corrected record is reviewed and explicitly replaced in a new publication.
This limitation is explained before correction; the implementation does not weaken
quality rules to hide it. A real database test verifies the finding and successful
return to a passing graph after republication.

Passed: 1,054 unit tests (203.808 seconds), 49 final focused checks, 18 HTTP E2E,
57 security and two real-Neo4j lifecycle/comparison tests (121.390 seconds).
Earlier fixtures attempted to remove a schema-required relationship or republish
without explicit replacement; the unchanged service correctly rejected both.
The corrected fixtures exercise valid publication operations. UI wording
assertions were updated to the authorized Chinese redesign; behavioral guards
remain tested. JavaScript, compilation and diff/secret checks passed. Chrome and
in-app browser are both unavailable, so visual QA remains unverified.
