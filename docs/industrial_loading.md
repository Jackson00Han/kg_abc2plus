# Resumable industrial reference loading

The industrial loader adds the versioned electrical-maintenance corpus to the
existing local Neo4j service without resetting its database or changing the
financial/legal fixtures. Its dedicated tenant is `industrial-schneider-demo`;
access groups are `public`, `engineering`, and `maintenance`.

`prepare_industrial_load` and `Neo4jIndustrialLoader.load` are reusable Python
entry points. The CLI is `scripts/load_industrial_corpus.py`. Every public entry
validates the complete industrial corpus against the pinned source catalog,
acceptance contract and composed industrial T-Box before a data mutation. A
small lifecycle fixture is available only in the test suite and cannot bypass
the public CLI's acceptance checks.

## Preview and load

Preview performs no embedding/model calls or database writes:

```sh
uv run --locked python scripts/load_industrial_corpus.py
```

Add verified official excerpts, kept outside the repository:

```sh
uv run --locked python scripts/load_industrial_corpus.py \
  --normalized-source /tmp/graphrag-industrial-cache/canalis-selected-en.v1.json \
  --normalized-source /tmp/graphrag-industrial-cache/hvx-selected-final.v1.json \
  --original-cache /tmp/graphrag-industrial-cache
```

Add `--load` to perform the authorized local load. `--report` writes an optional
aggregate JSON report to an explicitly chosen path. The CLI reads existing
`.env` settings without displaying values. Live loading requires explicit
`EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`,
`PLAYGROUND_NEO4J_URI`, `PLAYGROUND_NEO4J_USER`, `PLAYGROUND_NEO4J_PASSWORD`, and
`PLAYGROUND_ALLOW_DISPOSABLE_DB=1`. The database defaults to `neo4j` unless
`PLAYGROUND_NEO4J_DATABASE` is set. The URI must be loopback; the embedding URL
must be an official DashScope HTTPS endpoint.

The provider adapter uses the same named model, revision, dimensions and
normalization as the existing Playground vector space. Calls have a 30-second
timeout and at most one SDK retry. The fixture embedder is confined to isolated
tests and is never a silent fallback when live configuration is missing.

## Ordered lifecycle and resumability

1. Validate source identities, corpus scale, exact sections, source editions,
   model applicability, ontology relationships and the complete record count.
2. Acquire a database-backed tenant lease. Another live process or different
   manifest for the same tenant is rejected. The lease expires after 180 seconds
   after a crash; an active loader renews it every 30 seconds and at operation
   boundaries. Provider calls check ownership before and after execution.
3. Record an identifiable `IndustrialCorpusLoad` with a stable manifest ID and
   actual initial ingestion timestamp. Resume reuses that timestamp.
4. Import/publish the exact composed T-Box, or require the same active version.
5. Ingest one bounded source at a time through `Neo4jIncrementalPipeline`.
   Document versions, chunks, provider artifacts and failures use the existing
   cache-before-compute lifecycle. Source-only extraction adds no unsupported
   graph claims.
6. Persist source provenance against the matching active immutable document
   version. It cannot replace metadata already attached to that version.
7. Write each bounded A-Box section together with an atomic replay checkpoint.
   The existing knowledge store enforces ontology, identity, exact evidence and
   authority-lane validation inside the same transaction.
8. Approve only the declared records of this versioned project reference
   fixture, then publish the complete manifest. Replays reuse original approved
   revision IDs even after publication creates new immutable revisions. The
   loader compares factual payload, origin and project review identity/time;
   an independent approval at the same revision number is preserved and blocks
   automatic bootstrap continuation.
9. Prepare/backfill and activate the industrial tenant's embedding generation.
   A new tenant is initialized explicitly; existing coverage and vector space
   are checked before reuse. The intended generation version is recorded before
   preparation so an interrupted cutover reuses that identified generation.

The loader records its current phase and safe exception type; it does not put
provider error bodies, protected text or credentials in reports. A normal
interruption can be resumed with the same command. Independent ontology edits,
record revisions, source deletion/recreation, vector-space changes or a replaced
active publication require an explicit migration/review decision; the loader
fails instead of overwriting those changes.

Bounds include at most 100 sources, 1,000 chunks total, 64 chunks per source,
5 MiB per normalized source, 100 records per atomic section batch and 500 records
in the complete publication. Authored semantic sections remain exact chunks of
at most 1,200 characters. Current developer-scale evidence does not qualify as
production-candidate validation.

