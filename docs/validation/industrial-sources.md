# I1: industrial sources and PDF provenance

Development-only validation, 2026-09-06. Starting checkpoint: `fd6dd13`.

## Delivered

- Versioned industrial acceptance contract with 32 authored documents and
  300–1,000 Chunks as the next-stage target, two explicit product families,
  independent quality cases and unchanged authorization/provenance requirements.
- Eight official source catalog entries, four immutable byte/checksum pins;
  product/revision/region exclusions and OCR requirements remain explicit.
- Opt-in, isolated and bounded PDF normalization with physical page, row/cell
  geometry and exact normalized character locations. Existing text ingestion
  remains compatible. Original downloads and full normalized excerpts live in
  an external cache, not the repository.
- Repeatable acquisition/normalization commands in `docs/industrial_sources.md`
  and parser limits in `docs/pdf_source_ingestion.md`.

## Checks

Frozen implementation passed 797 unit, 16 HTTP E2E, 33 security and two regression
tests, with no skips, failures or errors. Run each suite with:

```sh
.venv/bin/python scripts/run_test_suite.py --start tests/unit --output /tmp/industrial-unit.json --require-no-skips
.venv/bin/python scripts/run_test_suite.py --start tests/e2e --output /tmp/industrial-e2e.json --require-no-skips
.venv/bin/python scripts/run_test_suite.py --start tests/security --output /tmp/industrial-security.json --require-no-skips
.venv/bin/python scripts/run_test_suite.py --start tests/regression --output /tmp/industrial-regression.json --require-no-skips
uv lock --check
.venv/bin/python scripts/validate_industrial_contract.py
.venv/bin/python scripts/fetch_industrial_sources.py --check
```

Acceptance-contract validation, compile checks and package build also passed.
This stage does not alter database persistence/query behavior; real-Neo4j
industrial lifecycle checks belong to I2. Diff and staged secret checks are
required immediately before its commit.

## Actual official-document checks

- Canalis QGH3492101-04: physical pages 30, 71, 72, 76, 77 produce 17 exact
  Chunks. Final artifact checksum:
  `c5c81c61c0064e9db91425048ee4c91a4936272b4fcaea83d11a2a2fcbf179b3`.
- EvoPacT HVX BQT34371-02: physical pages 9, 10, 14, 103, 104, 105 produce
  23 exact Chunks. Final artifact checksum:
  `a15e20ab8e29894127d0d21fa752a909b55048e4bbbd20bc7804319653b705ec`.
- Canalis commissioning tables preserve original numeric text and traceable
  cells. Continuation-page qualifications remain in their original locations.
- The Chinese 12 kV maintenance manual has outlined technical glyphs: selected
  technical pages return `OCR_REQUIRED`, rather than fabricated readable text.
- Selected actual pages were rendered and inspected. HVX merged diagnostic
  cells do not receive invented bounding boxes for empty placeholders.

## Limits carried forward

Normalization is not semantic validation or expert approval. Diagram-heavy,
borderless, multi-column and cross-page content requires review. Ordinary
chunking can divide diagnostic groups; I3 must measure and improve context
completion. I2 must durably attach source-location maps when loading sources.
The public manuals do not establish a universal busbar temperature threshold.
Chinese HVX, HVX-O and SureSeT material cannot automatically support claims about
the core up-to-24-kV HVX variant. No Schneider field data or expert certification
is implied by the forthcoming authored reference and synthetic field corpus.
