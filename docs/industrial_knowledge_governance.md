# Industrial Property-Graph Knowledge Governance

This extension keeps the project on Neo4j's property-graph model. It does not
introduce RDF or OWL. The governed construction path is:

> versioned T-Box -> traceable source Chunk -> extracted candidate -> human
> decision -> published A-Box snapshot -> authorization-safe graph retrieval

## Authority and lifecycle are separate

`authority_level` describes the source of a knowledge record:

- `AUTHORITATIVE`: an expert-created or expert-imported A-Box record;
- `SECONDARY`: an LLM-extracted, rule-derived, or fixture record.

`governance_status` describes its current lifecycle state:

- `CANDIDATE` -> `APPROVED` -> `PUBLISHED`;
- a candidate or published record can be quarantined when evidence, identity,
  or schema checks fail;
- rejection and supersession are terminal audit outcomes.

Expert approval does not rewrite an LLM record as authoritative. This preserves
the distinction between source quality and workflow state.

## T-Box contract

A tenant-owned T-Box declares:

- entity types and canonical-key namespaces;
- typed entity properties and declared identity properties;
- directed relationship types and allowed source/target types;
- typed relationship-property definitions as T-Box metadata;
- property and endpoint cardinality;
- numeric units and human-readable descriptions.

T-Box versions are immutable after publication. Draft updates require a
checksum compare-and-swap, activation requires an active-version
compare-and-swap, and activating a newer version retires the previous version.
Every A-Box record pins the exact `ontology_version_id` under which it was
validated, so a new T-Box cannot silently reinterpret old facts.
Declared numeric units are validated against the pinned Pint registry before
any draft write; an unrecognized unit is invalid input, not a concurrent-write
conflict.

The T-Box is persisted as ordinary Neo4j property-graph nodes and
relationships. User-defined type names are stored as data; they are never
concatenated into Cypher labels or relationship types.

Relationship-property definitions are enforced as A-Box contracts. Each value
is a stably identified `RelationshipPropertyValue` with typed and
unit/time-normalized semantics plus its own exact evidence span. Its evidence
must be nested inside the parent assertion and match that assertion's tenant,
document/version, Chunk, access policy, and ACL. Extraction, authoritative
import, immutable review, publication, retrieval, and the Playground preserve
this representation.

## Evidence rule

Graph objects are derived navigation data. Every accepted entity mention and
relationship assertion must retain its tenant, access groups, source Document
and Version, evidence Chunk, and exact character range. Dynamic industrial
properties are represented as evidence-bearing assertions rather than an
unverifiable property bag on a canonical Entity.

## Document and extraction boundary

Expert initialization may use the governed construction API in `SOURCE_ONLY`
mode. This creates the traceable source and vector representation without LLM
extraction; expert instances must then be explicitly imported and published.
The source-only audit has its own profile and operation identity, preserving
the same immutable source/Chunk identities and all normal authorization,
lifecycle, resource and recovery checks. It does not create authoritative
facts just because the document has been described as authoritative.

The construction layer accepts bytes, not filesystem paths. Its built-in
allowlist covers UTF-8 plain text, Markdown, CSV, and JSON with pre- and
post-parse resource bounds. Format selection is by registered MIME type only.
The normalized source is split into deterministic, gapless `ChunkSeed`
records, preserving exact source offsets. Richer binary formats can be added
only through explicit parser plugins with the same output bounds.

Every construction request selects a non-empty source `access_groups` ACL. The
selected groups must be a subset of the authenticated principal's JWT groups;
the workflow independently rechecks the subset before parsing and persists
only the selected groups. It never widens a document to every group held by a
multi-group operator. Updating an existing source with a different ACL is
rejected rather than silently reclassifying its evidence.

The LLM extractor receives an injected OpenAI-compatible client and a
`PUBLISHED` tenant T-Box; it never reads credentials itself. The prompt and
strict response schema enumerate permitted types and directions. The server
then independently verifies every type, relationship endpoint, exact mention,
evidence substring, and offset. Model-supplied persistent IDs and unknown
fields are rejected. Valid output is marked `LLM_EXTRACTED + SECONDARY +
CANDIDATE`; low-confidence output is quarantined rather than silently
published.

The extractor supports provider-neutral response handling: strict JSON Schema,
generic JSON-object mode, or no provider `response_format` parameter. The latter
two modes embed the same compact response schema in the prompt, and all three
still pass through identical server-side JSON, T-Box, and evidence validation.

Every entity type exposed to model extraction must explicitly allow the
system-reserved provisional canonical-key namespace `llm-candidate`.
Extractor construction fails before a model call if any type omits it, and the
extractor rejects alternate provisional namespaces. The A-Box store retains a
second hard check: model-derived identities must use `llm-candidate` and their
entity type must declare it, while non-model identities must use a declared
ordinary namespace and may never use the reserved candidate namespace. This
prevents models from occupying expert identities and authoritative imports
from masquerading as machine candidates. A future customizable candidate
namespace requires a distinct T-Box declaration rather than reuse of the
ordinary canonical-key namespace list.

