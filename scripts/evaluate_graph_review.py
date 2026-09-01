#!/usr/bin/env python3
"""Evaluate the committed Stage 4 adjudicated graph-review fixture."""

from __future__ import annotations

import argparse
from pathlib import Path

from graphrag_prod.graph import evaluate_graph_review_dataset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("evaluation/graph-review-v1.json"),
    )
    parser.add_argument("--target", type=float, default=0.95)
    args = parser.parse_args()
    metrics = evaluate_graph_review_dataset(args.dataset)
    print(f"items={metrics.item_count}")
    print(f"entity_precision={metrics.entity_precision:.4f}")
    print(f"relationship_precision={metrics.relationship_precision:.4f}")
    print(f"entity_resolution_accuracy={metrics.entity_resolution_accuracy:.4f}")
    return 0 if metrics.meets(args.target) else 1


if __name__ == "__main__":
    raise SystemExit(main())
