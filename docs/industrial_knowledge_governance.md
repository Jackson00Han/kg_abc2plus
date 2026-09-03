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

## Retrieval rule

Normal retrieval may use only graph objects that are eligible under the
requested governance policy. Tenant and access-group checks apply to the
source Chunk on every graph path. Graph results always return the evidence
Chunks that support them; graph structure never becomes independent factual
evidence.
