#!/usr/bin/env python3
"""Create a source-text-free, tenant-scoped graph quality report."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path

import neo4j

from graphrag_prod.graph import Neo4jGraphQualityService, load_governance_policy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--policy-id", required=True)
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--sample-seed", default="graph-review-v1")
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--policy-catalog",
        type=Path,
        default=Path("contracts/graph_governance.v1.json"),
    )
    args = parser.parse_args()
    required = ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        parser.error(f"missing required environment variables: {', '.join(missing)}")
    generated_at = datetime.fromisoformat(args.generated_at)
    policy = load_governance_policy(args.policy_catalog, args.policy_id)
    driver = neo4j.GraphDatabase.driver(
        os.environ["NEO4J_URI"],
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )
    try:
        report = Neo4jGraphQualityService(
            driver,
            os.getenv("NEO4J_DATABASE", "neo4j"),
        ).audit(
            args.tenant_id,
            policy,
            generated_at=generated_at,
            sample_seed=args.sample_seed,
            sample_size=args.sample_size,
        )
    finally:
        driver.close()
    args.output.write_text(report.to_json() + "\n", encoding="utf-8")
    print(f"wrote {args.output} ({'PASS' if report.passed else 'FAIL'})")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
