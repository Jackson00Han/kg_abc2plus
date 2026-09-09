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
| B2 | Four flows, entity dossiers and exact evidence | Executed JS, API/security contracts, existing suites | Pending |
| B3 | Linked graph with separate model/loading/layout/rendering | Graph interaction, bounded reads, ACL, stale-view tests | Pending |
| B4 | Source catalogue and bidirectional knowledge navigation | Source/version/permission and browser tests | Pending |
| B5 | Readable quality checks and durable human dispositions | Neo4j persistence, scope, replay and stale-version checks | Pending |
| B6 | Contextual corrections, removal impact, publication comparison | Review/publication/retirement/rollback regression | Pending |
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
