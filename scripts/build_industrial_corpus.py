#!/usr/bin/env python3
"""Rebuild/check industrial-v1 from committed authored Markdown, entirely offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from graphrag_prod.industrial.corpus import (
    DEFAULT_CORPUS_ROOT, build_corpus, corpus_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_CORPUS_ROOT)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true", help="write the deterministic source manifest")
    action.add_argument("--check", action="store_true", help="verify the committed manifest (default)")
    args = parser.parse_args()
    corpus = build_corpus(args.root)
    report = corpus_report(corpus)
    expected = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    target = args.root / "manifest.json"
    if args.write:
        target.write_text(expected, encoding="utf-8")
    elif not target.is_file() or target.read_text(encoding="utf-8") != expected:
        parser.exit(1, "Industrial corpus manifest differs; review authored changes, then rebuild with --write.\n")
    print(json.dumps({"status": "written" if args.write else "verified", "manifest_checksum": corpus.manifest_checksum, **report["counts"]}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
