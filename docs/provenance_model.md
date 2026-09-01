# Production Provenance Model

Stage 2 introduces the first production package under `src/graphrag_prod`.
The numbered scripts remain tutorial examples and do not define the production
data contract.

## First Principle

An extracted graph fact is not evidence by itself. A usable fact must retain an
unbroken, tenant-scoped path to immutable source text:

```text
Document -[:ACTIVE_VERSION]-> DocumentVersion -[:HAS_CHUNK]-> Chunk
                                                               ^
                                                               |
Assertion -[:EVIDENCED_BY]-------------------------------------+
    |                         ^
    +--[:SUBJECT|OBJECT]--> Entity <--[:REFERS_TO]-- EntityMention
                                                   |
                                                   +--[:IN_CHUNK]-->
```

`Assertion` is a reified relationship. Its stable `assertion_id`, predicate,
endpoints, extraction signature, confidence, and exact evidence range are
therefore addressable and auditable. Neo4j relationship IDs are never used as
business identifiers.

## Records and Identity

All application IDs are deterministic UUIDv5 values produced by identity
scheme version `1`. The namespace and ordered identity inputs are permanent
once published. Changing the scheme is a breaking data migration, and an ID is
only a locator—not proof of authorization.

| Record | Stable identity inputs | Purpose |
| --- | --- | --- |
| Document | tenant + canonical URI | Logical source inside one tenant |
| DocumentVersion | document + normalized checksum + original checksum | Immutable source revision |
| Chunk | version + splitter signature + ordinal + exact range + checksum | Exact source slice |
| ChunkEmbedding | chunk + embedding-space ID | One vector-space representation |
| Entity | tenant + type + adjudicated canonical key | Tenant-local concept |
| EntityMention | chunk + type + range + surface + extractor signature | Entity extraction evidence |
| Assertion | tenant + endpoints/value + predicate + evidence + extractor/schema signatures | Auditable graph fact |

Changing source content, splitter configuration, embedding space, extractor,
or assertion schema changes the corresponding derived identity. Display names,
aliases, titles, ACL snapshots, and acceptance state are not identity inputs.
Golden tests protect the published scheme from accidental changes.

## Source and Citation Coordinates

`DocumentVersion.normalized_text` is the authoritative citation coordinate
space. A Chunk stores absolute, half-open character offsets `[char_start,
char_end)` and must equal that exact slice. Mentions and Assertion evidence use
the same absolute Python Unicode character coordinate system, not UTF-8 byte
offsets. Evidence text and mention surfaces retain leading and trailing
whitespace. `published_at=None` means unknown; ingestion time never substitutes
for source publication time.

An Assertion currently carries one contiguous evidence span in one Chunk.
Multi-chunk claim aggregation belongs to the later governance/retrieval model;
it must not be simulated by weakening the exact-span invariant.

Both hashes are retained:

- `checksum` hashes the authoritative normalized text.
- `original_checksum` hashes the original source bytes before normalization.

Including both in `version_id` prevents two different byte sources from being
silently folded into one version. Exact byte-offset reconstruction is outside
this stage; a later source adapter must persist the original object in durable
storage when byte-level replay is required.

## Immutable Provenance and Mutable State

Identity and evidence fields are immutable after first write. Reusing a stable
ID with different immutable fields aborts the transaction.

Mutable operational state is deliberately separate:

- The active publication is an `ACTIVE_VERSION` pointer, not a field on every
  Chunk. Stage 2 permits idempotent publication and rejects a second active
  version; Stage 3 owns the controlled switch and delete lifecycle.
- Document and Chunk access snapshots contain policy ID, positive integer
  policy version, and explicit groups. Updates are serialized with a Neo4j
  write lock. A lower version is rejected, and the same version cannot carry
  different policy state. Document and Chunk updates commit in one transaction.
- Entity display profile and Assertion `accepted` state are mutable. Stage 4
  will add the review and governance lifecycle for these fields.

An empty group set is invalid; intentionally public material needs an explicit,
deployment-defined public group. The snapshot is not an external IAM system,
and mapping authenticated identities into trusted Principals belongs to Stage 7.

## Authorization Boundary

Authorization is default-deny. Evidence is returned only when all of these are
true in the database query itself:

1. principal, Document, Version, Chunk, Assertion, and Entity share a tenant;
2. the principal intersects both Document and Chunk access groups;
3. Document and Chunk use the same access policy ID and version;
4. the Version is reached by the Document's `ACTIVE_VERSION` pointer; and
5. the Assertion is accepted.

Filtering after retrieval is not considered authorization. Direct traversal or
future vector/full-text recall must preserve the same predicates.

## Constraints and Supported Database

The executable schema sources are the ordered files under
`src/graphrag_prod/graph/migrations`. They provide stable-ID uniqueness,
natural business-identity uniqueness, lifecycle constraints, and lookup
indexes. Migration application is idempotent, and verification checks label,
properties, constraint/index type, and online index state.

The validation environment uses `neo4j:5.26.12-community`. Community Edition
does not provide every Enterprise existence/type constraint, so required-field,
checksum, range, tenant, evidence, and identity validation is enforced by the
frozen Python domain model and transactional store. Production writes must go
through that boundary; unrestricted direct Cypher writes are not supported.

## Current Boundary

Stage 3 now supplies whole-document incremental orchestration, active snapshot
switching/deletion, vector values, and isolated index-generation cutover. See
`docs/incremental_ingestion.md`. Graph quality adjudication, production
retrieval, answer generation, and APIs remain gated by Stages 4-7.

`Neo4jProvenanceStore.write_bundle` is retained as a Stage 2 compatibility
writer for unmanaged legacy tenants. Once the incremental lifecycle marks a
tenant `MANAGED_INCREMENTAL`, that direct writer fails closed under the shared
tenant corpus lock; all later writes must use the Stage 3 ingestion boundary.
