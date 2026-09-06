#!/usr/bin/env python3
"""Validate source-only industrial gold and report ranking ceilings before runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from graphrag_prod.industrial.gold import (
    DEFAULT_GOLD_PATH, gold_report, load_gold, load_pdf_gold,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD_PATH)
    args = parser.parse_args()
    try:
        gold = load_gold(args.gold)
        report = gold_report(gold)
        expected = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if args.gold.with_name("manifest.json").read_text(encoding="utf-8") != expected:
            raise ValueError("gold manifest differs from source annotations")
        pdf_gold = load_pdf_gold(args.gold.with_name("pdf-integration.json"))
    except (ValueError, OSError, KeyError, TypeError):
        print("Industrial gold validation failed; inspect the reviewed source pins, scope and annotations.", file=sys.stderr)
        return 1
    report["pdf_integration"] = {
        "cases": len(pdf_gold["cases"]), "gold_checksum": pdf_gold["checksum"],
        "runtime_status": "NOT_RUN_EXTERNAL_CACHE_REQUIRED",
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