### Typed property facts, units, and time

Entity properties are extracted as evidence-bearing literal assertions, never
as a free-form property bag. Each model proposal names an entity-local
reference and a property declared on that entity's T-Box type, and supplies an
exact raw literal, optional exact source unit, optional `valid_from`,
`valid_to`, and `observed_at` tokens, one exact evidence range, and confidence.
The evidence range must enclose the entity mention and every non-null source
token. Document metadata is not accepted as an implicit temporal qualifier.

The server parses the declared datatype and validates cardinality before a
candidate can leave extraction. Numeric units are parsed by the pinned Pint
unit registry and converted to the T-Box's canonical unit; incompatible,
missing, and unexpected units fail closed. Decimal arithmetic is used during
conversion, and dates and date-times require strict ISO 8601/RFC 3339 source
text. Both source and canonical representations are retained:

- `literal_datatype`, `literal_typed_value`, and `literal_canonical_value`;
- `literal_raw_value`, `literal_raw_unit`, and `literal_canonical_unit`;
- canonical UTC validity/observation instants plus their exact raw source
  tokens.

These fields form part of the Assertion's stable identity, review revision,
portable extraction artifact, and publication materialization. Neo4j stores
them as flat scalar Assertion properties; it does not store a nested object.
Extraction artifact format v3 carries entity- and relationship-property
mappings, while the reader continues to decode legacy v1/v2 artifacts with no
relationship-property values. Invalid datatype,
unit, span, time range, cardinality, or fabricated qualifier findings are
explicitly rejected; low-confidence property facts enter quarantine.

Legacy compatibility is read/replay-only. Every newly persisted literal
candidate, authoritative import, human-review edit, approved revision, and
published revision must carry server-validated typed semantics. An old
untyped `PUBLISHED` revision may remain in or be restored from its historical
publication manifest, but it cannot be used to create a new untyped revision.
This explicit gate prevents the legacy decoder from becoming a datatype or
unit-validation bypass.

At the service boundary, `/v1/knowledge/authoritative:import` accepts a literal
assertion only through a nested `literal` object containing `raw_literal` and
optional `raw_unit`, `raw_valid_from`, `raw_valid_to`, and `raw_observed_at`
source tokens. The assertion evidence range must contain every supplied token.
Clients cannot provide `typed_value`, canonical fields, or parsed timestamps,
and an entity object and literal object are mutually exclusive. The server
loads the exact requested T-Box, verifies that it is the tenant's currently
active `PUBLISHED` version, resolves its `PropertyDefinition`, normalizes the
raw values into `TypedLiteralValue`, and only then calls the knowledge store.

The review queue returns the complete immutable `literal_semantics` projection
so an expert can compare source and canonical representations. A review edit
uses the same raw-only `literal` contract. The server reloads the original
assertion's active T-Box and recomputes semantics; a client cannot change the
ontology version or submit canonical values. Invalid units, datatypes, temporal
ranges, and inactive ontology versions fail before review persistence.

The upload workflow is resumable and idempotent. A stable operation key binds
the request fingerprint, source lifecycle generation, active snapshot, T-Box,
and per-Chunk outcomes. The ordinary ingestion pipeline receives a canonical
empty graph extraction, so it can publish only the Document, immutable Version,
Chunk, and Embedding. Separately cached ontology extraction artifacts are then
converted into governed candidate revisions. Provider failures remain
retryable. The extractor defaults to one validation attempt, preserving the
legacy policy signature. The Playground explicitly enables two attempts:
bounded structural/evidence failures receive one model correction with the
specific findings, while every response and failure remains in immutable
source/tenant/ACL/policy-bound audit artifacts. Final structural failures are
recorded as rejected outcomes. Provider failures, timeouts and oversized
responses do not trigger correction; raw oversized responses are not retained.

Parsed uploads are rejected before embedding, ingestion, or extraction if they
exceed the configured Chunk count, potential model-call count, or total
extraction-character budget. Configuration itself is capped at 512 Chunks,
512 model calls, 5 MiB of extraction text, and a 900-second deadline, so an
operator cannot turn the per-request guard into an unbounded value. The
workflow also has a monotonic cooperative deadline. It checks that deadline
before each stage and before reserving each model call, and starts no call
unless the configured provider timeout fits in the remaining budget. An
in-flight synchronous provider call still relies on its own timeout;
cooperative cancellation prevents subsequent calls, not an unsafe
interruption of a running thread.

## Resolution, review, and publication

Entity resolution is deliberately conservative. Exact canonical keys and a
single unambiguous governed alias may produce an automatic link proposal.
Canonical-name and similarity matches are review suggestions only, and
homonyms or ambiguous aliases are conflicts. Every proposal records its rule
and matcher versions plus the authorized evidence supporting the target; it
never rewires graph records by itself.

