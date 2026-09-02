#!/usr/bin/env python3
"""Ensure every required negative/security test was discovered and passed."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "evaluation" / "security-suite.v1.json",
    )
    parser.add_argument("--results", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    results = json.loads(args.results.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "security-suite-manifest-v1":
        raise SystemExit("security manifest schema is invalid")
    if results.get("schema_version") != "unittest-suite-result-v1":
        raise SystemExit("security result schema is invalid")
    required = set(manifest.get("required_test_ids", []))
    passed = set(results.get("passed_test_ids", []))
    started = set(results.get("started_test_ids", []))
    if not required:
        raise SystemExit("security manifest cannot be empty")
    missing = sorted(required - started)
    not_passed = sorted(required - passed)
    if missing or not_passed or results.get("skipped"):
        raise SystemExit(
            f"security suite incomplete: missing={missing}, "
            f"not_passed={not_passed}, skipped={results.get('skipped')}"
        )
    print(f"security suite complete: {len(required)} required tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
