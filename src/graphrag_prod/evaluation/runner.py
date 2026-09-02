"""Unified Stage 8 evaluation report construction."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import platform
import subprocess
from typing import Any, Mapping, Sequence

from graphrag_prod.generation import OUTPUT_SCHEMA_VERSION, PROMPT_VERSION
from graphrag_prod.retrieval.metrics import evaluate_retrieval_results

from .answers import calculate_answer_metrics, evaluate_answer_results
from .datasets import (
    canonical_json_bytes,
    load_gold_dataset,
    load_json,
    load_jsonl,
    sha256_file,
)
from .gates import (
    baseline_candidate,
    baseline_failures,
    contract_metric_rows,
    invariant_failures,
    validate_policy,
)
from .metrics import evaluate_graph_results, evaluate_operational_observations


ROOT = Path(__file__).resolve().parents[3]


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _git_value(*args: str) -> str:
    completed = subprocess.run(
        ("git", *args),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def _load_suite_result(path: Path) -> dict[str, Any]:
    result = load_json(path)
    if result.get("schema_version") != "unittest-suite-result-v1":
        raise ValueError(f"suite result schema is invalid: {path}")
    if (
        result.get("failures")
        or result.get("errors")
        or result.get("skipped")
        or result.get("expected_failures")
        or result.get("unexpected_successes")
        or result.get("tests_run") != len(result.get("passed_test_ids", []))
    ):
        raise ValueError(f"suite result is incomplete or unsuccessful: {path}")
    return result


def _migration_identity() -> dict[str, str]:
    directory = ROOT / "src" / "graphrag_prod" / "graph" / "migrations"
    return {
        path.name: sha256_file(path)
        for path in sorted(directory.glob("*.cypher"))
    }


def _case_digests(
    graph_results: Sequence[dict[str, Any]],
    retrieval_results: Sequence[dict[str, Any]],
    answer_results: Sequence[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    def section(items: Sequence[dict[str, Any]]) -> dict[str, str]:
        indexed = {item["id"]: item for item in items}
        if len(indexed) != len(items):
            raise ValueError("case result IDs must be unique")
        return {item_id: _sha256(indexed[item_id]) for item_id in sorted(indexed)}

    return {
        "answer": section(answer_results),
        "graph": section(graph_results),
        "retrieval": section(retrieval_results),
    }


def build_evaluation_report(
    *,
    gold_manifest: Path,
    graph_results_path: Path,
    retrieval_results_path: Path,
    answer_results_path: Path,
    conflict_results_path: Path,
    operational_path: Path,
    contract_path: Path,
    profile_path: Path,
    policy_path: Path,
    suite_result_paths: Mapping[str, Path],
    security_manifest_path: Path,
    baseline_path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    gold = load_gold_dataset(gold_manifest)
    graph_payload = load_json(graph_results_path)
    graph_results_value = graph_payload.get("items")
    if not isinstance(graph_results_value, list):
        raise ValueError("graph result items must be a list")
    graph_results = graph_results_value
    retrieval_payload = load_json(retrieval_results_path)
    if retrieval_payload.get("schema_version") != "retrieval-results-v1" or not isinstance(
        retrieval_payload.get("items"), list
    ):
        raise ValueError("retrieval result schema is invalid")
    retrieval_results = retrieval_payload["items"]
    answer_results = list(load_jsonl(answer_results_path))
    conflict_results = list(load_jsonl(conflict_results_path))
    operational_payload = load_json(operational_path)
    contract = load_json(contract_path)
    profile = load_json(profile_path)
    policy = load_json(policy_path)
    security_manifest = load_json(security_manifest_path)

    profile_id = profile.get("profile_id")
    if not isinstance(profile_id, str):
        raise ValueError("evaluation profile ID is invalid")
    validate_policy(policy, profile_id)
    if contract.get("contract_version") != profile.get("base_contract_version"):
        raise ValueError("profile and acceptance contract versions do not match")
    if operational_payload.get("profile_id") != profile_id:
        raise ValueError("operational observations use a different profile")
    versions = operational_payload.get("versions", {})
    if versions.get("prompt_version") != PROMPT_VERSION or versions.get(
        "output_schema_version"
    ) != OUTPUT_SCHEMA_VERSION:
        raise ValueError("operational prompt/output schema versions are stale")
    if versions.get("gold_version") != gold.manifest["version"]:
        raise ValueError("operational gold version is stale")
    if versions.get("contract_version") != contract["contract_version"]:
        raise ValueError("operational contract version is stale")
    if versions.get("profile_version") != profile["profile_version"]:
        raise ValueError("operational profile version is stale")

    graph_metrics = evaluate_graph_results(gold.graph_items, graph_results)
    retrieval_metrics = evaluate_retrieval_results(
        gold.questions, retrieval_results
    )
    base_answer_metrics = evaluate_answer_results(gold.answers, answer_results)
    del base_answer_metrics
    combined_gold = list(gold.answers) + list(gold.conflict_answers)
    combined_actual = answer_results + conflict_results
    answer_metrics = calculate_answer_metrics(combined_gold, combined_actual)
    operational_metrics = evaluate_operational_observations(operational_payload)

    suite_results = {
        name: _load_suite_result(path)
        for name, path in sorted(suite_result_paths.items())
    }
    required_suites = {"unit", "integration", "e2e", "security", "regression"}
    if set(suite_results) != required_suites:
        raise ValueError("evaluation suite results are incomplete")
    required_security = set(security_manifest.get("required_test_ids", []))
    passed_security = set(suite_results["security"]["passed_test_ids"])
    if (
        security_manifest.get("schema_version") != "security-suite-manifest-v1"
        or not required_security
        or not required_security <= passed_security
    ):
        raise ValueError("required security cases were not all executed and passed")

    contract_observed: dict[str, int | float] = {
        "answer_p95_ms": operational_metrics.answer_p95_ms,
        "citation_coverage": answer_metrics.citation_coverage,
        "citation_precision": answer_metrics.citation_precision,
        "deletion_residue_count": operational_metrics.deletion_residue_count,
        "entity_precision": graph_metrics.entity_precision,
        "entity_resolution_accuracy": graph_metrics.entity_resolution_accuracy,
        "idempotency_mismatch_count": operational_metrics.idempotency_mismatch_count,
        "ingestion_success_rate": operational_metrics.ingestion_success_rate,
        "mrr": retrieval_metrics.mrr,
        "ndcg_at_5": retrieval_metrics.ndcg_at_5,
        "numerical_fidelity": answer_metrics.numerical_fidelity,
        "recall_at_5": retrieval_metrics.recall_at_5,
        "recovery_success_rate": operational_metrics.recovery_success_rate,
        "refusal_f1": answer_metrics.refusal_f1,
        "relationship_precision": graph_metrics.relationship_precision,
        "retrieval_p95_ms": operational_metrics.retrieval_p95_ms,
        "retrieval_throughput_rps": operational_metrics.retrieval_throughput_rps,
        "server_error_rate": operational_metrics.server_error_rate,
        "supported_claim_rate": answer_metrics.supported_claim_rate,
        "unauthorized_exposure_count": retrieval_metrics.unauthorized_exposure_count,
    }
    performance_gates = profile.get("metric_policy", {}).get(
        "performance_results"
    ) == "gate"
    metric_rows, failures = contract_metric_rows(
        contract, contract_observed, performance_gates=performance_gates
    )
    diagnostics: dict[str, int | float | bool | None] = {
        "answer_correctness": answer_metrics.answer_correctness,
        "answer_sample_count": operational_metrics.answer_sample_count,
        "conflict_handling_rate": answer_metrics.conflict_handling_rate,
        "estimated_cost_usd": operational_metrics.estimated_cost_usd,
        "evidence_recall_at_5": retrieval_metrics.evidence_recall_at_5,
        "forbidden_answer_exposure_count": (
            answer_metrics.forbidden_answer_exposure_count
        ),
        "generation_failure_count": answer_metrics.generation_failure_count,
        "graph_case_outcome_accuracy": graph_metrics.case_outcome_accuracy,
        "input_token_count": operational_metrics.input_token_count,
        "mean_answer_cost_usd": operational_metrics.mean_answer_cost_usd,
        "model_call_count": operational_metrics.model_call_count,
        "output_token_count": operational_metrics.output_token_count,
        "retrieval_sample_count": operational_metrics.retrieval_sample_count,
        "security_suite_complete": required_security <= passed_security,
        "temporal_comparison_rate": answer_metrics.temporal_comparison_rate,
    }
    failures.extend(invariant_failures(policy, diagnostics))

    identities = {
        "acceptance_contract": {
            "sha256": sha256_file(contract_path),
            "version": contract["contract_version"],
        },
        "configuration": versions,
        "gold": {
            "sha256": sha256_file(gold_manifest),
            "version": gold.manifest["version"],
        },
        "graph_migrations": _migration_identity(),
        "profile": {
            "id": profile_id,
            "production_candidate_eligible": profile["production_candidate_eligible"],
            "sha256": sha256_file(profile_path),
            "version": profile["profile_version"],
        },
        "regression_policy": {
            "sha256": sha256_file(policy_path),
            "version": policy["version"],
        },
        "security_manifest": {
            "sha256": sha256_file(security_manifest_path),
            "version": security_manifest["version"],
        },
    }
    all_answer_results = answer_results + conflict_results
    case_digests = _case_digests(
        graph_results, retrieval_results, all_answer_results
    )
    deterministic_projection = {
        "case_digests": case_digests,
        "contract_metrics": contract_observed,
        "diagnostics": diagnostics,
        "identities": identities,
        "suite_passed_test_ids": {
            name: result["passed_test_ids"]
            for name, result in sorted(suite_results.items())
        },
    }
    semantic_digest = _sha256(deterministic_projection)
    candidate = baseline_candidate(
        profile_id=profile_id,
        gold_version=gold.manifest["version"],
        deterministic_projection=deterministic_projection,
        semantic_digest=semantic_digest,
        rationale=policy["rationale"],
    )
    if baseline_path is not None:
        baseline = load_json(baseline_path)
        failures.extend(
            baseline_failures(
                baseline,
                profile_id=profile_id,
                gold_version=gold.manifest["version"],
                deterministic_projection=deterministic_projection,
                semantic_digest=semantic_digest,
            )
        )

    report = {
        "case_digests": case_digests,
        "contract_metrics": metric_rows,
        "diagnostics": diagnostics,
        "environment": {
            "git_commit": _git_value("rev-parse", "HEAD"),
            "git_dirty": bool(_git_value("status", "--porcelain")),
            "python": platform.python_version(),
        },
        "failures": sorted(set(failures)),
        "identities": identities,
        "limitations": [
            "dev-mini is smoke evidence and is not production-candidate qualification",
            "fixture embeddings are gold-derived and do not measure provider quality",
            "the deterministic answer model validates orchestration, not open-ended LLM quality",
            "Stage 8 latency and cost observations are non-qualifying fixtures",
        ],
        "passed": not failures,
        "production_candidate_eligible": False,
        "schema_version": "evaluation-report-v1",
        "semantic_digest": semantic_digest,
        "suite_counts": {
            name: result["tests_run"] for name, result in sorted(suite_results.items())
        },
    }
    return report, candidate
