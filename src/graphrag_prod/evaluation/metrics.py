"""Hand-computable graph, operational, latency, and cost metrics."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
from numbers import Real
from typing import Any, Mapping, Sequence

from .models import GraphEvaluationMetrics, OperationalMetrics


def _finite_nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return number


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def nearest_rank_percentile(samples: Sequence[int | float], percentile: float) -> float:
    """Return the nearest-rank percentile: ``ceil(p*n) - 1`` after sorting."""

    if not samples:
        raise ValueError("percentile samples must not be empty")
    if isinstance(percentile, bool) or not isinstance(percentile, Real):
        raise ValueError("percentile must be in (0, 1]")
    checked_percentile = float(percentile)
    if not math.isfinite(checked_percentile) or not 0 < checked_percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    checked = sorted(
        _finite_nonnegative(value, "latency sample") for value in samples
    )
    index = math.ceil(checked_percentile * len(checked)) - 1
    return checked[index]


def _indexed(items: Sequence[Mapping[str, Any]], name: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"{name}[{index}] must be an object")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip() or item_id in result:
            raise ValueError(f"{name} IDs must be unique and non-empty")
        result[item_id] = item
    return result


def evaluate_graph_results(
    gold_items: Sequence[Mapping[str, Any]],
    actual_items: Sequence[Mapping[str, Any]],
) -> GraphEvaluationMetrics:
    """Evaluate independently stored graph adjudication and actual decisions."""

    gold = _indexed(gold_items, "graph gold")
    actual = _indexed(actual_items, "graph results")
    if set(gold) != set(actual):
        missing = sorted(set(gold) - set(actual))
        extra = sorted(set(actual) - set(gold))
        raise ValueError(f"graph result coverage mismatch: missing={missing}, extra={extra}")

    accepted_entities: list[bool] = []
    accepted_relationships: list[bool] = []
    resolution_correct: list[bool] = []
    case_correct: list[bool] = []
    for item_id, expected_item in gold.items():
        actual_item = actual[item_id]
        kind = expected_item.get("kind")
        if kind != actual_item.get("kind"):
            raise ValueError(f"graph result {item_id} kind does not match gold")
        expected = expected_item.get("expected")
        if not isinstance(expected, Mapping):
            raise ValueError(f"graph gold {item_id} requires expected labels")
        if kind == "entity":
            if set(actual_item) != {"id", "kind", "accepted"} or not isinstance(
                actual_item.get("accepted"), bool
            ):
                raise ValueError(f"graph entity result {item_id} is invalid")
            correct = expected.get("adjudicated_correct")
            if not isinstance(correct, bool):
                raise ValueError(f"graph entity gold {item_id} is invalid")
            if actual_item["accepted"]:
                accepted_entities.append(correct)
            case_correct.append(actual_item["accepted"] == correct)
        elif kind == "relationship":
            if set(actual_item) != {"id", "kind", "accepted"} or not isinstance(
                actual_item.get("accepted"), bool
            ):
                raise ValueError(f"graph relationship result {item_id} is invalid")
            supported = expected.get("adjudicated_supported")
            if not isinstance(supported, bool):
                raise ValueError(f"graph relationship gold {item_id} is invalid")
            if actual_item["accepted"]:
                accepted_relationships.append(supported)
            case_correct.append(actual_item["accepted"] == supported)
        elif kind == "resolution":
            if set(actual_item) != {"id", "kind", "predicted_outcome"}:
                raise ValueError(f"graph resolution result {item_id} is invalid")
            expected_outcome = expected.get("outcome")
            predicted = actual_item.get("predicted_outcome")
            if not isinstance(expected_outcome, str) or not isinstance(predicted, str):
                raise ValueError(f"graph resolution labels for {item_id} are invalid")
            matched = predicted == expected_outcome
            resolution_correct.append(matched)
            case_correct.append(matched)
        else:
            raise ValueError(f"graph gold {item_id} has an unknown kind")

    if not accepted_entities or not accepted_relationships or not resolution_correct:
        raise ValueError("graph metric denominators must not be empty")
    return GraphEvaluationMetrics(
        item_count=len(gold),
        entity_precision=sum(accepted_entities) / len(accepted_entities),
        relationship_precision=(
            sum(accepted_relationships) / len(accepted_relationships)
        ),
        entity_resolution_accuracy=(
            sum(resolution_correct) / len(resolution_correct)
        ),
        case_outcome_accuracy=sum(case_correct) / len(case_correct),
    )


def _ratio(numerator: int, denominator: int, field: str) -> float:
    if denominator <= 0 or numerator > denominator:
        raise ValueError(f"{field} counters are invalid")
    return numerator / denominator


def _decimal_cost(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be finite and non-negative")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be finite and non-negative") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return amount


def evaluate_operational_observations(payload: Mapping[str, Any]) -> OperationalMetrics:
    """Calculate lifecycle, latency, throughput, reliability, and cost metrics."""

    if payload.get("schema_version") != "operational-observations-v1":
        raise ValueError("operational observation schema is invalid")
    if payload.get("performance_qualification") is not False:
        raise ValueError("Stage 8 observations must not claim performance qualification")
    ingestion = payload.get("ingestion")
    retrieval = payload.get("retrieval_load")
    answers = payload.get("answer_load")
    usage = payload.get("usage")
    versions = payload.get("versions")
    if not all(
        isinstance(value, Mapping)
        for value in (ingestion, retrieval, answers, usage, versions)
    ):
        raise ValueError("operational observations require all metric sections")
    required_versions = {
        "contract_version",
        "profile_version",
        "corpus_version",
        "gold_version",
        "prompt_version",
        "output_schema_version",
        "embedding_model",
        "embedding_revision",
        "embedding_space_id",
        "answer_model",
        "answer_model_revision",
        "index_version",
        "configuration_version",
        "neo4j_image",
        "neo4j_image_digest",
    }
    if set(versions) != required_versions or any(
        not isinstance(versions[field], str) or not versions[field].strip()
        for field in required_versions
    ):
        raise ValueError("operational version inventory is incomplete")

    valid_jobs = _count(ingestion.get("valid_jobs"), "valid_jobs")
    completed_jobs = _count(ingestion.get("completed_jobs"), "completed_jobs")
    idempotency_mismatches = _count(
        ingestion.get("idempotency_mismatches"), "idempotency_mismatches"
    )
    deletion_residue = _count(
        ingestion.get("deletion_residue_count"), "deletion_residue_count"
    )
    recovery_scenarios = _count(
        ingestion.get("recovery_scenarios"), "recovery_scenarios"
    )
    recovered_scenarios = _count(
        ingestion.get("recovered_scenarios"), "recovered_scenarios"
    )

    retrieval_samples = retrieval.get("latency_ms")
    answer_samples = answers.get("end_to_end_latency_ms")
    if not isinstance(retrieval_samples, list) or not isinstance(answer_samples, list):
        raise ValueError("latency observations must be lists")
    retrieval_valid = _count(retrieval.get("valid_requests"), "retrieval valid_requests")
    retrieval_success = _count(
        retrieval.get("successful_requests"), "retrieval successful_requests"
    )
    retrieval_errors = _count(
        retrieval.get("unexpected_server_errors"),
        "retrieval unexpected_server_errors",
    )
    duration_seconds = _finite_nonnegative(
        retrieval.get("duration_seconds"), "retrieval duration_seconds"
    )
    if duration_seconds == 0:
        raise ValueError("retrieval duration_seconds must be positive")
    answer_valid = _count(answers.get("valid_requests"), "answer valid_requests")
    if answer_valid != len(answer_samples):
        raise ValueError("answer latency sample count must equal valid requests")
    if len(retrieval_samples) > retrieval_valid:
        raise ValueError("retrieval latency samples cannot exceed valid requests")
    if retrieval_success > retrieval_valid or retrieval_errors > retrieval_valid:
        raise ValueError("retrieval request counters are invalid")

    model_calls = _count(usage.get("model_calls"), "model_calls")
    input_tokens = _count(usage.get("input_tokens"), "input_tokens")
    output_tokens = _count(usage.get("output_tokens"), "output_tokens")
    request_costs = usage.get("answer_cost_usd")
    if not isinstance(request_costs, list) or len(request_costs) != answer_valid:
        raise ValueError("answer cost samples must equal answer requests")
    costs = [
        _decimal_cost(value, f"answer_cost_usd[{index}]")
        for index, value in enumerate(request_costs)
    ]
    if model_calls == 0 and (input_tokens or output_tokens or any(costs)):
        raise ValueError("zero model calls cannot report usage or cost")
    total_cost = sum(costs, Decimal("0"))

    return OperationalMetrics(
        ingestion_success_rate=_ratio(
            completed_jobs, valid_jobs, "ingestion success"
        ),
        idempotency_mismatch_count=idempotency_mismatches,
        deletion_residue_count=deletion_residue,
        recovery_success_rate=_ratio(
            recovered_scenarios, recovery_scenarios, "recovery success"
        ),
        retrieval_p95_ms=nearest_rank_percentile(retrieval_samples, 0.95),
        answer_p95_ms=nearest_rank_percentile(answer_samples, 0.95),
        retrieval_throughput_rps=retrieval_success / duration_seconds,
        server_error_rate=_ratio(
            retrieval_errors, retrieval_valid, "server error"
        ),
        model_call_count=model_calls,
        input_token_count=input_tokens,
        output_token_count=output_tokens,
        estimated_cost_usd=float(total_cost),
        mean_answer_cost_usd=float(total_cost / answer_valid),
        answer_sample_count=answer_valid,
        retrieval_sample_count=len(retrieval_samples),
    )
