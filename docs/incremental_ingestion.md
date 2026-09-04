# Idempotent Incremental Ingestion

Stage 3 adds the production ingestion boundary under
`src/graphrag_prod/ingestion`. The numbered tutorial scripts remain examples;
they do not provide the lifecycle, authorization, or recovery guarantees
described here.

## First Principles

Publication is a visibility decision, not a sequence of partially visible
writes. Provider work is slow and retryable, while Neo4j transactions must be
short. The implementation therefore separates the workflow into four parts:

1. durably register a tenant-scoped job and all chunk tasks;
2. reuse or compute immutable extraction and embedding artifacts;
3. materialize and verify an invisible `KnowledgeSnapshot`; and
4. atomically replace `ACTIVE_SNAPSHOT` and `ACTIVE_VERSION` pointers.

Every production read must begin at an authorized `Document` and its active
snapshot/version. A `BUILDING`, `READY`, failed, or retired snapshot is not
published evidence. The provenance evidence reader enforces this path in the
database query; later retrieval code must preserve the same rule.

## Independent Identities

- `DocumentVersion` identifies immutable normalized/original source content.
- `GraphPipelineProfile` identifies normalization, splitting, extraction,
  prompt, schema, and code signatures.
- `KnowledgeSnapshot` identifies one DocumentVersion processed by one graph
  profile. Its sealed manifest contains chunks, their material location
  metadata, entities and aliases, mentions and confidence, and assertions with
  their confidence and accepted state.
- `ChunkEmbedding` identifies one chunk in one embedding space.
- `EmbeddingIndexGeneration` identifies a tenant, embedding space, and
  monotonically selected generation version.

Embedding IDs are deliberately absent from the graph snapshot manifest. A
vector migration must not rewrite graph provenance or masquerade as a graph
pipeline change. Vectors remain linked to chunks and become searchable only
through a separately verified active index generation.

## Cache Before Compute

`Neo4jIncrementalPipeline.run` receives stable chunk seeds and provider
profiles. Before invoking either provider it creates a `PREPARE_UPSERT`
`IngestionJob` and one `IngestionTask` per chunk. Providers receive the stable
artifact ID as their idempotency key.

Extraction artifact identity uses tenant, the complete chunk provider-input
hash, and graph profile ID. Its payload stores entity/claim output with
chunk-relative coordinates, allowing unchanged content at the same configured
input position to be rebound to a new immutable source version. Embedding
artifact identity uses tenant, chunk checksum, and embedding-space ID.

Each artifact is checksum-verified and committed immediately. If a later
provider call fails, the job enters `RETRY_WAIT`; retrying the same request
reads completed artifacts before making any provider call. A changed artifact
payload under an existing deterministic ID is rejected. Provider work is
at-least-once, so adapters should honor the supplied artifact ID as their own
external idempotency key.

## Job Recovery and Fencing

Jobs have a bounded attempt count, phase, error code, worker lease, expiry, and
random fencing token. Every staging, verification, publication, completion,
and failure write checks the current token. Once a lease expires and another
worker takes over, a late result or exception from the previous worker cannot
finish the job or clear the new lease.

Task and snapshot writes use stable IDs and immutable-property comparisons, so
replaying a committed step is a no-op. A response lost after publication is
recovered by reading the already-terminal job. Failed partial state remains
identified through `IngestionJob` -> `BUILDS`/`HAS_TASK` links and is never on
an active read path.

Artifact timestamps are derived from the immutable request rather than the
worker clock. Retrying the same request after a process restart or clock
advance therefore retains the same job fingerprint and artifact identities.

## Publication and Metadata CAS

The tenant `TenantCorpusState` node serializes document publication/deletion
with embedding-generation cutover. Publication also locks the target Document,
checks source tombstone generation, checks the expected active snapshot, and
verifies the exact stored manifest before changing either active pointer.

Replaying an exactly active snapshot returns `UNCHANGED`. A title or newer ACL
snapshot is an explicit atomic `METADATA_UPDATED` operation across the
Document and every active chunk. ACL versions cannot move backward, and the
same version cannot carry different policy ID/groups.

Entity identity can be shared across documents. Staging therefore writes only
identity fields. Publication may fill a missing display profile but never
overwrites a profile supported by another active document. Full alias and
homonym governance belongs to Stage 4.

The public Stage 2 `write_bundle` adapter remains available only for an
unmanaged legacy tenant. It takes the same tenant corpus lock and fails before
writing as soon as that tenant enters `MANAGED_INCREMENTAL` mode. Managed ACL,
source, graph, and vector mutations must go through this ingestion lifecycle.

## Delete and Tombstones

