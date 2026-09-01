"""Validate and resolve production-reference or local development profiles."""

from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "contracts" / "acceptance.v1.json"
PROFILE_PATHS = {
    "dev-mini": ROOT / "contracts" / "profiles" / "dev-mini.v1.json",
    "production-reference": (
        ROOT / "contracts" / "profiles" / "production-reference.v1.json"
    ),
}
DEFAULT_PROFILE_ID = "dev-mini"

REQUIRED_QUESTION_CLASSES = {
    "single_chunk",
    "cross_chunk",
    "graph_relationship",
    "exact_value",
    "temporal_conflict",
    "unanswerable",
    "unauthorized",
}
REQUIRED_METRIC_IDS = {
    "entity_precision",
    "relationship_precision",
    "entity_resolution_accuracy",
    "recall_at_5",
    "mrr",
    "ndcg_at_5",
    "unauthorized_exposure_count",
    "supported_claim_rate",
    "citation_precision",
    "citation_coverage",
    "numerical_fidelity",
    "refusal_f1",
    "ingestion_success_rate",
    "idempotency_mismatch_count",
    "deletion_residue_count",
    "recovery_success_rate",
    "retrieval_p95_ms",
    "answer_p95_ms",
    "retrieval_throughput_rps",
    "server_error_rate",
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
SCALE_SCOPE_FIELDS = {
    "maximum_document_bytes",
    "expected_daily_version_changes",
    "burst_version_changes_per_minute",
    "minimum_validation_chunks",
    "retrieval_concurrency",
}
PROFILE_FIELDS = {
    "profile_version",
    "profile_id",
    "base_contract_version",
    "purpose",
    "production_candidate_eligible",
    "overrides",
    "execution",
    "metric_policy",
}
OVERRIDE_FIELDS = {"scope", "datasets", "question_classes"}
QUESTION_QUOTA_FIELDS = {
    "minimum_success_cases",
    "minimum_boundary_cases",
}
EXECUTION_FIELDS = {
    "answer_latency_samples",
    "sustained_load_seconds",
    "neo4j",
}
LOCAL_NEO4J_FIELDS = {
    "mode",
    "container_memory_mb",
    "container_cpus",
    "heap_initial_mb",
    "heap_max_mb",
    "pagecache_mb",
    "readiness_timeout_seconds",
}
METRIC_POLICY_FIELDS = {
    "thresholds",
    "quality_results",
    "performance_results",
}
EXPECTED_PROFILE_VERSION = "1.0.0"
EXPECTED_PROFILE_PURPOSES = {
    "dev-mini": "local_development_smoke",
    "production-reference": "production_candidate_validation",
}
DEV_SCALE_MAXIMUMS = {
    "maximum_document_bytes": 262144,
    "expected_daily_version_changes": 5,
    "burst_version_changes_per_minute": 2,
    "minimum_validation_chunks": 100,
    "retrieval_concurrency": 2,
}
DEV_DATASET_MAXIMUMS = {
    "gold-v1": 14,
    "graph-review-v1": 14,
    "load-v1": 100,
}
DEV_EXECUTION_MAXIMUMS = {
    "answer_latency_samples": 5,
    "sustained_load_seconds": 30,
}
DEV_NEO4J_MAXIMUMS = {
    "container_memory_mb": 1536,
    "container_cpus": 2,
    "heap_initial_mb": 256,
    "heap_max_mb": 512,
    "pagecache_mb": 128,
    "readiness_timeout_seconds": 120,
}
PRODUCTION_EXECUTION_MINIMUMS = {
    "answer_latency_samples": 30,
    "sustained_load_seconds": 300,
}


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def load_contract(path: Path = DEFAULT_CONTRACT) -> dict[str, Any]:
    """Load a JSON contract from disk."""
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_profile(profile_id: str = DEFAULT_PROFILE_ID) -> dict[str, Any]:
    """Load one named validation profile."""
    if not isinstance(profile_id, str) or profile_id not in PROFILE_PATHS:
        raise ValueError(f"unknown validation profile: {profile_id!r}")
    path = PROFILE_PATHS[profile_id]
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_profiles() -> dict[str, dict[str, Any]]:
    """Load every repository-owned validation profile by stable ID."""
    return {profile_id: load_profile(profile_id) for profile_id in PROFILE_PATHS}


def validate_contract(contract: dict[str, Any]) -> list[str]:
    """Return human-readable validation errors; an empty list means valid."""
    if not isinstance(contract, dict):
        return ["contract must be an object"]
    errors: list[str] = []

    for field in ("contract_version", "target_milestone", "owner"):
        if not _nonempty_text(contract.get(field)):
            errors.append(f"missing required top-level field: {field}")

    datasets = contract.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        errors.append("datasets must be a non-empty list")
        datasets = []

    dataset_ids: set[str] = set()
    dataset_owners: dict[str, str] = {}
    for index, dataset in enumerate(datasets):
        prefix = f"datasets[{index}]"
        if not isinstance(dataset, dict):
            errors.append(f"{prefix} must be an object")
            continue
        dataset_id = dataset.get("id")
        if not _nonempty_text(dataset_id):
            errors.append(f"{prefix} is missing id")
            dataset_id = None
        elif dataset_id in dataset_ids:
            errors.append(f"duplicate dataset id: {dataset_id}")
        else:
            dataset_ids.add(dataset_id)
        if not _nonempty_text(dataset.get("owner")):
            errors.append(f"{prefix} is missing owner")
        elif dataset_id:
            dataset_owners[dataset_id] = dataset["owner"]
        if not _nonempty_text(dataset.get("kind")):
            errors.append(f"{prefix} is missing kind")
        if not _positive_int(dataset.get("minimum_items")):
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
        if not isinstance(question_class, dict):
            errors.append(f"{prefix} must be an object")
            continue
        question_id = question_class.get("id")
        if not _nonempty_text(question_id):
            errors.append(f"{prefix} is missing id")
            question_id = None
        elif question_id in question_ids:
            errors.append(f"duplicate question class id: {question_id}")
        else:
            question_ids.add(question_id)
        for count_field in ("minimum_success_cases", "minimum_boundary_cases"):
            value = question_class.get(count_field)
            if not _positive_int(value):
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
        if not isinstance(metric, dict):
            errors.append(f"{prefix} must be an object")
            continue
        metric_id = metric.get("id")
        if not _nonempty_text(metric_id):
            errors.append(f"{prefix} is missing id")
            metric_id = None
        elif metric_id in metric_ids:
            errors.append(f"duplicate metric id: {metric_id}")
        else:
            metric_ids.add(metric_id)

        area = metric.get("area")
        if not _nonempty_text(area):
            errors.append(f"{prefix} is missing area")
        else:
            metric_areas.add(area)

        operator = metric.get("operator")
        if not isinstance(operator, str) or operator not in VALID_OPERATORS:
            errors.append(f"{prefix} has an invalid operator")
        target = metric.get("target")
        if not _finite_number(target):
            errors.append(f"{prefix} target must be numeric")
        for field in ("unit", "method", "dataset_owner"):
            if not _nonempty_text(metric.get(field)):
                errors.append(f"{prefix} is missing {field}")
        dataset_reference = metric.get("dataset")
        if not _nonempty_text(dataset_reference):
            errors.append(f"{prefix} references an unknown dataset")
        elif dataset_reference not in dataset_ids:
            errors.append(f"{prefix} references an unknown dataset")
        elif (
            metric.get("dataset_owner")
            != dataset_owners.get(dataset_reference)
        ):
            errors.append(f"{prefix} dataset_owner does not match its dataset")
        if metric.get("unit") == "ratio" and isinstance(target, (int, float)):
            if isinstance(target, bool) or not 0 <= target <= 1:
                errors.append(f"{prefix} ratio target must be between zero and one")

    missing_metrics = REQUIRED_METRIC_IDS - metric_ids
    extra_metrics = metric_ids - REQUIRED_METRIC_IDS
    if missing_metrics:
        errors.append(f"missing metrics: {sorted(missing_metrics)}")
    if extra_metrics:
        errors.append(f"unknown metrics: {sorted(extra_metrics)}")

    missing_areas = REQUIRED_METRIC_AREAS - metric_areas
    extra_areas = metric_areas - REQUIRED_METRIC_AREAS
    if missing_areas:
        errors.append(f"missing metric areas: {sorted(missing_areas)}")
    if extra_areas:
        errors.append(f"unknown metric areas: {sorted(extra_areas)}")

    scope = contract.get("scope", {})
    if not isinstance(scope, dict):
        errors.append("scope must be an object")
        scope = {}
    for field in (
        "authoritative_input",
        "domain",
        "tenancy",
        "authorization",
    ):
        if not _nonempty_text(scope.get(field)):
            errors.append(f"scope is missing {field}")
    for field in SCALE_SCOPE_FIELDS:
        if field not in scope:
            errors.append(f"scope is missing {field}")
        elif not _positive_int(scope[field]):
            errors.append(f"scope {field} must be a positive integer")

    return errors


def _resolve_validation_profile_unchecked(
    contract: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Apply only validated, scale-only fields to a deep copy."""
    effective = copy.deepcopy(contract)
    overrides = profile.get("overrides", {})
    for field in SCALE_SCOPE_FIELDS:
        if field in overrides.get("scope", {}):
            effective["scope"][field] = overrides["scope"][field]

    datasets = {item["id"]: item for item in effective["datasets"]}
    for dataset_id, values in overrides.get("datasets", {}).items():
        if dataset_id in datasets and "minimum_items" in values:
            datasets[dataset_id]["minimum_items"] = values["minimum_items"]

    quotas = overrides.get("question_classes", {})
    for question_class in effective["question_classes"]:
        for field in QUESTION_QUOTA_FIELDS:
            if field in quotas:
                question_class[field] = quotas[field]

    effective["validation_profile"] = {
        "profile_version": profile.get("profile_version"),
        "profile_id": profile.get("profile_id"),
        "purpose": profile.get("purpose"),
        "production_candidate_eligible": profile.get(
            "production_candidate_eligible"
        ),
        "execution": copy.deepcopy(profile.get("execution")),
        "metric_policy": copy.deepcopy(profile.get("metric_policy")),
    }
    return effective


def validate_profile(
    contract: dict[str, Any],
    profile: dict[str, Any],
) -> list[str]:
    """Validate one fail-closed, scale-only profile overlay."""
    errors: list[str] = []
    if not isinstance(profile, dict):
        return ["profile must be an object"]
    if validate_contract(contract):
        return ["base contract is invalid; profile cannot be validated"]

    raw_profile_id = profile.get("profile_id")
    profile_id = raw_profile_id if isinstance(raw_profile_id, str) else "<invalid>"
    prefix = f"profile[{profile_id}]"
    missing_fields = PROFILE_FIELDS - set(profile)
    unknown_fields = set(profile) - PROFILE_FIELDS
    if missing_fields:
        errors.append(f"{prefix} missing fields: {sorted(missing_fields)}")
    if unknown_fields:
        errors.append(f"{prefix} unknown fields: {sorted(unknown_fields)}")
    if not isinstance(raw_profile_id, str) or raw_profile_id not in PROFILE_PATHS:
        errors.append(f"{prefix} has an unknown profile_id")
    for field in ("profile_version", "profile_id", "purpose"):
        if not isinstance(profile.get(field), str) or not profile.get(
            field, ""
        ).strip():
            errors.append(f"{prefix} {field} must be non-empty text")
    if profile.get("base_contract_version") != contract.get("contract_version"):
        errors.append(f"{prefix} base_contract_version does not match contract")
    if profile.get("profile_version") != EXPECTED_PROFILE_VERSION:
        errors.append(f"{prefix} profile_version is unsupported")
    if profile.get("purpose") != EXPECTED_PROFILE_PURPOSES.get(profile_id):
        errors.append(f"{prefix} purpose does not match its profile ID")
    eligible = profile.get("production_candidate_eligible")
    if not isinstance(eligible, bool):
        errors.append(f"{prefix} production_candidate_eligible must be boolean")

    overrides = profile.get("overrides")
    if not isinstance(overrides, dict):
        errors.append(f"{prefix} overrides must be an object")
        overrides = {}
    missing_override_fields = OVERRIDE_FIELDS - set(overrides)
    unknown_override_fields = set(overrides) - OVERRIDE_FIELDS
    if missing_override_fields:
        errors.append(
            f"{prefix} missing override sections: {sorted(missing_override_fields)}"
        )
    if unknown_override_fields:
        errors.append(
            f"{prefix} unknown override sections: {sorted(unknown_override_fields)}"
        )

    scope_overrides = overrides.get("scope", {})
    if not isinstance(scope_overrides, dict):
        errors.append(f"{prefix} scope overrides must be an object")
        scope_overrides = {}
    for field, value in scope_overrides.items():
        if field not in SCALE_SCOPE_FIELDS:
            errors.append(f"{prefix} cannot override semantic scope field: {field}")
            continue
        if not _positive_int(value):
            errors.append(f"{prefix} scope {field} must be a positive integer")
        elif value > contract["scope"][field]:
            errors.append(f"{prefix} scope {field} exceeds the production reference")

    base_datasets = {item["id"]: item for item in contract.get("datasets", [])}
    dataset_overrides = overrides.get("datasets", {})
    if not isinstance(dataset_overrides, dict):
        errors.append(f"{prefix} dataset overrides must be an object")
        dataset_overrides = {}
    for dataset_id, values in dataset_overrides.items():
        if dataset_id not in base_datasets:
            errors.append(f"{prefix} references unknown dataset: {dataset_id}")
            continue
        if not isinstance(values, dict) or set(values) != {"minimum_items"}:
            errors.append(
                f"{prefix} dataset {dataset_id} may only override minimum_items"
            )
            continue
        value = values["minimum_items"]
        if not _positive_int(value):
            errors.append(
                f"{prefix} dataset {dataset_id} minimum_items must be positive"
            )
        elif value > base_datasets[dataset_id]["minimum_items"]:
            errors.append(
                f"{prefix} dataset {dataset_id} exceeds the production reference"
            )

    question_overrides = overrides.get("question_classes", {})
    if not isinstance(question_overrides, dict):
        errors.append(f"{prefix} question overrides must be an object")
        question_overrides = {}
    unknown_question_fields = set(question_overrides) - QUESTION_QUOTA_FIELDS
    if unknown_question_fields:
        errors.append(
            f"{prefix} unknown question quota fields: {sorted(unknown_question_fields)}"
        )
    for field, value in question_overrides.items():
        if field not in QUESTION_QUOTA_FIELDS:
            continue
        if not _positive_int(value):
            errors.append(f"{prefix} {field} must be a positive integer")
        elif value > min(item[field] for item in contract["question_classes"]):
            errors.append(f"{prefix} {field} exceeds the production reference")

    execution = profile.get("execution")
    if not isinstance(execution, dict):
        errors.append(f"{prefix} execution must be an object")
        execution = {}
    if set(execution) != EXECUTION_FIELDS:
        errors.append(f"{prefix} execution fields must be {sorted(EXECUTION_FIELDS)}")
    for field in ("answer_latency_samples", "sustained_load_seconds"):
        if not _positive_int(execution.get(field)):
            errors.append(f"{prefix} execution {field} must be a positive integer")

    neo4j = execution.get("neo4j", {})
    if not isinstance(neo4j, dict):
        errors.append(f"{prefix} execution neo4j must be an object")
        neo4j = {}
    mode = neo4j.get("mode")
    if mode == "local_capped":
        if set(neo4j) != LOCAL_NEO4J_FIELDS:
            errors.append(
                f"{prefix} local Neo4j fields must be {sorted(LOCAL_NEO4J_FIELDS)}"
            )
        for field in LOCAL_NEO4J_FIELDS - {"mode"}:
            if not _positive_int(neo4j.get(field)):
                errors.append(f"{prefix} Neo4j {field} must be a positive integer")
        if all(
            _positive_int(neo4j.get(field))
            for field in (
                "container_memory_mb",
                "heap_initial_mb",
                "heap_max_mb",
                "pagecache_mb",
            )
        ):
            if neo4j["heap_initial_mb"] > neo4j["heap_max_mb"]:
                errors.append(f"{prefix} Neo4j initial heap exceeds max heap")
            if (
                neo4j["heap_max_mb"] + neo4j["pagecache_mb"]
                > neo4j["container_memory_mb"]
            ):
                errors.append(f"{prefix} Neo4j configured memory exceeds its cap")
    elif mode == "deployment_sized":
        if set(neo4j) != {"mode"}:
            errors.append(f"{prefix} deployment-sized Neo4j must not guess a cap")
    else:
        errors.append(f"{prefix} has an invalid Neo4j mode")

    metric_policy = profile.get("metric_policy")
    if not isinstance(metric_policy, dict):
        errors.append(f"{prefix} metric_policy must be an object")
        metric_policy = {}
    if set(metric_policy) != METRIC_POLICY_FIELDS:
        errors.append(
            f"{prefix} metric policy fields must be {sorted(METRIC_POLICY_FIELDS)}"
        )
    if metric_policy.get("thresholds") != "inherited_unchanged":
        errors.append(f"{prefix} metric thresholds cannot be overridden")
    quality_results = metric_policy.get("quality_results")
    if not isinstance(quality_results, str) or quality_results not in {
        "smoke_only",
        "gate",
    }:
        errors.append(f"{prefix} has an invalid quality-results policy")
    performance_results = metric_policy.get("performance_results")
    if not isinstance(performance_results, str) or performance_results not in {
        "informational_only",
        "gate",
    }:
        errors.append(f"{prefix} has an invalid performance-results policy")

    if profile_id == "production-reference":
        if eligible is not True:
            errors.append(f"{prefix} must be production-candidate eligible")
        if any(overrides.get(section) for section in OVERRIDE_FIELDS):
            errors.append(f"{prefix} must preserve every production reference value")
        if metric_policy.get("quality_results") != "gate" or metric_policy.get(
            "performance_results"
        ) != "gate":
            errors.append(f"{prefix} must gate quality and performance")
    elif eligible is not False:
        errors.append(f"{prefix} reduced profiles cannot be production eligible")

    if profile_id == "dev-mini":
        if mode != "local_capped":
            errors.append(f"{prefix} must use local_capped Neo4j resources")
        if metric_policy.get("quality_results") != "smoke_only":
            errors.append(f"{prefix} quality results must be smoke_only")
        if metric_policy.get("performance_results") != "informational_only":
            errors.append(
                f"{prefix} performance results must be informational_only"
            )
        for field, maximum in DEV_SCALE_MAXIMUMS.items():
            value = scope_overrides.get(field)
            if value is None:
                errors.append(f"{prefix} must bound scope {field}")
            elif _positive_int(value) and value > maximum:
                errors.append(f"{prefix} scope {field} exceeds the dev-mini cap")
        for dataset_id, maximum in DEV_DATASET_MAXIMUMS.items():
            values = dataset_overrides.get(dataset_id)
            value = values.get("minimum_items") if isinstance(values, dict) else None
            if value is None:
                errors.append(f"{prefix} must bound dataset {dataset_id}")
            elif _positive_int(value) and value > maximum:
                errors.append(f"{prefix} dataset {dataset_id} exceeds the dev-mini cap")
        for field in QUESTION_QUOTA_FIELDS:
            if question_overrides.get(field) != 1:
                errors.append(f"{prefix} {field} must equal one")
        for field, maximum in DEV_EXECUTION_MAXIMUMS.items():
            value = execution.get(field)
            if _positive_int(value) and value > maximum:
                errors.append(f"{prefix} execution {field} exceeds the dev-mini cap")
        for field, maximum in DEV_NEO4J_MAXIMUMS.items():
            value = neo4j.get(field)
            if _positive_int(value) and value > maximum:
                errors.append(f"{prefix} Neo4j {field} exceeds the dev-mini cap")
    elif profile_id == "production-reference":
        if mode != "deployment_sized":
            errors.append(f"{prefix} must use deployment_sized Neo4j resources")
        for field, minimum in PRODUCTION_EXECUTION_MINIMUMS.items():
            value = execution.get(field)
            if not _positive_int(value) or value < minimum:
                errors.append(
                    f"{prefix} execution {field} is below the production reference"
                )

    if not errors:
        effective = _resolve_validation_profile_unchecked(contract, profile)
        if effective["metrics"] != contract["metrics"]:
            errors.append(f"{prefix} changed metric definitions or thresholds")
        dataset_counts = {
            item["id"]: item["minimum_items"] for item in effective["datasets"]
        }
        required_gold = sum(
            item["minimum_success_cases"] + item["minimum_boundary_cases"]
            for item in effective["question_classes"]
        )
        if dataset_counts.get("gold-v1", 0) < required_gold:
            errors.append(f"{prefix} gold-v1 is smaller than its question quotas")
        if dataset_counts.get("load-v1", 0) < effective["scope"].get(
            "minimum_validation_chunks", 0
        ):
            errors.append(f"{prefix} load-v1 is smaller than the validation corpus")
    return errors


def resolve_validation_profile(
    contract: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """Resolve a validated scale-only profile or fail before applying it."""
    contract_errors = validate_contract(contract)
    profile_errors = [] if contract_errors else validate_profile(contract, profile)
    errors = [*contract_errors, *profile_errors]
    if errors:
        raise ValueError("invalid validation profile: " + "; ".join(errors))
    return _resolve_validation_profile_unchecked(contract, profile)


def validate_profiles(
    contract: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
) -> list[str]:
    """Validate the complete repository profile set and default relationship."""
    if validate_contract(contract):
        return ["base contract is invalid; profiles cannot be validated"]
    if not isinstance(profiles, dict):
        return ["profiles must be an object keyed by profile ID"]
    errors: list[str] = []
    missing = set(PROFILE_PATHS) - set(profiles)
    extra = set(profiles) - set(PROFILE_PATHS)
    if missing:
        errors.append(f"missing validation profiles: {sorted(missing)}")
    if extra:
        errors.append(f"unknown validation profiles: {sorted(extra)}")
    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict):
            errors.append(f"profile[{profile_id}] must be an object")
            continue
        if profile.get("profile_id") != profile_id:
            errors.append(f"profile filename key does not match ID: {profile_id}")
        errors.extend(validate_profile(contract, profile))

    dev_profile = profiles.get("dev-mini", {})
    production_profile = profiles.get("production-reference", {})
    dev = dev_profile.get("execution", {}) if isinstance(dev_profile, dict) else {}
    production = (
        production_profile.get("execution", {})
        if isinstance(production_profile, dict)
        else {}
    )
    for field in ("answer_latency_samples", "sustained_load_seconds"):
        if _positive_int(dev.get(field)) and _positive_int(production.get(field)):
            if dev[field] > production[field]:
                errors.append(f"dev-mini execution {field} exceeds production")
    if DEFAULT_PROFILE_ID not in profiles:
        errors.append("default validation profile is missing")
    return errors


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILE_PATHS),
        default=DEFAULT_PROFILE_ID,
        help="validation workload profile (default: dev-mini)",
    )
    args = parser.parse_args(argv)
    try:
        contract = load_contract()
    except (OSError, json.JSONDecodeError) as error:
        print(f"Acceptance contract could not be loaded: {error}")
        return 1
    errors = validate_contract(contract)
    if errors:
        print("Acceptance contract or profile is invalid:")
        for error in errors:
            print(f"- {error}")
        return 1
    try:
        profiles = load_profiles()
    except (OSError, json.JSONDecodeError, ValueError) as error:
        print(f"Acceptance profiles could not be loaded: {error}")
        return 1
    errors.extend(validate_profiles(contract, profiles))
    if errors:
        print("Acceptance contract or profile is invalid:")
        for error in errors:
            print(f"- {error}")
        return 1
    effective = resolve_validation_profile(contract, profiles[args.profile])
    profile = effective["validation_profile"]
    print(
        "Acceptance contract and profiles are valid: "
        f"active={args.profile}, "
        f"production_candidate_eligible={profile['production_candidate_eligible']}, "
        f"{len(contract['question_classes'])} question classes, "
        f"{len(contract['metrics'])} measurable targets, "
        f"{effective['scope']['minimum_validation_chunks']} validation chunks."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
