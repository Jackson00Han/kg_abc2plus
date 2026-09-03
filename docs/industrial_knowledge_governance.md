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