`identity_properties` are validated and persisted as ontology declarations,
included in the extraction prompt, and consumed by the resolution service.
The matcher reads the candidate's server-normalized typed property facts and
requires exactly one value for every identity property declared by its entity
type. A globally unique, active, published authoritative entity with the same
datatype, canonical value, and canonical unit produces an `AUTO_LINK`
suggestion. Missing values, duplicate values, partial keys, or a value shared
by multiple authoritative entities produce a conservative conflict or no
match; they never fall through to an automatic alias link.

Suggestions are computed inside the caller's tenant and evidence ACL and carry
the exact authoritative Chunk evidence plus rule and matcher versions. Applying
one recomputes the suggestion, verifies the expected candidate revision and
selected target, then atomically creates an approved mention revision and
rebinds every dependent candidate assertion to it. The assertions keep their
existing candidate/quarantine status and still require separate human review.

Review decisions create immutable record revisions through optimistic
compare-and-swap. Reviewers may approve, reject, quarantine, or edit a bounded
batch, and each decision records reviewer identity, time, and notes. Approval
does not promote a secondary model record to authoritative status.

Data visibility and action authority are independent. Access groups decide
which evidence a principal may read, while explicit `knowledge:construct`,
`knowledge:review`, and `knowledge:publish` capabilities authorize mutations.
Holding a source access group alone never grants construction or approval
authority.

Publication is a separate atomic operation over approved revisions. It writes
a content-addressed manifest, materializes only the validated records, and
activates a monotonically versioned tenant publication. Rollback changes the
active publication pointer while retaining all revisions, manifests, and
activation history. Stale source snapshots, unauthorized evidence, and T-Box
mismatches fail closed.

Once a source has a governed revision, publication/snapshot binding,
construction audit record, or evidence-bearing relationship-property value,
the generic document delete endpoint cannot physically remove it. Its
tenant/document-scoped preflight runs before the tombstone and source mutation,
also recognizing damaged records through either their immutable evidence
identity or their surviving Chunk edge. The operation returns one generic
conflict rather than disclosing which governed record exists. Operators must
use logical retirement to withdraw the source from active vector, BM25, and
graph retrieval while preserving review and publication history. Destructive
retention erasure is intentionally outside this workflow and would require its
own governed policy and audit design.

Governed logical retirement requires `knowledge:lifecycle`, complete Chunk
visibility, an expected active-snapshot CAS, and the current source generation.
It is blocked by an active publication, a current candidate/quarantined/approved
review revision, or an in-flight construction/ingestion job. A successful
retirement atomically removes active pointers and retrieval eligibility,
invalidates the tenant embedding generation, and advances the corpus revision,
while preserving the source, every review revision, and immutable actor/time
audit links. Re-ingesting identical content may reactivate the source, but it
cannot rewrite a prior retirement event. The lifecycle inventory exposes only
bounded metadata and stable blocker codes to a fully authorized steward; it is
not a document-content export.

Relationship endpoint cardinality is a closed-world publication invariant.
Across the complete final manifest, source cardinality counts distinct target
IDs per predicate/source and target cardinality counts distinct source IDs per
predicate/target; duplicate evidence for one canonical edge counts once.
Required and single-valued constraints are checked against the exact bound
T-Box before materialization or activation changes. Partial ACL-filtered
retrieval subgraphs are never treated as a complete world. Omitted legacy
endpoint cardinalities retain the `ZERO_OR_MORE` default.

Every `KnowledgePublication` stores its exact immutable
`ontology_version_id` and a `USES_TBOX_VERSION` edge. A fresh publication may
bind only the tenant's currently active `PUBLISHED` T-Box. After a newer T-Box
is activated, an existing publication remains queryable and may be replayed or
selected by rollback against its now-`RETIRED` bound version; upgrading the
published knowledge requires a new extraction/import and publication. API
publication, rollback, and history responses expose the binding. Migration 010
backfills a legacy publication only when all immutable manifest revisions
prove one exact T-Box; ambiguous or missing legacy bindings remain invisible
and fail closed until repaired through an audited migration.

## Retrieval rule

Normal retrieval may use only graph objects that are eligible under the
requested governance policy. Tenant and access-group checks apply to the
source Chunk on every graph path. Graph results always return the evidence
Chunks that support them; graph structure never becomes independent factual
evidence.

## Active publication quality

The governed A-Box can be audited from its active `KnowledgePublication`, not
only from extraction fixtures or pre-publication snapshots. The bounded audit
reconciles immutable revisions, exact source evidence, the pinned T-Box,
current navigation materializations, relationship-property values, entity and
endpoint cardinalities, duplicates, orphans, and anomalous hubs. It requires
complete tenant/ACL visibility and never returns source text. See
[`published_graph_quality.md`](published_graph_quality.md) for the executable
boundary and limitations.
