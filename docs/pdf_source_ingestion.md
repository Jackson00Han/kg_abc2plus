# Bounded industrial PDF normalization

The construction parser retains its original UTF-8 text/Markdown/CSV/JSON
allowlist. `PdfDocumentParser` is an explicit plugin for an acquisition or
construction caller that opts in. It uses the documented text and ruled-table
extraction methods from [pdfplumber](https://github.com/jsvine/pdfplumber), with
no model calls, OCR, URL fetching, or execution of embedded PDF actions.

This extension does not increase the existing online source/job byte limit.
Large official manuals are acquired separately under the industrial offline
96 MiB cap and normalized into explicitly selected page derivatives; the
source-only construction input remains small and bounded.

## Exact source chain

A successful `ParsedDocument` contains:

- `original_checksum`: SHA-256 of the complete original PDF, including when
  only a page selection is normalized;
- `normalized_text` and its distinct `normalized_checksum`;
- immutable `source_locations`: exact normalized character ranges with
  physical PDF page number, kind (`page`, `text`, `table_row`, `table_cell`),
  bounding box, and one-based table/row/column indexes where applicable;
- `selected_pages`: the explicit ordered physical page selection, never
  renumbered; a full-document parse also records all selected pages;
- `parser_version` and a splitter signature that includes the page selection;
- gapless `ChunkSeed` ranges that reproduce every normalized character and
  never cross a physical page. `page_number` and `section` travel with each
  seed through existing ingestion.

Boxes use pdfplumber's `(x0, top, x1, bottom)` page coordinate system in PDF
points. They describe the extracted page, not a browser pixel viewport. Table
cells preserve extracted numbers, currencies, dates and unit strings. Empty
and merged-cell placeholders remain empty; values are never copied from an
adjacent row. An absent merged-cell placeholder preserves its tab delimiter
but has no invented cell bounding box; only the actual spanning cell is mapped.
Rows are tab-separated and terminated by a newline; internal
cell newlines are retained. The final page newline belongs to that page.

Normalization uses NFC and geometry-based word separation, not a verbatim
byte representation of PDF content streams. The pinned pdfplumber dependency
and parser version identify that algorithm. Its documented font-relative
`x_tolerance_ratio=0.15` preserves word boundaries in small-font official
manuals where a fixed 3-point gap would concatenate words and units.

The immutable map is a parsing result. Callers persisting a normalized source
must retain this map, the full original checksum, original URI and page
selection together. Existing text-only ingestion does not automatically store
PDF map metadata merely because it receives a `.txt` derivative. A derivative
must never be presented as a complete normalization of unselected pages.

## Programmatic opt-in

```python
from graphrag_prod.construction.parser import BoundedDocumentParser, ParserLimits
from graphrag_prod.construction.pdf_parser import PdfDocumentParser, PdfParserLimits

# An explicitly authorized offline acquisition, not the default online limit.
offline_bytes = 96 * 1024 * 1024
parser = BoundedDocumentParser(
    limits=ParserLimits(max_source_bytes=offline_bytes),
    plugins=(PdfDocumentParser(
        limits=PdfParserLimits(max_source_bytes=offline_bytes),
        selected_pages=(71, 72, 73),
    ),),
)
parsed = parser.parse(pdf_bytes, mime_type="application/pdf")
```

Passing `selected_pages=None` requests every page and fails if the document
exceeds the selected-page cap. Selections must be positive, sorted, unique and
in range. The parser never silently omits an unreadable selected page.
Existing plugins returning a plain `str` remain compatible. Rich plugins
return a validated `NormalizedSource` whose offsets already refer to normalized
text; they cannot rely on a later Unicode rewrite changing their offsets.

## Bounds and failure behavior

Default PDF plugin bounds are 5 MiB input, 1,000 physical document pages,
24 selected pages, 32 MiB cumulatively decoded streams, 200,000 characters per
page, 500,000 normalized characters, 10,000 table cells, 50,000 source-map
locations, 250,000 layout objects per page, and a 30-second worker deadline.
The outer `ParserLimits` remains an independent pre/post bound.

Each parse uses a separate Python process. A timeout kills and reaps the
worker. The worker has a CPU limit; Linux also applies a 1 GiB address-space
limit before loading the PDF libraries. On macOS, `RLIMIT_AS` is not reliably
implemented, so the cumulative extraction bounds and process deadline remain
active without claiming an OS-enforced address-space ceiling. In particular,
an individual decoder can allocate before a decoded-stream length check;
untrusted high-volume production processing should run this worker in the
resource-limited Linux environment. This development implementation does not
claim to be a malware sandbox.

Errors fail the whole selected derivative with a fixed category:

| Category | Meaning |
| --- | --- |
| `PDF_MALFORMED` | Invalid envelope, broken structure, or unsafe extraction failure |
| `PDF_ENCRYPTED` | Password protection/encryption is unsupported |
| `PDF_PAGE_SELECTION` | A requested physical page does not exist |
| `PDF_LIMIT` | A configured resource bound was exceeded |
| `PDF_TIMEOUT` | Worker exceeded its deadline |
| `PDF_WORKER_FAILED` | Worker crashed, was killed, or returned an invalid result |
| `PDF_TABLE_AMBIGUOUS` | Overlapping detected cells claim the same text |
| `OCR_REQUIRED` | At least one selected page has insufficient reliable machine-readable text |

Text-sparse pages fail conservatively: fewer than 24 alphanumeric characters,
a dominant page image plus fewer than 120 alphanumeric characters, or unresolved
`(cid:...)` glyph text. Blank pages also require an explicit acquisition decision;
they are not silently dropped. OCR and diagram interpretation require a later
reviewed workflow. A text-rich page can still contain a diagram that this parser
does not understand. Ruled tables get row/cell mapping; borderless tables and
complex multi-column layouts require human QA and are not asserted to have
reconstructed semantics. These distinctions must remain visible to a caller.
The existing character splitter can divide a long table row or merged diagnosis
group. It does not copy table headers, repeat merged symptoms, join tables across
pages, or supply missing diagnostic context. The source map enables later
context completion; a successful parse alone does not demonstrate retrieval
completeness for such a table.

## Repeatable checks

```sh
uv run --locked python -m unittest discover -s tests/unit -p 'test_pdf_parser.py'
uv run --locked python -m unittest discover -s tests/unit -p 'test_construction_parser.py'
```

The PDF tests build tiny deterministic fixtures entirely in memory. They cover
multipage provenance, source selection, exact chunks, table numbers/units and
boxes, determinism, malformed/encrypted/sparse pages, compressed-stream and
other resource caps, deadlines, and strict source-map validation. No official
PDF, generated cache, or provider call is required by routine tests.

Manual acquisition QA for the industrial round additionally uses the original
Canalis `QGH3492101-04` and the Chinese `PKR2441601` guide, retained outside the
repository. Canalis physical pages 71–73 are compared against rendered page
images; Chinese guide physical page 2 must return `OCR_REQUIRED` instead of
publishing its sparse text layer as a complete source. Exact run evidence is
recorded with the industrial milestone validation.
