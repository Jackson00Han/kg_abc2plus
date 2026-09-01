#!/usr/bin/env python3
"""Evaluate the versioned Stage 5 retrieval regression dataset."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from graphrag_prod.retrieval.metrics import evaluate_retrieval_dataset


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=ROOT / "evaluation" / "retrieval-gold-v1.json",
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=ROOT / "contracts" / "acceptance.v1.json",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = evaluate_retrieval_dataset(args.dataset)
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    targets = {item["id"]: item for item in contract["metrics"]}
    observed = {
        "recall_at_5": result.recall_at_5,
        "mrr": result.mrr,
        "ndcg_at_5": result.ndcg_at_5,
        "unauthorized_exposure_count": result.unauthorized_exposure_count,
    }
    failures: list[str] = []
    for metric_id, value in observed.items():
        metric = targets[metric_id]
        target = metric["target"]
        operator = metric["operator"]
        passed = {
            ">=": value >= target,
            "<=": value <= target,
            "=": value == target,
        }[operator]
        if not passed:
            failures.append(f"{metric_id}: {value} {operator} {target} failed")
    print(f"items={result.item_count}")
    print(f"answerable_items={result.answerable_count}")
    print(f"recall_at_5={result.recall_at_5:.4f}")
    print(f"mrr={result.mrr:.4f}")
    print(f"ndcg_at_5={result.ndcg_at_5:.4f}")
    print(f"unauthorized_exposure_count={result.unauthorized_exposure_count}")
    if failures:
        raise SystemExit("; ".join(failures))


if __name__ == "__main__":
    main()
