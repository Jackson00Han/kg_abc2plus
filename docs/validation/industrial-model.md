# I2: industrial model, corpus and governed loading

Development validation, 2026-09-06 UTC. I1 checkpoint: `6edfa33`.
I2 checks passed; the focused milestone commit records completion.

## Scope

- One composed industrial T-Box with explicit classification and physical
  composition DAGs, separate electrical connectivity, product/model/asset
  identities, event-scoped observations and diagnostic conditions.
- 34 authored documents and 330 exact semantic sections across Canalis KT and
  EvoPacT HVX: two AI-assisted curated reference documents and 32 explicitly
  synthetic field records for eight assets and three access groups.
- 62 selected entities and 76 source-supported relationships. The conservative
  record bound is 352; the loader separately reports actual governed records,
  source chunks, official excerpts and distinct original PDFs.
- Two narrow positive cases: correcting a demonstrated report-channel mapping
  error and explaining one historical remote request rejected in local mode.
  Other cases preserve conflicting or missing evidence. These are authored
  examples, not Schneider field reports or completed SME adjudication.
- A resumable, single-tenant industrial loader, source-provenance maps and
  explicit embedding-generation initialization, preserving existing service
  data and using the established ingestion/publication lifecycles.

## Reproducible artifacts

Authored corpus checksum:
`f79af571b20364e08d2a966da129714b417b6445cd46f5744339697050d06703`.

The source catalog is now version `1.0.1`: the Canalis installation manual's
language metadata is corrected to English. Original PDF bytes/checksums and
embedded editions are unchanged. The newly normalized Canalis artifact records
that corrected metadata and has checksum
`1be221e9ffec4086efd571793f01613af6784f0013b53d01433197cfd8deb1e9`.
The I1 artifact remains historical evidence of its earlier metadata.

```sh
uv run --locked python scripts/build_industrial_corpus.py --check
uv run --locked python scripts/validate_industrial_model.py
uv run --locked python scripts/load_industrial_corpus.py
```

The model gate verifies source editions, physical pages, product scope, primary
asset facets, hierarchy presence/cycles, endpoint types/cardinalities and the
complete corpus checksum before public loading. Exact original PDF reparsing
is required when official excerpts are supplied; see
[industrial loading](../industrial_loading.md).

## Verification

The complete disposable-Neo4j regression passed 146 tests with no skips,
failures or errors. It included immutable hierarchy round trips, atomic cycle
rejection and six industrial lifecycle/provenance checks. The same `dev-mini`
container caps apply: one CPU, 1,536 MiB total memory, 512 MiB maximum heap and
128 MiB page cache. The owned test container was removed after completion.

```sh
sh scripts/run_stage8_neo4j_tests.sh /tmp/industrial-all/integration.json /tmp/industrial-all/observations
sh scripts/run_industrial_neo4j_tests.sh /tmp/industrial-focused/integration.json /tmp/industrial-focused/observations
```

The final focused runner passed all ten industrial Neo4j tests without skips,
failures or errors (403.618 seconds). This includes the full 34-document,
330-Chunk public loader followed by an exact replay with no additional embedding
calls, permission-isolated provenance reads, prepared-index recovery,
independent-review preservation and immutable query-facet checks. Fixture
vectors in these database tests establish lifecycle behavior, not semantic
retrieval quality. The focused runner supplements full-system checks; it never
substitutes for the complete Stage 8 inventory or production qualification.

A pre-final focused run exposed a missing Cypher clause separator in index
recovery and an incorrect default-status lookup in its independent-review test.
Both were corrected before the complete ten-test rerun passed; the failing
capture was retained separately rather than counted as successful evidence.

The final staged whitespace check removed one redundant terminal newline from
27 authored files. A byte comparison confirmed no other text changed. Their
manifest and exact final-section ranges were rebuilt; the 67 industrial unit
tests and representative public-load/replay test were rerun for the final bytes.

The complete unit suite passed 837 tests. After the final chronology correction,
all 67 industrial unit tests passed, including its new chronology regression;
the combined current inventory is 838 unit tests. HTTP E2E passed 16 tests,
security passed 34, and regression passed two, with no skips or failures.
Locked dependencies, contract/model validation, deterministic corpus rebuild,
Python compilation and source/wheel packaging also passed.

Actual selected-original reparsing confirmed 36 versioned sources: 34 authored
documents plus two official excerpts from two distinct pinned original PDFs.
There are 370 exact Chunks and 193 governed records: 23 project-curated reference
records and 170 secondary records. This preview made zero provider calls; it is
not a claim that live embeddings or industrial retrieval have been evaluated.

Before live loading, the existing service contained 1,167 nodes and 12 documents
across its two original tenants. Their node-property fingerprint was captured
outside the repository for a post-load preservation comparison. This stage did
not reset or replace that service database.

## Content review

Initial repeated workflow prose was replaced with case-specific timestamps,
signal samples, instrument settings, inline attachment excerpts and decisions.
Long sentences repeated at least four times account for about 3.92% of field
body characters after that review, excluding headings and the per-document
synthetic-origin declaration. This literal repetition check is a corpus-quality
check, not a retrieval scoring formula or proof of industrial correctness.

Final chronology review corrected three first-observation reports that had
included later measurements or instrument settings. Those later facts remain
in the already dated follow-up documents. The eight initial-event reports were
checked against publication time, with regression coverage for the identified
future-observation leaks. Planned future follow-ups remain explicitly plans.

The original manuals are acquired separately into an external cache; full PDFs
and normalized official text are not committed. Authored reference summaries
retain bibliography and `SME_REVIEW_PENDING`. Field samples and their logical
attachment identifiers are explicitly fictional.

## Remaining milestones

I3 measures retrieval and addresses product/asset/context gaps. I4 adds the
same-port workbench and reusable graph explorer. I5 captures complete final
regression and browser/live-provider evidence. This I2 report does not claim
those features are already delivered or that industrial SME review is complete.
