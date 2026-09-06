#!/usr/bin/env python3
"""Prepare explicit page excerpts from a pinned official PDF in an external cache."""

import argparse
from pathlib import Path

from graphrag_prod.industrial.normalization import (
    normalize_industrial_source, write_normalized_source,
)
from graphrag_prod.industrial.sources import SourceAcquisitionError, acquire_source, load_source_catalog


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--catalog", type=Path, default=root / "datasets/industrial-v1/sources.json")
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--pages", required=True, help="Explicit physical pages, e.g. 30,76,77")
    parser.add_argument("--chunk-chars", type=int, default=900)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == root or root in args.output.resolve().parents:
        parser.error("official source excerpts must be written outside the repository")
    try:
        pages = tuple(int(item) for item in args.pages.split(","))
        source = load_source_catalog(args.catalog).get(args.source)
        original = acquire_source(source, args.cache_dir, excluded_roots=(root,))
        bundle = normalize_industrial_source(source, original.read_bytes(), selected_pages=pages, chunk_chars=args.chunk_chars)
        write_normalized_source(bundle, args.output)
    except (ValueError, OSError, SourceAcquisitionError) as error:
        parser.exit(2, f"Source normalization failed: {error}\n")
    print(
        f"Prepared {source.source_id}: {len(bundle['selected_pages'])} physical pages, "
        f"{len(bundle['chunks'])} exact chunks; checksum {bundle['artifact_checksum']}"
    )


if __name__ == "__main__":
    main()