Deletion holds the same tenant/document locks as publication, validates the
active-snapshot CAS, advances a durable `DocumentTombstone.generation`, and
removes the source versions, snapshots, chunks, embeddings, mentions,
assertions, document-scoped tasks, and orphan artifacts in one transaction.
Entities are deleted only when no remaining mention or assertion supports
them. Audit `IngestionJob` and tombstone records remain; they contain
operational metadata rather than source text.

An absent Document is not automatically a no-op: registered preparation work,
tasks, or a staged snapshot are deletion targets too. Deleting that state
advances the tombstone generation and fences provider output that returns late,
including a first create that had never published a Document.

An old in-flight upsert carries the prior source generation and cannot
resurrect a deleted document. Repeating the same delete returns the committed
terminal result; a new delete against an already absent document is a no-op.

Physical deletion is restricted to documents that have never entered the
governed knowledge workflow. Before creating a tombstone or mutating any source
node, the delete transaction checks the exact tenant/document boundary for all
governed mention/assertion revisions, current or historical knowledge
publications, construction-job audit records, and evidence-bearing
`RelationshipPropertyValue` nodes. It checks both immutable identity properties
and evidence edges, so a partially damaged edge/property pair fails closed
instead of silently severing provenance. Such a document must use governed
logical retirement, which removes it from active retrieval while retaining its
source, review, publication, and activation audit chain. A foreign tenant with
the same document identifier cannot affect this decision, and the public API
maps every blocker to the same non-enumerating conflict response.

This guard deliberately does not change the Stage 3 semantics for an
ungoverned document: its physical delete remains atomic, repeatable, and
tombstone-fenced. A future compliance purge that also destroys governed audit
history would require a separate, explicitly authorized retention workflow; it
must not reuse the generic delete operation.

### Governed logical retirement

`Neo4jDocumentRetirementService` is the audit-preserving withdrawal path for
governed sources. A principal needs the `knowledge:lifecycle` capability and
complete access to every Chunk owned by the active document. The request binds
an operation key to the expected active snapshot and source generation; the
service rechecks that compare-and-swap boundary while holding the tenant corpus,
publication, and document locks.

Retirement is allowed only when no active publication, current review record,
or in-flight construction/ingestion job depends on the source. It removes the
active snapshot/version pointers, clears every owned Chunk's retrieval scope,
advances the corpus revision, and invalidates the active embedding generation.
It does **not** delete source text, versions, Chunks, governed revisions, review
decisions, or publication history. The Document, Snapshot, Version, Tombstone,
and immutable `RETIRE` job retain actor, time, before/after generation, and
exact graph links. An exact replay returns the same result only after validating
that complete audit projection; missing links or changed fields fail closed.

Re-ingesting the same content is an explicit managed reactivation. It restores
the current Document/Snapshot/Version lifecycle projection but never edits the
prior tombstone or retirement event, so a later second retirement produces a
new immutable event. The bounded active-document listing returns metadata,
source generation, ACL, active IDs, Chunk count, and stable blocker codes only;
it never returns source text and hides partially authorized or provenance-broken
documents.

## Vector Generation Cutover

`Neo4jEmbeddingIndexManager` provides an explicit migration path:

1. `materialize` validates stable embedding identity and a non-zero finite
   float32 cosine norm, then stores vectors only for active tenant chunks;
2. `prepare` creates a generation-specific label and Neo4j vector index, waits
   for it to become online, verifies label, property, dimensions, and cosine
   similarity configuration, then labels the exact active snapshot corpus
   while holding the tenant corpus lock;
3. `coverage` measures the exact active snapshot corpus; and
4. `activate` rebuilds exact label membership, requires 100% coverage, and
   atomically swaps the tenant pointer using the shared corpus lock.

Publishing, changing ACL metadata, or deleting a document increments the
corpus revision and atomically removes the active embedding pointer, marking
that generation `STALE`. Retrieval can therefore never silently use an index
verified against an older corpus. Re-preparing and activating a complete
generation is required after corpus mutation, and generation versions cannot
roll back.

An embedding-profile-only replay does not rebuild the graph snapshot. It
conditionally materializes the new space only while that exact snapshot and
source generation remain active; a superseded or deleted source becomes an
atomic no-op rather than attaching stale vectors.

## Current Boundary

Stage 3 validates lifecycle correctness on deterministic fixtures and a real
disposable Neo4j Community database. It does not claim live queue durability,
provider SLAs, graph entity-resolution quality, production retrieval, answer
grounding, API authentication, or load/backup validation. Those remain gated
by Stages 4-9. The caller or durable queue must replay the same immutable
`IncrementalIngestionRequest` after process loss; this repository does not yet
operate an external message broker or original-object store.
