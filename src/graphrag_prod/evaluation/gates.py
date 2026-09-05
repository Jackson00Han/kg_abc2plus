"""Acceptance-contract and immutable regression-baseline gates."""

from __future__ import annotations

from typing import Any, Mapping


EVALUATION_BASELINE_SCHEMA_VERSION = "evaluation-baseline-v1"
EVALUATION_BASELINE_VERSION = "1.3.0"


def compare(operator: str, observed: int | float, target: int | float) -> bool:
    if operator == ">=":
        return observed >= target
    if operator == "<=":
        return observed <= target
    if operator == "=":
        return observed == target
    raise ValueError(f"unsupported metric operator: {operator}")


def contract_metric_rows(
    contract: Mapping[str, Any],
    observed: Mapping[str, int | float],
    *,
    performance_gates: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    definitions = contract.get("metrics")
    if not isinstance(definitions, list) or not definitions:
        raise ValueError("acceptance contract metrics are invalid")
    expected_ids = {item.get("id") for item in definitions}
    missing = sorted(expected_ids - set(observed))
    extra_contract = [value for value in expected_ids if not isinstance(value, str)]
    if missing or extra_contract:
        raise ValueError(f"contract metric observations are incomplete: {missing}")
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for definition in definitions:
        metric_id = definition["id"]
        value = observed[metric_id]
        target = definition["target"]
        passed = compare(definition["operator"], value, target)
        gating = definition["area"] != "performance" or performance_gates
        rows.append(
            {
                "area": definition["area"],
                "gating": gating,
                "id": metric_id,
                "observed": value,
                "operator": definition["operator"],
                "passed": passed,
                "target": target,
                "unit": definition["unit"],
            }
        )
        if gating and not passed:
            failures.append(
                f"{metric_id}: {value} {definition['operator']} {target} failed"
            )
    return rows, failures


def validate_policy(policy: Mapping[str, Any], profile_id: str) -> None:
    if policy.get("schema_version") != "evaluation-regression-policy-v1":
        raise ValueError("regression policy schema is invalid")
    if policy.get("profile_id") != profile_id:
        raise ValueError("regression policy profile does not match the run")
    if policy.get("baseline_policy") != "exact_deterministic_projection":
        raise ValueError("only exact deterministic baselines are supported")
    hard = policy.get("hard_invariants")
    informational = policy.get("informational_metrics")
    if not isinstance(hard, Mapping) or not hard or not isinstance(
        informational, list
    ):
        raise ValueError("regression policy rules are incomplete")


def invariant_failures(
    policy: Mapping[str, Any], observed: Mapping[str, Any]
) -> list[str]:
    failures: list[str] = []
    for metric_id, expected in policy["hard_invariants"].items():
        if metric_id not in observed:
            failures.append(f"hard invariant is missing: {metric_id}")
        elif observed[metric_id] != expected:
            failures.append(
                f"hard invariant {metric_id}: {observed[metric_id]} != {expected}"
            )
    return failures


def baseline_failures(
    baseline: Mapping[str, Any],
    *,
    profile_id: str,
    gold_version: str,
    deterministic_projection: Mapping[str, Any],
    semantic_digest: str,
) -> list[str]:
    if baseline.get("schema_version") != EVALUATION_BASELINE_SCHEMA_VERSION:
        raise ValueError("evaluation baseline schema is invalid")
    if baseline.get("version") != EVALUATION_BASELINE_VERSION:
        raise ValueError("evaluation baseline version is stale")
    if baseline.get("profile_id") != profile_id:
        raise ValueError("evaluation baseline profile does not match")
    if baseline.get("gold_version") != gold_version:
        raise ValueError("evaluation baseline gold version is stale")
    failures: list[str] = []
    if baseline.get("deterministic_projection") != deterministic_projection:
        failures.append("deterministic evaluation projection changed from baseline")
    if baseline.get("semantic_digest") != semantic_digest:
        failures.append("semantic evaluation digest changed from baseline")
    return failures


def baseline_candidate(
    *,
    profile_id: str,
    gold_version: str,
    deterministic_projection: Mapping[str, Any],
    semantic_digest: str,
    rationale: str,
) -> dict[str, Any]:
    return {
        "deterministic_projection": deterministic_projection,
        "gold_version": gold_version,
        "profile_id": profile_id,
        "rationale": rationale,
        "schema_version": EVALUATION_BASELINE_SCHEMA_VERSION,
        "semantic_digest": semantic_digest,
        "version": EVALUATION_BASELINE_VERSION,
    }
