# Industrial source catalog and task contract

The first industrial scope is Canalis KT (KTA/KTC) and the explicitly identified
EvoPacT HVX up-to-24 kV guide. This is an industrial knowledge and diagnostic-
evidence workbench, not an assertion of actual field diagnosis accuracy.
The executable [contract](../contracts/industrial_knowledge.v1.json) preserves
eight positive/negative task categories and separates source corpus scale from
the existing 500-record governed-publication bound.

## Source editions and applicability

[`datasets/industrial-v1/sources.json`](../datasets/industrial-v1/sources.json)
records eight official sources. Four originals have been downloaded, hashed and
inspected. Catalog metadata keeps the portal publication date/version separate
from embedded revision/date and filename. A country-specific download portal
does not establish exclusive technical applicability to that country.

The Canalis installation guide is `QGH3492101-04`, dated 12/2025 internally and
published on the portal on 2026-01-13. The English HVX guide is `BQT34371-02`,
05/2025 internally, with portal version 3.0. Its rating table distinguishes
12, 17.5 and 24 kV and configurations; "up to 24 kV" is not one equipment rating.

The Chinese 12 kV `PKR2441601` guide is a distinct source. Inspection found
outlined Chinese glyphs without a usable text layer on its technical pages.
It is explicitly `OCR_REQUIRED`, not silently indexed as a successful Chinese
normalization. HVX-O and EvoPacT/SureSeT materials are separately identified
applicability exclusions. KTA/KTC catalog listings are metadata-only until their
originals are acquired and pinned; listing a source does not validate its claims.

## Reproducible acquisition

Routine validation does not download files or call a model:

```sh
uv run --locked python scripts/validate_industrial_contract.py
uv run --locked python scripts/fetch_industrial_sources.py --check
```

Acquire an inspected edition into an explicit cache outside the repository:

```sh
uv run --locked python scripts/fetch_industrial_sources.py \
  --source canalis-kt-installation --cache-dir /tmp/industrial-originals
```

Downloads are restricted to allowlisted official HTTPS hosts, including each
redirect; bytes, elapsed time, source identity and SHA-256 are checked. Existing
changed files are preserved and rejected. Metadata-only records cannot be
downloaded without a separately reviewed catalog pin. Complete manuals and
normalized source excerpts are not committed to this repository.

The offline cap is 96 MiB per original. It supports the inspected 23.6 MB
Canalis and 65.4 MB HVX guides without changing the 5 MiB online upload cap.
The larger 114.1 MB MCSeT original was excluded by that cap. Future expansion
requires an explicit resource/design review, not a silent limit increase.

## Explicit page normalization

Prepare only the declared physical pages, retaining the complete original hash:

```sh
uv run --locked python scripts/normalize_industrial_source.py \
  --source canalis-kt-installation --cache-dir /tmp/industrial-originals \
  --pages 30,71,72,76,77 --output /tmp/industrial-prepared/canalis.v1.json

uv run --locked python scripts/normalize_industrial_source.py \
  --source evopact-hvx-up-to-24kv --cache-dir /tmp/industrial-originals \
  --pages 9,10,14,103,104,105 --output /tmp/industrial-prepared/hvx.v1.json
```

These artifacts contain original metadata, normalized text/checksum, selected
physical pages, exact Chunk ranges, page/text/table-cell source maps, parser and
splitter versions, and an artifact digest. Changed output is not overwritten;
use a new artifact path to retain edition/normalization history. No ingestion,
expert approval, extraction, embedding or publication occurs at this step.

Canalis pages 71–72 include a continued table and a qualifier on the next page.
HVX pages 104–105 distinguish symptoms from multiple possible explanations.
Source maps preserve these locations; the ordinary character splitter does not
automatically reconstruct complete diagnostic groups or propagate merged-cell
headers. Retrieval must retrieve the relevant context before using such rows.
Some original notation is ambiguous; normalization preserves it rather than
silently correcting or promoting it as a validated rule.

The [PDF parser contract](pdf_source_ingestion.md) describes exact mapping,
geometry limitations, sparse-page rejection, process isolation and resource
bounds. These records support subsequent industrial corpus work; they do not
establish SME-reviewed diagnostic gold or genuine site history. Authored field
records in the next milestone must be labelled synthetic throughout the UI and
evaluation, independently of their eventual governance/publication status.
