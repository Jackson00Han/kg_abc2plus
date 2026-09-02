#!/usr/bin/env python3
"""Assemble and gate one evidence-bound Stage 9 validation report."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
from typing import Any, Iterable, Mapping

from graphrag_prod import __version__
from graphrag_prod.evaluation.gates import (
    EVALUATION_BASELINE_SCHEMA_VERSION,
    EVALUATION_BASELINE_VERSION,
)
from graphrag_prod.evaluation.metrics import nearest_rank_percentile
from graphrag_prod.evaluation.production import (
    PRODUCTION_OBSERVATION_SCHEMA_VERSION,
    REQUIRED_DEPLOYMENT_PREREQUISITES,
    build_production_candidate_report,
)
from graphrag_prod.evaluation.production_config import (
    resolve_production_answer_retrieval_limits,
)
from graphrag_prod.evaluation.quality_evidence import (
    QUALITY_CASE_EVIDENCE_SCHEMA_VERSION,
    evaluate_http_answer_commitments,
)
from graphrag_prod.evaluation.reference_predictions import (
    REFERENCE_PREDICTION_PROVIDER,
    REFERENCE_PREDICTION_SCHEMA_VERSION,
    REFERENCE_PREDICTION_SHA256,
    REFERENCE_PREDICTION_VERSION,
)
from graphrag_prod.generation import OUTPUT_SCHEMA_VERSION, PROMPT_VERSION
from scripts.build_load_corpus import (
    EMBEDDING_SPACE_ID,
    EXTRACTOR_SIGNATURE,
    PRIMARY_TENANT_ID,
    SPLITTER_SIGNATURE,
    deterministic_vector,
    iter_chunks,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "acceptance.v1.json"
PROFILE = ROOT / "contracts" / "profiles" / "production-reference.v1.json"
CONFIGURATION = ROOT / "evaluation" / "production-reference-config.v1.json"
LOAD_MANIFEST = ROOT / "datasets" / "load-v1" / "manifest.json"
DEV_CORPUS_MANIFEST = ROOT / "datasets" / "dev-corpus-v1" / "manifest.json"
DEV_CORPUS_VECTORS = ROOT / "datasets" / "dev-corpus-v1" / "vectors.jsonl"
GOLD_MANIFEST = ROOT / "evaluation" / "gold-v1" / "manifest.json"
GOLD_QUESTIONS = ROOT / "evaluation" / "gold-v1" / "questions.jsonl"
REFERENCE_PREDICTIONS = ROOT / "evaluation" / "reference-answer-predictions.v1.json"
STAGE8_BASELINE = ROOT / "evaluation" / "baselines" / "dev-mini.v1.json"
BACKUP_DUMP_EVIDENCE_SCHEMA_VERSION = "neo4j-database-dump-v1"
ACCESS_EVIDENCE_SCHEMA_VERSION = "load-v1-access-isolation-v1"

_LOAD_WINDOW_FIELDS = frozenset(
    {
        "answer_samples",
        "answer_warmup_requests",
        "configured_sustained_seconds",
        "forbidden_chunk_count",
        "http_port",
        "primary_tenant_active_chunks",
        "readiness_probe_status",
        "readiness_transaction_timeout_seconds",
        "retrieval_samples",
        "retrieval_transaction_timeout_seconds",
        "semantic_failure_count",
        "schema_version",
        "warmup_requests",
    }
)

_CONTAINER_INSPECTION_FIELDS = frozenset(
    {
        "actual_neo4j_image",
        "actual_neo4j_repo_digest",
        "code_commit",
        "database_initial_node_count",
        "database_initial_relationship_count",
        "schema_version",
    }
)

_LIFECYCLE_IDS = {
    "access_isolation",
    "backup_restore",
    "deletion",
    "idempotency",
    "interrupted_ingestion",
}
_DEPENDENCIES = ("neo4j", "embedding_provider", "llm")
_MODES = ("success", "timeout", "unavailable", "failure")
_PROVIDER_TIMEOUT_SCENARIOS = {
    "embedding_provider": "embedding_provider_timeout",
    "llm_provider": "llm_timeout",
}
_SCENARIO_IDS = _LIFECYCLE_IDS | {
    f"{dependency}_{mode}"
    for dependency in _DEPENDENCIES
    for mode in _MODES
}
_REQUEST_FIELDS = (
    "answer_evidence",
    "case_id",
    "client_id",
    "completed_monotonic_ms",
    "dataset_id",
    "domain_failure_code",
    "domain_status",
    "embedding_space_id",
    "error_code",
    "expected_chunk_ids",
    "inactive_chunk_ids",
    "inactive_version_count",
    "request_id",
    "query_vector_checksum",
    "retrieval_stage_ms",
    "selected_chunk_ids",
    "selected_chunk_count",
    "semantic_success",
    "started_monotonic_ms",
    "status_code",
    "trace_id",
    "unauthorized_chunk_ids",
    "unauthorized_chunk_count",
    "visible_chunk_ids",
)


def _provider_timeout_window_ms(
    config: Mapping[str, Any],
    provider_id: str,
) -> tuple[float, float]:
    """Validate and derive the production API deadline observation window."""

    if provider_id not in _PROVIDER_TIMEOUT_SCENARIOS:
        raise ValueError(f"unsupported provider timeout probe: {provider_id}")
    values = {
        "timeout_seconds": config["api"]["timeout_seconds"],
        "timeout_observation_early_tolerance_ms": config["api"][
            "timeout_observation_early_tolerance_ms"
        ],
        "timeout_observation_late_tolerance_ms": config["api"][
            "timeout_observation_late_tolerance_ms"
        ],
        "timeout_delay_ms": config["dependencies"][provider_id][
            "timeout_delay_ms"
        ],
    }
    checked: dict[str, float] = {}
    for name, value in values.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0.0
        ):
            raise ValueError(f"{provider_id} {name} must be finite and non-negative")
        checked[name] = float(value)
    deadline_ms = checked["timeout_seconds"] * 1_000.0
    early_ms = checked["timeout_observation_early_tolerance_ms"]
    late_ms = checked["timeout_observation_late_tolerance_ms"]
    if deadline_ms <= 0.0 or early_ms >= deadline_ms:
        raise ValueError("API timeout window must retain a positive lower bound")
    lower_ms = deadline_ms - early_ms
    upper_ms = deadline_ms + late_ms
    if checked["timeout_delay_ms"] <= upper_ms:
        raise ValueError(
            f"{provider_id} timeout probe must outlive the API timeout window"
        )
    return lower_ms, upper_ms


def _validate_provider_timeout_scenarios(
    config: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
) -> None:
    """Bind provider timeout evidence to the configured API deadline."""

    for provider_id, scenario_id in _PROVIDER_TIMEOUT_SCENARIOS.items():
        lower_ms, upper_ms = _provider_timeout_window_ms(config, provider_id)
        row = rows[scenario_id]
        observed_ms = (
            float(row["finished_ns"]) - float(row["started_ns"])
        ) / 1_000_000
        if not lower_ms <= observed_ms <= upper_ms:
            raise ValueError(
                f"{scenario_id} did not return at the configured API deadline"
            )


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSON object required: {path}:{line_number}")
            rows.append(value)
    if not rows:
        raise ValueError(f"JSONL evidence must not be empty: {path}")
    return rows


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bind_backup_dump(
    source_path: Path,
    destination_path: Path,
    backup_observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Copy and manifest the exact dump measured by the restore observation."""

    if not source_path.is_file():
        raise ValueError("backup dump artifact is missing")
    expected_size = backup_observation.get("backup_size_bytes")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size <= 0
    ):
        raise ValueError("backup observation size is invalid")
    expected_digest = _prefixed(str(backup_observation.get("backup_sha256", "")))
    source_size = source_path.stat().st_size
    source_digest = f"sha256:{_file_sha256(source_path)}"
    if source_size != expected_size or source_digest != expected_digest:
        raise ValueError("backup dump does not match the backup observation")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, destination_path)
    copied_size = destination_path.stat().st_size
    copied_digest = f"sha256:{_file_sha256(destination_path)}"
    if copied_size != expected_size or copied_digest != expected_digest:
        raise ValueError("copied backup dump does not match the backup observation")
    return {
        "path": "evidence/backup_dump.dump",
        "record_count": 1,
        "schema": BACKUP_DUMP_EVIDENCE_SCHEMA_VERSION,
        "sha256": copied_digest,
    }


def _prefixed(value: str) -> str:
    return value if value.startswith("sha256:") else f"sha256:{value}"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _host_memory_bytes() -> int:
    """Return physical host memory without an optional platform dependency."""

    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    page_count = int(os.sysconf("SC_PHYS_PAGES"))
    memory_bytes = page_size * page_count
    if memory_bytes <= 0:
        raise ValueError("host physical memory must be discoverable")
    return memory_bytes


def _nearest(samples: Iterable[Mapping[str, Any]], percentile: float) -> float:
    latencies = [
        float(item["completed_monotonic_ms"])
        - float(item["started_monotonic_ms"])
        for item in samples
    ]
    return float(nearest_rank_percentile(latencies, percentile))


