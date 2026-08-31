"""Validate the production-candidate acceptance contract using stdlib only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "contracts" / "acceptance.v1.json"

REQUIRED_QUESTION_CLASSES = {
    "single_chunk",
    "cross_chunk",
    "graph_relationship",
    "exact_value",
    "temporal_conflict",
    "unanswerable",
    "unauthorized",
}
REQUIRED_METRIC_AREAS = {
    "graph",
    "retrieval",
    "answer",
    "ingestion",
    "security",
    "reliability",
    "performance",
}
VALID_OPERATORS = {">=", "<=", "="}


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    """Load a JSON contract from disk."""
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def validate_contract(contract: dict[str, Any]) -> list[str]:
    """Return human-readable validation errors; an empty list means valid."""
    errors: list[str] = []

    for field in ("contract_version", "target_milestone", "owner", "scope"):
        if not contract.get(field):
            errors.append(f"missing required top-level field: {field}")

    datasets = contract.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        errors.append("datasets must be a non-empty list")
        datasets = []

    dataset_ids: set[str] = set()
    for index, dataset in enumerate(datasets):
        prefix = f"datasets[{index}]"
        dataset_id = dataset.get("id")
        if not dataset_id:
            errors.append(f"{prefix} is missing id")
        elif dataset_id in dataset_ids:
            errors.append(f"duplicate dataset id: {dataset_id}")
        else:
            dataset_ids.add(dataset_id)
        if not dataset.get("owner"):
            errors.append(f"{prefix} is missing owner")
        if not dataset.get("kind"):
            errors.append(f"{prefix} is missing kind")
        if not isinstance(dataset.get("minimum_items"), int) or dataset.get(
            "minimum_items", 0
        ) <= 0:
            errors.append(f"{prefix} minimum_items must be a positive integer")
        if dataset.get("versioned") is not True:
            errors.append(f"{prefix} must be versioned")

    question_classes = contract.get("question_classes")
    if not isinstance(question_classes, list):
        errors.append("question_classes must be a list")
        question_classes = []

    question_ids: set[str] = set()
    for index, question_class in enumerate(question_classes):
        prefix = f"question_classes[{index}]"
        question_id = question_class.get("id")
        if not question_id:
            errors.append(f"{prefix} is missing id")
        elif question_id in question_ids:
            errors.append(f"duplicate question class id: {question_id}")
        else:
            question_ids.add(question_id)
        for count_field in ("minimum_success_cases", "minimum_boundary_cases"):
            value = question_class.get(count_field)
            if not isinstance(value, int) or value <= 0:
                errors.append(f"{prefix} {count_field} must be a positive integer")

    missing_classes = REQUIRED_QUESTION_CLASSES - question_ids
    extra_classes = question_ids - REQUIRED_QUESTION_CLASSES
    if missing_classes:
        errors.append(f"missing question classes: {sorted(missing_classes)}")
    if extra_classes:
        errors.append(f"unknown question classes: {sorted(extra_classes)}")

    metrics = contract.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        errors.append("metrics must be a non-empty list")
        metrics = []

    metric_ids: set[str] = set()
    metric_areas: set[str] = set()
    for index, metric in enumerate(metrics):
        prefix = f"metrics[{index}]"
        metric_id = metric.get("id")
        if not metric_id:
            errors.append(f"{prefix} is missing id")
        elif metric_id in metric_ids:
            errors.append(f"duplicate metric id: {metric_id}")
        else:
            metric_ids.add(metric_id)

        area = metric.get("area")
        if not area:
            errors.append(f"{prefix} is missing area")
        else:
            metric_areas.add(area)

        if metric.get("operator") not in VALID_OPERATORS:
            errors.append(f"{prefix} has an invalid operator")
        target = metric.get("target")
        if not isinstance(target, (int, float)) or isinstance(target, bool):
            errors.append(f"{prefix} target must be numeric")
        for field in ("unit", "method", "dataset_owner"):
            if not metric.get(field):
                errors.append(f"{prefix} is missing {field}")
        if metric.get("dataset") not in dataset_ids:
            errors.append(f"{prefix} references an unknown dataset")

    missing_areas = REQUIRED_METRIC_AREAS - metric_areas
    if missing_areas:
        errors.append(f"missing metric areas: {sorted(missing_areas)}")

    scope = contract.get("scope", {})
    for field in (
        "authoritative_input",
        "domain",
        "tenancy",
        "authorization",
        "minimum_validation_chunks",
        "retrieval_concurrency",
    ):
        if not scope.get(field):
            errors.append(f"scope is missing {field}")

    return errors


def main() -> int:
    contract = load_contract()
    errors = validate_contract(contract)
    if errors:
        print("Acceptance contract is invalid:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(
        "Acceptance contract is valid: "
        f"{len(contract['question_classes'])} question classes, "
        f"{len(contract['metrics'])} measurable targets."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

