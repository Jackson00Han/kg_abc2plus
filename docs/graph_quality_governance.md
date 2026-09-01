# Knowledge Graph Quality Governance

Stage 4 makes graph quality an enforced, versioned boundary rather than an
extractor convention. Source chunks remain the factual evidence. Entities and
Assertions are governed navigation data and are never allowed to replace the
source-to-chunk provenance path.

## Versioned schema policy

`contracts/graph_governance.v1.json` is the executable policy catalog for the
supported English company-filings domain. Each policy declares:

- allowed entity types and canonical-key namespaces;
- the exact required and allowed Entity and Assertion properties;
- allowed predicates, subject types, object kind, and object types;
- minimum entity-mention and Assertion confidence; and
- an explicit anomalous-hub degree threshold.

A graph policy ID must equal the `GraphPipelineProfile.schema_signature`.
Changing policy semantics therefore requires a new signature and creates a new
pipeline profile/snapshot identity. The complete policy payload and checksum
are persisted once and immutable-ID conflicts fail closed.

## Publication gate

`IngestionPlan.build` applies the policy before sealing the snapshot manifest.
The gate behaves as follows:

| Condition | Result |
| --- | --- |
| Unknown entity type or canonical-key namespace | Reject the plan |
| Entity mention below the declared threshold | Reject the plan |
| Undeclared predicate or endpoint pattern | Reject the plan |
| Assertion below the declared threshold | Preserve provenance but set the snapshot membership to unaccepted |
| Unicode/whitespace or duplicate-alias variation | Normalize before the manifest is sealed |

Every automatic normalization/quarantine is represented by an immutable
`GraphGovernanceFinding` linked to the snapshot. Rejected data may remain in a
provider artifact for retry/audit purposes, but cannot reach an active graph
snapshot.

The Stage 2 exact-range invariants remain mandatory: entities require exact
mention evidence, relationship endpoints require mentions inside the
Assertion evidence range, and literal values must occur in that range.

## Name and alias handling

Display names use conservative Unicode NFKC plus whitespace normalization.
Comparison keys additionally case-fold and replace punctuation with spaces.
These comparison keys are only used to find review candidates. They never
change stable IDs and are never sufficient to merge two entities.

Aliases are normalized, deduplicated by comparison key, and do not repeat the
canonical name. A normalized name or alias shared by distinct entity IDs is
reported as a potential duplicate or alias collision for review.

## Entity resolution

Resolution uses deterministic, non-scored rules in this order:

1. different tenants remain separate;
2. different entity types remain separate;
3. conflicting values in the same authoritative namespace remain separate;
4. an exact shared authoritative identifier permits merge; and
5. name/alias overlap without authoritative identity requires human review.

Examples of authoritative namespaces are ticker, CIK, and LEI. Every
`ResolutionDecision` records the versioned rule ID, candidate IDs, rationale,
and complete mention-evidence IDs. `resolve_and_record` refuses missing or
cross-tenant evidence and persists `EVIDENCE_MENTION` links. There is no fuzzy
similarity threshold or custom merge score.

## Tenant-scoped quality audit

`Neo4jGraphQualityService.audit` begins at tenant-owned Documents and their
published `ACTIVE_SNAPSHOT` pointers. It detects:

- invalid entity types and canonical-key namespaces;
- active entities with no active mention;
- entities with no accepted graph relationship;
- normalized duplicate names and alias collisions;
- accepted Assertions outside the declared endpoint pattern;
- missing, multiple, inactive, out-of-range, or endpoint-incomplete evidence;
- literal values absent from their exact evidence range;
- tenant entities with no mention or Assertion provenance anywhere; and
- accepted entity degrees above the declared hub threshold.

Reports contain stable IDs, counts, issue codes, and evidence IDs, but no
source text. A deterministic SHA-256 ordering over `sample_seed`, object kind,
and stable ID produces a reproducible human-review sample. The report, policy,
issues, corpus revision, and timestamp are persisted as an immutable
`GraphQualityRun`.

Run an audit against an explicitly configured database with:

```bash
uv run --locked python scripts/run_graph_quality_report.py \
  --tenant-id TENANT \
  --policy-id company-filings:v1 \
  --generated-at 2026-09-01T12:00:00+08:00 \
  --output /tmp/graph-quality.json
```

The command reads `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, and optional
`NEO4J_DATABASE`. Authentication/authorization for a future API wrapper belongs
to Stage 7; callers of this administrative service must supply a trusted tenant
scope.

## Human quarantine

Reviewers may `ACCEPT` or `QUARANTINE` an Entity or Assertion with reviewer ID,
rationale, policy version, and timestamp. Decisions are immutable and linked
to their target. Acceptance is refused while structural source-support or
schema errors remain.

Quarantine does not rewrite the sealed snapshot manifest. It adds governed
state to the Entity/Assertion, and evidence reads require accepted snapshot
membership plus accepted governance state for the Assertion and both entity
endpoints. Stage 5 retrieval must preserve the same predicates on every graph
expansion path.

## Adjudicated fixture and metrics

`evaluation/graph-review-v1.json` contains 60 versioned positive and negative
entity, relationship, and resolution cases. The metric evaluator implements
the exact Stage 1 definitions:

```bash
uv run --locked python scripts/evaluate_graph_review.py
```

The committed fixture produces entity precision 1.0, relationship precision
1.0, and entity-resolution accuracy 1.0 against targets of 0.95.

## Current boundary

The resolver is deliberately conservative and pairwise. Candidate generation
and human workflow UI are not supplied, and name-only matches remain review
items. Semantic entailment beyond the enforced source coordinates is measured
by adjudication rather than a homemade score. Stage 8 will incorporate these
metrics into the unified regression runner; Stage 9 owns representative-scale
validation. Graph retrieval and answer generation remain gated by Stages 5
and 6.