def _validate_retrieval_round_robin(samples: list[dict[str, Any]]) -> None:
    expected_clients = {f"retrieval-{index:02d}" for index in range(8)}
    by_client: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        by_client.setdefault(str(sample["client_id"]), []).append(sample)
    if set(by_client) != expected_clients:
        raise ValueError("retrieval requests must use the exact eight client identities")
    for client_id, client_samples in sorted(by_client.items()):
        client_index = int(client_id.rsplit("-", 1)[1])
        ordered = sorted(
            client_samples,
            key=lambda item: (
                float(item["started_monotonic_ms"]),
                str(item["request_id"]),
            ),
        )
        if len(ordered) < 64:
            raise ValueError(
                f"retrieval client {client_id} did not cover all 64 load cases"
            )
        for request_index, sample in enumerate(ordered):
            expected_case = f"load-anchor-{(client_index + request_index) % 64:02d}"
            if sample["case_id"] != expected_case:
                raise ValueError(
                    f"retrieval client {client_id} did not follow the load-v1 round-robin"
                )


def _validate_http_answer_evidence(
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    commitments = {str(item["case_id"]): item["answer_evidence"] for item in samples}
    if len(commitments) != len(samples):
        raise ValueError("HTTP answer commitment case IDs must be unique")
    normalized, metrics = evaluate_http_answer_commitments(commitments)
    for item in samples:
        item["answer_evidence"] = normalized[str(item["case_id"])]
    unit_rates = (
        "answer_correctness",
        "citation_coverage",
        "citation_precision",
        "numerical_fidelity",
        "supported_claim_rate",
    )
    if (
        any(float(metrics[field]) != 1.0 for field in unit_rates)
        or int(metrics["generation_failure_count"]) != 0
        or int(metrics["forbidden_answer_exposure_count"]) != 0
        or any(
            metrics[field] is not None and float(metrics[field]) != 1.0
            for field in ("conflict_handling_rate", "temporal_comparison_rate")
        )
    ):
        raise ValueError("HTTP answer commitments do not pass fixed gold metrics")
    return metrics


def _request_samples(
    rows: list[dict[str, Any]],
    config: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {"answer": [], "retrieval": []}
    for row in rows:
        if set(row) != set(_REQUEST_FIELDS) | {"kind"}:
            raise ValueError("measured request fields do not match the evidence schema")
        kind = row.get("kind")
        if kind not in result:
            raise ValueError(f"unknown measured request kind: {kind!r}")
        sample = {field: row[field] for field in _REQUEST_FIELDS}
        sample["query_vector_checksum"] = _prefixed(
            str(sample["query_vector_checksum"])
        )
        result[kind].append(sample)
    for kind, samples in result.items():
        if not samples:
            raise ValueError(f"measured {kind} requests are missing")
        samples.sort(key=lambda item: str(item["request_id"]))

    configured_case_ids = config.get("answer", {}).get("gold_case_ids")
    if (
        not isinstance(configured_case_ids, list)
        or len(configured_case_ids) != 30
        or len(set(configured_case_ids)) != len(configured_case_ids)
        or any(not isinstance(item, str) or not item for item in configured_case_ids)
    ):
        raise ValueError("production answer configuration must name 30 gold cases")
    if int(config["answer"]["latency_samples"]) != len(configured_case_ids):
        raise ValueError("answer latency sample count does not match gold cases")
    gold_questions = {item["id"]: item for item in _load_jsonl(GOLD_QUESTIONS)}
    dev_manifest = _load(DEV_CORPUS_MANIFEST)
    answer_space_id = str(dev_manifest["embedding_profile"]["embedding_space_id"])
    answer_vectors = {
        str(item["id"]): hashlib.sha256(
            _canonical_bytes(list(item["vector"]))
        ).hexdigest()
        for item in _load_jsonl(DEV_CORPUS_VECTORS)
        if item.get("kind") == "query"
    }
    observed_answer_ids = [str(item["case_id"]) for item in result["answer"]]
    if sorted(observed_answer_ids) != sorted(configured_case_ids):
        raise ValueError("answer request case set does not match the configuration")
    for item in result["answer"]:
        case_id = str(item["case_id"])
        question = gold_questions.get(case_id)
        if question is None or question.get("answerable") is not True:
            raise ValueError(f"answer request is not a gold answered case: {case_id}")
        expected = sorted(str(value) for value in question["relevance"])
        vector_checksum = answer_vectors.get(str(question.get("vector_id")))
        if vector_checksum is None:
            raise ValueError(f"answer request has no versioned query vector: {case_id}")
        if item["dataset_id"] != "gold-v1":
            raise ValueError("answer request dataset must be gold-v1")
        if (
            item["embedding_space_id"] != answer_space_id
            or item["query_vector_checksum"] != _prefixed(vector_checksum)
        ):
            raise ValueError(f"answer request embedding identity drifted: {case_id}")
        if item["expected_chunk_ids"] != expected:
            raise ValueError(f"answer request expected Chunks drifted: {case_id}")
        if set(item["selected_chunk_ids"]).isdisjoint(expected):
            raise ValueError(f"answer request selected no expected Chunk: {case_id}")
    if any(item["answer_evidence"] is not None for item in result["retrieval"]):
        raise ValueError("retrieval requests must not contain answer commitments")
    if any(not isinstance(item["answer_evidence"], Mapping) for item in result["answer"]):
        raise ValueError(
            "answer requests require prose-redacted checksum commitments"
        )
    _validate_http_answer_evidence(result["answer"])

    load_manifest = _load(LOAD_MANIFEST)
    load_workload = load_manifest.get("retrieval_workload", {})
    load_queries = load_workload.get("queries", [])
    if (
        load_workload.get("dataset_id") != "load-v1"
        or load_workload.get("query_count") != 64
        or not isinstance(load_queries, list)
        or len(load_queries) != 64
    ):
        raise ValueError("load-v1 retrieval workload is invalid")
    load_expectations = {
        str(item["case_id"]): {
            "embedding_space_id": str(item["embedding_space_id"]),
            "expected_chunk_ids": list(item["expected_chunk_ids"]),
            "query_vector_checksum": _prefixed(
                str(item["query_vector_checksum"])
            ),
        }
        for item in load_queries
    }
    if len(load_expectations) != 64:
        raise ValueError("load-v1 retrieval workload case IDs are not unique")
    load_case_ids = set(load_expectations)
    observed_load_case_ids: set[str] = set()
    for item in result["retrieval"]:
        case_id = str(item["case_id"])
        if item["dataset_id"] != "load-v1" or case_id not in load_case_ids:
            raise ValueError("retrieval request is not a load-v1 anchor")
        expected = item["expected_chunk_ids"]
        load_expectation = load_expectations[case_id]
        if expected != load_expectation["expected_chunk_ids"]:
            raise ValueError("retrieval request expected Chunks drifted from load-v1")
        if (
            item["embedding_space_id"]
            != load_expectation["embedding_space_id"]
            or item["query_vector_checksum"]
            != load_expectation["query_vector_checksum"]
        ):
            raise ValueError("retrieval request embedding identity drifted from load-v1")
        if set(item["selected_chunk_ids"]).isdisjoint(expected):
            raise ValueError("retrieval request selected no expected Chunk")
        observed_load_case_ids.add(case_id)
    if observed_load_case_ids != load_case_ids:
        raise ValueError("retrieval request evidence does not cover all load anchors")
    _validate_retrieval_round_robin(result["retrieval"])
    return result


def _retrieval_stage_samples(
    path: Path,
    request_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Verify the independent backend-stage stream against HTTP evidence."""

    rows = _load_jsonl(path)
    expected_fields = {"request_id", "retrieval_stage_ms", "trace_id"}
    normalized: list[dict[str, Any]] = []
    by_request: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if set(row) != expected_fields:
            raise ValueError(
                f"retrieval-stage sample {index} fields do not match the schema"
            )
        request_id = row["request_id"]
        trace_id = row["trace_id"]
        duration = row["retrieval_stage_ms"]
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("retrieval-stage request_id must be non-empty text")
        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("retrieval-stage trace_id must be non-empty text")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) <= 0.0
        ):
            raise ValueError("retrieval_stage_ms must be finite and positive")
        if request_id in by_request:
            raise ValueError("retrieval-stage request IDs must be unique")
        sample = {
            "request_id": request_id,
            "retrieval_stage_ms": float(duration),
            "trace_id": trace_id,
        }
        by_request[request_id] = sample
        normalized.append(sample)

    request_by_id = {str(row.get("request_id")): row for row in request_rows}
    if len(request_by_id) != len(request_rows) or set(request_by_id) != set(by_request):
        raise ValueError("retrieval-stage samples must exactly cover measured requests")
    for request_id, request in request_by_id.items():
        sample = by_request[request_id]
        if request.get("trace_id") != sample["trace_id"]:
            raise ValueError("retrieval-stage trace ID does not match HTTP evidence")
        if request.get("retrieval_stage_ms") != sample["retrieval_stage_ms"]:
            raise ValueError("retrieval-stage duration does not match HTTP evidence")
    return sorted(normalized, key=lambda item: item["request_id"])


def _unittest_projection(path: Path, prefix: str) -> dict[str, list[str] | int]:
    raw = _load(path)
    if raw.get("schema_version") != "unittest-suite-result-v1":
        raise ValueError(f"unrecognized suite result: {path}")

    def values(field: str) -> list[str]:
        source = raw.get(field)
        if not isinstance(source, list):
            raise ValueError(f"suite result field {field} is invalid: {path}")
        return [f"{prefix}:{item}" for item in source]

    projection: dict[str, list[str] | int] = {
        "error_test_ids": values("errors"),
        "failed_test_ids": values("failures") + values("unexpected_successes"),
        "passed_test_ids": values("passed_test_ids"),
        "skipped_test_ids": values("skipped") + values("expected_failures"),
        "tests_run": int(raw["tests_run"]),
    }
    observed = sum(
        len(projection[field])  # type: ignore[arg-type]
        for field in (
            "error_test_ids",
            "failed_test_ids",
            "passed_test_ids",
            "skipped_test_ids",
        )
    )
    if observed != projection["tests_run"]:
        raise ValueError(f"suite result coverage is incomplete: {path}")
    return projection


def _merge_suites(*suites: Mapping[str, Any]) -> dict[str, Any]:
    merged = {
        "error_test_ids": [],
        "failed_test_ids": [],
        "passed_test_ids": [],
        "skipped_test_ids": [],
        "tests_run": 0,
    }
    for suite in suites:
        merged["tests_run"] += int(suite["tests_run"])
        for field in (
            "error_test_ids",
            "failed_test_ids",
            "passed_test_ids",
            "skipped_test_ids",
        ):
            merged[field].extend(suite[field])
    for field in (
        "error_test_ids",
        "failed_test_ids",
        "passed_test_ids",
        "skipped_test_ids",
    ):
        merged[field] = sorted(merged[field])
    return merged


def _derived_suite(checks: Mapping[str, bool], prefix: str) -> dict[str, Any]:
    passed = sorted(f"{prefix}:{name}" for name, value in checks.items() if value)
    failed = sorted(f"{prefix}:{name}" for name, value in checks.items() if not value)
    return {
        "error_test_ids": [],
        "failed_test_ids": failed,
        "passed_test_ids": passed,
        "skipped_test_ids": [],
        "tests_run": len(checks),
    }


def _suite_results(
    suite_dir: Path,
    stage8_report: Mapping[str, Any],
    quality: Mapping[str, Any],
    scenario_rows: Mapping[str, Mapping[str, Any]],
    performance_checks: Mapping[str, bool],
) -> dict[str, Any]:
    unit = _unittest_projection(suite_dir / "unit.json", "unit")
    e2e = _unittest_projection(suite_dir / "e2e.json", "e2e")
    integration = _unittest_projection(
        suite_dir / "integration.json", "integration"
    )
    regression = _unittest_projection(
        suite_dir / "regression.json", "regression"
    )
    security = _unittest_projection(suite_dir / "security.json", "security")
    quality_checks = {
        "stage8_report": bool(stage8_report.get("passed")),
        "large_database_gold": bool(quality.get("passed")),
    }
    quality_derived = _derived_suite(quality_checks, "quality")
    recovery = _derived_suite(
        {
            scenario_id: bool(scenario_rows[scenario_id].get("passed"))
            for scenario_id in sorted(_LIFECYCLE_IDS)
        },
        "recovery",
    )
    performance = _derived_suite(performance_checks, "performance")
    return {
        "functional": _merge_suites(unit, e2e, integration),
        "performance": performance,
        "quality": _merge_suites(regression, quality_derived),
        "recovery": recovery,
        "security": security,
    }


def _scenario_rows(
    load_dir: Path,
    runtime_dir: Path,
    backup_path: Path,
) -> dict[str, dict[str, Any]]:
    raw_rows = (
        _load_jsonl(load_dir / "faults.jsonl")
        + _load_jsonl(runtime_dir / "runtime-faults.jsonl")
        + [_load(load_dir / "deletion.json"), _load(backup_path)]
    )
    indexed: dict[str, dict[str, Any]] = {}
    for row in raw_rows:
        scenario_id = str(row.get("scenario_id", ""))
        if scenario_id not in _SCENARIO_IDS:
            raise ValueError(f"unexpected Stage 9 scenario: {scenario_id!r}")
        if scenario_id in indexed:
            raise ValueError(f"duplicate Stage 9 scenario: {scenario_id}")
        indexed[scenario_id] = row
    if set(indexed) != _SCENARIO_IDS:
        raise ValueError(
            "Stage 9 scenario coverage mismatch: "
            f"missing={sorted(_SCENARIO_IDS - set(indexed))}, "
            f"extra={sorted(set(indexed) - _SCENARIO_IDS)}"
        )
    return indexed


_ACCESS_INVENTORY_IDS = frozenset(
    {"active", "all", "authorized", "forbidden", "inactive"}
)
_ACCESS_PROBE_FIELDS = frozenset(
    {
        "canary_chunk_id",
        "case_id",
        "citation_chunk_ids",
        "completed_monotonic_ms",
        "embedding_space_id",
        "error_code",
        "http_status",
        "kind",
        "query_text_sha256",
        "query_vector_checksum",
        "request_id",
        "response",
        "result_chunk_ids",
        "started_monotonic_ms",
        "target_tenant_id",
        "trace_chunk_ids",
        "trace_id",
        "version_id",
    }
)
_TRACE_STAGES = (
    "vector_recall",
    "bm25_recall",
    "seed_ranking",
    "graph_expansion",
    "candidate_vector_ranking",
    "final_ranking",
)


def _id_set_digest(values: Iterable[str]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(sorted(values))).hexdigest()}"


def _expected_access_contract(
    load_manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    coverage = load_manifest.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("load-v1 access coverage is invalid")
    groups = coverage.get("load_principal_groups")
    if (
        coverage.get("primary_load_tenant") != PRIMARY_TENANT_ID
        or not isinstance(groups, list)
        or len(groups) != 2
        or len(set(groups)) != 2
    ):
        raise ValueError("load-v1 access principal is invalid")

    all_ids: set[str] = set()
    active_ids: set[str] = set()
    authorized_ids: set[str] = set()
    chunks: dict[str, dict[str, Any]] = {}
    group_set = set(str(item) for item in groups)
    for chunk in iter_chunks():
        chunk_id = str(chunk["chunk_id"])
        if chunk_id in chunks:
            raise ValueError("load-v1 contains duplicate Chunk IDs")
        chunks[chunk_id] = chunk
        all_ids.add(chunk_id)
        if chunk["active"] is True:
            active_ids.add(chunk_id)
            if (
                chunk["tenant_id"] == PRIMARY_TENANT_ID
                and group_set.intersection(chunk["access_groups"])
            ):
                authorized_ids.add(chunk_id)
    sets = {
        "active": active_ids,
        "all": all_ids,
        "authorized": authorized_ids,
        "forbidden": active_ids - authorized_ids,
        "inactive": all_ids - active_ids,
    }
    counts = load_manifest.get("counts", {})
    if (
        len(sets["all"]) != int(counts.get("total_chunks", -1))
        or len(sets["active"]) != int(counts.get("active_chunks", -1))
        or len(sets["inactive"]) != int(counts.get("historical_chunks", -1))
    ):
        raise ValueError("load-v1 access inventory disagrees with its manifest")
    inventory = {
        name: {
            "count": len(values),
            "chunk_ids_sha256": _id_set_digest(values),
        }
        for name, values in sorted(sets.items())
    }

    expected_probes: dict[str, dict[str, Any]] = {}
    for kind, field in (
        ("same-tenant-denied", "protected_same_tenant_chunk_ids"),
        ("cross-tenant-denied", "cross_tenant_chunk_ids"),
    ):
        canaries = coverage.get(field)
        if not isinstance(canaries, list) or len(canaries) != 4:
            raise ValueError(f"load-v1 {kind} canaries are invalid")
        for index, chunk_id in enumerate(canaries):
            chunk = chunks.get(str(chunk_id))
            case_id = f"{kind}-{index:02d}"
            if chunk is None or str(chunk_id) not in sets["forbidden"]:
                raise ValueError(f"load-v1 access canary drifted: {chunk_id}")
            query_text = f"Stage 9 access-isolation probe {case_id}"
            vector_checksum = hashlib.sha256(
                _canonical_bytes(list(deterministic_vector(str(chunk_id))))
            ).hexdigest()
            expected_probes[case_id] = {
                "canary_chunk_id": str(chunk_id),
                "embedding_space_id": EMBEDDING_SPACE_ID,
                "kind": kind,
                "markers": {
                    str(chunk["chunk_id"]),
                    str(chunk["chunk_key"]),
                    str(chunk["document_id"]),
                    *(str(item) for item in chunk["access_groups"]),
                },
                "query_text_sha256": _prefixed(
                    hashlib.sha256(query_text.encode("utf-8")).hexdigest()
                ),
                "query_vector_checksum": _prefixed(vector_checksum),
                "source_text_sha256": _prefixed(str(chunk["checksum"])),
                "target_tenant_id": str(chunk["tenant_id"]),
                "version_id": str(chunk["version_id"]),
            }
            if chunk["tenant_id"] != PRIMARY_TENANT_ID:
                expected_probes[case_id]["markers"].add(str(chunk["tenant_id"]))
    if len(expected_probes) != 8:
        raise ValueError("load-v1 access canary coverage is incomplete")
    return (
        {
            "dataset_id": "load-v1",
            "inventory": inventory,
            "principal": {
                "groups": sorted(str(item) for item in groups),
                "tenant_id": PRIMARY_TENANT_ID,
            },
            "schema_version": ACCESS_EVIDENCE_SCHEMA_VERSION,
        },
        expected_probes,
    )


def _response_access_ids(response: Mapping[str, Any]) -> tuple[set[str], set[str], set[str]]:
    trace = response.get("trace", {})
    if not isinstance(trace, dict):
        raise ValueError("access probe response trace must be an object")
    trace_ids = {
        str(item)
        for item in trace.get("selected_chunk_ids", [])
        if isinstance(item, str)
    }
    for stage in _TRACE_STAGES:
        values = trace.get(stage, [])
        if not isinstance(values, list):
            raise ValueError("access probe trace stages must be arrays")
        trace_ids.update(
            str(item["chunk_id"])
            for item in values
            if isinstance(item, dict) and isinstance(item.get("chunk_id"), str)
        )
    chunks = response.get("chunks", [])
    citations = response.get("citations", [])
    if not isinstance(chunks, list) or not isinstance(citations, list):
        raise ValueError("access probe result and citations must be arrays")
    result_ids = {
        str(item["citation"]["chunk_id"])
        for item in chunks
        if isinstance(item, dict)
        and isinstance(item.get("citation"), dict)
        and isinstance(item["citation"].get("chunk_id"), str)
    }
    citation_ids = {
        str(item["chunk_id"])
        for item in citations
        if isinstance(item, dict) and isinstance(item.get("chunk_id"), str)
    }
    return trace_ids, result_ids, citation_ids


def _nested_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _nested_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nested_strings(item)


def _access_evidence_projection(
    value: Any,
    load_manifest: Mapping[str, Any],
    *,
    event_started_ms: float,
    event_completed_ms: float,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "dataset_id",
        "inventory",
        "principal",
        "probes",
        "schema_version",
    }:
        raise ValueError("access-isolation evidence schema is invalid")
    expected, expected_probes = _expected_access_contract(load_manifest)
    candidate = {key: value[key] for key in expected}
    if candidate != expected:
        raise ValueError("access-isolation inventory drifted from committed load-v1")
    raw_probes = value["probes"]
    if not isinstance(raw_probes, list) or len(raw_probes) != 8:
        raise ValueError("access-isolation evidence must contain eight HTTP probes")
    normalized: list[dict[str, Any]] = []
    seen_cases: set[str] = set()
    seen_requests: set[str] = set()
    for raw in raw_probes:
        if not isinstance(raw, dict) or set(raw) != _ACCESS_PROBE_FIELDS:
            raise ValueError("access-isolation HTTP probe schema is invalid")
        case_id = str(raw["case_id"])
        expectation = expected_probes.get(case_id)
        if expectation is None or case_id in seen_cases:
            raise ValueError("access-isolation HTTP probe identity is invalid")
        seen_cases.add(case_id)
        request_id = str(raw["request_id"])
        trace_id = raw["trace_id"]
        if not request_id or request_id in seen_requests:
            raise ValueError("access-isolation request identities must be unique")
        seen_requests.add(request_id)
        if not isinstance(trace_id, str) or not trace_id:
            raise ValueError("access-isolation probes require trace identities")
        started = float(raw["started_monotonic_ms"])
        completed = float(raw["completed_monotonic_ms"])
        if (
            not math.isfinite(started)
            or not math.isfinite(completed)
            or completed < started
            or started < event_started_ms
            or completed > event_completed_ms
        ):
            raise ValueError("access-isolation probe timeline is invalid")
        for field in (
            "canary_chunk_id",
            "embedding_space_id",
            "kind",
            "target_tenant_id",
            "version_id",
        ):
            if raw[field] != expectation[field]:
                raise ValueError("access-isolation canary identity drifted from load-v1")
        for field in ("query_text_sha256", "query_vector_checksum"):
            if _prefixed(str(raw[field])) != expectation[field]:
                raise ValueError("access-isolation query identity drifted from load-v1")
        response = raw["response"]
        if not isinstance(response, dict):
            raise ValueError("access-isolation response must be an object")
        trace_ids, result_ids, citation_ids = _response_access_ids(response)
        declared_ids = {
            "trace_chunk_ids": sorted(trace_ids),
            "result_chunk_ids": sorted(result_ids),
            "citation_chunk_ids": sorted(citation_ids),
        }
        if any(raw[field] != ids for field, ids in declared_ids.items()):
            raise ValueError("access-isolation response IDs are not independently bound")
        strings = tuple(_nested_strings(response))
        exposes_marker = any(
            marker in item
            for marker in expectation["markers"]
            for item in strings
        ) or any(
            _prefixed(hashlib.sha256(item.encode("utf-8")).hexdigest())
            == expectation["source_text_sha256"]
            for item in strings
        )
        trace = response.get("trace", {})
        nonempty_trace = any(trace.get(stage, []) for stage in _TRACE_STAGES) or bool(
            trace.get("decisions", [])
        )
        if (
            raw["http_status"] != 200
            or raw["error_code"] is not None
            or trace_ids
            or result_ids
            or citation_ids
            or response.get("chunks", [])
            or response.get("citations", [])
            or nonempty_trace
            or exposes_marker
        ):
            raise ValueError("access-isolation probe exposed a protected existence signal")
        normalized.append(
            {
                **{field: raw[field] for field in _ACCESS_PROBE_FIELDS},
                "completed_monotonic_ms": completed,
                "query_text_sha256": expectation["query_text_sha256"],
                "query_vector_checksum": expectation["query_vector_checksum"],
                "started_monotonic_ms": started,
            }
        )
    if seen_cases != set(expected_probes):
        raise ValueError("access-isolation canary coverage is incomplete")
    return {**expected, "probes": sorted(normalized, key=lambda item: item["case_id"])}


def _fault_projection(
    raw_rows: Mapping[str, Mapping[str, Any]],
    load_manifest: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    timeline: dict[str, dict[str, Any]] = {}
    scenarios: dict[str, dict[str, Any]] = {}
    for scenario_id, row in sorted(raw_rows.items()):
        started = float(row["started_ns"]) / 1_000_000
        completed = float(row["finished_ns"]) / 1_000_000
        if completed < started:
            raise ValueError(f"scenario time moved backwards: {scenario_id}")
        passed = row.get("passed")
        if not isinstance(passed, bool):
            raise ValueError(f"scenario passed is not boolean: {scenario_id}")
        http_status = int(row["http_status"])
        error_code = row.get("error_code")
        domain_status = row.get("domain_status")
        reason = row.get("reason")
        raw_latency = float(row["latency_ms"])
        derived_latency = completed - started
        if abs(raw_latency - derived_latency) > 1e-6:
            raise ValueError(f"scenario latency does not bind timeline: {scenario_id}")
        assertion_failures = [] if passed else ["raw scenario assertion failed"]
        event = {
            "assertion_failures": assertion_failures,
            "completed_monotonic_ms": completed,
            "domain_status": domain_status,
            "error_code": error_code,
            "http_status": http_status,
            "reason": reason,
            "started_monotonic_ms": started,
        }
        if scenario_id == "access_isolation":
            event["access_evidence"] = _access_evidence_projection(
                row.get("access_evidence"),
                load_manifest,
                event_started_ms=started,
                event_completed_ms=completed,
            )
        timeline[scenario_id] = event
        scenarios[scenario_id] = {
            "domain_status": domain_status,
            "error_code": error_code,
            "http_status": http_status,
            "latency_ms": derived_latency,
            "passed": passed,
            "reason": reason,
        }
    return timeline, scenarios


def _canonical_graph_state_projection(path: Path) -> dict[str, Any]:
    state = _load(path)
    expected_fields = {
        "business_node_count",
        "business_relationship_count",
        "label_counts",
        "schema_and_indexes_verified",
        "sha256",
    }
    if set(state) != expected_fields:
        raise ValueError(f"canonical graph state fields are invalid: {path}")
    if state["schema_and_indexes_verified"] is not True:
        raise ValueError(f"canonical graph schema was not verified: {path}")
    labels = state["label_counts"]
    if not isinstance(labels, Mapping) or not labels:
        raise ValueError(f"canonical graph label counts are invalid: {path}")
    return {
        "business_node_count": int(state["business_node_count"]),
        "business_relationship_count": int(
            state["business_relationship_count"]
        ),
        "label_counts": {
            str(label): int(count) for label, count in sorted(labels.items())
        },
        "schema_and_indexes_verified": True,
        "sha256": _prefixed(str(state["sha256"])),
    }


def _static_metrics(stage8_report: Mapping[str, Any]) -> dict[str, int | float]:
    rows = stage8_report.get("contract_metrics")
    if not isinstance(rows, list):
        raise ValueError("Stage 8 report has no contract metric rows")
    result = {str(item["id"]): item["observed"] for item in rows}
    if len(result) != len(rows):
        raise ValueError("Stage 8 contract metric IDs are not unique")
    return result


def _validate_stage8_report(
    stage8_report: Mapping[str, Any], suite_dir: Path, code_commit: str
) -> None:
    """Recompute and pin the complete deterministic Stage 8 evidence projection."""

    if stage8_report.get("schema_version") != "evaluation-report-v1":
        raise ValueError("Stage 8 report schema is invalid")
    if stage8_report.get("passed") is not True or stage8_report.get("failures") != []:
        raise ValueError("Stage 8 report did not pass")
    if stage8_report.get("production_candidate_eligible") is not False:
        raise ValueError("Stage 8 report cannot claim production qualification")
    environment = stage8_report.get("environment")
    if not isinstance(environment, Mapping) or set(environment) != {
        "git_commit",
        "git_dirty",
        "python",
    }:
        raise ValueError("Stage 8 report environment schema is invalid")
    if environment["git_commit"] != code_commit:
        raise ValueError("Stage 8 report Git commit does not match Stage 9")
    if environment["git_dirty"] is not False:
        raise ValueError("Stage 8 report must come from a clean checkout")
    if not isinstance(environment["python"], str) or not environment["python"]:
        raise ValueError("Stage 8 report Python version is invalid")

    contract_metrics = _static_metrics(stage8_report)
    suite_names = ("e2e", "integration", "regression", "security", "unit")
    suite_passed_test_ids: dict[str, list[str]] = {}
    suite_counts: dict[str, int] = {}
    for suite_name in suite_names:
        path = suite_dir / f"{suite_name}.json"
        raw = _load(path)
        if raw.get("schema_version") != "unittest-suite-result-v1":
            raise ValueError(f"Stage 8 suite result schema is invalid: {path}")
        passed_ids = raw.get("passed_test_ids")
        if (
            not isinstance(passed_ids, list)
            or any(not isinstance(item, str) or not item for item in passed_ids)
            or len(passed_ids) != len(set(passed_ids))
            or raw.get("tests_run") != len(passed_ids)
            or any(
                raw.get(field)
                for field in (
                    "errors",
                    "expected_failures",
                    "failures",
                    "skipped",
                    "unexpected_successes",
                )
            )
        ):
            raise ValueError(f"Stage 8 suite result is incomplete: {path}")
        suite_passed_test_ids[suite_name] = passed_ids
        suite_counts[suite_name] = len(passed_ids)
    if stage8_report.get("suite_counts") != suite_counts:
        raise ValueError("Stage 8 suite counts do not bind the supplied suite results")

    projection = {
        "case_digests": stage8_report.get("case_digests"),
        "contract_metrics": contract_metrics,
        "diagnostics": stage8_report.get("diagnostics"),
        "identities": stage8_report.get("identities"),
        "suite_passed_test_ids": suite_passed_test_ids,
    }
    calculated_digest = hashlib.sha256(
        _canonical_bytes(projection) + b"\n"
    ).hexdigest()
    if stage8_report.get("semantic_digest") != calculated_digest:
        raise ValueError("Stage 8 semantic digest does not bind its report and suites")

    baseline = _load(STAGE8_BASELINE)
    if (
        baseline.get("schema_version") != EVALUATION_BASELINE_SCHEMA_VERSION
        or baseline.get("version") != EVALUATION_BASELINE_VERSION
        or baseline.get("profile_id") != "dev-mini"
        or baseline.get("semantic_digest") != calculated_digest
        or baseline.get("deterministic_projection") != projection
    ):
        raise ValueError("Stage 8 evidence does not match the reviewed baseline")


def _normalized_cost(
    provider: Mapping[str, Any],
    request_costs: list[float],
) -> dict[str, int | float | str]:
    total = math.fsum(sorted(request_costs))
    return {
        "currency": "USD",
        "estimated_total_usd": total,
        "input_tokens": int(provider["input_tokens"]),
        "mean_request_usd": total / len(request_costs),
        "metered_requests": len(request_costs),
        "model_calls": int(provider["model_calls"]),
        "output_tokens": int(provider["output_tokens"]),
        "request_cost_sample_count": len(request_costs),
    }


def _ingestion_projection(raw: Mapping[str, Any]) -> dict[str, Any]:
    integer_recovery_job_fields = {
        "attempts",
        "built_chunk_count",
        "built_embedding_count",
        "completed_tasks",
        "corpus_revision",
        "expected_tasks",
        "max_attempts",
        "snapshot_expected_chunk_count",
        "source_generation",
    }
    recovered_job = {
        str(field): (
            int(value)
            if field in integer_recovery_job_fields
            else _prefixed(str(value))
            if field in {"request_fingerprint", "snapshot_manifest_hash"}
            else str(value)
        )
        for field, value in sorted(raw["recovered_job"].items())
    }
    return {
        "acl_coverage": {
            str(field): (
                sorted(str(item) for item in value)
                if field == "access_groups"
                else str(value)
                if field == "tenant_id"
                else int(value)
            )
            for field, value in sorted(raw["acl_coverage"].items())
        },
        "active_generations": {
            str(tenant_id): str(generation_id)
            for tenant_id, generation_id in sorted(
                raw["active_generations"].items()
            )
        },
        "clean_start": raw["clean_start"],
        "completed_monotonic_ms": float(raw["finished_ns"]) / 1_000_000,
        "completed_versions": int(raw["completed_versions"]),
        "database_documents": int(raw["database_documents"]),
        "database_versions": int(raw["database_versions"]),
        "failed_versions": int(raw["failed_versions"]),
        "initial_load_transaction_timeout_seconds": float(
            raw["initial_load_transaction_timeout_seconds"]
        ),
        "embedding_generation_coverage": {
            str(tenant_id): {
                "covered_chunks": int(coverage["covered_chunks"]),
                "generation_id": str(coverage["generation_id"]),
                "total_chunks": int(coverage["total_chunks"]),
            }
            for tenant_id, coverage in sorted(
                raw["embedding_generation_coverage"].items()
            )
        },
        "idempotency_after_state_sha256": _prefixed(
            str(raw["idempotency_after_state_sha256"])
        ),
        "idempotency_before_state_sha256": _prefixed(
            str(raw["idempotency_before_state_sha256"])
        ),
        "idempotency_mismatch_count": int(raw["idempotency_mismatch_count"]),
        "interrupted_after_state_sha256": _prefixed(
            str(raw["interrupted_after_state_sha256"])
        ),
        "interrupted_before_state_sha256": _prefixed(
            str(raw["interrupted_before_state_sha256"])
        ),
        "interrupted_job_count": int(raw["interrupted_job_count"]),
        "interrupted_task_node_count": int(
            raw["interrupted_task_node_count"]
        ),
        "primary_tenant_active_chunks": int(raw["primary_tenant_active_chunks"]),
        "primary_visible_chunks": int(raw["primary_visible_chunks"]),
        "recovered_job": recovered_job,
        "recovered_job_id": str(raw["recovered_job_id"]),
        "recovered_job_linked_task_count": int(
            raw["recovered_job_linked_task_count"]
        ),
        "recovered_job_task_node_count": int(
            raw["recovered_job_task_node_count"]
        ),
        "recovery_checkpoint": str(raw["recovery_checkpoint"]),
        "recovery_task_tracking_mode": str(
            raw["recovery_task_tracking_mode"]
        ),
        "replayed_active_versions": int(raw["replayed_active_versions"]),
        "replay_completed_monotonic_ms": float(raw["replay_finished_ns"])
        / 1_000_000,
        "replay_started_monotonic_ms": float(raw["replay_started_ns"])
        / 1_000_000,
        "query_ready_monotonic_ms": float(raw["query_ready_ns"]) / 1_000_000,
        "schema_version": str(raw["schema_version"]),
        "started_monotonic_ms": float(raw["started_ns"]) / 1_000_000,
        "submitted_chunks": int(raw["submitted_chunks"]),
        "total_active_chunks": int(raw["total_active_chunks"]),
        "total_historical_chunks": int(raw["total_historical_chunks"]),
        "total_versions": int(raw["total_versions"]),
    }


def _load_graph_state_projection(raw: Mapping[str, Any]) -> dict[str, Any]:
    def state(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "business_node_count": int(value["business_node_count"]),
            "business_relationship_count": int(
                value["business_relationship_count"]
            ),
            "label_counts": {
                str(label): int(count)
                for label, count in sorted(value["label_counts"].items())
            },
            "sha256": _prefixed(str(value["sha256"])),
        }

    return {
        "after_idempotent_replay": state(raw["after_idempotent_replay"]),
        "before_idempotent_replay": state(raw["before_idempotent_replay"]),
        "idempotency_mismatch_count": int(raw["idempotency_mismatch_count"]),
        "query_ready_state": state(raw["query_ready_state"]),
        "schema_version": str(raw["schema_version"]),
    }


def _load_window_timeouts(
    raw: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[float, float, str]:
    if set(raw) != _LOAD_WINDOW_FIELDS:
        raise ValueError("production load-window evidence schema is invalid")
    if raw["schema_version"] != "production-load-window-v2":
        raise ValueError("production load-window evidence version is invalid")
    configured_warmups = config["retrieval"]["warmup_requests"]
    observed_warmups = raw["warmup_requests"]
    if (
        isinstance(configured_warmups, bool)
        or not isinstance(configured_warmups, int)
        or isinstance(observed_warmups, bool)
        or not isinstance(observed_warmups, int)
        or observed_warmups != configured_warmups
    ):
        raise ValueError(
            "production retrieval warmup does not match the configured case set"
        )
    configured_answer_warmups = config["answer"]["warmup_requests"]
    observed_answer_warmups = raw["answer_warmup_requests"]
    if (
        isinstance(configured_answer_warmups, bool)
        or not isinstance(configured_answer_warmups, int)
        or isinstance(observed_answer_warmups, bool)
        or not isinstance(observed_answer_warmups, int)
        or observed_answer_warmups != configured_answer_warmups
    ):
        raise ValueError(
            "production answer preflight does not match the configured case set"
        )
    expected = float(config["neo4j"]["online_transaction_timeout_seconds"])
    observed: list[float] = []
    for field in (
        "retrieval_transaction_timeout_seconds",
        "readiness_transaction_timeout_seconds",
    ):
        value = raw[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) != expected
        ):
            raise ValueError(
                f"runtime {field} does not match the configured online timeout"
            )
        observed.append(float(value))
    readiness_probe_status = raw["readiness_probe_status"]
    if readiness_probe_status != "ready":
        raise ValueError("production readiness probe did not pass")
    return observed[0], observed[1], readiness_probe_status


def _container_inspection_projection(
    raw: Mapping[str, Any],
    *,
    code_commit: str,
) -> dict[str, Any]:
    if set(raw) != _CONTAINER_INSPECTION_FIELDS:
        raise ValueError("production container inspection evidence schema is invalid")
    if raw["schema_version"] != "production-container-inspection-v2":
        raise ValueError("production container inspection evidence version is invalid")
    observed_commit = raw["code_commit"]
    if (
        not isinstance(observed_commit, str)
        or len(observed_commit) != 40
        or any(character not in "0123456789abcdef" for character in observed_commit)
        or observed_commit != code_commit
    ):
        raise ValueError(
            "production container inspection code commit does not match the run"
        )
    image = raw["actual_neo4j_image"]
    if (
        not isinstance(image, str)
        or not image.strip()
        or any(character in image for character in ("\x00", "\r", "\n"))
    ):
        raise ValueError("production container inspection image is invalid")
    raw_repo_digest = raw["actual_neo4j_repo_digest"]
    if not isinstance(raw_repo_digest, str):
        raise ValueError("production container inspection RepoDigest is invalid")
    repo_digest = _prefixed(raw_repo_digest)
    if (
        len(repo_digest) != 71
        or any(
            character not in "0123456789abcdef"
            for character in repo_digest.removeprefix("sha256:")
        )
    ):
        raise ValueError("production container inspection RepoDigest is invalid")
    counts: dict[str, int] = {}
    for field in (
        "database_initial_node_count",
        "database_initial_relationship_count",
    ):
        value = raw[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"production container inspection {field} is invalid")
        counts[field] = value
    return {
        "actual_neo4j_image": image,
        "actual_neo4j_repo_digest": repo_digest,
        "code_commit": observed_commit,
        **counts,
    }


def _deletion_projection(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "deletion_residue_count": int(raw["deletion_residue_count"]),
        "delete_job_id": str(raw["delete_job_id"]),
        "document_id": str(raw["document_id"]),
        "durable_audit_job_count": int(raw["durable_audit_job_count"]),
        "durable_audit_job_ids": sorted(
            str(item) for item in raw["durable_audit_job_ids"]
        ),
        "durable_audit_jobs": sorted(
            (
                {
                    "completed_tasks": int(item["completed_tasks"]),
                    "document_id": str(item["document_id"]),
                    "expected_tasks": int(item["expected_tasks"]),
                    "job_id": str(item["job_id"]),
                    "operation": str(item["operation"]),
                    "operation_key": str(item["operation_key"]),
                    "outcome": str(item["outcome"]),
                    "phase": str(item["phase"]),
                    "status": str(item["status"]),
                    "target_snapshot_id": str(item["target_snapshot_id"]),
                    "target_version_id": str(item["target_version_id"]),
                    "tenant_id": str(item["tenant_id"]),
                }
                for item in raw["durable_audit_jobs"]
            ),
            key=lambda item: item["job_id"],
        ),
        "durable_audit_records_retained": raw["durable_audit_records_retained"],
        "expected_removed_counts": raw["expected_removed_counts"],
        "observed_removed_counts": raw["observed_removed_counts"],
        "other_tenant_preserved": raw["other_tenant_preserved"],
        "preserved_tenant_ids": sorted(str(item) for item in raw["preserved_tenant_ids"]),
        "residue_by_label": raw["residue_by_label"],
        "schema_version": str(raw["schema_version"]),
        "tenant_id": str(raw["tenant_id"]),
        "tombstone_generation": int(raw["tombstone_generation"]),
        "tombstone_deleted_by_job_id": str(
            raw["tombstone_deleted_by_job_id"]
        ),
        "target_active_chunk_ids": sorted(
            str(item) for item in raw["target_active_chunk_ids"]
        ),
        "target_active_snapshot_id": str(raw["target_active_snapshot_id"]),
        "target_active_version_id": str(raw["target_active_version_id"]),
    }


def _backup_projection(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "backup_sha256": _prefixed(str(raw["backup_sha256"])),
        "backup_size_bytes": int(raw["backup_size_bytes"]),
        "container_resources_match": raw["container_resources_match"],
        "database": str(raw["database"]),
        "dump_command": str(raw["dump_command"]),
        "load_command": str(raw["load_command"]),
        "restored_business_node_count": int(raw["restored_business_node_count"]),
        "restored_business_relationship_count": int(
            raw["restored_business_relationship_count"]
        ),
        "restored_container_resource_sha256": _prefixed(
            str(raw["restored_container_resource_sha256"])
        ),
        "restored_state_sha256": _prefixed(str(raw["restored_state_sha256"])),
        "schema_and_indexes_verified": raw["schema_and_indexes_verified"],
        "schema_version": str(raw["schema_version"]),
        "source_business_node_count": int(raw["source_business_node_count"]),
        "source_business_relationship_count": int(
            raw["source_business_relationship_count"]
        ),
        "source_container_resource_sha256": _prefixed(
            str(raw["source_container_resource_sha256"])
        ),
        "source_state_sha256": _prefixed(str(raw["source_state_sha256"])),
    }


def build(args: argparse.Namespace) -> dict[str, Any]:
    contract = _load(CONTRACT)
    profile = _load(PROFILE)
    config = _load(CONFIGURATION)
    answer_retrieval_limits = resolve_production_answer_retrieval_limits(config)
    load_manifest = _load(LOAD_MANIFEST)
    dev_corpus_manifest = _load(DEV_CORPUS_MANIFEST)
    gold_manifest = _load(GOLD_MANIFEST)
    if _file_sha256(REFERENCE_PREDICTIONS) != REFERENCE_PREDICTION_SHA256:
        raise ValueError("reference answer prediction artifact does not match its pin")
    stage8_report = _load(args.stage8_report)
    ingestion = _load(args.load_dir / "ingestion.json")
    load_graph_state_raw = _load(args.load_dir / "graph-state.json")
    deletion = _load(args.load_dir / "deletion.json")
    backup = _load(args.backup_observation)
    provider = _load(args.runtime_dir / "provider-usage.json")
    load_window = _load(args.runtime_dir / "load-window.json")
    quality = _load(args.large_database_quality)
    container_raw = _container_inspection_projection(
        _load(args.container_inspection),
        code_commit=args.code_commit,
    )
    container_resources = _load(args.container_resources)
    _validate_stage8_report(stage8_report, args.stage8_suites_dir, args.code_commit)
    expected_prediction_identity = {
        "prediction_provider": REFERENCE_PREDICTION_PROVIDER,
        "prediction_sha256": f"sha256:{REFERENCE_PREDICTION_SHA256}",
        "prediction_version": REFERENCE_PREDICTION_VERSION,
    }
    if {
        field: quality.get(field) for field in expected_prediction_identity
    } != expected_prediction_identity:
        raise ValueError(
            "large-database quality prediction identity does not match its pinned artifact"
        )
    if quality.get("answer_retrieval_limits") != asdict(answer_retrieval_limits):
        raise ValueError(
            "large-database quality answer profile does not match the production configuration"
        )
    if quality.get("production_configuration_sha256") != _prefixed(
        _file_sha256(CONFIGURATION)
    ):
        raise ValueError(
            "large-database quality configuration digest does not match the reviewed file"
        )
    if quality.get("case_count") != 49 or quality.get("passed") is not True:
        raise ValueError("large-database quality evidence must cover 49 cases")
    if quality.get("case_set_sha256") != _prefixed(
        str(gold_manifest["coverage"]["case_set_sha256"])
    ):
        raise ValueError("large-database quality case set drifted from gold-v1")

    request_rows = _load_jsonl(args.runtime_dir / "requests.jsonl")
    retrieval_stage_samples = _retrieval_stage_samples(
        args.runtime_dir / "retrieval-stage.jsonl",
        request_rows,
    )
    request_samples = _request_samples(request_rows, config)
    retrieval_samples = request_samples["retrieval"]
    answer_samples = request_samples["answer"]
    retrieval_start = min(float(item["started_monotonic_ms"]) for item in retrieval_samples)
    retrieval_end = max(float(item["completed_monotonic_ms"]) for item in retrieval_samples)
    duration_seconds = (retrieval_end - retrieval_start) / 1_000
    successful_retrievals = sum(
        item["semantic_success"] is True for item in retrieval_samples
    )
    server_errors = sum(
        500 <= int(item["status_code"]) <= 599 for item in retrieval_samples
    )
    retrieval_rps = successful_retrievals / duration_seconds
    server_error_rate = server_errors / len(retrieval_samples)
    retrieval_p95 = float(
        nearest_rank_percentile(
            [float(item["retrieval_stage_ms"]) for item in retrieval_samples],
            0.95,
        )
    )
    answer_p95 = _nearest(answer_samples, 0.95)
    semantic_failures = [
        row["request_id"] for row in request_rows if row.get("semantic_success") is not True
    ]
    unauthorized_requests = sum(
        int(row.get("unauthorized_chunk_count", 0)) for row in request_rows
    )
    inactive_requests = sum(
        int(row.get("inactive_version_count", 0)) for row in request_rows
    )
    if semantic_failures or unauthorized_requests or inactive_requests:
        raise ValueError(
            "measured request evidence contains semantic, authorization, or version failures"
        )

    raw_scenarios = _scenario_rows(args.load_dir, args.runtime_dir, args.backup_observation)
    _validate_provider_timeout_scenarios(config, raw_scenarios)
    fault_timeline, scenarios = _fault_projection(raw_scenarios, load_manifest)
    static_metrics = _static_metrics(stage8_report)
    retrieval_quality = quality["retrieval_metrics"]
    answer_quality = quality["answer_metrics"]
    metrics = {
        **static_metrics,
        "answer_p95_ms": answer_p95,
        "deletion_residue_count": int(deletion["deletion_residue_count"]),
        "idempotency_mismatch_count": int(ingestion["idempotency_mismatch_count"]),
        "ingestion_success_rate": int(ingestion["completed_versions"])
        / int(ingestion["total_versions"]),
        "recovery_success_rate": float(
            bool(raw_scenarios["interrupted_ingestion"].get("passed"))
        ),
        "retrieval_p95_ms": retrieval_p95,
        "retrieval_throughput_rps": retrieval_rps,
        "server_error_rate": server_error_rate,
        "recall_at_5": float(retrieval_quality["recall_at_5"]),
        "mrr": float(retrieval_quality["mrr"]),
        "ndcg_at_5": float(retrieval_quality["ndcg_at_5"]),
        "unauthorized_exposure_count": int(
            retrieval_quality["unauthorized_exposure_count"]
        ),
        "supported_claim_rate": float(answer_quality["supported_claim_rate"]),
        "citation_precision": float(answer_quality["citation_precision"]),
        "citation_coverage": float(answer_quality["citation_coverage"]),
        "numerical_fidelity": float(answer_quality["numerical_fidelity"]),
        "refusal_f1": float(answer_quality["refusal_f1"]),
    }
    primary_chunks = int(ingestion["primary_tenant_active_chunks"])
    performance_checks = {
        "answer_sample_count": len(answer_samples) >= 30,
        "answer_p95": answer_p95 <= 15_000,
        "corpus_scale": primary_chunks >= 10_000,
        "eight_clients": len({item["client_id"] for item in retrieval_samples}) == 8,
        "eight_concurrent_calls": int(provider["peak_concurrency"]) >= 8,
        "five_minute_window": duration_seconds >= 300,
        "retrieval_p95": retrieval_p95 <= 1_000,
        "retrieval_throughput": retrieval_rps >= 8,
        "server_error_rate": server_error_rate <= 0.005,
        "semantic_success": not semantic_failures,
    }
    suites = _suite_results(
        args.stage8_suites_dir,
        stage8_report,
        quality,
        raw_scenarios,
        performance_checks,
    )

    graph_pre = _canonical_graph_state_projection(args.pre_graph_state)
    graph_post = _canonical_graph_state_projection(args.post_graph_state)
    graph_backup_source = _canonical_graph_state_projection(
        args.backup_source_graph_state
    )
    graph_restore = _canonical_graph_state_projection(args.restore_graph_state)
    canonical_graph = {
        "backup_source_state": graph_backup_source,
        "post_validation_state": graph_post,
        "pre_validation_state": graph_pre,
        "restored_state": graph_restore,
    }
    (
        retrieval_transaction_timeout,
        readiness_transaction_timeout,
        readiness_probe_status,
    ) = _load_window_timeouts(load_window, config)
    runtime_environment = {
        "actual_memory_bytes": int(container_resources["actual_memory_bytes"]),
        "actual_memory_swap_bytes": int(
            container_resources["actual_memory_swap_bytes"]
        ),
        "actual_nano_cpus": int(container_resources["actual_nano_cpus"]),
        "actual_neo4j_image": container_raw["actual_neo4j_image"],
        "actual_neo4j_image_id": _prefixed(
            str(container_resources["actual_image_id"])
        ),
        "actual_neo4j_repo_digest": _prefixed(
            container_raw["actual_neo4j_repo_digest"]
        ),
        "api_process_resource_limit": "host-default-unbounded",
        "code_commit": container_raw["code_commit"],
        "configured_heap_initial": str(
            container_resources["configured_heap_initial"]
        ),
        "configured_heap_max": str(container_resources["configured_heap_max"]),
        "configured_pagecache": str(container_resources["configured_pagecache"]),
        "configured_transaction_timeout": str(
            container_resources["configured_transaction_timeout"]
        ),
        "database_initial_node_count": int(
            container_raw["database_initial_node_count"]
        ),
        "database_initial_relationship_count": int(
            container_raw["database_initial_relationship_count"]
        ),
        "host_cpu_count": int(os.cpu_count() or 0),
        "host_memory_bytes": _host_memory_bytes(),
        "host_platform": platform.platform(),
        "readiness_probe_status": readiness_probe_status,
        "readiness_transaction_timeout_seconds": readiness_transaction_timeout,
        "retrieval_transaction_timeout_seconds": retrieval_transaction_timeout,
    }
    versions = {
        "answer_embedding_corpus_digest": _file_sha256(DEV_CORPUS_MANIFEST),
        "answer_embedding_corpus_version": str(dev_corpus_manifest["version"]),
        "answer_embedding_model": str(
            dev_corpus_manifest["embedding_profile"]["model"]
        ),
        "answer_embedding_provider": str(
            dev_corpus_manifest["embedding_profile"]["provider"]
        ),
        "answer_embedding_revision": str(
            dev_corpus_manifest["embedding_profile"]["revision"]
        ),
        "answer_embedding_space_id": str(
            dev_corpus_manifest["embedding_profile"]["embedding_space_id"]
        ),
        "answer_prediction_digest": _file_sha256(REFERENCE_PREDICTIONS),
        "answer_prediction_provider": REFERENCE_PREDICTION_PROVIDER,
        "answer_prediction_version": REFERENCE_PREDICTION_VERSION,
        "api_version": __version__,
        "code_commit": args.code_commit,
        "configuration_digest": _file_sha256(CONFIGURATION),
        "configuration_version": str(config["version"]),
        "contract_digest": _file_sha256(CONTRACT),
        "contract_version": str(contract["contract_version"]),
        "embedding_model": str(config["dependencies"]["embedding_provider"]["model"]),
        "embedding_provider": str(
            config["dependencies"]["embedding_provider"]["provider"]
        ),
        "embedding_revision": str(
            config["dependencies"]["embedding_provider"]["revision"]
        ),
        "embedding_space_id": EMBEDDING_SPACE_ID,
        "extractor_version": EXTRACTOR_SIGNATURE,
        "governance_policy_version": "graph-governance-catalog-1.0.0",
        "graph_schema_version": "neo4j-migrations-001-through-005",
        "hardware_profile": "neo4j-8cpu-3072mb-loopback-v1",
        "index_version": "acl-partitioned-bm25-v2+exact-authorized-cosine-v1",
        "llm_model": str(config["dependencies"]["llm_provider"]["model"]),
        "llm_provider": str(config["dependencies"]["llm_provider"]["provider"]),
        "llm_revision": str(config["dependencies"]["llm_provider"]["revision"]),
        "load_corpus_digest": _file_sha256(LOAD_MANIFEST),
        "load_corpus_id": str(load_manifest["dataset_id"]),
        "load_corpus_version": str(load_manifest["version"]),
        "neo4j_image": str(config["neo4j"]["image"]),
        "neo4j_image_digest": _prefixed(str(config["neo4j"]["image_digest"])),
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "profile_digest": _file_sha256(PROFILE),
        "profile_version": str(profile["profile_version"]),
        "prompt_version": PROMPT_VERSION,
        "python_version": platform.python_version(),
        "splitter_version": SPLITTER_SIGNATURE,
        "stage8_gold_digest": _file_sha256(GOLD_MANIFEST),
        "stage8_gold_version": str(gold_manifest["version"]),
        "stage8_report_digest": _file_sha256(args.stage8_report),
        "stage8_report_semantic_digest": str(stage8_report["semantic_digest"]),
    }

    configured_initial_load_timeout = config["neo4j"][
        "initial_load_transaction_timeout_seconds"
    ]
    observed_initial_load_timeout = ingestion.get(
        "initial_load_transaction_timeout_seconds"
    )
    if (
        isinstance(configured_initial_load_timeout, bool)
        or not isinstance(configured_initial_load_timeout, (int, float))
        or isinstance(observed_initial_load_timeout, bool)
        or not isinstance(observed_initial_load_timeout, (int, float))
        or float(observed_initial_load_timeout)
        != float(configured_initial_load_timeout)
    ):
        raise ValueError(
            "ingestion evidence does not match the configured initial-load "
            "transaction timeout"
        )
    ingestion_evidence = _ingestion_projection(ingestion)
    load_graph_state = _load_graph_state_projection(load_graph_state_raw)
    deletion_evidence = _deletion_projection(deletion)
    backup_evidence = _backup_projection(backup)
    configured_answer_warmups = config.get("answer", {}).get("warmup_requests")
    configured_answer_cases = config.get("answer", {}).get("gold_case_ids")
    observed_answer_warmups = provider.get("answer_warmup_model_calls")
    observed_answer_preflights = provider.get("answer_preflight_case_ids")
    if (
        isinstance(configured_answer_warmups, bool)
        or not isinstance(configured_answer_warmups, int)
        or not isinstance(configured_answer_cases, list)
        or len(configured_answer_cases) != 30
        or any(
            not isinstance(case_id, str) or not case_id
            for case_id in configured_answer_cases
        )
        or len(set(configured_answer_cases)) != len(configured_answer_cases)
        or configured_answer_warmups != len(configured_answer_cases)
        or isinstance(observed_answer_warmups, bool)
        or not isinstance(observed_answer_warmups, int)
        or observed_answer_warmups != configured_answer_warmups
        or not isinstance(observed_answer_preflights, list)
        or len(observed_answer_preflights) != configured_answer_warmups
        or any(
            not isinstance(case_id, str) or not case_id
            for case_id in observed_answer_preflights
        )
        or len(set(observed_answer_preflights)) != len(observed_answer_preflights)
        or sorted(observed_answer_preflights) != sorted(configured_answer_cases)
    ):
        raise ValueError("provider evidence must prove the full answer preflight")
    provider_evidence = {
        "answer_preflight_case_ids": sorted(observed_answer_preflights),
        "answer_warmup_model_calls": observed_answer_warmups,
        "measured_answer_model_calls": int(provider["measured_answer_model_calls"]),
        "measured_embedding_model_calls": int(
            provider["measured_embedding_model_calls"]
        ),
        "mode": str(provider["mode"]),
        "peak_concurrency": int(provider["peak_concurrency"]),
    }
    request_costs = [float(item) for item in provider["answer_request_cost_usd"]]
    cost_observation = {
        "currency": "USD",
        "input_tokens": int(provider["input_tokens"]),
        "model_calls": int(provider["model_calls"]),
        "output_tokens": int(provider["output_tokens"]),
        "request_cost_usd": request_costs,
    }
    provider_usage = {
        "cost": _normalized_cost(provider, request_costs),
        "embedding_latency_ms": sorted(
            float(item) for item in provider["embedding_latency_ms"]
        ),
        "llm_latency_ms": sorted(float(item) for item in provider["llm_latency_ms"]),
        "provider_evidence": provider_evidence,
    }
    raw_artifacts = {
        "backup_observation": backup_evidence,
        "container_inspection": runtime_environment,
        "deletion_observation": deletion_evidence,
        "fault_timeline": fault_timeline,
        "graph_backup_source_state": graph_backup_source,
        "graph_post_state": graph_post,
        "graph_pre_state": graph_pre,
        "graph_restore_state": graph_restore,
        "ingestion_observation": ingestion_evidence,
        "load_graph_state": load_graph_state,
        "large_database_quality": quality,
        "large_database_quality_cases": quality["case_evidence"],
        "provider_usage": provider_usage,
        "request_samples": request_samples,
        "retrieval_stage_samples": retrieval_stage_samples,
        "suite_results": suites,
    }
    evidence_dir = args.output.parent / "evidence"
    backup_dump_manifest = _bind_backup_dump(
        args.backup_dump,
        evidence_dir / "backup_dump.dump",
        backup,
    )
    raw_artifact_paths: dict[str, Path] = {}
    for evidence_id, value in raw_artifacts.items():
        evidence_path = evidence_dir / f"{evidence_id}.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_bytes(_canonical_bytes(value))
        raw_artifact_paths[evidence_id] = evidence_path
    static_sources = {
        "acceptance_contract": CONTRACT,
        "answer_embedding_corpus": DEV_CORPUS_MANIFEST,
        "load_corpus": LOAD_MANIFEST,
        "production_configuration": CONFIGURATION,
        "reference_answer_predictions": REFERENCE_PREDICTIONS,
        "stage8_report": args.stage8_report,
        "validation_profile": PROFILE,
    }
    artifact_paths = dict(raw_artifact_paths)
    for evidence_id, source_path in static_sources.items():
        evidence_path = evidence_dir / f"{evidence_id}.json"
        evidence_path.write_bytes(source_path.read_bytes())
        artifact_paths[evidence_id] = evidence_path
    evidence_schemas = {
        "acceptance_contract": "acceptance-contract-v1",
        "answer_embedding_corpus": "dev-corpus-manifest-v1",
        "backup_dump": BACKUP_DUMP_EVIDENCE_SCHEMA_VERSION,
        "backup_observation": "production-backup-restore-observation-v1",
        "container_inspection": "production-runtime-environment-v1",
        "fault_timeline": "production-fault-timeline-v1",
        "graph_backup_source_state": "canonical-graph-state-v1",
        "graph_post_state": "canonical-graph-state-v1",
        "graph_pre_state": "canonical-graph-state-v1",
        "graph_restore_state": "canonical-graph-state-v1",
        "load_corpus": "load-corpus-manifest-v1",
        "large_database_quality": "production-large-database-quality-v1",
        "large_database_quality_cases": QUALITY_CASE_EVIDENCE_SCHEMA_VERSION,
        "load_graph_state": "canonical-graph-state-observation-v2",
        "deletion_observation": "production-deletion-observation-v1",
        "ingestion_observation": "production-ingestion-observation-v2",
        "production_configuration": "production-reference-config-v1",
        "provider_usage": "production-provider-usage-v1",
        "reference_answer_predictions": REFERENCE_PREDICTION_SCHEMA_VERSION,
        "request_samples": "production-request-samples-v1",
        "retrieval_stage_samples": "production-retrieval-stage-samples-v1",
        "stage8_report": "evaluation-report-v1",
        "suite_results": "production-suite-results-v1",
        "validation_profile": "validation-profile-v1",
    }
    record_counts = {
        "backup_dump": 1,
        "fault_timeline": len(fault_timeline),
        "large_database_quality_cases": int(quality["case_count"]),
        "load_graph_state": 1,
        "load_corpus": int(load_manifest["counts"]["load_items"]),
        "request_samples": len(retrieval_samples) + len(answer_samples),
        "reference_answer_predictions": 49,
        "retrieval_stage_samples": len(retrieval_stage_samples),
        "suite_results": len(suites),
    }
    evidence_manifest: dict[str, dict[str, Any]] = {}
    for evidence_id, schema in evidence_schemas.items():
        if evidence_id == "backup_dump":
            evidence_manifest[evidence_id] = backup_dump_manifest
            continue
        evidence_manifest[evidence_id] = {
            "path": f"evidence/{evidence_id}.json",
            "record_count": int(record_counts.get(evidence_id, 1)),
            "schema": schema,
            "sha256": _file_sha256(artifact_paths[evidence_id]),
        }

    ingestion_started_ms = float(ingestion["started_ns"]) / 1_000_000
    ingestion_completed_ms = float(ingestion["finished_ns"]) / 1_000_000
    observations = {
        "backup_evidence": backup_evidence,
        "canonical_graph": canonical_graph,
        "cost": cost_observation,
        "deletion_evidence": deletion_evidence,
        "deployment_prerequisites": list(REQUIRED_DEPLOYMENT_PREREQUISITES),
        "evidence_manifest": evidence_manifest,
        "fault_timeline": fault_timeline,
        "ingestion_evidence": ingestion_evidence,
        "latency_ms": {
            "embedding_provider": provider["embedding_latency_ms"],
            "ingestion": [ingestion_completed_ms - ingestion_started_ms],
            "llm": provider["llm_latency_ms"],
            "retrieval_stage_ms": [
                sample["retrieval_stage_ms"] for sample in retrieval_stage_samples
            ],
        },
        "limitations": [
            "The load corpus is deterministic synthetic evidence, not customer data.",
            "Validation covers one bounded five-minute loopback load window.",
            "Validation uses one Neo4j Community container rather than a clustered topology.",
            "The API and load-generator processes use disclosed host-default-unbounded resources.",
            "The atomic bulk initial-load path records task completion as "
            "IngestionJob aggregate counters and does not create IngestionTask "
            "or HAS_TASK records; recovery evidence verifies that actual "
            "zero-node behavior.",
            "Ingestion throughput measures atomic graph writes; embedding-generation activation and index refresh are outside that interval.",
            "The CI diagnostic artifact omits the database dump; its manifest hash is independently useful only while the full local evidence directory is retained.",
        ],
        "metrics": metrics,
        "load_graph_state": load_graph_state,
        "profile_id": "production-reference",
        "provider_evidence": provider_evidence,
        "quality_evidence": quality,
        "request_samples": request_samples,
        "residual_risks": [
            "Capacity and failure behavior can change with deployment data skew.",
            "Cluster failover and region-level disaster recovery remain deployment checks.",
        ],
        "runtime_environment": runtime_environment,
        "scenarios": scenarios,
        "schema_version": PRODUCTION_OBSERVATION_SCHEMA_VERSION,
        "suite_results": suites,
        "traffic": {
            "ingestion_chunks": int(ingestion["submitted_chunks"]),
            "ingestion_completed_monotonic_ms": ingestion_completed_ms,
            "ingestion_started_monotonic_ms": ingestion_started_ms,
        },
        "versions": versions,
        "workload": {
            "answer_samples": len(answer_samples),
            "chunk_count": primary_chunks,
            "concurrency": int(config["retrieval"]["concurrency"]),
            "sustained_seconds": duration_seconds,
            "warmed": True,
        },
    }
    report = build_production_candidate_report(observations, contract, profile)
    if args.observations_output is not None:
        _write_json(args.observations_output, observations)
    _write_json(args.output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage8-report", type=Path, required=True)
    parser.add_argument("--stage8-suites-dir", type=Path, required=True)
    parser.add_argument("--load-dir", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--container-inspection", type=Path, required=True)
    parser.add_argument("--container-resources", type=Path, required=True)
    parser.add_argument("--pre-graph-state", type=Path, required=True)
    parser.add_argument("--post-graph-state", type=Path, required=True)
    parser.add_argument("--backup-source-graph-state", type=Path, required=True)
    parser.add_argument("--restore-graph-state", type=Path, required=True)
    parser.add_argument("--backup-observation", type=Path, required=True)
    parser.add_argument("--backup-dump", type=Path, required=True)
    parser.add_argument("--large-database-quality", type=Path, required=True)
    parser.add_argument("--code-commit", required=True)
    parser.add_argument("--observations-output", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build(args)
    print(
        f"production candidate passed={report['passed']} "
        f"semantic_digest={report['semantic_digest']}"
    )
    for failure in report["failures"]:
        print(f"FAIL: {failure}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
