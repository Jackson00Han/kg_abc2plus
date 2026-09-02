#!/usr/bin/env python3
"""Compare the complete deterministic projections of two evaluation reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def projection(report: dict) -> dict:
    return {
        "case_digests": report.get("case_digests"),
        "contract_metrics": report.get("contract_metrics"),
        "diagnostics": report.get("diagnostics"),
        "identities": report.get("identities"),
        "semantic_digest": report.get("semantic_digest"),
        "suite_counts": report.get("suite_counts"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    args = parser.parse_args()
    first = json.loads(args.first.read_text(encoding="utf-8"))
    second = json.loads(args.second.read_text(encoding="utf-8"))
    if projection(first) != projection(second):
        raise SystemExit("evaluation reports are not reproducible")
    print(f"evaluation reports reproduce {first['semantic_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