## Authority and field evidence

`CURATED_REFERENCE` records are the project's documented reference baseline,
imported through the `EXPERT_IMPORT` / `AUTHORITATIVE` lane with the explicit
review identity `project-curated-reference:v1`. This identifies the application
baseline, **not Schneider Electric certification or completed industrial SME
approval**. The authored files retain their pending SME review status and source
bibliography. The visual interface must keep that qualification visible.

`SYNTHETIC_FIELD_RECORD` statements are persisted as `RULE_DERIVED` /
`SECONDARY` / `CANDIDATE`: a deterministic rule materializes only declared exact
source spans and relationships. A project reference validator performs the
fixture governance transition. Approval and publication preserve `SECONDARY`;
these records do not become actual site observations or validated diagnoses.
No arbitrary uploaded user document is automatically approved by this loader.

Official PDF excerpts are source evidence only. Their publication in the source
store does not itself create expert assertions or grant a claim authority.

## Original PDF proof and durable evidence maps

An artifact's self-checksum alone cannot establish that its text came from the
claimed original. The loading boundary therefore requires the cache of pinned
PDF originals named `{source_id}.pdf`. It:

- reads bounded regular files without following symlinks;
- validates the artifact's exact schema, checksum, original pin and complete
  catalog metadata;
- validates normalized-text checksums, gapless chunk ranges, selected physical
  pages, source boxes and the supported splitter configuration;
- hashes the actual original PDF, reparses its selected physical pages, and
  compares the complete normalized text and source map independently.

Changing a numerical value and recomputing the JSON checksum is rejected.
Caller-provided tenant/ACL fields are forbidden in these artifacts; official
excerpts receive the loader's explicit industrial `public` scope.

The resulting map is stored in immutable `DocumentVersion` properties alongside
its metadata checksum. It retains the full original checksum, official source
metadata, explicit page selection, parser version, and table/row/cell locations.
Authored sources instead retain their own exact checksum and bibliography;
they never impersonate PDF text or PDF page ranges.

Authored provenance also preserves product family, explicit primary asset keys
and the exact semantic section keys/titles/ranges. Mentioning another asset in
a document does not add it to the document's applicability. Official excerpt
families come from the reviewed catalog bindings; an unrecognized family stays
explicitly unspecified/reference-only.

The same transaction derives small query facets on `DocumentVersion`:
`industrial_family`, `industrial_contract_family`, `industrial_asset_keys`,
`industrial_source_kind` and `industrial_source_key`. Their parity with the
canonical metadata is checked on both replay and provenance reads. Later
queries can filter these fields inside tenant/ACL-bound Cypher without parsing
large PDF maps. Facet drift is a conflict, not a second source of truth.

Reports count authored documents, normalized official excerpts and distinct
original PDF checksums separately. Several derivatives of one original PDF
therefore do not inflate the original-source count.

`Neo4jIndustrialProvenanceStore.for_chunk(principal, chunk_id)` is the future
API/evidence-panel adapter. Its Cypher requires the same tenant, current
published source snapshot, exact document/version/chunk path, and current
intersecting document/chunk access groups. It verifies that stored provenance
still matches current identity, checksums and ACL metadata. Returned locations
are limited to the requested chunk's page and intersecting character ranges.
There is no unauthenticated or historical fallback. Normal version deletion
removes the attached source map rather than leaving detached text-bearing
provenance nodes.

PDF table geometry does not complete missing headers, merged diagnosis groups,
or cross-page context. Those retrieval limitations remain documented in
[PDF source ingestion](pdf_source_ingestion.md) and are evaluated separately.

## Checks

```sh
uv run --locked python -m unittest discover -s tests/unit -p 'test_industrial_loading.py'
```

`tests/integration/test_industrial_loading_neo4j.py` uses the established disposable
Neo4j environment and explicit isolated fixtures. It checks replay, provider
cache reuse, first-generation initialization, interrupted source/review recovery,
authority preservation, tenant/group isolation, immutable/tampered provenance,
ACL changes, loader lease expiry, original PDF map round trips and source-only
PDF deletion, independent reviews, interrupted embedding cutover and facet
drift. A separate test in the same suite uses the complete representative corpus
through the public loader, verifies permission-bound provenance reads, then
replays it without additional provider calls. Its evidence is recorded in the
industrial milestone report; the small fixtures do not stand in for that run.
