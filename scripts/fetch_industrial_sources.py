"""Validate the pinned industrial catalog or fetch one official PDF safely."""

from __future__ import annotations

import argparse
from pathlib import Path

from graphrag_prod.industrial.sources import (
    SourceAcquisitionError,
    SourceCatalogError,
    acquire_source,
    load_source_catalog,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "datasets" / "industrial-v1" / "sources.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate committed metadata without network access")
    mode.add_argument("--source", help="allowlisted source ID from the committed catalog")
    parser.add_argument("--cache-dir", type=Path, help="explicit external directory for untracked original PDFs")
    args = parser.parse_args()
    if args.source and args.cache_dir is None:
        parser.error("--source requires --cache-dir outside the repository")
    if args.check and args.cache_dir is not None:
        parser.error("--check does not use a cache directory")
    try:
        catalog = load_source_catalog(CATALOG_PATH)
        if args.check:
            pinned = sum(source.sha256 is not None for source in catalog.sources)
            print(f"Catalog {catalog.version}: {len(catalog.sources)} sources, {pinned} pinned originals; offline check passed.")
        else:
            result = acquire_source(catalog.get(args.source), args.cache_dir, excluded_roots=(ROOT,))
            print(f"Verified source: {result}")
    except (SourceCatalogError, SourceAcquisitionError, OSError) as error:
        parser.exit(1, f"Acquisition/check failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
