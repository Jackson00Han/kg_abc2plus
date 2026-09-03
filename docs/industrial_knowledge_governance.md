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
- typed entity properties and identity properties;
- directed relationship types and allowed source/target types;
- typed relationship properties;
- property and endpoint cardinality;
- numeric units and human-readable descriptions.

T-Box versions are immutable after publication. Draft updates require a
checksum compare-and-swap, activation requires an active-version
compare-and-swap, and activating a newer version retires the previous version.
Every A-Box record pins the exact `ontology_version_id` under which it was
validated, so a new T-Box cannot silently reinterpret old facts.

The T-Box is persisted as ordinary Neo4j property-graph nodes and
relationships. User-defined type names are stored as data; they are never
concatenated into Cypher labels or relationship types.

## Evidence rule

Graph objects are derived navigation data. Every accepted entity mention and
relationship assertion must retain its tenant, access groups, source Document
and Version, evidence Chunk, and exact character range. Dynamic industrial
properties are represented as evidence-bearing assertions rather than an
unverifiable property bag on a canonical Entity.

## Document and extraction boundary

The construction layer accepts bytes, not filesystem paths. Its built-in
allowlist covers UTF-8 plain text, Markdown, CSV, and JSON with pre- and
post-parse resource bounds. Format selection is by registered MIME type only.
The normalized source is split into deterministic, gapless `ChunkSeed`
records, preserving exact source offsets. Richer binary formats can be added
only through explicit parser plugins with the same output bounds.

The LLM extractor receives an injected OpenAI-compatible client and a
`PUBLISHED` tenant T-Box; it never reads credentials itself. The prompt and
strict response schema enumerate permitted types and directions. The server
then independently verifies every type, relationship endpoint, exact mention,
evidence substring, and offset. Model-supplied persistent IDs and unknown
fields are rejected. Valid output is marked `LLM_EXTRACTED + SECONDARY +
CANDIDATE`; low-confidence output is quarantined rather than silently
published.

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
Extraction artifact format v2 carries the structured mapping, while the reader
continues to decode legacy v1 artifacts as untyped literals. Invalid datatype,
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
retryable; structural model failures are recorded as rejected outcomes.

## Resolution, review, and publication

Entity resolution is deliberately conservative. Exact canonical keys and a
single unambiguous governed alias may produce an automatic link proposal.
Canonical-name and similarity matches are review suggestions only, and
homonyms or ambiguous aliases are conflicts. Every proposal records its rule
and matcher versions plus the authorized evidence supporting the target; it
never rewires graph records by itself.

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

## Retrieval rule

Normal retrieval may use only graph objects that are eligible under the
requested governance policy. Tenant and access-group checks apply to the
source Chunk on every graph path. Graph results always return the evidence
Chunks that support them; graph structure never becomes independent factual
evidence.
