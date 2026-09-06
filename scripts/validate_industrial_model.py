#!/usr/bin/env python3
"""Validate the composed industrial corpus, source editions and graph schema."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from graphrag_prod.industrial.contract import load_industrial_contract
from graphrag_prod.industrial.corpus import build_corpus
from graphrag_prod.industrial.ontology import build_industrial_tbox
from graphrag_prod.industrial.sources import load_source_catalog
from graphrag_prod.industrial.validation import validate_industrial_model


def main() -> int:
    try:
        corpus = build_corpus()
        report = validate_industrial_model(
            corpus,
            load_source_catalog(ROOT / "datasets/industrial-v1/sources.json"),
            load_industrial_contract(ROOT / "contracts/industrial_knowledge.v1.json"),
            build_industrial_tbox(corpus.tenant_id),
        )
    except (ValueError, OSError, KeyError, TypeError):
        print("Industrial model validation failed; inspect the versioned source and model inputs.", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
